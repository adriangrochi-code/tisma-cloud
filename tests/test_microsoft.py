"""Microsoft Store subscriptions (microsoft.py; cloud.md 5b): only what the Microsoft Store purchase API answers is
accepted. The tests answer as Microsoft documents its endpoints (learn.microsoft.com/windows/uwp/monetize/...)."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import Factory, new_app
from tisma_cloud import microsoft
from tisma_cloud.models import MicrosoftKey, MicrosoftSubscription, User, utcnow

SITE = {"Authorization": "Bearer site-secret"}
TENANT = "contoso.onmicrosoft.com"
CLIENT = "1d577369-5a3b-4492-8227-393bfef1e13d"
PRO, BASICO = "9NBLGGH52Q8X", "9NBLGGH4R315"


def iso(dt: datetime) -> str:
    # As Microsoft writes it: seven decimals and the offset.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "1+00:00"


class FakeMicrosoft:
    """Entra ID tokens, the purchase API (recurrences/query) and the key renewal."""

    def __init__(self) -> None:
        self.down = False
        self.keys: dict[str, str] = {}          # key -> Microsoft account
        self.subs: dict[str, list[dict]] = {}   # Microsoft account -> items
        self.refused: set[str] = set()
        self.calls: list[str] = []
        self.page_size = 25

    def key(self, account: str, user_id: str, days: float = 30, aud: str = microsoft.KEY_AUDIENCE,
            client: str = CLIENT.replace("-", "")) -> str:
        claims = {microsoft.CLAIM + "clientId": client, microsoft.CLAIM + "payload": base64.b64encode(account.encode()).decode(),
                  microsoft.CLAIM + "userId": user_id, microsoft.CLAIM + "refreshUri": microsoft.RENEW_URL,
                  "iat": int(time.time()), "iss": aud, "aud": aud, "exp": int(time.time() + days * 86400),
                  "nbf": int(time.time())}

        def part(d: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

        key = f"{part({'typ': 'JWT', 'alg': 'RS256'})}.{part(claims)}.{base64.urlsafe_b64encode(account.encode() + str(time.time()).encode()).rstrip(b'=').decode()}"
        self.keys[key] = account
        return key

    def subscribe(self, account: str, product: str = PRO, state: str = "Active", days: float = 30, sub_id: str = "",
                  grace_days: float | None = None) -> dict:
        until = datetime.now(timezone.utc) + timedelta(days=days)
        item = {"autoRenew": True, "beneficiary": "pub:x", "expirationTime": iso(until),
                "id": sub_id or f"mdr:0:{account}:{product}", "lastModified": iso(datetime.now(timezone.utc)),
                "market": "UY", "productId": product, "skuId": "0010", "startTime": iso(datetime.now(timezone.utc)),
                "recurrenceState": state, "isTrial": False}
        if grace_days is not None:
            item["expirationTimeWithGrace"] = iso(until + timedelta(days=grace_days))
        items = [i for i in self.subs.setdefault(account, []) if i["id"] != item["id"]]
        self.subs[account] = items + [item]
        return item

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        url = str(request.url)
        self.calls.append(url)
        if url == microsoft.TOKEN_URL.format(tenant=TENANT):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["client_id"] == CLIENT and form["client_secret"] == "s3cret"
            resource = httpx.QueryParams(request.content.decode())["resource"]
            return httpx.Response(200, json={"access_token": "tok:" + resource, "expires_in": "3599"})
        body = json.loads(request.content)
        if url == microsoft.RECURRENCES_URL:
            assert request.headers["Authorization"] == "Bearer tok:" + microsoft.AUDIENCE_API
            account = self.keys.get(body["b2bKey"])
            if account is None or body["b2bKey"] in self.refused:
                return httpx.Response(401, json={"code": "Unauthorized"})
            items = self.subs.get(account, [])
            start = int(body.get("continuationToken") or 0)
            page = items[start:start + self.page_size]
            out = {"items": page}
            if start + self.page_size < len(items):
                out["continuationToken"] = str(start + self.page_size)
            return httpx.Response(200, json=out)
        if url == microsoft.RENEW_URL:
            assert body["serviceTicket"] == "tok:" + microsoft.AUDIENCE_API
            account = self.keys.get(body["key"])
            if account is None or body["key"] in self.refused:
                return httpx.Response(401, json={"code": "Unauthorized", "innerError": "AuthenticationTokenInvalid"})
            user_id = microsoft.key_claims(body["key"])[microsoft.CLAIM + "userId"]
            return httpx.Response(200, json={"key": self.key(account, user_id)})
        return httpx.Response(404)


@pytest.fixture
def ms(settings):
    fake = FakeMicrosoft()
    app = new_app(settings)
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    app.state.services.microsoft = microsoft.store_for(TENANT, CLIENT, "s3cret", f"{BASICO}:basico, {PRO}:pro", http)
    with TestClient(app, client=("200.40.10.7", 1)) as c:
        for name in ("basico", "pro"):
            assert c.put(f"/v1/site/plans/{name}", headers=SITE, json={"monthly_hours": 3, "max_concurrent": 2,
                         "price_cents": 1000, "public": True}).status_code == 200
        c.fake = fake
        yield c


def ticket(c, user) -> dict:
    r = c.get("/v1/billing/microsoft/ticket", headers=user)
    assert r.status_code == 200, r.text
    return r.json()


def purchase(c, user, key: str):
    return c.post("/v1/billing/microsoft/purchase", headers=user, json={"purchase_key": key})


def test_a_subscription_bought_in_the_store_gives_the_plan(ms):
    f = Factory(ms)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    t = ticket(ms, user)
    assert t["service_ticket"] == "tok:" + microsoft.AUDIENCE_PURCHASE_KEY and t["expires"] > time.time()
    assert ticket(ms, user)["publisher_user_id"] == t["publisher_user_id"]
    ms.fake.subscribe("msa-ana", PRO)
    r = purchase(ms, user, ms.fake.key("msa-ana", t["publisher_user_id"]))
    assert r.status_code == 200, r.text
    assert r.json()["subscriptions"] == [{"plan": "pro", "state": "Active"}] and r.json()["paid"] is True
    assert ms.get("/v1/me", headers=user).json()["plan"] == "pro"
    assert ms.get("/v1/health").json()["microsoft_payments"] is True
    # The key is kept encrypted, never as it came.
    with ms.app.state.services.db.session() as s:
        raw = s.connection().exec_driver_sql("SELECT key FROM microsoft_keys").scalar_one()
        assert raw and "msa-ana" not in raw and raw != s.execute(select(MicrosoftKey.key)).scalar_one()


def test_keys_that_are_not_accepted(ms):
    f = Factory(ms)
    ana, bob = f.user("ana@x.com", paid_days=None), f.user("bob@x.com", paid_days=None)
    mine = ticket(ms, ana)["publisher_user_id"]
    ticket(ms, bob)
    ms.fake.subscribe("msa-ana", PRO)
    fake = ms.fake
    bad = {
        "another account's key": (bob, fake.key("msa-ana", mine)),
        "a collections key": (ana, fake.key("msa-ana", mine, aud="https://collections.mp.microsoft.com/v6.0/keys")),
        "another application": (ana, fake.key("msa-ana", mine, client="0" * 32)),
        "not a key": (ana, "x" * 40),
        "refused by Microsoft": (ana, "a" * 10 + "." + base64.urlsafe_b64encode(json.dumps(
            {"aud": microsoft.KEY_AUDIENCE, microsoft.CLAIM + "clientId": CLIENT, microsoft.CLAIM + "userId": mine,
             "exp": int(time.time()) + 3600}).encode()).decode().rstrip("=") + ".zz"),
    }
    for why, (user, key) in bad.items():
        r = purchase(ms, user, key)
        assert r.status_code == 422, (why, r.status_code, r.text)
    assert ms.get("/v1/me", headers=ana).json()["paid"] is False
    assert ms.get("/v1/me", headers=bob).json()["paid"] is False


def test_the_daily_check_follows_renewals_dunning_and_cancellation(ms):
    f = Factory(ms)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = ticket(ms, user)["publisher_user_id"]
    fake, services = ms.fake, ms.app.state.services
    fake.subscribe("msa-ana", PRO, days=2)
    assert purchase(ms, user, fake.key("msa-ana", acc)).status_code == 200
    # Not due yet: nobody is asked.
    calls = len(fake.calls)
    assert services.refresh_microsoft() == 0 and len(fake.calls) == calls
    tomorrow = utcnow() + timedelta(days=1, minutes=1)
    # Renewed by Microsoft: the access moves.
    fake.subscribe("msa-ana", PRO, days=32)
    assert services.refresh_microsoft(tomorrow) == 1
    with services.db.session() as s:
        assert s.execute(select(User).where(User.microsoft_account == acc)).scalar_one().store_until > \
            utcnow() + timedelta(days=31)
    # The payment fails: in dunning the access lasts until the end of the grace.
    fake.subscribe("msa-ana", PRO, state="InDunning", days=-1, grace_days=3)
    services.refresh_microsoft(tomorrow + timedelta(days=1, minutes=1))
    assert ms.get("/v1/me", headers=user).json()["paid"] is True
    # Canceled (refunded): no access, even with an expirationTime ahead.
    fake.subscribe("msa-ana", PRO, state="Canceled", days=10)
    services.refresh_microsoft(tomorrow + timedelta(days=2, minutes=2))
    assert ms.get("/v1/me", headers=user).json()["paid"] is False


def test_the_key_is_renewed_before_it_ends_and_dropped_when_refused(ms):
    f = Factory(ms)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = ticket(ms, user)["publisher_user_id"]
    fake, services = ms.fake, ms.app.state.services
    fake.subscribe("msa-ana", PRO, days=60)
    first = fake.key("msa-ana", acc, days=5)
    assert purchase(ms, user, first).status_code == 200
    services.refresh_microsoft(utcnow() + timedelta(days=1, minutes=1))
    assert microsoft.RENEW_URL in fake.calls
    with services.db.session() as s:
        row = s.execute(select(MicrosoftKey)).scalar_one()
        assert row.key != first and row.expires_at > utcnow() + timedelta(days=29)
        renewed = row.key
    # Microsoft revokes it: dropped; the access already given lasts (the app sends a new key when it starts).
    fake.refused.add(renewed)
    services.refresh_microsoft(utcnow() + timedelta(days=2, minutes=2))
    with services.db.session() as s:
        assert s.execute(select(func.count()).select_from(MicrosoftKey)).scalar_one() == 0
    assert ms.get("/v1/me", headers=user).json()["paid"] is True


def test_many_subscriptions_come_in_pages_and_others_products_are_ignored(ms):
    f = Factory(ms)
    user = f.user("ana@x.com", plan="basico", paid_days=None)
    acc = ticket(ms, user)["publisher_user_id"]
    fake = ms.fake
    fake.page_size = 2
    for i in range(4):
        fake.subscribe("msa-ana", "9OTHERAPP000", sub_id=f"other-{i}")
    fake.subscribe("msa-ana", BASICO, days=5)
    r = purchase(ms, user, fake.key("msa-ana", acc))
    assert r.json()["subscriptions"] == [{"plan": "basico", "state": "Active"}]


def test_one_microsoft_account_for_two_tisma_accounts(ms):
    f = Factory(ms)
    ana, bob = f.user("ana@x.com", paid_days=None), f.user("bob@x.com", paid_days=None)
    fake = ms.fake
    fake.subscribe("msa-shared", PRO)
    assert purchase(ms, ana, fake.key("msa-shared", ticket(ms, ana)["publisher_user_id"])).status_code == 200
    r = purchase(ms, bob, fake.key("msa-shared", ticket(ms, bob)["publisher_user_id"]))
    assert r.status_code == 200 and r.json()["subscriptions"] == []
    assert ms.get("/v1/me", headers=ana).json()["paid"] is True
    assert ms.get("/v1/me", headers=bob).json()["paid"] is False


def test_microsoft_down_and_store_off(ms, client, f):
    user = Factory(ms).user("ana@x.com", paid_days=None)
    acc = ticket(ms, user)["publisher_user_id"]
    key = ms.fake.key("msa-ana", acc)
    ms.fake.down = True
    assert purchase(ms, user, key).status_code == 503
    other = f.user("zed@x.com")
    assert client.get("/v1/billing/microsoft/ticket", headers=other).status_code == 404
    assert client.post("/v1/billing/microsoft/purchase", headers=other, json={"purchase_key": "x" * 30}).status_code == 404


def test_deleting_the_account_forgets_its_microsoft_store_data(ms):
    f = Factory(ms)
    user = f.user("ana@x.com", paid_days=None)
    acc = ticket(ms, user)["publisher_user_id"]
    ms.fake.subscribe("msa-ana", PRO)
    assert purchase(ms, user, ms.fake.key("msa-ana", acc)).status_code == 200
    services = ms.app.state.services
    with services.db.session() as s:
        uid = s.execute(select(User.id).where(User.microsoft_account == acc)).scalar_one()
    services.delete_account(uid)
    with services.db.session() as s:
        for table in (MicrosoftKey, MicrosoftSubscription):
            assert s.execute(select(func.count()).select_from(table)).scalar_one() == 0
        assert s.get(User, uid).microsoft_account is None


def test_microsoft_times():
    assert microsoft.parse_time("2017-06-11T03:07:49.2552941+00:00") == datetime(2017, 6, 11, 3, 7, 49, 255294)
    assert microsoft.parse_time("2017-06-11T03:07:49Z") == datetime(2017, 6, 11, 3, 7, 49)
    assert microsoft.parse_time("2017-06-11T00:07:49.1-03:00") == datetime(2017, 6, 11, 3, 7, 49, 100000)
    assert microsoft.parse_time("") is None and microsoft.parse_time("nope") is None


def test_one_account_that_fails_does_not_stop_the_others_nor_the_sweep(ms, monkeypatch):
    """An unexpected error with one account (a broken row, a bug) went out of the daily check and out of the sweep:
    the accounts after it were never asked, and the sweep's later work (the administrators' alerts) did not run."""
    f = Factory(ms)
    users = [f.user(f"u{i}@x.com", plan="basico", paid_days=None) for i in range(3)]
    fake, services = ms.fake, ms.app.state.services
    for i, user in enumerate(users):
        fake.subscribe(f"msa-{i}", PRO)
        assert purchase(ms, user, fake.key(f"msa-{i}", ticket(ms, user)["publisher_user_id"])).status_code == 200
    real = services.microsoft.refresh
    asked = []

    def refresh(session, user, now=None):  # noqa: ANN001, ANN202
        asked.append(user.id)
        if len(asked) == 1:
            raise RuntimeError("a broken row")
        return real(session, user, now)

    monkeypatch.setattr(services.microsoft, "refresh", refresh)
    later = utcnow() + timedelta(days=1, minutes=1)
    assert services.refresh_microsoft(later) == 3
    assert len(asked) == 3
    # And the sweep goes on to its alerts with it.
    alerts = []
    monkeypatch.setattr(services, "refresh_microsoft", lambda now=None: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(services, "check_alerts", lambda *a, **k: alerts.append(1), raising=False)
    services._microsoft_checked = None
    services._alerts_checked = None
    services.sweep()
    assert alerts
