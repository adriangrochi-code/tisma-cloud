from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import timedelta

from conftest import make_3mf
from sqlalchemy import select

from tisma_cloud import scheduler
from tisma_cloud.jobs import JobError, count_triangles, plan_job
from tisma_cloud.models import Job, Node, Usage, User, month_of, utcnow
from tisma_cloud.ratelimit import RateLimiter

SITE = {"Authorization": "Bearer site-secret"}


def finish_job(client, f, node, job_id, ran_s: float = 0.) -> None:
    """The node ends the job; ran_s moves its start back to charge that time."""
    if ran_s:
        with f.db.session() as s:
            s.get(Job, job_id).started_at = utcnow() - timedelta(seconds=ran_s)
    r = client.post(f"/v1/node/jobs/{job_id}/result", headers=node, files={"file": ("p.gcode", b"G1")})
    assert r.status_code == 200, r.text


def test_health_and_auth(client, f):
    assert client.get("/v1/health").json()["ok"] is True
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    user = f.user()
    me = f.me(user)
    assert me["email"] == "ana@x.com" and me["plan"] == "pro" and me["paid"] is True
    assert me["hours_included"] == 10 and me["hours_used"] == 0 and me["max_concurrent"] == 3
    # A node token is not a user token and the other way round.
    node = f.node()
    assert client.get("/v1/me", headers=node).status_code == 401
    assert client.post("/v1/node/claim", headers=user, json={}).status_code == 401


