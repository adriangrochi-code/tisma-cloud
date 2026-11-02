"""Hours given or taken by the administration (a reward or a penalty), disabling accounts and editing plans."""

from __future__ import annotations


def test_admin_gives_and_takes_hours(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user("eq@x.com", plan=f.plan("tiny", hours=0., concurrent=1))
    # No hours in the plan: the job is refused.
    f.submit(user, expect=402)
    r = client.post("/v1/admin/users/EQ@x.com/hours", headers=admin, json={"hours": 2.5, "note": "Good report"})
    assert r.status_code == 201, r.text
    assert r.json()["adjustment"]["hours"] == 2.5 and r.json()["account"]["hours_included"] == 2.5
    assert f.me(user)["hours_left"] == 2.5
    f.submit(user)
    # A penalty larger than what is left: nothing is left, never a negative balance.
    r = client.post("/v1/admin/users/eq@x.com/hours", headers=admin, json={"hours": -4, "note": "Abuse"})
    assert r.status_code == 201 and r.json()["account"]["hours_included"] == 0
    assert f.me(user)["hours_left"] == 0
    history = client.get("/v1/admin/users/eq@x.com/hours", headers=admin).json()["adjustments"]
    assert [a["hours"] for a in history] == [-4, 2.5] and history[0]["note"] == "Abuse"


def test_hours_on_top_of_the_plan(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user("ana@x.com", plan=f.plan("pro", hours=10.))
    client.post("/v1/admin/users/ana@x.com/hours", headers=admin, json={"hours": -3})
    assert f.me(user)["hours_included"] == 7


def test_hours_validation_and_rights(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user("eq@x.com")
    assert client.post("/v1/admin/users/eq@x.com/hours", headers=admin, json={"hours": 0}).status_code == 422
    assert client.post("/v1/admin/users/eq@x.com/hours", headers=admin, json={"hours": 20000}).status_code == 422
    assert client.post("/v1/admin/users/nobody@x.com/hours", headers=admin, json={"hours": 1}).status_code == 404
    assert client.post("/v1/admin/users/eq@x.com/hours", headers=user, json={"hours": 1}).status_code == 403
    assert client.get("/v1/admin/users/eq@x.com/hours", headers=user).status_code == 403


def test_admin_disables_and_enables_an_account(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user("eq@x.com")
    r = client.put("/v1/admin/users/eq@x.com/enabled", headers=admin, json={"enabled": False})
    assert r.status_code == 200 and not r.json()["user"]["enabled"]
    # Its open sessions are refused at once.
    assert client.get("/v1/me", headers=user).status_code == 401
    client.put("/v1/admin/users/eq@x.com/enabled", headers=admin, json={"enabled": True})
    assert client.get("/v1/me", headers=user).status_code == 200
    # An administrator is not disabled from here.
    assert client.put("/v1/admin/users/root@x.com/enabled", headers=admin, json={"enabled": False}).status_code == 422
    assert client.put("/v1/admin/users/eq@x.com/enabled", headers=user, json={"enabled": False}).status_code == 403


def test_admin_lists_and_edits_plans(client, f):
    admin = f.user("root@x.com", admin=True)
    f.plan("hidden", hours=1.)
    body = {"monthly_hours": 20, "max_concurrent": 2, "price_cents": 1000, "currency": "USD", "public": True}
    assert client.put("/v1/admin/plans/maker", headers=admin, json=body).status_code == 200
    names = {p["name"]: p for p in client.get("/v1/admin/plans", headers=admin).json()["plans"]}
    assert {"hidden", "maker"} <= set(names) and names["maker"]["monthly_hours"] == 20
    # Only the public plans are shown to users.
    public = {p["name"] for p in client.get("/v1/plans").json()["plans"]}
    assert "maker" in public and "hidden" not in public
    assert client.get("/v1/admin/plans", headers=f.user("eq@x.com")).status_code == 403
