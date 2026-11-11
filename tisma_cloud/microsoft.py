"""Microsoft Store subscriptions (Windows; cloud.md 5b, owner 2026-11-10): the plan bought in the Store as a subscription
add-on of the app. The master accepts only what the Microsoft Store purchase API answers.

As Microsoft documents it (learn.microsoft.com/windows/uwp/monetize/view-and-grant-products-from-a-service,
.../get-subscriptions-for-a-user, .../renew-a-windows-store-id-key):
- the master has an Entra ID application (tenant, client id and secret) associated with the app in Partner Center, and
  makes access tokens with the client credentials flow: one for the audience https://onestore.microsoft.com (the calls of
  the API, kept in the master) and one for https://onestore.microsoft.com/b2b/keys/create/purchase, which the app passes
  to StoreContext.GetCustomerPurchaseIdAsync with publisherUserId = User.microsoft_account (a random id of the account);
- the app sends back that Microsoft Store ID key (a JWT valid 30 days, renewable before it ends); the master keeps it
  encrypted, checks its audience, our client id and the account id in it, and asks
  POST https://purchase.mp.microsoft.com/v8.0/b2b/recurrences/query for the subscriptions of that Microsoft account;
- Microsoft sends no notifications for these subscriptions: the master asks again every REFRESH_S (renewing the key
  before it ends) and whenever the app sends a key. Active and InDunning give access until expirationTimeWithGrace (or
  expirationTime); Inactive, Canceled and Failed are terminal (Canceled moves expirationTime to the cancellation).
Subscription add-ons need a developer account Microsoft enabled for them (the same page says so).
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MicrosoftKey, MicrosoftSubscription, Plan, User, utcnow

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/token"
AUDIENCE_API = "https://onestore.microsoft.com"
AUDIENCE_PURCHASE_KEY = "https://onestore.microsoft.com/b2b/keys/create/purchase"
RECURRENCES_URL = "https://purchase.mp.microsoft.com/v8.0/b2b/recurrences/query"
RENEW_URL = "https://purchase.mp.microsoft.com/v6.0/b2b/keys/renew"
KEY_AUDIENCE = "https://purchase.mp.microsoft.com/v6.0/keys"
CLAIM = "http://schemas.microsoft.com/marketplace/2015/08/claims/key/"
ENTITLED = {"Active", "InDunning"}
REFRESH_S = 24 * 3600
RENEW_BEFORE = timedelta(days=7)
MAX_PAGES = 20


class MicrosoftError(Exception):
    """A key or a purchase the master refuses (the message says why)."""


class MicrosoftOtherAccount(MicrosoftError):
    """The key is of another account."""


class MicrosoftUnavailable(Exception):
    """Microsoft could not be asked now (network, credentials): try again later."""


def parse_products(text: str) -> dict[str, str]:
    """"Store ID of the add-on:plan, ..." -> {store id: plan}."""
    out = {}
    for part in text.split(","):
        if ":" in part:
            product, plan = (x.strip() for x in part.split(":", 1))
            if product and plan:
                out[product] = plan
    return out


def parse_time(text: str | None) -> datetime | None:
    """ISO 8601 of Microsoft ("2017-06-11T03:07:49.2552941+00:00", seven decimals) -> naive UTC."""
    if not text:
        return None
    text = text.strip().replace("Z", "+00:00")
    main, dot, rest = text.partition(".")
    if dot:
        # Python reads up to six decimals: the leading digits are cut, the offset after them kept.
        n = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{main}.{rest[:n][:6].ljust(6, '0')}{rest[n:]}"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def key_claims(key: str) -> dict:
    """The claims of a Microsoft Store ID key, read without its signature (Microsoft checks it when the key is used)."""
    parts = key.split(".")
    if len(parts) != 3:
        raise MicrosoftError("Not a Microsoft Store ID key")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        raise MicrosoftError("Not a Microsoft Store ID key") from None
    if not isinstance(claims, dict):
        raise MicrosoftError("Not a Microsoft Store ID key")
    return claims


def microsoft_account(session: Session, user: User) -> str:
    """The publisherUserId of the account (random: Microsoft never learns who it is), made the first time."""
    if not user.microsoft_account:
        user.microsoft_account = secrets.token_hex(16)
        session.flush()
    return user.microsoft_account


class Tokens:
    """Entra ID access tokens of the master's application (client credentials), cached until a minute before they end."""

    def __init__(self, tenant: str, client_id: str, secret: str, http: httpx.Client) -> None:
        self.url = TOKEN_URL.format(tenant=tenant)
        self.client_id = client_id
        self.secret = secret
        self.http = http
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def token(self, resource: str) -> tuple[str, float]:
        """(access token, when it ends as a time.time())."""
        with self._lock:
            hit = self._cache.get(resource)
            if hit and time.time() < hit[1] - 60:
                return hit
            try:
                r = self.http.post(self.url, data={"grant_type": "client_credentials", "client_id": self.client_id,
                                                   "client_secret": self.secret, "resource": resource})
            except httpx.HTTPError as ex:
                raise MicrosoftUnavailable(f"Entra ID token: {ex}") from None
            if r.status_code != 200:
                raise MicrosoftUnavailable(f"Entra ID token: HTTP {r.status_code}")
            data = r.json()
            hit = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
            self._cache[resource] = hit
            return hit


