"""One purchase route for every store (cloud.md 5c, CC21 to CC23): POST /v1/me/purchases, the products of each store in
GET /v1/plans and the subscription in GET /v1/me."""

from __future__ import annotations

from datetime import timedelta

from conftest import Factory
from test_apple import account_of as apple_account, store  # noqa: F401 (fixture)
from test_billing import register
from test_microsoft import PRO as MS_PRO, ms, ticket  # noqa: F401 (fixture)
from test_play import account_id, notification, notify, play, rfc3339  # noqa: F401 (fixture)
from tisma_cloud.models import utcnow


def buy(c, user, **body):
    return c.post("/v1/me/purchases", headers=user, json=body)


def test_cc21_cc22_cc23_google_play(play):
    ana, bob = register(play, "ana@correo.com"), register(play, "bob@correo.com")
    plans = {p["name"]: p for p in play.get("/v1/plans").json()["plans"]}
    assert plans["pro"]["store_products"] == {"google_play": "tisma_pro"}
    assert plans["pro"]["play_product"] == "tisma_pro"
    assert play.get("/v1/me", headers=ana).json()["subscription"] is None
    play.google.buy("tok-1", account_id(play, ana), "tisma_pro")
    play.google.subs["tok-1"]["lineItems"][0]["autoRenewingPlan"] = {"autoRenewEnabled": True}
    # CC21: confirmed by the store: the plan, its end, the subscription.
    r = buy(play, ana, store="google_play", product_id="tisma_pro", purchase_token="tok-1")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["plan"] == "pro" and me["paid"] is True and me["email"] == "ana@correo.com"
    sub = me["subscription"]
    assert sub["store"] == "google_play" and sub["product_id"] == "tisma_pro" and sub["auto_renew"] is True
    assert sub["expires"].startswith((utcnow() + timedelta(days=30)).strftime("%Y-%m-%d"))
    # CC22: the same purchase from another account / again from the same.
    r = buy(play, bob, store="google_play", product_id="tisma_pro", purchase_token="tok-1")
    assert r.status_code == 409 and r.json()["detail"] == "This purchase belongs to another account"
    again = buy(play, ana, store="google_play", product_id="tisma_pro", purchase_token="tok-1")
    assert again.status_code == 200 and again.json() == me
    # CC23: the store's refund notice: the free plan again, without the app.
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_EXPIRED"
    play.google.subs["tok-1"]["lineItems"][0]["expiryTime"] = rfc3339(utcnow() - timedelta(minutes=1))
    assert notify(play, notification("tok-1", 12)).status_code == 200
    me = play.get("/v1/me", headers=ana).json()
    assert me["subscription"] is None and me["paid"] is False and me["plan"] == "trial"


def test_what_the_store_does_not_confirm(play):
    ana = register(play, "ana@correo.com")
    acc = account_id(play, ana)
    play.google.buy("tok-1", acc, "tisma_basico")
    cases = {
        "unknown token": (dict(store="google_play", product_id="tisma_pro", purchase_token="nope"), 402),
        "another product than the one bought": (dict(store="google_play", product_id="tisma_pro",
                                                     purchase_token="tok-1"), 402),
        "no proof": (dict(store="google_play", product_id="tisma_pro"), 422),
        "the proof of another store": (dict(store="google_play", product_id="tisma_pro",
                                            signed_transaction="x" * 20), 422),
        "unknown store": (dict(store="paypal", product_id="x", purchase_token="tok-1"), 422),
        "a store not set up here": (dict(store="app_store", product_id="x", signed_transaction="x" * 20), 404),
    }
    for why, (body, code) in cases.items():
        r = buy(play, ana, **body)
        assert r.status_code == code, (why, r.status_code, r.text)
        if code == 402:
            assert r.json()["detail"] == "The store did not confirm this purchase"
    # The plan is not given by the product the app claims: the one Google confirmed is (basico).
    assert play.get("/v1/me", headers=ana).json()["plan"] == "basico"


def test_app_store_and_microsoft_store(store, ms):  # noqa: F811
    f = Factory(store)
    ana = f.user("ana@x.com", plan="basico", paid_days=None)
    tx = store.fake.transaction(apple_account(store, ana), "tisma_pro")
    r = buy(store, ana, store="app_store", product_id="tisma_pro", signed_transaction=tx)
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["store"] == "app_store" and r.json()["plan"] == "pro"
    bob = f.user("bob@x.com", paid_days=None)
    assert buy(store, bob, store="app_store", product_id="tisma_pro", signed_transaction=tx).status_code == 409

    g = Factory(ms)
    eva = g.user("eva@x.com", plan="basico", paid_days=None)
    ms.fake.subscribe("msa-eva", MS_PRO)
    ms.fake.subs["msa-eva"][0]["autoRenew"] = False
    key = ms.fake.key("msa-eva", ticket(ms, eva)["publisher_user_id"])
    r = buy(ms, eva, store="microsoft_store", product_id=MS_PRO, store_id_key=key)
    assert r.status_code == 200, r.text
    assert r.json()["subscription"] == {**r.json()["subscription"], "store": "microsoft_store", "product_id": MS_PRO,
                                        "auto_renew": False}
    zoe = g.user("zoe@x.com", paid_days=None)
    ticket(ms, zoe)
    assert buy(ms, zoe, store="microsoft_store", product_id=MS_PRO, store_id_key=key).status_code == 409
    plans = {p["name"]: p for p in ms.get("/v1/plans").json()["plans"]}
    # This server sells through both (the two fixtures share the settings): each store's own product.
    assert plans["pro"]["store_products"] == {"app_store": "tisma_pro", "microsoft_store": MS_PRO}


def test_a_manual_payment_and_a_store_subscription_together(play):
    """The plan is that of the access that lasts longest (cloud.md 5b): a manual payment of another plan recorded
    during a store subscription took the plan away from what the store keeps charging; and when the store's plan
    ended, the account stayed in it although only the manual payment of another plan was still paid."""
    from test_play import user_row

    ana = register(play, "ana@correo.com")
    play.google.buy("tok-1", account_id(play, ana), "tisma_pro", days=60)
    assert buy(play, ana, store="google_play", product_id="tisma_pro", purchase_token="tok-1").json()["plan"] == "pro"
    # One month of basico by transfer: Google still charges pro for 60 days.
    # (As the panel records it: the panel answers only on the local network, this client comes from internet.)
    from tisma_cloud import billing
    from tisma_cloud.models import Plan, User

    with play.app.state.services.db.session() as s:
        u = s.get(User, user_row(play, "ana@correo.com").id)
        billing.record_manual(s, u, s.get(Plan, "basico"), 1, 1000, "USD", "transfer")
    assert play.get("/v1/me", headers=ana).json()["plan"] == "pro"
    # Google refunds: basico, paid by the transfer, not pro nor the free plan.
    play.google.subs["tok-1"]["subscriptionState"] = "SUBSCRIPTION_STATE_EXPIRED"
    play.google.subs["tok-1"]["lineItems"][0]["expiryTime"] = rfc3339(utcnow() - timedelta(minutes=1))
    assert notify(play, notification("tok-1", 12)).status_code == 200
    me = play.get("/v1/me", headers=ana).json()
    assert me["plan"] == "basico" and me["paid"] is True and me["subscription"] is None
    assert user_row(play, "ana@correo.com").store_until is None
