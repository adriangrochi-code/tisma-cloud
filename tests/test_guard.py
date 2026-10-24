from __future__ import annotations

import asyncio

from conftest import make_3mf
from fastapi.testclient import TestClient

from conftest import new_app as create_app
from tisma_cloud.guard import Bans, Uploads

INTERNET = ("200.40.10.7", 40000)


def call(app, method: str, path: str, headers: dict[str, str], client=INTERNET) -> tuple[int, int]:
    """Calls the app directly; returns the status and how many times the body was read."""
    reads = 0
    status = 0

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "scheme": "http",
             "server": ("testserver", 80), "client": client,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(app(scope, receive, send))
    return status, reads


def test_uploads_are_refused_before_reading_them(client, f):
    app = client.app
    user = f.user()
    huge = {"content-type": "multipart/form-data; boundary=x", "content-length": str(150 << 20)}
    # Without a valid token, nothing of the upload is read.
    assert call(app, "POST", "/v1/jobs", huge) == (401, 0)
    assert call(app, "POST", "/v1/jobs", {**huge, "authorization": "Bearer tu_fake"}) == (401, 0)
    assert call(app, "POST", "/v1/node/jobs/abc/result", {**huge, **user}) == (401, 0)
    # Larger than the limit, or without a length: refused at once too.
    assert call(app, "POST", "/v1/jobs", {**huge, "content-length": str(500 << 20), **user}) == (413, 0)
    assert call(app, "POST", "/v1/jobs", {"content-type": "multipart/form-data; boundary=x", **user}) == (411, 0)
    assert not list((client.app.state.services.storage.root / "jobs").iterdir())


def test_addresses_with_many_refused_requests_are_blocked(settings):
    settings.ban_threshold = 5
    app = create_app(settings)
    with TestClient(app, client=INTERNET) as internet, TestClient(app, client=("192.168.1.9", 1)) as local:
        for _ in range(5):
            assert internet.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
        # Now everything from that address is refused for a while, without touching the application.
        r = internet.get("/v1/health")
        assert r.status_code == 403 and "Blocked" in r.text
        # Another address is fine; the local network is never blocked.
        assert TestClient(app, client=("200.40.10.8", 1)).get("/v1/health").status_code == 200
        for _ in range(10):
            local.get("/v1/me", headers={"Authorization": "Bearer nope"})
        assert local.get("/v1/health").status_code == 200


def test_pages_without_account_are_limited_per_address(settings):
    settings.anonymous_per_minute = 3
    app = create_app(settings)
    with TestClient(app, client=INTERNET) as internet:
        assert [internet.get("/v1/health").status_code for _ in range(4)] == [200, 200, 200, 429]
        assert internet.post("/v1/auth/login", json={"email": "a@x.com", "password": "x"}).status_code == 429
    with TestClient(app, client=("192.168.1.9", 1)) as local:
        assert all(local.get("/v1/health").status_code == 200 for _ in range(10))


def test_valid_uploads_still_work_from_internet(client, f):
    user = f.user()
    with TestClient(client.app, client=INTERNET) as internet:
        r = internet.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
        assert r.status_code == 201, r.text


def test_upload_places():
    up = Uploads(total=3, per_account=2)
    assert up.take("u:1") and up.take("u:1")
    assert not up.take("u:1")          # per account
    assert up.take("u:2")
    assert not up.take("u:3")          # in all
    up.give("u:1")
    assert up.take("u:3")


def test_bans_expire():
    bans = Bans(threshold=2, minutes=1)
    bans.refused("1.2.3.4", now=0.)
    assert not bans.banned("1.2.3.4", now=1.)
    bans.refused("1.2.3.4", now=1.)
    bans.refused("1.2.3.4", now=2.)
    assert bans.banned("1.2.3.4", now=3.)
    assert not bans.banned("1.2.3.4", now=3. + 60.)