@dataclass
class Store:
    """The Microsoft Store on the master: our Entra ID application and our subscription add-ons (store id -> plan)."""

    tokens: Tokens
    client_id: str
    products: dict[str, str]
    http: httpx.Client
    _locks: dict[int, threading.Lock] = field(default_factory=dict)

    def service_ticket(self) -> dict:
        """What the app passes to GetCustomerPurchaseIdAsync (with the account's publisherUserId)."""
        token, until = self.tokens.token(AUDIENCE_PURCHASE_KEY)
        return {"service_ticket": token, "expires": int(until)}

    def _check_key(self, user: User, key: str) -> datetime | None:
        claims = key_claims(key)
        if claims.get("aud") != KEY_AUDIENCE:
            raise MicrosoftError("Not a key of the Microsoft Store purchase API")
        # Microsoft's example shows the application id without its dashes.
        if str(claims.get(CLAIM + "clientId", "")).replace("-", "").lower() != self.client_id.replace("-", "").lower():
            raise MicrosoftError("The key is not of our application")
        if not user.microsoft_account or claims.get(CLAIM + "userId") != user.microsoft_account:
            raise MicrosoftOtherAccount("The key is not of this account")
        exp = claims.get("exp")
        return datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None) if isinstance(exp, (int, float)) \
            else None

    def _post(self, url: str, body: dict, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.tokens.token(AUDIENCE_API)[0]
        try:
            r = self.http.post(url, json=body, headers=headers)
        except httpx.HTTPError as ex:
            raise MicrosoftUnavailable(f"Microsoft Store: {ex}") from None
        if r.status_code in (400, 401, 403):
            raise MicrosoftError(f"Microsoft Store refused the key (HTTP {r.status_code})")
        if r.status_code != 200:
            raise MicrosoftUnavailable(f"Microsoft Store: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise MicrosoftUnavailable("Microsoft Store: not JSON") from None

    def subscriptions(self, key: str) -> list[dict]:
        items, token = [], None
        for _ in range(MAX_PAGES):
            body = {"b2bKey": key}
            if token:
                body["continuationToken"] = token
            data = self._post(RECURRENCES_URL, body)
            items += [i for i in data.get("items") or [] if isinstance(i, dict)]
            token = data.get("continuationToken")
            if not token:
                break
        return items

    def link(self, session: Session, user: User, key: str) -> list[MicrosoftSubscription]:
        """A purchase key the app of this user sends: kept, and the subscriptions of that Microsoft account applied."""
        microsoft_account(session, user)
        expires = self._check_key(user, key)
        row = session.get(MicrosoftKey, user.id)
        if row is None:
            row = MicrosoftKey(user_id=user.id)
            session.add(row)
        row.key, row.expires_at, row.checked_at = key, expires, utcnow()
        session.flush()
        applied = self._apply(session, user, self.subscriptions(key))
        self._keep_while_needed(session, row, applied)
        return applied

    @staticmethod
    def _keep_while_needed(session: Session, row: MicrosoftKey, applied: list[MicrosoftSubscription]) -> None:
        """The key is a credential of the person's Microsoft account: kept only while a subscription of ours gives
        access (to follow its renewal); the app sends it again at its next start."""
        now = utcnow()
        if not any(sub.entitled_until is not None and sub.entitled_until > now for sub in applied):
            session.delete(row)
            session.flush()

    def refresh(self, session: Session, user: User, now: datetime | None = None) -> list[MicrosoftSubscription] | None:
        """Asks Microsoft again with the kept key (renewed first when it ends within RENEW_BEFORE). A key Microsoft
        refuses is dropped and None returned (the app sends a new one); the access already given lasts until its end."""
        now = now or utcnow()
        row = session.get(MicrosoftKey, user.id)
        if row is None:
            return []
        try:
            if row.expires_at is None or row.expires_at - now < RENEW_BEFORE:
                renewed = self._post(RENEW_URL, {"serviceTicket": self.tokens.token(AUDIENCE_API)[0],
                                                 "key": row.key}, auth=False).get("key")
                if not isinstance(renewed, str) or not renewed:
                    raise MicrosoftError("Microsoft Store renewed no key")
                row.expires_at = self._check_key(user, renewed)
                row.key = renewed
            items = self.subscriptions(row.key)
        except MicrosoftError:
            session.delete(row)
            session.flush()
            return None
        row.checked_at = now
        applied = self._apply(session, user, items)
        self._keep_while_needed(session, row, applied)
        return applied

    def _apply(self, session: Session, user: User, items: list[dict]) -> list[MicrosoftSubscription]:
        applied = []
        for item in items:
            product = str(item.get("productId") or "")
            plan_name = self.products.get(product)
            sub_id = str(item.get("id") or "")
            if plan_name is None or not sub_id or session.get(Plan, plan_name) is None:
                continue
            sub = session.execute(select(MicrosoftSubscription).where(
                MicrosoftSubscription.subscription_id == sub_id)).scalar_one_or_none()
            if sub is not None and sub.user_id != user.id:
                # The same Microsoft account used with two Tisma accounts: the subscription stays with the first.
                continue
            if sub is None:
                sub = MicrosoftSubscription(user_id=user.id, subscription_id=sub_id)
                session.add(sub)
            state = str(item.get("recurrenceState") or "")
            until = parse_time(item.get("expirationTimeWithGrace")) or parse_time(item.get("expirationTime"))
            sub.product_id, sub.plan_name, sub.state = product, plan_name, state[:20]
            sub.entitled_until = until if state in ENTITLED else None
            sub.auto_renew = bool(item["autoRenew"]) if isinstance(item.get("autoRenew"), bool) else None
            sub.updated_at = utcnow()
            applied.append(sub)
        session.flush()
        from .stores import update_access

        update_access(session, user)
        return applied

    def due(self, session: Session, now: datetime | None = None) -> list[int]:
        """The accounts whose key was not asked about for REFRESH_S."""
        now = now or utcnow()
        limit = now - timedelta(seconds=REFRESH_S)
        return list(session.execute(select(MicrosoftKey.user_id).where(
            (MicrosoftKey.checked_at.is_(None)) | (MicrosoftKey.checked_at < limit))).scalars())


def store_for(tenant: str, client_id: str, secret: str, products: str, http: httpx.Client | None = None) -> Store | None:
    """The Microsoft Store of the settings, or None when it is not set up."""
    if not (tenant and client_id and secret and products):
        return None
    http = http or httpx.Client(timeout=30)
    return Store(Tokens(tenant, client_id, secret, http), client_id, parse_products(products), http)
