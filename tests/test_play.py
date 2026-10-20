"""Google Play subscriptions with a fake Google (token endpoint, Play Developer API, Pub/Sub push tokens)."""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, unquote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import new_app as create_app
from test_accounts import FakeJwks, KEY as GOOGLE_KEY
from test_billing import INTERNET, SITE, register
from tisma_cloud import google_play
from tisma_cloud.models import PlaySubscription, User, utcnow

PACKAGE = "net.tismaservidores.app"
SA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA_EMAIL = "tisma-play@tisma.iam.gserviceaccount.com"
PUSH_ACCOUNT = "pubsub-push@tisma.iam.gserviceaccount.com"
AUDIENCE = "https://tismaservidores.net/v1/billing/play/notify"


def rfc3339(when) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.123Z")


class FakeGoogle:
    """The Google endpoints the master calls, as an httpx transport."""

    def __init__(self) -> None:
        self.subs: dict[str, dict] = {}
        self.acks: list[tuple[str, str]] = []
        self.down = False

    def buy(self, token: str, account: str | None, product: str = "tisma_basico", days: int = 30,
            state: str = "SUBSCRIPTION_STATE_ACTIVE", linked: str | None = None) -> None:
        sub = {"kind": "androidpublisher#subscriptionPurchaseV2", "subscriptionState": state,
               "acknowledgementState": "ACKNOWLEDGEMENT_STATE_PENDING",
               "lineItems": [{"productId": product, "expiryTime": rfc3339(utcnow() + timedelta(days=days)),
                              "latestSuccessfulOrderId": f"GPA.{token}"}]}
        if account:
            sub["externalAccountIdentifiers"] = {"obfuscatedExternalAccountId": account}
        if linked:
            sub["linkedPurchaseToken"] = linked
        self.subs[token] = sub

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            claims = jwt.decode(form["assertion"][0], SA_KEY.public_key(), algorithms=["RS256"],
                                audience="https://oauth2.googleapis.com/token")
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            assert claims["iss"] == SA_EMAIL and claims["scope"] == google_play.SCOPE
            return httpx.Response(200, json={"access_token": "ya29.fake", "expires_in": 3600, "token_type": "Bearer"})
        assert request.headers["authorization"] == "Bearer ya29.fake"
        path = unquote(request.url.path)
        prefix = f"/androidpublisher/v3/applications/{PACKAGE}/purchases/"
        assert path.startswith(prefix)
        rest = path[len(prefix):]
        if request.method == "GET" and rest.startswith("subscriptionsv2/tokens/"):
            sub = self.subs.get(rest.split("/", 2)[2])
            return httpx.Response(200, json=sub) if sub else httpx.Response(404, json={"error": {"code": 404}})
        if request.method == "POST" and rest.endswith(":acknowledge"):
            _, product, _, token = rest[:-len(":acknowledge")].split("/", 3)
            sub = self.subs[token]
            sub["acknowledgementState"] = "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
            self.acks.append((product, token))
            return httpx.Response(200)
        return httpx.Response(404)


def push_token(email: str = PUSH_ACCOUNT, aud: str = AUDIENCE, verified: bool = True, key=GOOGLE_KEY) -> str:
    claims = {"iss": "https://accounts.google.com", "aud": aud, "email": email, "email_verified": verified,
              "sub": "1234", "iat": int(time.time()), "exp": int(time.time() + 600)}
    return jwt.encode(claims, key, algorithm="RS256")


def notification(token: str, kind: int = 2, message_id: str = "m-1") -> dict:
    data = {"version": "1.0", "packageName": PACKAGE, "eventTimeMillis": str(int(time.time() * 1000)),
            "subscriptionNotification": {"version": "1.0", "notificationType": kind, "purchaseToken": token}}
    return {"message": {"data": base64.b64encode(json.dumps(data).encode()).decode(), "messageId": message_id,
                        "attributes": {}}, "subscription": "projects/tisma/subscriptions/play"}


