"""Subscriptions bought with Google Play (the phone app), on the master. Google charges the user (cards, carrier billing,
Google Play balance, in the countries where Play sells) and the master only verifies the purchase and gives the access,
which then works everywhere the account is used (the app, Tisma, the account page).

As Google documents it (developer.android.com/google/play/billing/backend, .../lifecycle/subscriptions,
.../getting-ready#configure-rtdn):
- the app buys with obfuscatedAccountId = User.play_account and sends the purchase token to the master;
- the master asks the Google Play Developer API (purchases.subscriptionsv2.get) for the state of the token, checks that
  it belongs to this account and to one of our products, acknowledges a new purchase (it is refunded after 3 days
  otherwise) and gives access until expiryTime while the state gives access (active, grace period, canceled but not yet
  expired);
- Google sends Real-time developer notifications through a Cloud Pub/Sub push subscription; each one is only a hint:
  the master asks the API again for the state of the token.
The API is called with a token of a service account (OAuth 2.0 JWT bearer, developers.google.com/identity/protocols/
oauth2/service-account#httprest). The push of Pub/Sub carries an OIDC token of a service account, verified like the
tokens of "Sign in with Google" (cloud.google.com/pubsub/docs/authenticate-push-subscriptions).
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .google_auth import GOOGLE_CERTS, GOOGLE_ISSUERS
from .models import Plan, PlaySubscription, User, utcnow
from .security import hash_token

API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# States of subscriptionsv2 in which the user keeps the access until expiryTime.
ENTITLED = {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"}


class PlayError(Exception):
    """A purchase the master refuses (not ours, not of this account...)."""


class PlayUnavailable(Exception):
    """Google could not be asked now (network, credentials): try again later."""


def parse_products(text: str) -> dict[str, str]:
    """"product:plan,product:plan" -> {product: plan}."""
    products = {}
    for item in text.split(","):
        if ":" in item:
            product, plan = (p.strip() for p in item.split(":", 1))
            if product and plan:
                products[product] = plan
    return products


def parse_time(text: str | None) -> datetime | None:
    """RFC 3339 of Google ("2026-11-20T10:00:00.123Z") -> naive UTC like the rest of the database."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


class ServiceAccount:
    """Access tokens of a service account (its JSON key file of Google Cloud), cached until a minute before they end."""

    def __init__(self, info: dict, http: httpx.Client) -> None:
        self.email = info["client_email"]
        self.key = info["private_key"]
        self.key_id = info.get("private_key_id")
        self.token_uri = info.get("token_uri") or TOKEN_URL
        self.http = http
        self._token = ""
        self._until = 0.
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._until - 60:
                return self._token
            now = int(time.time())
            assertion = jwt.encode({"iss": self.email, "scope": SCOPE, "aud": self.token_uri, "iat": now,
                                    "exp": now + 3600}, self.key, algorithm="RS256",
                                   headers={"kid": self.key_id} if self.key_id else None)
            try:
                r = self.http.post(self.token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                         "assertion": assertion})
            except httpx.HTTPError as ex:
                raise PlayUnavailable(f"Google token: {ex}") from None
            if r.status_code != 200:
                raise PlayUnavailable(f"Google token: HTTP {r.status_code}")
            data = r.json()
            self._token = data["access_token"]
            self._until = now + int(data.get("expires_in", 3600))
            return self._token


class PlayClient:
    """The calls of the Google Play Developer API the master uses."""

    def __init__(self, package: str, account: ServiceAccount, http: httpx.Client, api: str = API) -> None:
        self.package = package
        self.account = account
        self.http = http
        self.api = api.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.api}/applications/{quote(self.package, safe='')}/purchases/{path}"
        try:
            r = self.http.request(method, url, json=body, headers={"Authorization": f"Bearer {self.account.token()}"})
        except httpx.HTTPError as ex:
            raise PlayUnavailable(f"Google Play: {ex}") from None
        if r.status_code in (400, 404, 410):
            # An unknown or invalid token: not a purchase of this app.
            raise PlayError("Unknown purchase")
        if r.status_code != 200:
            raise PlayUnavailable(f"Google Play: HTTP {r.status_code}")
        return r.json() if r.content else {}

    def subscription(self, token: str) -> dict:
        return self._call("GET", f"subscriptionsv2/tokens/{quote(token, safe='')}")

    def acknowledge(self, product_id: str, token: str) -> None:
        self._call("POST", f"subscriptions/{quote(product_id, safe='')}/tokens/{quote(token, safe='')}:acknowledge", {})


class PushVerifier:
    """The OIDC token of a Pub/Sub push: signed by Google, for our audience, of the service account of the subscription."""

    def __init__(self, audience: str, email: str, jwk_client=None) -> None:  # noqa: ANN001
        self.audience = audience
        self.email = email
        self.jwk_client = jwk_client or jwt.PyJWKClient(GOOGLE_CERTS, cache_keys=True, lifespan=3600)

    def verify(self, authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise PlayError("No token")
        token = authorization[7:].strip()
        try:
            key = self.jwk_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience,
                                options={"require": ["iss", "aud", "exp"]}, leeway=30)
        except (jwt.PyJWTError, ValueError) as ex:
            raise PlayError(f"Invalid token: {ex}") from None
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise PlayError("Invalid token: issuer")
        if claims.get("email") != self.email or claims.get("email_verified") not in (True, "true"):
            raise PlayError("Invalid token: not our Pub/Sub service account")


def play_account(session: Session, user: User) -> str:
    """The id of the account for obfuscatedAccountId (random, so that Google never sees who the user is)."""
    if not user.play_account:
        user.play_account = secrets.token_hex(16)
        session.flush()
    return user.play_account


