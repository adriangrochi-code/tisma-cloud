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