@pytest.fixture
def play(settings):
    settings.public_url = "https://tismaservidores.net"
    app = create_app(settings)
    google = FakeGoogle()
    http = httpx.Client(transport=httpx.MockTransport(google.handler))
    pem = SA_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode()
    account = google_play.ServiceAccount({"client_email": SA_EMAIL, "private_key": pem, "private_key_id": "k1",
                                          "token_uri": google_play.TOKEN_URL}, http)
    services = app.state.services
    services.play = google_play.Store(google_play.PlayClient(PACKAGE, account, http),
                                      google_play.parse_products("tisma_basico:basico, tisma_pro:pro"))
    services.play_push = google_play.PushVerifier(AUDIENCE, PUSH_ACCOUNT, jwk_client=FakeJwks())
    with TestClient(app, client=INTERNET) as c:
        for name, price in (("basico", 1000), ("pro", 2500)):
            assert c.put(f"/v1/site/plans/{name}", headers=SITE, json={"monthly_hours": 3, "max_concurrent": 2,
                         "price_cents": price, "public": True}).status_code == 200
        c.google = google
        yield c


def account_id(c, user) -> str:
    r = c.get("/v1/billing/play/account", headers=user)
    assert r.status_code == 200
    return r.json()["account_id"]


def notify(c, body: dict, token: str | None = None):
    return c.post("/v1/billing/play/notify", json=body,
                  headers={"Authorization": f"Bearer {token or push_token()}"})


def user_row(c, email: str) -> User:
    with c.app.state.services.db.session() as s:
        from sqlalchemy import select

        return s.execute(select(User).where(User.email == email)).scalar_one()


def test_plans_show_play_products(play):
    plans = {p["name"]: p for p in play.get("/v1/plans").json()["plans"]}
    assert plans["basico"]["play_product"] == "tisma_basico" and plans["pro"]["play_product"] == "tisma_pro"
    assert play.get("/v1/health").json()["play_payments"] is True


def test_purchase_gives_access_everywhere(play):
    user = register(play, "ana@correo.com")
    assert play.get("/v1/me", headers=user).json()["paid"] is False
    acc = account_id(play, user)
    assert acc == account_id(play, user) and len(acc) == 32          # stable, random, no personal data
    play.google.buy("tok-1", acc, "tisma_pro")
    r = play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"})
    assert r.status_code == 200, r.text
    assert r.json()["paid"] is True and r.json()["plan"] == "pro"
    assert play.google.acks == [("tisma_pro", "tok-1")]                 # acknowledged once
    # The same account from another device (Tisma on a PC) has the access.
    other = play.post("/v1/auth/login", json={"email": "ana@correo.com", "password": "una clave larga"}).json()["token"]
    assert play.get("/v1/me", headers={"Authorization": f"Bearer {other}"}).json()["paid"] is True
    # Reported again (the app restarts): nothing changes, no second acknowledgement.
    assert play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"}).status_code == 200
    assert len(play.google.acks) == 1


def test_purchase_of_other_account_refused(play):
    ana = register(play, "ana@correo.com")
    bob = register(play, "bob@correo.com")
    play.google.buy("tok-ana", account_id(play, ana))
    # Bob cannot claim Ana's purchase (another obfuscatedAccountId), nor a purchase without account id.
    assert play.post("/v1/billing/play/purchase", headers=bob, json={"purchase_token": "tok-ana"}).status_code == 422
    play.google.buy("tok-anon", None)
    assert play.post("/v1/billing/play/purchase", headers=bob, json={"purchase_token": "tok-anon"}).status_code == 422
    # Unknown token, unknown product.
    assert play.post("/v1/billing/play/purchase", headers=bob, json={"purchase_token": "tok-none"}).status_code == 422
    play.google.buy("tok-x", account_id(play, bob), "otra_app_producto")
    assert play.post("/v1/billing/play/purchase", headers=bob, json={"purchase_token": "tok-x"}).status_code == 422
    assert play.get("/v1/me", headers=bob).json()["paid"] is False
    assert play.google.acks == []


def test_google_down_is_retryable(play):
    user = register(play)
    play.google.buy("tok-1", account_id(play, user))
    play.google.down = True
    assert play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"}).status_code == 503
    play.google.down = False
    assert play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"}).status_code == 200


def test_notifications_follow_the_state(play):
    user = register(play)
    acc = account_id(play, user)
    play.google.buy("tok-1", acc)
    assert play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"}).status_code == 200
    # Canceled: access until the end of the period.
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_CANCELED"
    assert notify(play, notification("tok-1", 3)).status_code == 200
    assert play.get("/v1/me", headers=user).json()["paid"] is True
    # Revoked (refund, chargeback): EXPIRED with an expiry in the past -> no access at once.
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_EXPIRED"
    play.google.subs["tok-1"]["lineItems"][0]["expiryTime"] = rfc3339(utcnow() - timedelta(minutes=1))
    assert notify(play, notification("tok-1", 12, "m-2")).status_code == 200
    assert play.get("/v1/me", headers=user).json()["paid"] is False
    assert user_row(play, "ana@correo.com").store_until is None
    # The same notification again (Pub/Sub delivers at least once): same result.
    assert notify(play, notification("tok-1", 12, "m-2")).status_code == 200
    assert play.get("/v1/me", headers=user).json()["paid"] is False


