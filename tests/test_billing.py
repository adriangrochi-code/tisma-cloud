from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_3mf
from fastapi.testclient import TestClient

from tisma_cloud import billing
from conftest import new_app as create_app
from tisma_cloud.models import Payment, User, utcnow

SITE = {"Authorization": "Bearer site-secret"}
INTERNET = ("200.40.10.7", 40000)


@pytest.fixture
def shop(settings):
    """A master with the test payment provider, seen from internet, with two public plans and a private one."""
    settings.billing_provider = "test"
    settings.public_url = "https://tismaservidores.net"
    app = create_app(settings)
    with TestClient(app, client=INTERNET) as c:
        for name, price, public in (("basico", 1000, True), ("pro", 2500, True), ("interno", 0, False)):
            assert c.put(f"/v1/site/plans/{name}", headers=SITE, json={"monthly_hours": 3, "max_concurrent": 2,
                         "price_cents": price, "public": public}).status_code == 200
        yield c


def register(c, email="ana@correo.com", password="una clave larga", plan=None) -> dict:
    """Creates an account and confirms it with the code of the e-mail."""
    r = c.post("/v1/auth/register", json={"email": email, "password": password, "plan": plan})
    assert r.status_code == 201, r.text
    code = last_code(c, email)
    r = c.post("/v1/auth/verify", json={"email": email.lower(), "code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def last_code(c, email: str) -> str:
    import re

    to, _subject, text = [m for m in c.app.state.services.mailer.sent if m[0] == email.lower()][-1]
    return re.search(r"\b(\d{6})\b", text).group(1)


def test_register_and_plans(shop):
    plans = shop.get("/v1/plans").json()["plans"]
    assert [p["name"] for p in plans] == ["basico", "pro"]          # public ones, cheapest first
    assert plans[0]["price_cents"] == 1000 and plans[0]["currency"] == "USD"
    user = register(shop, "Ana@Correo.com")
    me = shop.get("/v1/me", headers=user).json()
    assert me["email"] == "ana@correo.com" and me["plan"] == "basico" and me["paid"] is False
    # Not paid: no jobs yet.
    r = shop.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 402
    assert shop.post("/v1/auth/register", json={"email": "ana@correo.com", "password": "otra clave larga"}).status_code == 409
    assert shop.post("/v1/auth/register", json={"email": "no-es-correo", "password": "una clave larga"}).status_code == 422
    assert shop.post("/v1/auth/register", json={"email": "b@correo.com", "password": "corta"}).status_code == 422
    # A private plan cannot be chosen.
    assert shop.get("/v1/me", headers=register(shop, "c@correo.com", plan="interno")).json()["plan"] == "basico"
    assert shop.post("/v1/auth/login", json={"email": "ana@correo.com", "password": "una clave larga"}).status_code == 200
    assert shop.get("/cuenta").status_code == 200


def test_payment_with_the_test_provider(shop):
    user = register(shop)
    r = shop.post("/v1/billing/checkout", headers=user, json={"plan": "pro"})
    assert r.status_code == 200, r.text
    url, pid = r.json()["url"], r.json()["payment"]["id"]
    assert url == f"https://tismaservidores.net/billing/test/{pid}"
    assert r.json()["payment"]["amount_cents"] == 2500                     # the price of the plan, set by the master
    assert "USD 25.00" in shop.get(f"/billing/test/{pid}").text
    r = shop.post(f"/billing/test/{pid}/approved", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cuenta?pago=approved"
    me = shop.get("/v1/me", headers=user).json()
    assert me["paid"] and me["plan"] == "pro"
    until = me["paid_until"]
    # A notice applied twice does not pay twice.
    shop.post(f"/billing/test/{pid}/approved", follow_redirects=False)
    assert shop.get("/v1/me", headers=user).json()["paid_until"] == until
    pays = shop.get("/v1/billing/payments", headers=user).json()["payments"]
    assert [(p["status"], p["provider"], p["amount_cents"]) for p in pays] == [("approved", "test", 2500)]
    # Now the user computes.
    r = shop.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 201
    # A second month starts when the first ends.
    pid2 = shop.post("/v1/billing/checkout", headers=user, json={"plan": "pro"}).json()["payment"]["id"]
    shop.post(f"/billing/test/{pid2}/approved", follow_redirects=False)
    db = shop.app.state.services.db
    with db.session() as s:
        u = s.query(User).filter_by(email="ana@correo.com").one()
        assert abs((u.paid_until - utcnow()) - 2 * billing.MONTH) < timedelta(minutes=1)


def test_rejected_and_bad_notices(shop):
    user = register(shop)
    pid = shop.post("/v1/billing/checkout", headers=user, json={"plan": "basico"}).json()["payment"]["id"]
    shop.post(f"/billing/test/{pid}/rejected", follow_redirects=False)
    assert shop.get("/v1/me", headers=user).json()["paid"] is False
    assert shop.post("/v1/billing/checkout", headers=user, json={"plan": "interno"}).status_code == 422
    assert shop.post("/v1/billing/checkout", json={"plan": "basico"}).status_code == 401
    assert shop.post("/v1/billing/webhook/otro", content=b"{}").status_code == 404
    db = shop.app.state.services.db
    pid = shop.post("/v1/billing/checkout", headers=user, json={"plan": "basico"}).json()["payment"]["id"]
    with db.session() as s:
        # A provider that says less was paid than the price: refused.
        with pytest.raises(billing.BillingError):
            billing.apply(s, billing.Notice(pid, "x1", "approved", 1, "USD"), "test")
        with pytest.raises(billing.BillingError):
            billing.apply(s, billing.Notice(pid, "x1", "approved", 1000, "UYU"), "test")
        with pytest.raises(billing.BillingError):
            billing.apply(s, billing.Notice("nope", "x1", "approved", 1000, "USD"), "test")
        assert s.get(Payment, pid).status == "pending"


def test_without_provider_payments_are_closed(client, f):
    user = f.user()
    assert client.post("/v1/billing/checkout", headers=user, json={"plan": "pro"}).status_code == 503
    assert client.get("/v1/health").json()["payments"] is False
    assert client.get("/billing/test/x").status_code == 404


def test_manual_payments_from_the_panel(client, f):
    admin = f.user("root@x.com", admin=True)
    f.user("eva@x.com", paid_days=None)
    r = client.post("/v1/admin/users/eva@x.com/payments", headers=admin,
                    json={"months": 2, "amount_cents": 2000, "currency": "usd", "note": "Transferencia BROU"})
    assert r.status_code == 201, r.text
    users = {u["email"]: u for u in client.get("/v1/admin/users", headers=admin).json()["users"]}
    assert users["eva@x.com"]["account"]["paid"] is True
    pays = client.get("/v1/admin/payments", headers=admin).json()["payments"]
    assert pays[0]["provider"] == "manual" and pays[0]["currency"] == "USD" and pays[0]["note"] == "Transferencia BROU"
    with TestClient(client.app, client=INTERNET) as internet:
        assert internet.post("/v1/admin/users/eva@x.com/payments", headers=admin,
                             json={"months": 1, "amount_cents": 0}).status_code == 404