def test_site_creates_users_and_records_payments(client, f):
    assert client.put("/v1/site/plans/basic", json={"monthly_hours": 5, "max_concurrent": 2}).status_code == 401
    assert client.put("/v1/site/plans/basic", headers={"Authorization": "Bearer wrong"},
                      json={"monthly_hours": 5, "max_concurrent": 2}).status_code == 401
    r = client.put("/v1/site/plans/basic", headers=SITE, json={"monthly_hours": 5, "max_concurrent": 2,
                                                                 "requests_per_minute": 100, "priority": 1})
    assert r.status_code == 200 and r.json()["plan"]["monthly_hours"] == 5
    # A new user needs an e-mail and a plan.
    assert client.put("/v1/site/users/42", headers=SITE, json={"email": "eva@x.com"}).status_code == 422
    assert client.put("/v1/site/users/42", headers=SITE,
                      json={"email": "eva@x.com", "plan": "gold"}).status_code == 422
    r = client.put("/v1/site/users/42", headers=SITE,
                   json={"email": "Eva@X.com", "plan": "basic", "password": "correct horse"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "eva@x.com" and r.json()["account"]["paid"] is False
    # Login from Tisma.
    assert client.post("/v1/auth/login", json={"email": "eva@x.com", "password": "wrong pass"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "nobody@x.com", "password": "x"}).status_code == 401
    r = client.post("/v1/auth/login", json={"email": "EVA@x.com", "password": "correct horse", "device": "PC"})
    assert r.status_code == 200
    eva = {"Authorization": f"Bearer {r.json()['token']}"}
    # Not paid yet: no jobs.
    r = client.post("/v1/jobs", headers=eva, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 402 and "not paid" in r.json()["detail"]
    # The site records the payment (with a time zone).
    until = (utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    r = client.put("/v1/site/users/42", headers=SITE, json={"paid_until": until})
    assert r.json()["account"]["paid"] is True
    f.submit(eva)
    me = f.me(eva)
    assert me["plan"] == "basic" and me["active_jobs"] == 1
    # The site may ask for the state of a user.
    assert client.get("/v1/site/users/42", headers=SITE).json()["account"]["active_jobs"] == 1
    assert client.get("/v1/site/users/43", headers=SITE).status_code == 404
    # Another user cannot take the e-mail.
    assert client.put("/v1/site/users/43", headers=SITE,
                      json={"email": "eva@x.com", "plan": "basic"}).status_code == 409
    # A new password closes the sessions.
    client.put("/v1/site/users/42", headers=SITE, json={"password": "another horse"})
    assert client.get("/v1/me", headers=eva).status_code == 401
    # Disabled by the site.
    r = client.post("/v1/auth/login", json={"email": "eva@x.com", "password": "another horse"})
    eva = {"Authorization": f"Bearer {r.json()['token']}"}
    client.put("/v1/site/users/42", headers=SITE, json={"enabled": False})
    assert client.get("/v1/me", headers=eva).status_code == 401


def test_site_api_disabled_without_key(client, f, settings):
    settings.site_key = ""
    assert client.put("/v1/site/plans/x", headers={"Authorization": "Bearer "},
                      json={"monthly_hours": 1, "max_concurrent": 1}).status_code == 401


def test_logout(client, f):
    client.put("/v1/site/users/7", headers=SITE, json={"email": "u@x.com", "plan": "trial", "password": "12345678"})
    t1 = client.post("/v1/auth/login", json={"email": "u@x.com", "password": "12345678"}).json()["token"]
    t2 = client.post("/v1/auth/login", json={"email": "u@x.com", "password": "12345678"}).json()["token"]
    assert client.post("/v1/auth/logout", headers={"Authorization": f"Bearer {t1}"}).status_code == 200
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {t1}"}).status_code == 401
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {t2}"}).status_code == 200


def test_login_attempts_are_limited(client, f, settings):
    settings.login_per_minute = 3
    for _ in range(3):
        assert client.post("/v1/auth/login", json={"email": "a@x.com", "password": "x"}).status_code == 401
    assert client.post("/v1/auth/login", json={"email": "a@x.com", "password": "x"}).status_code == 429


def test_grace_days(client, f, settings):
    late = f.user("late@x.com", paid_days=-(settings.grace_days - 1))
    gone = f.user("gone@x.com", paid_days=-(settings.grace_days + 1))
    never = f.user("never@x.com", paid_days=None)
    f.submit(late)
    f.submit(gone, expect=402)
    f.submit(never, expect=402)


def test_concurrent_jobs_are_limited(client, f):
    user = f.user(plan=f.plan("solo", concurrent=2))
    node = f.node("n", "slice")
    j1 = f.submit(user)
    f.submit(user)
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 429 and "2 at a time" in r.json()["detail"]
    # One ends: a new one may be submitted.
    assert f.claim(node)["id"] == j1["id"]
    finish_job(client, f, node, j1["id"])
    f.submit(user)
    # A cancelled job frees its place too.
    jobs = [j for j in client.get("/v1/jobs", headers=user).json()["jobs"] if j["status"] == "queued"]
    client.post(f"/v1/jobs/{jobs[0]['id']}/cancel", headers=user)
    f.submit(user)


def test_monthly_hours(client, f):
    user = f.user(plan=f.plan("small", hours=1., concurrent=5))
    node = f.node("n", "slice")
    job = f.submit(user)
    f.claim(node)
    finish_job(client, f, node, job["id"], ran_s=2700.)
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert 2699 < view["seconds"] < 2702
    me = f.me(user)
    assert 0.74 < me["hours_used"] < 0.76 and 0.24 < me["hours_left"] < 0.26
    # Still some time left: one more job; it runs longer than what was left.
    job = f.submit(user)
    f.claim(node)
    finish_job(client, f, node, job["id"], ran_s=1800.)
    assert f.me(user)["hours_left"] == 0
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 402 and "used up" in r.json()["detail"]
    # Failed jobs are not charged; the usage of another month does not count.
    with f.db.session() as s:
        s.execute(Usage.__table__.update().values(month="2000-01"))
    job = f.submit(user)
    f.claim(node)
    client.post(f"/v1/node/jobs/{job['id']}/fail", headers=node, json={"error": "bad"})
    assert f.me(user)["hours_used"] == 0


def test_admins_are_not_limited(client, f):
    admin = f.user("root@x.com", plan=f.plan("tiny", hours=0., concurrent=1), paid_days=None, admin=True)
    f.submit(admin)
    f.submit(admin)


def test_requests_per_minute(client, f):
    user = f.user(plan=f.plan("slow", rpm=5))
    for _ in range(5):
        assert client.get("/v1/me", headers=user).status_code == 200
    r = client.get("/v1/me", headers=user)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_rate_limiter_window():
    rl = RateLimiter(window_s=60.)
    assert all(rl.allow("k", 3, now=t) for t in (0., 1., 2.))
    assert not rl.allow("k", 3, now=30.)
    assert rl.allow("k", 3, now=60.5)
    assert rl.allow("other", 1, now=30.)


def test_submit_classifies(client, f):
    user = f.user(plan=f.plan("big", concurrent=10))
    assert f.submit(user)["job_class"] == "slice"
    assert f.submit(user, data=make_3mf(triangles=1500))["job_class"] == "slice_large"
    fea = f.submit(user, kind="fea", params=json.dumps({"mode": "analyze", "quality": "high"}))
    assert fea["job_class"] == "fea" and fea["params"]["zones"] is True and fea["params"]["object"] == 0
    opt = f.submit(user, kind="fea", params=json.dumps({"mode": "lightest-infill", "ribs": True}))
    assert opt["job_class"] == "fea_optimize"


def test_bad_jobs_are_refused(client, f):
    user = f.user()
    f.submit(user, kind="print", expect=422)
    f.submit(user, kind="fea", params="{not json", expect=422)
    f.submit(user, kind="fea", params=json.dumps({"mode": "melt"}), expect=422)
    f.submit(user, kind="fea", params=json.dumps({"quality": "best"}), expect=422)
    f.submit(user, kind="fea", params=json.dumps({"object": -1}), expect=422)
    f.submit(user, kind="fea", params=json.dumps({"solid": "yes"}), expect=422)
    f.submit(user, kind="slice", params=json.dumps({"extra": 1}), expect=422)
    f.submit(user, data=b"not a zip", expect=422)
    f.submit(user, kind="fea", data=b"PK\x03\x04 broken", expect=422)
    assert f.me(user)["active_jobs"] == 0


def test_upload_limit(client, f, settings):
    settings.max_upload_mb = 1
    user = f.user()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("3D/3dmodel.model", b"<triangle " * 10)
        z.writestr("Metadata/big.bin", os.urandom(2 << 20))
    f.submit(user, data=buf.getvalue(), expect=413)
    assert f.me(user)["active_jobs"] == 0


def test_nodes_follow_their_priorities(client, f):
    user = f.user(plan=f.plan("big", concurrent=10))
    i5 = f.node("i5", "fea_optimize,fea,slice_large,slice")
    i7 = f.node("i7-1", "slice,slice_large")
    s1 = f.submit(user)
    s2 = f.submit(user, data=make_3mf(triangles=5000))
    a1 = f.submit(user, kind="fea", params="{}")
    o1 = f.submit(user, kind="fea", params=json.dumps({"mode": "lightest-infill"}))
    # The i7 takes slicing first, small plates before large ones, and never analyses.
    assert f.claim(i7)["id"] == s1["id"]
    assert f.claim(i7)["id"] == s2["id"]
    assert f.claim(i7) is None
    # The i5 takes the optimizations, then the analyses.
    assert f.claim(i5)["id"] == o1["id"]
    assert f.claim(i5)["id"] == a1["id"]
    assert f.claim(i5) is None
    # The administrator changes the priorities of the i7: now it may take analyses too.
    admin = f.user("root@x.com", admin=True)
    r = client.patch("/v1/admin/nodes/i7-1", headers=admin, json={"priorities": ["fea", "slice"]})
    assert r.status_code == 200 and r.json()["node"]["priorities"] == ["fea", "slice"]
    a2 = f.submit(user, kind="fea", params="{}")
    s3 = f.submit(user)
    assert f.claim(i7)["id"] == a2["id"]
    assert f.claim(i7)["id"] == s3["id"]


def test_plan_priority_and_age(client, f):
    free = f.user("free@x.com", plan=f.plan("free", concurrent=5, priority=0))
    pro = f.user("pro@x.com", plan=f.plan("business", concurrent=5, priority=10))
    node = f.node("n", "slice")
    j1 = f.submit(free)
    j2 = f.submit(free)
    j3 = f.submit(pro)
    assert [f.claim(node)["id"] for _ in range(3)] == [j3["id"], j1["id"], j2["id"]]


def test_disabled_node_gets_nothing(client, f):
    user = f.user()
    node = f.node("n", "slice")
    admin = f.user("root@x.com", admin=True)
    f.submit(user)
    client.patch("/v1/admin/nodes/n", headers=admin, json={"enabled": False})
    assert client.post("/v1/node/claim", headers=node, json={}).status_code == 401


def test_full_job_flow(client, f):
    user = f.user()
    node = f.node("n", "slice")
    job = f.submit(user)
    claimed = f.claim(node)
    assert claimed["id"] == job["id"] and claimed["kind"] == "slice"
    r = client.get(f"/v1/node/jobs/{job['id']}/input", headers=node)
    assert r.status_code == 200 and r.content == make_3mf()
    r = client.post(f"/v1/node/jobs/{job['id']}/progress", headers=node, json={"progress": 40})
    assert r.json() == {"cancel": False}
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "running" and view["progress"] == 40
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).status_code == 409
    # A result of a type the master does not serve is refused.
    r = client.post(f"/v1/node/jobs/{job['id']}/result", headers=node, files={"file": ("x.exe", b"MZ")})
    assert r.status_code == 422
    r = client.post(f"/v1/node/jobs/{job['id']}/result", headers=node, files={"file": ("plate.gcode", b"G1 X1\n")},
                    data={"summary": json.dumps({"print_time": "10m 8s"})})
    assert r.status_code == 200, r.text
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "done" and view["summary"]["print_time"] == "10m 8s"
    assert view["result_filename"] == "part.gcode"
    r = client.get(f"/v1/jobs/{job['id']}/result", headers=user)
    assert r.status_code == 200 and r.content == b"G1 X1\n"
    assert "part.gcode" in r.headers["content-disposition"]
    # The node cannot touch a finished job any more.
    assert client.post(f"/v1/node/jobs/{job['id']}/progress", headers=node, json={"progress": 1}).status_code == 404


def test_jobs_are_private(client, f):
    ana = f.user("ana@x.com")
    bob = f.user("bob@x.com")
    n1 = f.node("n1", "slice")
    n2 = f.node("n2", "fea")
    job = f.submit(ana)
    assert client.get(f"/v1/jobs/{job['id']}", headers=bob).status_code == 404
    assert client.post(f"/v1/jobs/{job['id']}/cancel", headers=bob).status_code == 404
    f.claim(n1)
    # Another node cannot read the project of a job it does not run.
    assert client.get(f"/v1/node/jobs/{job['id']}/input", headers=n2).status_code == 404
    assert client.get("/v1/admin/nodes", headers=ana).status_code == 403


def test_failure(client, f):
    user = f.user()
    node = f.node("n", "fea")
    job = f.submit(user, kind="fea", params=json.dumps({"quality": "ultra"}))
    f.claim(node)
    r = client.post(f"/v1/node/jobs/{job['id']}/fail", headers=node, json={"error": "No fixed faces"})
    assert r.status_code == 200
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and view["error"] == "No fixed faces" and view["seconds"] == 0


def test_cancel(client, f):
    user = f.user()
    node = f.node("n", "slice")
    queued = f.submit(user)
    running = f.submit(user)
    assert client.post(f"/v1/jobs/{queued['id']}/cancel", headers=user).json()["status"] == "cancelled"
    assert f.claim(node)["id"] == running["id"]
    client.post(f"/v1/jobs/{running['id']}/cancel", headers=user)
    # The node learns it from the progress and from the heartbeat, and stops the job.
    assert client.post(f"/v1/node/jobs/{running['id']}/progress", headers=node, json={"progress": 5}).json()["cancel"]
    hb = client.post("/v1/node/heartbeat", headers=node, json={"running": [running["id"]]}).json()
    assert hb["cancel"] == [running["id"]]
    client.post(f"/v1/node/jobs/{running['id']}/fail", headers=node, json={"error": "Cancelled", "cancelled": True})
    assert client.get(f"/v1/jobs/{running['id']}", headers=user).json()["status"] == "cancelled"


def test_lost_node_jobs_are_requeued(client, f, settings):
    user = f.user()
    lost = f.node("lost", "slice")
    other = f.node("other", "slice")
    job = f.submit(user)
    assert f.claim(lost)["id"] == job["id"]
    later = utcnow() + timedelta(seconds=settings.node_timeout_s + 5)
    with f.db.session() as s:
        assert scheduler.requeue_stale(s, settings.node_timeout_s, settings.max_attempts, now=later) == 1
    # The other node takes it; the lost one is told to drop it when it comes back.
    assert f.claim(other)["id"] == job["id"]
    hb = client.post("/v1/node/heartbeat", headers=lost, json={"running": [job["id"]]}).json()
    assert hb["cancel"] == [job["id"]]
    assert client.post(f"/v1/node/jobs/{job['id']}/progress", headers=lost, json={"progress": 9}).status_code == 404


def test_requeue_gives_up_after_max_attempts(client, f, settings):
    user = f.user()
    job = f.submit(user)
    for attempt in range(settings.max_attempts):
        node = f.node(f"n{attempt}", "slice")
        assert f.claim(node)["id"] == job["id"]
        with f.db.session() as s:
            scheduler.requeue_stale(s, settings.node_timeout_s, settings.max_attempts,
                                    now=utcnow() + timedelta(seconds=settings.node_timeout_s + 5))
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and "stopped answering" in view["error"]


def test_admin_api(client, f):
    admin = f.user("root@x.com", admin=True)
    r = client.post("/v1/admin/nodes", headers=admin, json={"name": "i7-2", "priorities": ["slice", "slice_large"]})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("tn_")
    assert client.post("/v1/node/claim", headers={"Authorization": f"Bearer {token}"}, json={}).status_code == 204
    assert client.post("/v1/admin/nodes", headers=admin, json={"name": "x", "priorities": ["gpu"]}).status_code == 422
    assert client.post("/v1/admin/nodes", headers=admin,
                       json={"name": "x", "priorities": ["slice", "slice"]}).status_code == 422
    nodes = client.get("/v1/admin/nodes", headers=admin).json()["nodes"]
    assert [n["name"] for n in nodes] == ["i7-2"] and nodes[0]["last_seen"]
    r = client.put("/v1/admin/plans/maker", headers=admin, json={"monthly_hours": 3, "max_concurrent": 1})
    assert r.status_code == 200
    # Only public plans are offered.
    assert "maker" not in [p["name"] for p in client.get("/v1/plans").json()["plans"]]
    client.put("/v1/admin/plans/maker", headers=admin, json={"monthly_hours": 3, "max_concurrent": 1, "public": True,
                                                             "price_cents": 1000})
    assert "maker" in [p["name"] for p in client.get("/v1/plans").json()["plans"]]
    assert [u["email"] for u in client.get("/v1/admin/users", headers=admin).json()["users"]] == ["root@x.com"]
    f.submit(admin)
    queue = client.get("/v1/admin/queue", headers=admin).json()["queue"]
    assert queue["slice"] == {"queued": 1, "running": 0}


def test_old_files_are_removed(client, f, settings):
    user = f.user()
    node = f.node("n", "slice")
    job = f.submit(user)
    f.claim(node)
    finish_job(client, f, node, job["id"])
    services = client.app.state.services
    with services.db.session() as s:
        s.get(Job, job["id"]).finished_at = utcnow() - timedelta(hours=settings.retention_h + 1)
    services.sweep()
    assert not services.storage.job_dir(job["id"]).exists()
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).status_code == 409
    assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["result_filename"] == ""


def test_count_triangles_and_plans():
    assert count_triangles(io.BytesIO(make_3mf(triangles=37)), 1000) == 37
    assert count_triangles(io.BytesIO(make_3mf(triangles=50)), 10) > 10
    try:
        count_triangles(io.BytesIO(b"nope"), 10)
        raise AssertionError("not a zip")
    except JobError:
        pass
    assert plan_job("fea", {"quality": "ultra"}, 0, 10).job_class == "fea"
    assert plan_job("fea", {"mode": "resin-structure"}, 0, 10).job_class == "fea_optimize"
    assert plan_job("slice", {}, 11, 10).job_class == "slice_large"


def test_node_info_is_saved(client, f):
    node = f.node("n", "slice")
    client.post("/v1/node/claim", headers=node, json={"info": {"cpus": 6, "gpu": True}})
    with f.db.session() as s:
        assert s.execute(select(Node)).scalar_one().info_dict == {"cpus": 6, "gpu": True}


def test_usage_month(client, f):
    user = f.user()
    node = f.node("n", "slice")
    job = f.submit(user)
    f.claim(node)
    finish_job(client, f, node, job["id"], ran_s=60.)
    with f.db.session() as s:
        usage = s.execute(select(Usage)).scalar_one()
        assert usage.month == month_of(utcnow()) and 59 < usage.seconds < 62
        assert s.execute(select(User)).scalar_one().email == "ana@x.com"


def test_admin_panel_and_jobs(client, f, settings):
    page = client.get("/admin")
    assert page.status_code == 200 and "Tisma Cloud" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    admin = f.user("root@x.com", admin=True)
    user = f.user()
    i5 = f.node("i5", "fea,slice")
    f.node("i7", "slice")
    running = f.submit(user)
    waiting = f.submit(user, kind="fea", params="{}")
    done = f.submit(user)
    assert f.claim(i5)["id"] == waiting["id"]
    assert f.claim(i5)["id"] == running["id"]
    client.post(f"/v1/node/jobs/{running['id']}/progress", headers=i5, json={"progress": 30})
    client.post(f"/v1/node/jobs/{waiting['id']}/fail", headers=i5, json={"error": "bad"})
    nodes = {n["name"]: n for n in client.get("/v1/admin/nodes", headers=admin).json()["nodes"]}
    # The i5 claimed jobs just now; the i7 never.
    assert nodes["i5"]["online"] is True and nodes["i5"]["running"] == 1
    assert nodes["i7"]["online"] is False
    jobs = client.get("/v1/admin/jobs", headers=admin).json()
    active = {j["id"]: j for j in jobs["active"]}
    assert active[running["id"]]["node"] == "i5" and active[running["id"]]["progress"] == 30
    assert active[running["id"]]["user"] == "ana@x.com"
    assert active[done["id"]]["status"] == "queued" and active[done["id"]]["node"] is None
    assert [j["id"] for j in jobs["ended"]] == [waiting["id"]] and jobs["ended"][0]["error"] == "bad"
    assert client.get("/v1/admin/jobs", headers=user).status_code == 403
    # A node not seen for longer than the timeout is shown offline.
    with f.db.session() as s:
        from sqlalchemy import select as sel

        s.execute(sel(Node).where(Node.name == "i5")).scalar_one().last_seen = utcnow() - timedelta(seconds=settings.node_timeout_s + 1)
    nodes = {n["name"]: n for n in client.get("/v1/admin/nodes", headers=admin).json()["nodes"]}
    assert nodes["i5"]["online"] is False


def test_administration_only_from_the_local_network(client, f):
    from fastapi.testclient import TestClient

    admin = f.user("root@x.com", admin=True)
    user = f.user()
    assert client.get("/v1/admin/nodes", headers=admin).status_code == 200
    assert client.get("/admin").status_code == 200
    with TestClient(client.app, client=("200.40.10.7", 40000)) as internet:
        # From internet the administration does not exist, even with the token of an administrator.
        assert internet.get("/admin").status_code == 404
        for path in ("/v1/admin/nodes", "/v1/admin/jobs", "/v1/admin/queue", "/v1/admin/users"):
            assert internet.get(path, headers=admin).status_code == 404
        assert internet.patch("/v1/admin/nodes/x", headers=admin, json={"enabled": False}).status_code == 404
        # The users keep using the service.
        assert internet.get("/v1/me", headers=user).status_code == 200
        f.submit(user)
    # Through Caddy (a local address) with the address of an internet user: also 404.
    assert client.get("/admin", headers={"X-Forwarded-For": "200.40.10.7"}).status_code == 404
    assert client.get("/v1/admin/nodes", headers={**admin, "X-Forwarded-For": "200.40.10.7"}).status_code == 404
    assert client.get("/v1/admin/nodes", headers={**admin, "X-Forwarded-For": "192.168.1.30"}).status_code == 200


def test_site_api_only_from_the_site_address(settings):
    from fastapi.testclient import TestClient

    from conftest import new_app as create_app

    settings.site_networks = "190.64.1.10/32"
    settings.billing_url = "https://tisma.com.uy/planes"
    app = create_app(settings)
    body = {"monthly_hours": 3, "max_concurrent": 2}
    with TestClient(app, client=("190.64.1.10", 1)) as site, TestClient(app, client=("200.40.10.7", 1)) as other:
        assert site.put("/v1/site/plans/basico", headers=SITE, json=body).status_code == 200
        assert other.put("/v1/site/plans/basico", headers=SITE, json=body).status_code == 404
        assert other.get("/v1/health").json()["billing_url"] == "https://tisma.com.uy/planes"


def test_projects_are_deleted_when_the_job_ends(client, f):
    user = f.user()
    node = f.node("n", "slice")
    storage = client.app.state.services.storage
    done, failed, cancelled = f.submit(user), f.submit(user), f.submit(user)
    assert storage.input_path(done["id"]).exists()
    f.claim(node)
    client.post(f"/v1/node/jobs/{done['id']}/result", headers=node, files={"file": ("p.gcode", b"G1")})
    f.claim(node)
    client.post(f"/v1/node/jobs/{failed['id']}/fail", headers=node, json={"error": "x"})
    client.post(f"/v1/jobs/{cancelled['id']}/cancel", headers=user)
    for job in (done, failed, cancelled):
        assert not storage.input_path(job["id"]).exists()
    # The result stays for its download.
    assert client.get(f"/v1/jobs/{done['id']}/result", headers=user).content == b"G1"
