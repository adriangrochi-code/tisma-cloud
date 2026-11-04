"""The kinds of computing a plan includes, and the new kinds: aerodynamic analysis (CFD) and automatic orientation."""

from __future__ import annotations

import json

import pytest


def test_new_kinds_are_checked(client, f):
    user = f.user()
    assert f.submit(user, kind="aero")["job_class"] == "aero"
    assert f.submit(user, kind="orient", params=json.dumps({"object": 1}))["job_class"] == "orient"
    for kind, params in (("aero", {"speed": 0}), ("aero", {"speed": 500}), ("aero", {"quality": "ultra"}),
                         ("aero", {"direction": "w"}), ("aero", {"direction": [0, 0, 0]}),
                         ("aero", {"direction": [1, "a", 0]}), ("orient", {"quality": "fast"}),
                         ("render", {})):
        f.submit(user, kind=kind, params=json.dumps(params), expect=422)


def test_plan_limits_the_kinds(client, f):
    admin = f.user("root@x.com", admin=True)
    body = {"monthly_hours": 5, "max_concurrent": 5, "public": True}
    assert client.put("/v1/admin/plans/basico", headers=admin, json={**body, "classes": ["slice", "slice_large"]}).status_code == 200
    assert client.put("/v1/admin/plans/pro", headers=admin, json=body).status_code == 200
    user = f.user("ana@x.com", plan="basico")
    f.submit(user)
    r = client.post("/v1/jobs", headers=user, data={"kind": "aero", "params": "{}"},
                    files={"file": ("part.3mf", _project(), "model/3mf")})
    assert r.status_code == 402 and "aerodynamic" in r.json()["detail"] and "pro" in r.json()["detail"]
    # The public list says what each plan includes.
    plans = {p["name"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert plans["basico"]["classes"] == ["slice", "slice_large"] and "aero" in plans["pro"]["classes"]
    # An unknown kind of computing is refused; an internal (unlimited) account is not limited by its plan.
    assert client.put("/v1/admin/plans/x", headers=admin, json={**body, "classes": ["magic"]}).status_code == 422
    internal = f.user("dev@x.com", plan="basico", admin=True)
    f.submit(internal, kind="aero")


def _project() -> bytes:
    from conftest import make_3mf

    return make_3mf()


def test_capacity_by_plan(client, f):
    """Each job of a plan takes its share of the nodes; the report says how many users of each plan they can serve."""
    from tisma_cloud.models import Node

    admin = f.user("root@x.com", admin=True)
    # Two nodes: 8 + 4 processor cores, one card of 1024 CUDA cores; a disabled node does not count.
    for name, info, enabled in (("a", {"cpus": 8, "cuda_cores": 1024}, True), ("b", {"cpus": 4}, True),
                                ("c", {"cpus": 64, "cuda_cores": 9999}, False)):
        f.node(name)
        with f.db.session() as s:
            node = s.query(Node).filter_by(name=name).one()
            node.info, node.enabled = json.dumps(info), enabled
    base = {"public": True, "max_concurrent": 1}
    client.put("/v1/admin/plans/basico", headers=admin, json={**base, "monthly_hours": 10, "cpu_cores": 1})
    client.put("/v1/admin/plans/pro", headers=admin, json={**base, "monthly_hours": 50, "max_concurrent": 2,
                                                           "cpu_cores": 2, "cuda_cores": 256})
    cap = client.get("/v1/admin/capacity", headers=admin).json()
    assert cap["resources"] == {"nodes": 2, "cpu_cores": 12, "cuda_cores": 1024}
    plans = {p["plan"]: p for p in cap["plans"]}
    # basico: 12 cores / 1 = 12 users at once; 12 × 730.5 h / 10 h = 876 per month.
    assert plans["basico"]["users_at_once"] == 12 and plans["basico"]["users_per_month"] == 876
    # pro: by processor 12 / (2 × 2) = 3, by CUDA 1024 / (256 × 2) = 2 → 2 at once;
    # per month min(12 × 730.5 / (2 × 50), 1024 × 730.5 / (256 × 50)) = min(87.66, 58.44) → 58.
    assert plans["pro"]["users_at_once"] == 2 and plans["pro"]["users_per_month"] == 58
    assert client.get("/v1/admin/capacity", headers=f.user("eq@x.com")).status_code == 403
    assert client.put("/v1/admin/plans/x", headers=admin, json={**base, "monthly_hours": 1, "cpu_cores": 0}).status_code == 422


def test_extreme_quality_is_accepted():
    # The engine's finest level, for the structural analysis and the CFD (cloud.md, rules 19 and 19b).
    from tisma_cloud.jobs import plan_job
    assert plan_job("fea", {"quality": "extreme"}, 0, 10).params["quality"] == "extreme"
    assert plan_job("aero", {"quality": "extreme"}, 0, 10).params["quality"] == "extreme"



def test_plan_set_classes_command(settings, client, f, monkeypatch, capsys):
    # The engine's plan lock reads "classes" from GET /v1/plans: plan-set --classes sets them from the command line.
    from tisma_cloud import admin as admin_cli

    monkeypatch.setenv("TISMA_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("TISMA_STORAGE_DIR", str(settings.storage_dir))
    base = ["plan-set", "solo", "--hours", "5", "--concurrent", "2"]
    assert admin_cli.main([*base, "--classes", "slice_large, slice", "--public"]) == 0
    assert "classes slice,slice_large, public" in capsys.readouterr().out
    plans = {p["name"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert plans["solo"]["classes"] == ["slice", "slice_large"]
    user = f.user("solo@x.com", plan="solo")
    f.submit(user)
    r = client.post("/v1/jobs", headers=user, data={"kind": "aero", "params": "{}"},
                    files={"file": ("part.3mf", _project(), "model/3mf")})
    assert r.status_code == 402
    # Without --classes a plan that exists keeps them; "all" gives back every class.
    assert admin_cli.main([*base, "--hours", "6"]) == 0
    assert "classes slice,slice_large" in capsys.readouterr().out
    assert admin_cli.main([*base, "--classes", "all"]) == 0
    assert "classes all" in capsys.readouterr().out
    assert f.submit(user, kind="aero")["job_class"] == "aero"
    with pytest.raises(SystemExit):
        admin_cli.main([*base, "--classes", "slice,nope"])
