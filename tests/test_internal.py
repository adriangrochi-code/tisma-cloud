"""Internal accounts: unlimited computing, never charged (TISMA_INTERNAL_EMAILS, the administration)."""

from __future__ import annotations

import re

from conftest import new_app
from fastapi.testclient import TestClient
from sqlalchemy import select

from tisma_cloud import admin as admin_cli
from tisma_cloud import crypto
from tisma_cloud.api import create_app
from tisma_cloud.models import User
from tisma_cloud.settings import Settings


def set_unlimited(f, email: str, value: bool = True) -> None:
    with f.db.session() as s:
        s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one().unlimited = value


def test_internal_emails_setting():
    s = Settings(internal_emails=" Equipo@Tisma.com.uy , @lab.tisma.uy ")
    assert s.is_internal_email("equipo@tisma.com.uy")
    assert s.is_internal_email("ana@lab.tisma.uy")
    assert not s.is_internal_email("ana@tisma.com.uy")
    assert not s.is_internal_email("ana@xlab.tisma.uy.com")
    assert not Settings(internal_emails="").is_internal_email("a@b.c")


def test_unlimited_account_skips_plan_and_payment(client, f):
    user = f.user("eq@x.com", plan=f.plan("tiny", hours=0., concurrent=1), paid_days=None)
    f.submit(user, expect=402)
    set_unlimited(f, "eq@x.com")
    for _ in range(3):
        f.submit(user)
    me = f.me(user)
    assert me["unlimited"] and me["paid"]
    # Back to a normal account: its plan applies again.
    set_unlimited(f, "eq@x.com", False)
    f.submit(user, expect=402)


def test_admin_makes_an_account_internal(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user("eq@x.com", plan=f.plan("tiny", hours=0., concurrent=1), paid_days=None)
    r = client.put("/v1/admin/users/EQ@x.com/unlimited", headers=admin, json={"unlimited": True})
    assert r.status_code == 200 and r.json()["user"]["unlimited"] and r.json()["account"]["unlimited"]
    f.submit(user)
    assert client.put("/v1/admin/users/eq@x.com/unlimited", headers=user, json={"unlimited": True}).status_code == 403
    assert client.put("/v1/admin/users/nobody@x.com/unlimited", headers=admin, json={"unlimited": True}).status_code == 404
    users = {u["email"]: u for u in client.get("/v1/admin/users", headers=admin).json()["users"]}
    assert users["eq@x.com"]["unlimited"] and not users["root@x.com"]["unlimited"]


def test_unverified_internal_account_is_limited(client, f):
    user = f.user("eq@x.com", plan=f.plan("tiny", hours=0., concurrent=1), paid_days=None)
    with f.db.session() as s:
        u = s.execute(select(User).where(User.email_index == crypto.index("eq@x.com"))).scalar_one()
        u.unlimited, u.email_verified = True, False
    f.submit(user, expect=402)


def test_registration_with_an_internal_email(settings):
    settings.internal_emails = "@tisma.com.uy"
    settings.login_per_minute = 100
    with TestClient(new_app(settings), client=("200.40.10.7", 40000)) as web:
        for email in ("eq@tisma.com.uy", "otro@correo.uy"):
            assert web.post("/v1/auth/register", json={"email": email, "password": "una clave larga"}).status_code == 201
            mail = [m for m in web.app.state.services.mailer.sent if m[0] == email][-1]
            code = re.search(r"\b(\d{6})\b", mail[2]).group(1)
            token = web.post("/v1/auth/verify", json={"email": email, "code": code}).json()["token"]
            me = web.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
            assert me["unlimited"] == (email == "eq@tisma.com.uy")


def test_internal_emails_apply_at_start(settings, f):
    f.user("eq@tisma.com.uy", paid_days=None)
    f.user("otro@correo.uy", paid_days=None)
    settings.internal_emails = "eq@tisma.com.uy"
    app = create_app(settings)
    with app.state.services.db.session() as s:
        flags = dict(s.execute(select(User.email, User.unlimited)).all())
    assert flags == {"eq@tisma.com.uy": True, "otro@correo.uy": False}


def test_admin_command(settings, f, monkeypatch, capsys):
    f.user("eq@x.com", paid_days=None)
    monkeypatch.setenv("TISMA_DATABASE_URL", settings.database_url)
    # The command reads the key of the personal data from the same storage as the service.
    monkeypatch.setenv("TISMA_STORAGE_DIR", str(settings.storage_dir))
    assert admin_cli.main(["user-set", "eq@x.com", "--unlimited"]) == 0
    assert "unlimited" in capsys.readouterr().out
    with f.db.session() as s:
        assert s.execute(select(User.unlimited).where(User.email_index == crypto.index("eq@x.com"))).scalar_one()
    assert admin_cli.main(["user-set", "eq@x.com", "--limited"]) == 0
    with f.db.session() as s:
        assert not s.execute(select(User.unlimited).where(User.email_index == crypto.index("eq@x.com"))).scalar_one()
