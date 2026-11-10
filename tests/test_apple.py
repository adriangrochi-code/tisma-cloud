"""App Store subscriptions (apple.py; cloud.md 5b): only what Apple signed is accepted. The tests sign with a chain made
like Apple's (a root, an intermediate with Apple's mark 1.2.840.113635.100.6.2.1 and a leaf with 1.2.840.113635.100.6.11.1),
given to the verifier as its root: Apple's own library checks it as it checks the real one."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from conftest import Factory, new_app
from tisma_cloud import apple

BUNDLE = "uy.com.tisma.app"
SITE = {"Authorization": "Bearer site-secret"}


def _cert(name, key, issuer_name, issuer_key, ca, oid=None):
    now = datetime.now(timezone.utc)
    b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
         .issuer_name(issuer_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False))
    if ca:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                          crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
    if oid:
        b = b.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), b"\x05\x00"), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


class FakeApple:
    """Signs App Store data as Apple does (ES256 JWS with the chain in x5c)."""

    def __init__(self) -> None:
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root CA - G3")])
        self.root = _cert("Test Root CA - G3", self.root_key, root_name, self.root_key, True)
        self.mid_key = ec.generate_private_key(ec.SECP256R1())
        self.mid = _cert("Test WWDR", self.mid_key, root_name, self.root_key, True, "1.2.840.113635.100.6.2.1")
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self.leaf = _cert("Test Prod ECC Mac App Store", self.leaf_key, self.mid.subject, self.mid_key, False,
                          "1.2.840.113635.100.6.11.1")

    def root_der(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.DER)

    def sign(self, payload: dict, key=None) -> str:  # noqa: ANN001
        import jwt

        x5c = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in (self.leaf, self.mid, self.root)]
        return jwt.encode(payload, key or self.leaf_key, algorithm="ES256", headers={"x5c": x5c})

    def transaction(self, account: str, product: str = "tisma_pro", days: float = 30, original: str = "1000001",
                    revoked: bool = False, signed_ms: int | None = None, bundle: str = BUNDLE) -> str:
        now = int(time.time() * 1000)
        tx = {"transactionId": str(uuid.uuid4().int)[:16], "originalTransactionId": original, "bundleId": bundle,
              "productId": product, "purchaseDate": now, "originalPurchaseDate": now,
              "expiresDate": now + int(days * 86400000), "quantity": 1, "type": "Auto-Renewable Subscription",
              "appAccountToken": account, "inAppOwnershipType": "PURCHASED", "signedDate": signed_ms or now,
              "environment": "Sandbox", "transactionReason": "PURCHASE", "storefront": "URY", "price": 2500,
              "currency": "USD"}
        if revoked:
            tx["revocationDate"] = now
            tx["revocationReason"] = 0
        return self.sign(tx)

    def notification(self, signed_transaction: str, kind: str = "DID_RENEW", bundle: str = BUNDLE) -> str:
        return self.sign({"notificationType": kind, "notificationUUID": str(uuid.uuid4()), "version": "2.0",
                          "signedDate": int(time.time() * 1000),
                          "data": {"bundleId": bundle, "environment": "Sandbox",
                                   "signedTransactionInfo": signed_transaction, "status": 1}})


@pytest.fixture
def store(settings, tmp_path):
    fake = FakeApple()
    root = tmp_path / "root.cer"
    root.write_bytes(fake.root_der())
    settings.apple_bundle_id = BUNDLE
    settings.apple_environment = "Sandbox"
    settings.apple_products = "tisma_basico:basico, tisma_pro:pro"
    settings.apple_root_certs = str(root)
    # Offline in the tests: the test chain has no OCSP responder (Apple's is asked in production).
    settings.apple_online_checks = False
    with TestClient(new_app(settings), client=("200.40.10.7", 1)) as c:
        for name in ("basico", "pro"):
            assert c.put(f"/v1/site/plans/{name}", headers=SITE, json={"monthly_hours": 3, "max_concurrent": 2,
                         "price_cents": 1000, "public": True}).status_code == 200
        c.fake = fake
        yield c


def account_of(c, user) -> str:
    r = c.get("/v1/billing/apple/account", headers=user)
    assert r.status_code == 200, r.text
    return r.json()["app_account_token"]


def test_a_purchase_signed_by_apple_gives_the_plan_on_the_account(store):
    f = Factory(store)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = account_of(store, user)
    assert acc == account_of(store, user) and uuid.UUID(acc)
    r = store.post("/v1/billing/apple/purchase", headers=user,
                   json={"signed_transaction": store.fake.transaction(acc, "tisma_pro")})
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "pro" and r.json()["paid"] is True
    # The same purchase reported again (the app restarts): the same subscription.
    assert store.post("/v1/billing/apple/purchase", headers=user,
                      json={"signed_transaction": store.fake.transaction(acc, "tisma_pro")}).status_code == 200
    with store.app.state.services.db.session() as s:
        from sqlalchemy import func, select

        from tisma_cloud.models import AppleSubscription
        assert s.execute(select(func.count()).select_from(AppleSubscription)).scalar_one() == 1


def test_what_is_not_accepted(store):
    f = Factory(store)
    ana, bob = f.user("ana@x.com", paid_days=None), f.user("bob@x.com", paid_days=None)
    acc = account_of(store, ana)
    fake = store.fake
    bad = {
        "another account's purchase": (bob, fake.transaction(acc)),
        "another app": (ana, fake.transaction(acc, bundle="com.otra.app")),
        "not our product": (ana, fake.transaction(acc, "otro_producto")),
        "signed by someone else": (ana, FakeApple().transaction(acc)),
        "not a JWS": (ana, "x" * 40),
    }
    for why, (user, signed) in bad.items():
        r = store.post("/v1/billing/apple/purchase", headers=user, json={"signed_transaction": signed})
        assert r.status_code == 422, (why, r.status_code, r.text)
    assert store.get("/v1/me", headers=ana).json()["paid"] is False


def test_notifications_renew_expire_and_refund(store):
    f = Factory(store)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = account_of(store, user)
    fake = store.fake

    def notify(signed_tx, kind):
        return store.post("/v1/billing/apple/notify", json={"signedPayload": fake.notification(signed_tx, kind)})

    # Bought outside the app (or the app did not report it): found by its appAccountToken.
    assert notify(fake.transaction(acc, days=30), "SUBSCRIBED").status_code == 200
    assert store.get("/v1/me", headers=user).json()["paid"] is True
    # A renewal moves the access; an older transaction arriving late does not undo it.
    long_ms = int(time.time() * 1000) + 5000
    assert notify(fake.transaction(acc, days=60, signed_ms=long_ms), "DID_RENEW").status_code == 200
    assert notify(fake.transaction(acc, days=1, signed_ms=long_ms - 100000), "DID_RENEW").status_code == 200
    with store.app.state.services.db.session() as s:
        from sqlalchemy import select

        from tisma_cloud.models import AppleSubscription, User
        sub = s.execute(select(AppleSubscription)).scalar_one()
        assert sub.entitled_until - datetime.now(timezone.utc).replace(tzinfo=None) > timedelta(days=59)
    # A refund takes the access away.
    assert notify(fake.transaction(acc, signed_ms=long_ms + 1000, revoked=True), "REFUND").status_code == 200
    assert store.get("/v1/me", headers=user).json()["paid"] is False
    # Not Apple's signature, or another app's: refused (400, nothing for Apple to send again).
    assert store.post("/v1/billing/apple/notify",
                      json={"signedPayload": FakeApple().notification(fake.transaction(acc))}).status_code == 400
    assert store.post("/v1/billing/apple/notify",
                      json={"signedPayload": fake.notification(fake.transaction(acc), bundle="com.otra")}).status_code == 400
    assert store.post("/v1/billing/apple/notify", json={}).status_code == 400
    with store.app.state.services.db.session() as s:
        assert s.execute(select(User).where(User.apple_account == acc)).scalar_one().store_until is None


def test_without_settings_the_app_store_is_off(client, f):
    user = f.user()
    assert client.get("/v1/billing/apple/account", headers=user).status_code == 404
    assert client.post("/v1/billing/apple/notify", json={"signedPayload": "x"}).status_code == 404


def test_the_bundled_root_is_apples():
    der = apple.ROOT_CERT.read_bytes()
    cert = x509.load_der_x509_certificate(der)
    assert cert.subject.rfc4514_string() == "C=US,O=Apple Inc.,OU=Apple Certification Authority,CN=Apple Root CA - G3"
    assert cert.fingerprint(hashes.SHA256()).hex() == ("63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179")


def test_web_payments_are_off_unless_turned_on(settings):
    settings.billing_provider = "test"
    with TestClient(new_app(settings), client=("200.40.10.7", 1)) as c:
        assert c.get("/v1/health").json()["payments"] is False
    settings.web_payments = True
    with TestClient(new_app(settings), client=("200.40.10.7", 1)) as c:
        assert c.get("/v1/health").json()["payments"] is True


def test_deleting_the_account_forgets_its_app_store_subscription(store):
    f = Factory(store)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = account_of(store, user)
    tx = store.fake.transaction(acc)
    assert store.post("/v1/billing/apple/purchase", headers=user, json={"signed_transaction": tx}).status_code == 200
    services = store.app.state.services
    with services.db.session() as s:
        from sqlalchemy import func, select

        from tisma_cloud.models import AppleSubscription, User
        uid = s.execute(select(User.id).where(User.apple_account == acc)).scalar_one()
    services.delete_account(uid)
    with services.db.session() as s:
        assert s.execute(select(func.count()).select_from(AppleSubscription)).scalar_one() == 0
        assert s.get(User, uid).apple_account is None
    # A notification of that subscription afterwards finds no account.
    r = store.post("/v1/billing/apple/notify", json={"signedPayload": store.fake.notification(tx, "DID_RENEW")})
    assert r.status_code == 200
    with services.db.session() as s:
        assert s.execute(select(func.count()).select_from(AppleSubscription)).scalar_one() == 0
