"""The owner's metrics of the beta (metrics.py, GET /v1/admin/metrics, «tisma-cloud admin metrics»)."""

from __future__ import annotations

from datetime import timedelta

from tisma_cloud import metrics
from tisma_cloud.models import Job, utcnow


def test_metrics_of_the_last_day(client, f):
    admin = f.user("root@x.com", admin=True)
    ana = f.user("ana@x.com", plan=f.plan("big", concurrent=10))
    bob = f.user("bob@x.com")
    node = f.node("n1", "slice,fea")
    for _ in range(3):
        f.submit(ana)
    f.submit(bob, kind="fea", params="{}")
    f.submit(ana)
    # The queue gives the shortest first: four of the five, whatever their order; two end well, two fail.
    claimed = [f.claim(node) for _ in range(4)]
    expected: dict[str, dict[str, int]] = {}
    for i, job in enumerate(claimed):
        with f.db.session() as s:
            j = s.get(Job, job["id"])
            j.created_at = utcnow() - timedelta(seconds=100 + 10 * i)
            j.started_at = utcnow() - timedelta(seconds=40)
        outcome = "done" if i < 2 else "failed"
        if outcome == "done":
            assert client.post(f"/v1/node/jobs/{job['id']}/result", headers=node,
                               files={"file": ("p.gcode", b"G1")}).status_code == 200
        else:
            assert client.post(f"/v1/node/jobs/{job['id']}/fail", headers=node,
                               json={"error": "Object too large for the bed\nmore lines"}).status_code == 200
        if i > 0:  # the first one is moved out of the window below
            counts = expected.setdefault(job["job_class"], {"done": 0, "failed": 0, "cancelled": 0})
            counts[outcome] += 1
    # An old job is out of the window.
    with f.db.session() as s:
        s.get(Job, claimed[0]["id"]).finished_at = utcnow() - timedelta(days=3)
    m = client.get("/v1/admin/metrics", headers=admin).json()
    assert m["done"] == 1 and m["failed"] == 2 and m["failure_rate"] == round(2 / 3, 3)
    assert m["jobs"] == expected
    assert m["top_errors"] == [{"error": "Object too large for the bed", "count": 2}]
    assert 79 <= m["wait_s"]["median"] <= 81 and 30 <= m["run_s"]["median"] <= 50
    with f.db.session() as s:
        senders = {s.get(Job, job["id"]).user_id for job in claimed[1:]}
    assert m["now"]["queued"] == 1 and m["accounts"]["with_jobs"] == len(senders) and m["accounts"]["total"] == 3
    assert m["nodes"] == {"enabled": 1, "online": 1}
    # A week back takes the old job too.
    assert client.get("/v1/admin/metrics?days=7", headers=admin).json()["done"] == 2
    # Only administrators, and a sane window.
    assert client.get("/v1/admin/metrics", headers=ana).status_code == 403
    assert client.get("/v1/admin/metrics?days=0", headers=admin).status_code == 422
    # The text of «tisma-cloud admin metrics».
    with f.db.session() as s:
        text = metrics.text(metrics.report(s, 1.))
    assert "1 bien, 2 fallidos (66.7 %)" in text and "Object too large for the bed" in text


def test_percentiles():
    assert metrics._percentile([], .5) is None
    assert metrics._percentile([3., 1., 2.], .5) == 2.
    assert metrics._percentile([float(i) for i in range(1, 11)], .9) == 9.


def test_public_status(client, f, monkeypatch):
    """GET /v1/status and /estado: down without nodes, slow with a long wait or a kind of computing nobody takes, ok
    otherwise; no data of any user."""
    from tisma_cloud import api
    from tisma_cloud.models import Node

    monkeypatch.setattr(api, "STATUS_CACHE_S", 0.)
    s = client.get("/v1/status").json()
    assert s["status"] == "down" and s["label_es"] == "Caído" and s["label_en"] == "Down" and s["nodes_online"] == 0
    node = f.node("n1", ",".join(["fea_optimize", "fea", "slice_large", "slice", "aero", "orient", "scan"]))
    client.post("/v1/node/claim", headers=node, json={})  # the node is seen
    s = client.get("/v1/status").json()
    assert s["status"] == "ok" and s["unavailable"] == [] and s["message_en"] == "Everything is working normally."
    # A job waiting longer than TISMA_STATUS_SLOW_MINUTES (10).
    user = f.user()
    job = f.submit(user)
    with f.db.session() as session:
        session.get(Job, job["id"]).created_at = utcnow() - timedelta(minutes=11)
    s = client.get("/v1/status").json()
    assert s["status"] == "slow" and s["longest_wait_s"] >= 660 and s["label_es"] == "Con demoras"
    assert "ana@x.com" not in str(s)
    # A kind no enabled node takes (scans without a scan node) is not offered: no delay.
    with f.db.session() as session:
        session.get(Job, job["id"]).created_at = utcnow()
        session.query(Node).one().priorities = "slice"
    assert client.get("/v1/status").json()["status"] == "ok"
    # A kind only an offline node takes is a delay.
    aero = f.node("n2", "aero")
    client.post("/v1/node/claim", headers=aero, json={})
    with f.db.session() as session:
        session.query(Node).filter(Node.name == "n2").one().last_seen = utcnow() - timedelta(minutes=6)
    s = client.get("/v1/status").json()
    assert s["status"] == "slow" and s["unavailable"] == ["aero"] and "Algunos cálculos" in s["message_es"]
    # Only that: no word of a long wait.
    assert s["message_es"] == "Algunos cálculos no están disponibles ahora." and "Waiting" not in s["message_en"]
    # The nodes stopped answering.
    with f.db.session() as session:
        for n in session.query(Node).all():
            n.last_seen = utcnow() - timedelta(minutes=6)
    assert client.get("/v1/status").json()["status"] == "down"
    # Without a session, and the page.
    page = client.get("/estado")
    assert page.status_code == 200 and "/v1/status" in page.text and "script-src 'sha256-" in \
        page.headers["content-security-policy"]


def test_public_status_is_cached(client, f):
    first = client.get("/v1/status").json()
    f.node("n1", "slice")
    assert client.get("/v1/status").json() == first
