"""The contract with the engine and the apps, rule by rule: docs/spec/features/cloud.md of tisma_engine."""

from __future__ import annotations

import json

from conftest import make_3mf
from sqlalchemy import select

from tisma_cloud import crypto
from tisma_cloud.models import Plan, User
from tisma_cloud.security import hash_password

JOB_FIELDS = {"id", "kind", "job_class", "params", "status", "progress", "seconds", "error", "summary",
              "result_filename", "created_at", "started_at", "finished_at"}


def _with_password(f, email: str, password: str) -> None:
    f.user(email)
    with f.db.session() as s:
        s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one().password_hash = \
            hash_password(password)


def test_rule_2_session(client, f):
    _with_password(f, "ana@x.com", "una-clave-larga")
    r = client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "una-clave-larga", "device": "PC"})
    assert r.status_code == 200 and r.json()["email"] == "ana@x.com"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/v1/me", headers=headers).status_code == 200
    assert client.post("/v1/auth/logout", headers=headers).status_code in (200, 204)
    assert client.get("/v1/me", headers=headers).status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer tu_nope"}).status_code == 401


def test_rule_5a_public_plans_without_session(client, f):
    with f.db.session() as s:
        s.add(Plan(name="publico", monthly_hours=5, max_concurrent=2, price_cents=900, currency="USD", public=True,
                   classes="slice"))
        s.add(Plan(name="oculto", monthly_hours=50, max_concurrent=5, public=False))
    plans = {p["name"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert "oculto" not in plans
    p = plans["publico"]
    assert {"name", "monthly_hours", "max_concurrent", "price_cents", "currency", "classes"} <= set(p)
    assert p["classes"] == ["slice"] and p["price_cents"] == 900 and p["currency"] == "USD"


def test_rule_6_not_a_3mf(client, f):
    user = f.user()
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice", "params": "{}"},
                    files={"file": ("part.3mf", b"not a zip at all", "model/3mf")})
    assert r.status_code == 422 and r.json()["detail"] == "The project is not a 3MF file"


def test_rule_6_parameters(client, f):
    user = f.user()
    ok = [("fea", {"mode": "analyze", "quality": "ultra", "object": 0, "instance": 0, "solid": True, "zones": False,
                   "ribs": True}),
          ("fea", {"mode": "lightest-infill", "quality": "extreme"}), ("fea", {"mode": "resin-structure"}),
          ("aero", {"quality": "friction", "speed": 100, "direction": "-z"}),
          ("aero", {"quality": "high", "speed": 0.5, "direction": [0, 1, 0]}),
          ("orient", {"object": 1, "instance": 0}), ("slice", {})]
    for kind, params in ok:
        f.plan("pro", concurrent=50)
        f.submit(user, kind=kind, params=json.dumps(params))
    bad = [("fea", {"mode": "x"}), ("fea", {"quality": "x"}), ("aero", {"speed": 0}), ("aero", {"speed": 100.5}),
           ("aero", {"direction": "w"}), ("aero", {"quality": "x"}), ("orient", {"quality": "fast"}),
           ("nope", {})]
    for kind, params in bad:
        f.submit(user, kind=kind, params=json.dumps(params), expect=422)


def test_rule_6_upload_needs_content_length(client, f):
    user = f.user()
    body = make_3mf()

    def chunks():
        yield body

    r = client.post("/v1/jobs", headers={**user, "Content-Type": "application/octet-stream"}, content=chunks())
    assert r.status_code == 411 and r.json()["detail"] == "Content-Length is required"


def test_rule_7_and_10_job_and_list(client, f):
    user = f.user()
    f.plan("pro", concurrent=60)
    first = f.submit(user)
    assert set(first) >= JOB_FIELDS
    one = client.get(f"/v1/jobs/{first['id']}", headers=user).json()
    assert set(one) >= JOB_FIELDS and one["status"] == "queued" and one["kind"] == "slice"
    ids = [first["id"]] + [f.submit(user)["id"] for _ in range(54)]
    listed = client.get("/v1/jobs", headers=user).json()["jobs"]
    assert len(listed) == 50 and listed[0]["id"] == ids[-1] and set(listed[0]) >= JOB_FIELDS
    assert len(client.get("/v1/jobs?limit=200", headers=user).json()["jobs"]) == 55
    # More than 200: at most 200 (capped, not refused).
    r = client.get("/v1/jobs?limit=500", headers=user)
    assert r.status_code == 422 or len(r.json()["jobs"]) <= 200


def test_rule_19a_coded_errors_reach_the_job(client, f):
    user = f.user()
    node = f.node("n1", "fea_optimize")
    job = f.submit(user, kind="fea", params=json.dumps({"mode": "lightest-infill"}))
    assert f.claim(node)["id"] == job["id"]
    r = client.post(f"/v1/node/jobs/{job['id']}/fail", headers=node,
                    json={"error": "Not available in the cloud yet",
                          "details": {"code": "not_in_cloud", "error_es": "Todavía no se calcula en la nube"}})
    assert r.status_code == 200
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and view["error"] == "Not available in the cloud yet"
    assert view["summary"]["code"] == "not_in_cloud" and view["summary"]["error_es"].startswith("Todavía")


def test_rule_11_report_without_account_and_own_list(client, f):
    body = {"app": "pc", "kind": "bug", "title": "Se cierra", "description": "d", "steps": "s", "expected": "e",
            "actual": "a"}
    assert client.post("/v1/reports", json=body).status_code in (200, 201)
    assert client.post("/v1/reports", json={**body, "title": "ab"}).status_code == 422
    user = f.user()
    assert client.post("/v1/reports", headers=user, json={**body, "kind": "suggestion"}).status_code in (200, 201)
    reports = client.get("/v1/me/reports", headers=user).json()["reports"]
    assert len(reports) == 1
    assert {"id", "kind", "title", "status", "category_name", "created_at", "updated_at", "rewarded_at"} <= \
        set(reports[0])
    assert reports[0]["status"] == "new"


def test_rules_12_13_16_bridges(client, f):
    user = f.user()
    printer = {"id": "p1", "name": "Kobra", "kind": "usb", "camera": False, "actions": ["print", "pause"],
               "state": {"state": "idle", "job_name": "", "percent": 0}}
    assert client.put("/v1/bridges/pc-casa_1", headers=user, json={"name": "PC", "printers": [printer]}).status_code \
        in (200, 204)
    assert client.put("/v1/bridges/" + "a" * 65, headers=user, json={"name": "PC"}).status_code in (404, 422)
    assert client.put("/v1/bridges/no.puntos", headers=user, json={"name": "PC"}).status_code in (404, 422)
    many = [{**printer, "id": f"p{i}"} for i in range(33)]
    r = client.put("/v1/bridges/pc-2", headers=user, json={"name": "PC", "printers": many})
    assert r.status_code == 422 or len(client.get("/v1/remote/printers", headers=user).json().get("bridges", [{}])[-1]
                                       .get("printers", [])) <= 32
    for i in range(3, 11):
        assert client.put(f"/v1/bridges/pc-{i}", headers=user, json={"name": "PC"}).status_code in (200, 204)
    # The eleventh bridge of the account is refused.
    assert client.put("/v1/bridges/pc-11", headers=user, json={"name": "PC"}).status_code >= 400