@dataclass
class Store:
    """Google Play on the master: the API client and our products (product id of Play -> plan)."""

    client: PlayClient
    products: dict[str, str]

    def link(self, session: Session, user: User, token: str) -> PlaySubscription:
        """A purchase the app of this user reports: verified, acknowledged and applied."""
        data = self.client.subscription(token)
        owner = (data.get("externalAccountIdentifiers") or {}).get("obfuscatedExternalAccountId")
        if not owner or owner != play_account(session, user):
            raise PlayError("The purchase is not of this account")
        sub = session.execute(select(PlaySubscription).where(PlaySubscription.token_hash == hash_token(token))
                              ).scalar_one_or_none()
        if sub is not None and sub.user_id != user.id:
            raise PlayError("The purchase is of another account")
        return self._apply(session, user, token, data, sub)

    def refresh(self, session: Session, token: str) -> PlaySubscription | None:
        """A notification of Google about a token: its state is asked again. A token not linked yet (bought outside the
        app, or the app did not report it) is linked by its obfuscatedAccountId, or by the token it replaces."""
        sub = session.execute(select(PlaySubscription).where(PlaySubscription.token_hash == hash_token(token))
                              ).scalar_one_or_none()
        data = self.client.subscription(token)
        if sub is not None:
            user = session.get(User, sub.user_id)
        else:
            user = self._owner(session, data)
            if user is None:
                return None
        return self._apply(session, user, token, data, sub)

    def _owner(self, session: Session, data: dict) -> User | None:
        for ids in (data.get("externalAccountIdentifiers"),
                    (data.get("outOfAppPurchaseContext") or {}).get("expiredExternalAccountIdentifiers")):
            account = (ids or {}).get("obfuscatedExternalAccountId")
            if account:
                user = session.execute(select(User).where(User.play_account == account)).scalar_one_or_none()
                if user is not None:
                    return user
        for old in (data.get("linkedPurchaseToken"),
                    (data.get("outOfAppPurchaseContext") or {}).get("expiredPurchaseToken")):
            if old:
                previous = session.execute(select(PlaySubscription).where(
                    PlaySubscription.token_hash == hash_token(old))).scalar_one_or_none()
                if previous is not None:
                    return session.get(User, previous.user_id)
        return None

    def _apply(self, session: Session, user: User, token: str, data: dict,
               sub: PlaySubscription | None) -> PlaySubscription:
        items = data.get("lineItems") or []
        item = max(items, key=lambda i: parse_time(i.get("expiryTime")) or datetime.min, default=None)
        product = (item or {}).get("productId", "")
        plan_name = self.products.get(product)
        if plan_name is None or session.get(Plan, plan_name) is None:
            raise PlayError("Not one of our products")
        state = data.get("subscriptionState", "")
        expiry = parse_time(item.get("expiryTime"))
        if sub is None:
            sub = PlaySubscription(user_id=user.id, token_hash=hash_token(token), purchase_token=token)
            session.add(sub)
        sub.product_id = product
        sub.plan_name = plan_name
        if sub.state != "REPLACED":
            sub.state = state
        sub.order_id = item.get("latestSuccessfulOrderId", "") or sub.order_id or ""
        sub.entitled_until = expiry if state in ENTITLED and sub.state != "REPLACED" and expiry else None
        sub.updated_at = utcnow()
        # An upgrade, downgrade or resubscription replaces the previous token (it gives no access any more).
        linked = data.get("linkedPurchaseToken")
        if linked:
            old = session.execute(select(PlaySubscription).where(PlaySubscription.token_hash == hash_token(linked))
                                  ).scalar_one_or_none()
            if old is not None and old.user_id == user.id:
                old.state = "REPLACED"
                old.entitled_until = None
        acknowledged = data.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED"
        if not acknowledged and state in ENTITLED:
            self.client.acknowledge(product, token)
            acknowledged = True
        sub.acknowledged = acknowledged
        session.flush()
        self._update_user(session, user)
        return sub

    @staticmethod
    def _update_user(session: Session, user: User) -> None:
        """The access of the store is that of the subscription that lasts longest; its plan becomes the plan of the user."""
        now = utcnow()
        best = session.execute(select(PlaySubscription).where(
            PlaySubscription.user_id == user.id, PlaySubscription.entitled_until.is_not(None),
            PlaySubscription.entitled_until > now).order_by(PlaySubscription.entitled_until.desc())).scalars().first()
        user.store_until = best.entitled_until if best else None
        if best is not None:
            user.plan = session.get(Plan, best.plan_name)


def notification_token(body: bytes) -> tuple[str, str | None]:
    """The message id of a Pub/Sub push and the purchase token of its developer notification (None: a test, or a
    notification of a one-time product)."""
    try:
        envelope = json.loads(body)
        message = envelope["message"]
        notification = json.loads(base64.b64decode(message["data"]))
    except (ValueError, KeyError, TypeError) as ex:
        raise PlayError(f"Not a Pub/Sub message: {ex}") from None
    token = ((notification.get("subscriptionNotification") or {}).get("purchaseToken")
             or (notification.get("voidedPurchaseNotification") or {}).get("purchaseToken"))
    return str(message.get("messageId", "")), token


def store_for(package: str, credentials_file: str, products: str, http: httpx.Client | None = None,
              api: str = API) -> Store | None:
    if not package or not credentials_file:
        return None
    with open(credentials_file, encoding="utf-8") as f:
        info = json.load(f)
    http = http or httpx.Client(timeout=20.)
    return Store(PlayClient(package, ServiceAccount(info, http), http, api), parse_products(products))
