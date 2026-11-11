"""App Store auto-renewable subscriptions (iPhone and Mac; cloud.md 5b, spec/next cada191): the master accepts only what
Apple signed.

- The app buys with StoreKit and sends the signed transaction (Transaction.jwsRepresentation) with the account's
  appAccountToken in it (a random UUID of the account, GET /v1/billing/apple/account: Apple never learns who it is).
- App Store Server Notifications v2 (renewals, expiry, refunds, changes) arrive at POST /v1/billing/apple/notify.
- Both are JWS signed by Apple with its certificate chain in the header; Apple's own library (app-store-server-library,
  MIT) checks the chain up to Apple Root CA - G3 and Apple's marks on its certificates, the bundle id and the environment.
- The access is the transaction's expiresDate, unless Apple revoked it (a refund); a transaction signed before the one
  applied last is not applied (notifications may arrive out of order).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppleSubscription, Plan, User, utcnow

# Apple Root CA - G3, from https://www.apple.com/certificateauthority/AppleRootCA-G3.cer (2026-11-10).
ROOT_CERT = Path(__file__).parent / "data" / "AppleRootCA-G3.cer"


class AppleError(Exception):
    """Not a purchase this master accepts (the message says why)."""


class AppleOtherAccount(AppleError):
    """The purchase is of another account."""


def apple_account(session: Session, user: User) -> str:
    """The appAccountToken of the account (a random UUID), made the first time."""
    if not user.apple_account:
        user.apple_account = str(uuid.uuid4())
        session.flush()
    return user.apple_account


def parse_products(text: str) -> dict[str, str]:
    """«product id:plan, ...» -> {product id: plan}."""
    out = {}
    for part in text.split(","):
        if ":" in part:
            product, plan = (x.strip() for x in part.split(":", 1))
            if product and plan:
                out[product] = plan
    return out


def _time(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _value(x) -> str:  # noqa: ANN001
    """An enum of the library (or its raw value when Apple sent one it does not know) as text."""
    return str(getattr(x, "value", x) or "")


@dataclass
class Store:
    """The App Store on the master: Apple's verifier for our app and our products (product id -> plan)."""

    verifier: object
    products: dict[str, str]

    def _decode_transaction(self, signed: str):  # noqa: ANN202
        from appstoreserverlibrary.signed_data_verifier import VerificationException

        try:
            return self.verifier.verify_and_decode_signed_transaction(signed)
        except VerificationException as ex:
            raise AppleError(f"Not a transaction signed by Apple for this app ({ex.status.name})") from None

    def link(self, session: Session, user: User, signed_transaction: str) -> AppleSubscription:
        """A purchase the app of this user reports: Apple's signed transaction, of this account."""
        tx = self._decode_transaction(signed_transaction)
        if not tx.appAccountToken or str(tx.appAccountToken).lower() != apple_account(session, user).lower():
            raise AppleOtherAccount("The purchase is not of this account")
        return self._apply(session, user, tx)

    def notify(self, session: Session, signed_payload: str) -> AppleSubscription | None:
        """A notification v2 of Apple: the transaction it carries is applied to its account (found by its
        appAccountToken, or by the subscription already known). None when it is of no account or has no transaction."""
        from appstoreserverlibrary.signed_data_verifier import VerificationException

        try:
            note = self.verifier.verify_and_decode_notification(signed_payload)
        except VerificationException as ex:
            raise AppleError(f"Not a notification signed by Apple for this app ({ex.status.name})") from None
        signed = getattr(note.data, "signedTransactionInfo", None) if note.data else None
        if not signed:
            return None
        tx = self._decode_transaction(signed)
        sub = session.execute(select(AppleSubscription).where(
            AppleSubscription.original_transaction_id == str(tx.originalTransactionId))).scalar_one_or_none()
        if sub is not None:
            user = session.get(User, sub.user_id)
        elif tx.appAccountToken:
            user = session.execute(select(User).where(User.apple_account == str(tx.appAccountToken).lower())
                                   ).scalar_one_or_none()
        else:
            user = None
        if user is None:
            return None
        sub = self._apply(session, user, tx)
        renewal = getattr(note.data, "signedRenewalInfo", None)
        if renewal:
            try:
                info = self.verifier.verify_and_decode_renewal_info(renewal)
            except VerificationException:
                info = None
            status = getattr(info, "autoRenewStatus", None) if info is not None else None
            if status is not None:
                # AutoRenewStatus: 1 on, 0 off (App Store Server API, JWSRenewalInfoDecodedPayload).
                sub.auto_renew = _value(status) == "1"
        return sub

    def _apply(self, session: Session, user: User, tx) -> AppleSubscription:  # noqa: ANN001
        product = tx.productId or ""
        plan_name = self.products.get(product)
        if plan_name is None or session.get(Plan, plan_name) is None:
            raise AppleError("Not one of our products")
        original = str(tx.originalTransactionId or "")
        if not original:
            raise AppleError("The transaction has no originalTransactionId")
        sub = session.execute(select(AppleSubscription).where(
            AppleSubscription.original_transaction_id == original)).scalar_one_or_none()
        if sub is not None and sub.user_id != user.id:
            raise AppleOtherAccount("The purchase is of another account")
        signed_at = _time(tx.signedDate)
        if sub is None:
            sub = AppleSubscription(user_id=user.id, original_transaction_id=original, product_id=product,
                                    plan_name=plan_name)
            session.add(sub)
        elif sub.signed_at and signed_at and signed_at < sub.signed_at:
            # An older transaction arriving after a newer one: it does not undo it.
            return sub
        revoked = tx.revocationDate is not None
        expires = _time(tx.expiresDate)
        sub.transaction_id = str(tx.transactionId or "")
        sub.product_id = product
        sub.plan_name = plan_name
        sub.environment = _value(tx.environment)
        sub.revoked = revoked
        sub.entitled_until = None if revoked else expires
        sub.signed_at = signed_at
        sub.updated_at = utcnow()
        session.flush()
        from .stores import update_access

        update_access(session, user)
        return sub


def store_for(bundle_id: str, app_apple_id: str, environment: str, products: str, root_certs: str = "",
              online_checks: bool = True) -> Store | None:
    """The App Store of the settings, or None when it is not set up (no bundle id or no products)."""
    if not bundle_id or not products:
        return None
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    env = {"production": Environment.PRODUCTION, "sandbox": Environment.SANDBOX}.get(environment.strip().lower())
    if env is None:
        raise ValueError("TISMA_APPLE_ENVIRONMENT is Production or Sandbox")
    paths = [Path(p.strip()) for p in root_certs.split(",") if p.strip()] or [ROOT_CERT]
    roots = [p.read_bytes() for p in paths]
    app_id = int(app_apple_id) if app_apple_id.strip() else None
    verifier = SignedDataVerifier(roots, online_checks, env, bundle_id, app_id)
    return Store(verifier, parse_products(products))
