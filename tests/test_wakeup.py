"""Long polls woken by what they wait for (wakeup.py), instead of asking the database every second."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

from tisma_cloud.models import Job, Node, utcnow
from tisma_cloud.wakeup import Wakeup


def test_a_notification_from_another_thread_wakes_the_wait():
    wakeup = Wakeup()
    looks: list[float] = []

    async def main() -> list[str]:
        found: list[str] = []

        async def check() -> list[str]:
            looks.append(time.monotonic())
            return list(found)

        def later() -> None:
            time.sleep(0.3)
            found.append("job")
            wakeup.notify("jobs")

        threading.Thread(target=later).start()
        started = time.monotonic()
        result = await wakeup.poll("jobs", check, wait_s=20, recheck_s=20)
        assert time.monotonic() - started < 2.
        return result

    assert asyncio.run(main()) == ["job"]
    # Once at the start and once when notified: no looks every second in between.
    assert len(looks) == 2
    assert wakeup.waiting() == 0


def test_a_notification_during_the_look_is_not_lost():
    wakeup = Wakeup()
    calls = 0

    async def check() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Notified while looking (the change was committed just after the query): the next wait ends at once.
            wakeup.notify("k")
            return ""
        return "found"

    async def main() -> str:
        started = time.monotonic()
        result = await wakeup.poll("k", check, wait_s=20, recheck_s=20)
        assert time.monotonic() - started < 1.
        return result

    assert asyncio.run(main()) == "found"


def test_without_notification_it_looks_only_at_the_start():
    wakeup = Wakeup()
    looks = 0

    async def check() -> list:
        nonlocal looks
        looks += 1
        return []

    async def main() -> list:
        task = asyncio.create_task(wakeup.poll("k", check, wait_s=0.5))
        await asyncio.sleep(0.1)
        # Other keys do not wake it.
        wakeup.notify("other")
        return await task

    started = time.monotonic()
    assert asyncio.run(main()) == []
    assert 0.5 <= time.monotonic() - started < 1.5
    assert looks == 1
    assert wakeup.waiting() == 0
    # With recheck_s it also looks every recheck_s.
    looks = 0
    assert asyncio.run(wakeup.poll("k", check, wait_s=0.5, recheck_s=0.2)) == []
    assert looks == 3


def test_a_waiting_node_gets_a_new_job_at_once(client, f, settings):
    settings.claim_wait_s = 15
    user = f.user()
    node = f.node()
    claimed: list = []

    def claim() -> None:
        r = client.post("/v1/node/claim", headers=node, json={})
        claimed.append((r.status_code, time.monotonic()))

    waiter = threading.Thread(target=claim)
    waiter.start()
    time.sleep(0.5)
    submitted = time.monotonic()
    job = f.submit(user)
    waiter.join(10)
    assert claimed and claimed[0][0] == 200
    # Woken by the submission, not by the next look of the database (every 10 s).
    assert claimed[0][1] - submitted < 3.
    with f.db.session() as s:
        assert s.get(Job, job["id"]).status == "running"


def test_jobs_back_in_the_queue_wake_the_nodes(client, f):
    services = client.app.state.services
    user = f.user()
    f.node("lost")
    job = f.submit(user)
    # Running on a node that stopped answering an hour ago.
    with f.db.session() as s:
        node = s.query(Node).filter_by(name="lost").one()
        j = s.get(Job, job["id"])
        j.status, j.node_id, j.started_at, j.attempts = "running", node.id, utcnow(), 1
        node.last_seen = utcnow() - timedelta(hours=1)
    keys: list[str] = []
    services.wakeup.notify = keys.append
    services.sweep()
    assert keys == ["jobs"]
    with f.db.session() as s:
        assert s.get(Job, job["id"]).status == "queued"


def test_a_sweep_in_flight_ends_before_the_server_stops(settings):
    """The sweep runs in a thread: stopping the server waits for it (before, the database was closed and the tables of
    the next test dropped under its DELETE, a deadlock in PostgreSQL)."""
    import threading
    import time

    from fastapi.testclient import TestClient

    from conftest import new_app

    app = new_app(settings)
    services = app.state.services
    started, ended = threading.Event(), threading.Event()

    def slow_sweep() -> None:
        started.set()
        time.sleep(0.5)
        ended.set()

    services.sweep = slow_sweep
    with TestClient(app):
        assert started.wait(5)
    assert ended.is_set()
