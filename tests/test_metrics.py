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
