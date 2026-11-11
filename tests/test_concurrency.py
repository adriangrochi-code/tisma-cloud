from __future__ import annotations

import threading
from collections import Counter

import pytest
from conftest import make_3mf
from sqlalchemy import select

from tisma_cloud import scheduler
from tisma_cloud.models import Node


def test_parallel_claims_never_share_a_job(client, f):
    user = f.user(plan=f.plan("big", concurrent=1000))
    db = f.db
    jobs = 120
    for _ in range(jobs):
        f.submit(user)
    for i in range(8):
        f.node(f"n{i}", "slice")
    taken: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            with db.session() as s:
                node = s.execute(select(Node).where(Node.name == name)).scalar_one()
                job = scheduler.claim(s, node, db.is_sqlite)
                job_id = job.id if job else None
            if job_id is None:
                return
            with lock:
                taken.append(job_id)

    threads = [threading.Thread(target=worker, args=(f"n{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(taken) == jobs
    assert max(Counter(taken).values()) == 1


@pytest.fixture
def postgres(client):
    if client.app.state.services.db.is_sqlite:
        pytest.skip("the row lock of the admission needs PostgreSQL (SQLite runs one writer at a time anyway)")


def test_parallel_submissions_respect_the_limit(client, f, postgres):
    user = f.user(plan=f.plan("two", concurrent=2))
    codes: list[int] = []
    lock = threading.Lock()

    def submit() -> None:
        r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(codes) == [201, 201] + [429] * 8


def test_jobs_of_one_user_ending_at_once_all_count(client, f):
    """The first jobs of a month of one user, ending at the same moment on several nodes: each adds its time to the
    one usage row of the month (on PostgreSQL two of them both inserted it, and one result was lost with a 500)."""
    from datetime import timedelta

    from tisma_cloud.models import Job, Usage, User, utcnow

    user = f.user(plan=f.plan("big", concurrent=1000))
    db = f.db
    ids = [f.submit(user)["id"] for _ in range(4)]
    end = utcnow()
    with db.session() as s:
        for job_id in ids:
            job = s.get(Job, job_id)
            job.started_at, job.finished_at = end - timedelta(seconds=10), end
        user_id = s.get(Job, ids[0]).user_id
        assert s.get(User, user_id) is not None
    start = threading.Barrier(len(ids))
    errors: list[BaseException] = []

    def finish(job_id: str) -> None:
        try:
            if db.is_sqlite:  # one writer at a time: the sessions cannot all be open together
                start.wait(10)
            with db.session() as s:
                job = s.get(Job, job_id)
                if not db.is_sqlite:  # all in their transaction, then all at once
                    start.wait(10)
                scheduler.charge_time(s, job)
        except BaseException as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=finish, args=(job_id,)) for job_id in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    with db.session() as s:
        rows = s.execute(select(Usage).where(Usage.user_id == user_id)).scalars().all()
        assert len(rows) == 1 and abs(rows[0].seconds - 10 * len(ids)) < 1e-6


def test_a_result_sent_twice_at_once_is_kept_and_charged_once(client, f):
    """A node that times out while the master still receives its result sends it again: both arrive together."""
    import threading
    from datetime import timedelta

    from tisma_cloud.models import Job, utcnow

    user = f.user()
    node = f.node("n", "slice")
    job = f.submit(user)
    f.claim(node)
    with f.db.session() as s:
        s.get(Job, job["id"]).started_at = utcnow() - timedelta(minutes=30)
    gcode = b"G1 X1\n" * 200_000
    barrier = threading.Barrier(2)
    codes = []

    def send():
        barrier.wait()
        try:
            r = client.post(f"/v1/node/jobs/{job['id']}/result", headers=node,
                            files={"file": ("p.gcode", gcode, "text/plain")}, data={"summary": "{}"})
            codes.append(r.status_code)
        except Exception as ex:  # noqa: BLE001 (a server error in the test client)
            codes.append(repr(ex)[:300])

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes, key=str) == [200, 404], codes
    assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["status"] == "done"
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content == gcode
    assert round(f.me(user)["hours_used"], 2) == 0.5, f.me(user)


def test_a_report_rewarded_twice_at_once_is_rewarded_once(client, f, postgres):
    """Two administrators (or a double click) reward the same report: the second saw it not rewarded before the first
    committed, waited for the account's lock and gave the days again."""
    import time

    from test_reports import report as report_body

    services = client.app.state.services
    admin = f.user("admin@x.com", admin=True)
    ana = f.user("ana@x.com", paid_days=None)
    rid = client.post("/v1/reports", json=report_body(), headers=ana).json()["id"]
    sending = threading.Event()

    def slow_send(to, subject, text):  # noqa: ANN001, ANN202
        sending.set()
        time.sleep(1)

    services.mailer.send = slow_send
    codes: list[int] = []

    def reward() -> None:
        codes.append(client.post(f"/v1/admin/reports/{rid}/reward", json={}, headers=admin).status_code)

    first = threading.Thread(target=reward)
    first.start()
    assert sending.wait(10)
    second = threading.Thread(target=reward)
    second.start()
    first.join(30)
    second.join(30)
    assert sorted(codes) == [200, 409]
    me = client.get("/v1/me", headers=ana).json()
    from datetime import datetime, timezone

    left = datetime.fromisoformat(me["paid_until"].replace("Z", "+00:00")) - datetime.now(timezone.utc)
    assert left.days < 8
