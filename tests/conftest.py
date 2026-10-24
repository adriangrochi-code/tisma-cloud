from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tisma_cloud.api import create_app
from datetime import timedelta

from tisma_cloud.models import ApiToken, Node, Plan, User, utcnow
from tisma_cloud.security import hash_token, new_token
from tisma_cloud.settings import Settings

DATA = Path(__file__).parent / "data"


def make_3mf(triangles: int = 12, config: str | None = None, extra: dict[str, str] | None = None) -> bytes:
    """A small 3MF with the given number of triangle tags (enough for the estimation of the master)."""
    tris = "".join(f'<triangle v1="0" v2="1" v3="2"/>' for _ in range(triangles))
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter"><resources><object id="1" type="model">'
             '<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
             f'</vertices><triangles>{tris}</triangles></mesh></object></resources><build><item objectid="1"/></build>'
             '</model>')
    buf = io.BytesIO()

    def put(z: zipfile.ZipFile, name: str, content: str) -> None:
        # A fixed date: the same arguments give the same bytes (the current time changes every 2 s in a ZIP).
        z.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), content, compress_type=zipfile.ZIP_DEFLATED)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        put(z, "3D/3dmodel.model", model)
        if config is not None:
            put(z, "Metadata/Slic3r_PE.config", config)
        for name, content in (extra or {}).items():
            put(z, name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def fast_passwords(monkeypatch):
    """Passwords hashed with lighter parameters in the tests (the real ones take half a second each);
    test_privacy.py checks the real ones."""
    from tisma_cloud import security

    monkeypatch.setattr(security, "_N", 1 << 12)


@pytest.fixture(autouse=True)
def data_key(tmp_path: Path, monkeypatch):
    """A key of the personal data for the models used without the app (the app loads the one of its storage)."""
    from types import SimpleNamespace

    from tisma_cloud import crypto

    monkeypatch.delenv("TISMA_DATA_KEY", raising=False)
    crypto.configure(SimpleNamespace(storage_dir=tmp_path / "key"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    url = os.environ.get("TISMA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    # No public address asked to internet (publicip.py) in the tests.
    return Settings(database_url=url, storage_dir=tmp_path / "storage", claim_wait_s=0, large_slice_triangles=1000,
                    site_key="site-secret", public_ip_url="")


def new_app(settings: Settings):
    """An app on an empty database (a shared PostgreSQL test database is emptied first: its data was encrypted with
    the key of another test)."""
    if not settings.database_url.startswith("sqlite"):
        from tisma_cloud.db import Base, Database

        db = Database(settings.database_url)
        Base.metadata.drop_all(db.engine)
        db.engine.dispose()
    return create_app(settings)


@pytest.fixture
def client(settings: Settings):
    app = new_app(settings)
    # A computer of the local network (the administration is only for it).
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        yield c


class Factory:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.db = client.app.state.services.db

    def plan(self, name: str = "pro", hours: float = 10., concurrent: int = 3, rpm: int = 1000,
             priority: int = 0, remote_printers: int = 3) -> str:
        with self.db.session() as s:
            plan = s.get(Plan, name) or Plan(name=name)
            plan.monthly_hours, plan.max_concurrent, plan.requests_per_minute, plan.priority = hours, concurrent, rpm, priority
            plan.remote_printers = remote_printers
            s.add(plan)
        return name

    def user(self, email: str = "ana@x.com", plan: str | None = None, paid_days: float | None = 30.,
             admin: bool = False) -> dict:
        plan = plan or self.plan()
        token = new_token("tu")
        with self.db.session() as s:
            user = User(email=email, plan_name=plan, is_admin=admin,
                        paid_until=utcnow() + timedelta(days=paid_days) if paid_days is not None else None)
            s.add(user)
            s.flush()
            s.add(ApiToken(user_id=user.id, token_hash=hash_token(token)))
        return {"Authorization": f"Bearer {token}"}

    def node(self, name: str = "i5", priorities: str = "fea_optimize,fea,slice_large,slice") -> dict:
        token = new_token("tn")
        with self.db.session() as s:
            s.add(Node(name=name, token_hash=hash_token(token), priorities=priorities))
        return {"Authorization": f"Bearer {token}"}

    def submit(self, headers: dict, kind: str = "slice", params: str = "{}", data: bytes | None = None,
               expect: int = 201) -> dict:
        r = self.client.post("/v1/jobs", headers=headers, data={"kind": kind, "params": params},
                             files={"file": ("part.3mf", data if data is not None else make_3mf(), "model/3mf")})
        assert r.status_code == expect, r.text
        return r.json()

    def claim(self, headers: dict) -> dict | None:
        r = self.client.post("/v1/node/claim", headers=headers, json={})
        if r.status_code == 204:
            return None
        assert r.status_code == 200, r.text
        return r.json()["job"]

    def me(self, headers: dict) -> dict:
        return self.client.get("/v1/me", headers=headers).json()


@pytest.fixture
def f(client: TestClient) -> Factory:
    return Factory(client)