def test_refund_keeps_other_payments(play):
    user = register(play)
    with play.app.state.services.db.session() as s:
        from sqlalchemy import select

        u = s.execute(select(User).where(User.email == "ana@correo.com")).scalar_one()
        u.paid_until = utcnow() + timedelta(days=10)                  # paid by transfer in Montevideo
    play.google.buy("tok-1", account_id(play, user))
    play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"})
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_EXPIRED"
    assert notify(play, notification("tok-1", 12)).status_code == 200
    me = play.get("/v1/me", headers=user).json()
    assert me["paid"] is True and me["paid_until"].startswith((utcnow() + timedelta(days=10)).strftime("%Y-%m-%d"))


def test_on_hold_and_recovered(play):
    user = register(play)
    play.google.buy("tok-1", account_id(play, user))
    play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"})
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_ON_HOLD"
    play.google.subs["tok-1"]["lineItems"][0]["expiryTime"] = rfc3339(utcnow() - timedelta(days=1))
    notify(play, notification("tok-1", 5))
    assert play.get("/v1/me", headers=user).json()["paid"] is False
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_ACTIVE"
    play.google.subs["tok-1"]["lineItems"][0]["expiryTime"] = rfc3339(utcnow() + timedelta(days=30))
    notify(play, notification("tok-1", 1))
    assert play.get("/v1/me", headers=user).json()["paid"] is True


def test_upgrade_replaces_the_old_token(play):
    user = register(play)
    acc = account_id(play, user)
    play.google.buy("tok-1", acc, "tisma_basico")
    play.post("/v1/billing/play/purchase", headers=user, json={"purchase_token": "tok-1"})
    # Upgrade to pro: a new token linked to the old one; Google notifies before the app reports it.
    play.google.buy("tok-2", acc, "tisma_pro", days=30, linked="tok-1")
    assert notify(play, notification("tok-2", 4)).status_code == 200
    me = play.get("/v1/me", headers=user).json()
    assert me["paid"] is True and me["plan"] == "pro"
    assert ("tisma_pro", "tok-2") in play.google.acks                  # acknowledged by the master, without the app
    with play.app.state.services.db.session() as s:
        from sqlalchemy import select

        states = dict(s.execute(select(PlaySubscription.product_id, PlaySubscription.state)).all())
    assert states == {"tisma_basico": "REPLACED", "tisma_pro": "SUBSCRIPTION_STATE_ACTIVE"}


def test_notifications_must_come_from_our_pubsub(play):
    user = register(play)
    play.google.buy("tok-1", account_id(play, user))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in (push_token(email="evil@example.com"), push_token(aud="https://otro"), push_token(verified=False),
                push_token(key=other)):
        assert notify(play, notification("tok-1", 4), bad).status_code == 401
    assert play.post("/v1/billing/play/notify", json=notification("tok-1", 4)).status_code == 401
    assert play.get("/v1/me", headers=user).json()["paid"] is False   # nothing was applied
    # Malformed message, test notification, a purchase of nobody: answered without error.
    assert notify(play, {"message": {"data": "%%%"}}).status_code == 400
    test = {"message": {"data": base64.b64encode(json.dumps({"version": "1.0", "packageName": PACKAGE,
            "testNotification": {"version": "1.0"}}).encode()).decode(), "messageId": "t"}}
    assert notify(play, test).status_code == 200
    play.google.buy("tok-nobody", "no-such-account")
    assert notify(play, notification("tok-nobody", 4)).status_code == 200
    assert ("tisma_basico", "tok-nobody") not in play.google.acks


def test_disabled_without_settings(settings):
    with TestClient(create_app(settings), client=INTERNET) as c:
        assert c.get("/v1/health").json()["play_payments"] is False
        assert c.post("/v1/billing/play/notify", json={}).status_code == 404
        user = register(c)
        assert c.get("/v1/billing/play/account", headers=user).status_code == 503
