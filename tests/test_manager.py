"""The service on one machine (manager.py) and its commands for SSH (cli.py)."""

from __future__ import annotations

import io
import socket
import sys
from pathlib import Path

import pytest

from tisma_cloud import cli
from tisma_cloud.manager import Manager
from tisma_cloud.node.config import NodeConfig

SERVER_DIR = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_role_and_settings(tmp_path):
    m = Manager(tmp_path)
    assert m.role() == "" and m.status().detail.startswith("Sin rol")
    with pytest.raises(ValueError):
        m.set_role("slave")
    m.set_role("master")
    assert m.role() == "master" and m.read_env()["TISMA_DATABASE_URL"] == "sqlite:///./tisma.db"
    m.set_port(8123)
    assert m.port() == 8123 and m.role() == "master"
    # The other lines and comments of .env are kept.
    m.env_path.write_text("# comment\nTISMA_SITE_KEY=old\nOTHER=1\n", encoding="utf-8")
    m.set_setting("TISMA_SITE_KEY", "new")
    m.set_setting("TISMA_INTERNAL_EMAILS", "@tisma.com.uy")
    text = m.env_path.read_text(encoding="utf-8")
    assert "# comment" in text and "OTHER=1" in text
    assert m.read_env() == {"TISMA_SITE_KEY": "new", "OTHER": "1", "TISMA_INTERNAL_EMAILS": "@tisma.com.uy"}


def test_node_settings_load_in_the_node(tmp_path):
    m = Manager(tmp_path)
    m.set_role("node")
    m.set_setting("master_url", "http://192.168.1.10:8000/")
    m.set_setting("token", "tn_abc")
    m.set_setting("tisma_slicer", 'C:\\Program Files\\Tisma "x"\\tisma-slicer-console.exe')
    m.set_setting("tisma_fea", "/opt/tisma/tisma-fea")
    m.set_setting("slots", "2")
    m.set_setting("gpu", "no")
    with pytest.raises(ValueError):
        m.set_setting("unknown", "1")
    cfg = NodeConfig.load(m.node_path)
    assert cfg.master_url == "http://192.168.1.10:8000" and cfg.token == "tn_abc" and cfg.slots == 2
    assert cfg.tisma_slicer == 'C:\\Program Files\\Tisma "x"\\tisma-slicer-console.exe' and cfg.gpu is False


def test_start_and_stop_the_master(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(SERVER_DIR))
    m = Manager(tmp_path)
    m.set_role("master")
    m.set_port(free_port())
    st = m.start(wait_s=40)
    try:
        assert st.running and st.healthy, m.logs()
        assert m.pid_path.is_file() and m.health()
        assert any("Uvicorn running" in line or "Application startup" in line for line in m.logs(200)), m.logs(200)
        with pytest.raises(RuntimeError):
            m.set_role("node")  # not while it runs
    finally:
        st = m.stop()
    assert not st.running and not m.pid_path.exists() and not m.health()


def test_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # the commands work in the home; restored after the test
    monkeypatch.setenv("TISMA_HOME", str(tmp_path))
    assert cli.main(["status"]) == 3
    assert cli.main(["role", "node"]) == 0
    assert cli.main(["config", "master_url", "http://10.0.0.2:8000"]) == 0
    out = capsys.readouterr().out
    assert "master_url = http://10.0.0.2:8000" in out and "token = " in out
    assert cli.main(["role"]) == 0 and capsys.readouterr().out.strip() == "node"
    assert cli.main(["role", "boss"]) == 1


def test_admin_from_the_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TISMA_HOME", str(tmp_path))
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    m.write_env({"TISMA_DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite'}"})
    code, out = cli.admin_output(m, ["plan-set", "basico", "--hours", "5", "--concurrent", "1"])
    assert code == 0 and "Plan basico" in out
    code, out = cli.admin_output(m, ["user-add", "eq@tisma.com.uy", "--plan", "basico", "--unlimited"])
    assert code == 0 and "created" in out
    code, out = cli.admin_output(m, ["user-list"])
    assert "eq@tisma.com.uy" in out and "unlimited" in out
    assert (tmp_path / "db.sqlite").is_file()


def test_console(tmp_path, monkeypatch, capsys):
    m = Manager(tmp_path)
    # Role node, then change its slots in the settings, then leave.
    monkeypatch.setattr(sys, "stdin", io.StringIO("5\n2\n6\n6\n3\n0\n"))
    assert cli.console(m) == 0
    assert m.role() == "node" and m.read_node()["slots"] == 3
    assert "Guardado" in capsys.readouterr().out


def test_what_each_node_does(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path.parent)  # a relative SQLite file is still the one of the home
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    assert m.nodes() == []
    code, _out = cli.admin_output(m, ["node-add", "i7-1", "--priorities", "slice,slice_large"])
    code2, _out = cli.admin_output(m, ["node-add", "i5", "--priorities", "fea_optimize,fea"])
    assert code == 0 and code2 == 0
    nodes = {n["name"]: n for n in m.nodes()}
    assert nodes["i7-1"]["priorities"] == ["slice", "slice_large"] and not nodes["i7-1"]["online"]
    m.set_node("i7-1", priorities=["fea", "slice"])
    m.set_node("i5", enabled=False)
    nodes = {n["name"]: n for n in m.nodes()}
    assert nodes["i7-1"]["priorities"] == ["fea", "slice"] and not nodes["i5"]["enabled"]
    for bad in ([], ["slice", "slice"], ["render"]):
        with pytest.raises(ValueError):
            m.set_node("i7-1", priorities=bad)
    with pytest.raises(ValueError):
        m.set_node("nobody", enabled=True)
    assert (tmp_path / "tisma.db").is_file()
    # The console: node 1 (i5, by name) does slicing and large plates, then it is enabled again.
    monkeypatch.setattr(sys, "stdin", io.StringIO("10\n1\n1,2\n10\n1\nh\n0\n"))
    assert cli.console(m) == 0
    nodes = {n["name"]: n for n in m.nodes()}
    assert nodes["i5"]["priorities"] == ["slice", "slice_large"] and nodes["i5"]["enabled"]
    assert "Guardado" in capsys.readouterr().out


def test_panel_signs_in_the_administrator(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tisma_cloud.api import create_app
    from tisma_cloud.settings import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    assert not m.has_admin()
    with pytest.raises(RuntimeError):
        m.panel_token()
    with pytest.raises(ValueError):
        m.create_admin("yo@tisma.com.uy", "corta")
    m.create_admin("Yo@Tisma.com.uy", "una clave larga")
    assert m.has_admin()
    first = m.panel_token()
    token = m.panel_url().split("#token=")[1]
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'tisma.db'}", storage_dir=tmp_path / "storage"))
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        auth = {"Authorization": f"Bearer {token}"}
        assert c.get("/v1/admin/nodes", headers=auth).status_code == 200
        # A new panel replaces the session of the previous one.
        assert c.get("/v1/admin/nodes", headers={"Authorization": f"Bearer {first}"}).status_code == 401
        page = c.get("/admin")
        assert page.status_code == 200 and "location.hash" in page.text
        # The administrator can log in with the password too.
        r = c.post("/v1/auth/login", json={"email": "yo@tisma.com.uy", "password": "una clave larga"})
        assert r.status_code == 200


def test_dashboard_of_the_master(tmp_path, monkeypatch):
    from datetime import timedelta

    from tisma_cloud.models import Alert, Job, Node, Payment, Plan, Usage, User, month_of, utcnow

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    empty = m.dashboard()
    assert empty["nodes"] == [] and empty["jobs"] == [] and empty["users"] == 0 and empty["hours_month"] == 0
    db, _settings = m._database()
    now = utcnow()
    with db.session() as s:
        s.add(Plan(name="pro", monthly_hours=10))
        s.flush()
        ana = User(email="ana@x.uy", plan_name="pro", paid_until=now + timedelta(days=20))
        old = User(email="old@x.uy", plan_name="pro", created_at=now - timedelta(days=30))
        busy = Node(name="i7", token_hash="h1", priorities="slice,fea", last_seen=now, info='{"cpus": 8}')
        idle = Node(name="i5", token_hash="h2", priorities="fea", last_seen=now - timedelta(hours=1))
        s.add_all([ana, old, busy, idle])
        s.flush()
        s.add_all([
            Job(user_id=ana.id, kind="slice", job_class="slice", status="running", node_id=busy.id, progress=40,
                input_name="benchy.3mf", created_at=now - timedelta(minutes=1)),
            Job(user_id=ana.id, kind="slice", job_class="slice", status="queued", created_at=now),
            Job(user_id=old.id, kind="fea", job_class="fea", status="done", seconds=90., finished_at=now,
                created_at=now - timedelta(minutes=5)),
            Job(user_id=old.id, kind="fea", job_class="fea", status="failed", error="no memory", finished_at=now,
                created_at=now - timedelta(minutes=6)),
            # Ended yesterday: not one of today.
            Job(user_id=old.id, kind="slice", job_class="slice", status="done", finished_at=now - timedelta(days=2),
                created_at=now - timedelta(days=2)),
            Usage(user_id=ana.id, month=month_of(now), seconds=5400.),
            Usage(user_id=old.id, month="2001-01", seconds=99999.),
            Payment(user_id=ana.id, plan_name="pro", provider="manual", amount_cents=900, currency="USD",
                    status="approved", paid_at=now),
            Payment(user_id=old.id, plan_name="pro", provider="manual", amount_cents=900, status="pending"),
            Alert(key="nodes", message="Sin nodos en línea"),
            Alert(key="disk", message="Disco lleno", resolved_at=now),
        ])
    d = m.dashboard()
    nodes = {n["name"]: n for n in d["nodes"]}
    assert nodes["i7"]["online"] and nodes["i7"]["running"] == 1 and nodes["i7"]["info"] == {"cpus": 8}
    assert not nodes["i5"]["online"] and nodes["i5"]["priorities"] == ["fea"]
    assert (d["queued"], d["running"], d["done_today"], d["failed_today"]) == (1, 1, 1, 1)
    assert [j["status"] for j in d["jobs"]] == ["queued", "running", "done", "failed", "done"]
    assert d["jobs"][1]["node"] == "i7" and d["jobs"][1]["user"] == "ana@x.uy" and d["jobs"][3]["error"] == "no memory"
    assert (d["users"], d["new_users"], d["paying_users"]) == (2, 1, 1)
    assert d["hours_month"] == 1.5
    assert d["payments_month"] == [{"currency": "USD", "count": 1, "amount_cents": 900}]
    assert d["alerts"] == [{"message": "Sin nodos en línea", "started_at": d["alerts"][0]["started_at"]}]
    # The last 7 days: today has one done and one failed, two days ago one done.
    assert len(d["days"]) == 7 and d["days"][-1]["done"] == 1 and d["days"][-1]["failed"] == 1
    assert d["days"][-3] == {"day": (now - timedelta(days=2)).date().isoformat(), "done": 1, "failed": 0}
    users = m.users()
    assert [u["email"] for u in users] == ["ana@x.uy", "old@x.uy"]
    assert users[0]["paid"] and users[0]["hours_month"] == 1.5 and not users[1]["paid"]
    r = m.resources()
    assert r["cpus"] >= 1 and r["disk_free_gb"] <= r["disk_gb"]


def test_the_master_computes_too(tmp_path, monkeypatch):
    """The node of the master: registered in its database, connected to the master of the machine, run by the service."""
    from sqlalchemy import select

    from tisma_cloud import system
    from tisma_cloud.manager import MASTER_NODE
    from tisma_cloud.models import Node
    from tisma_cloud.security import hash_token

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    monkeypatch.delenv("TISMA_STORAGE_DIR", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    m.set_port(8123)
    m.write_env({"TISMA_DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite'}"})
    assert m.master_computes()
    # Without Tisma Slicer on the machine the master serves without computing.
    monkeypatch.setattr(system, "find_tisma_programs", lambda: (None, None))
    assert m.prepare_master_node() is None and list(m.children()) == ["main"]
    slicer, fea = tmp_path / "tisma-slicer", tmp_path / "tisma-fea"
    slicer.write_text("x")
    fea.write_text("x")
    monkeypatch.setattr(system, "find_tisma_programs", lambda: (str(slicer), str(fea)))
    path = m.prepare_master_node()
    cfg = NodeConfig.load(path)
    assert cfg.master_url == "http://127.0.0.1:8123" and cfg.tisma_slicer == str(slicer)
    db, _settings = m._database()
    with db.session() as s:
        node = s.execute(select(Node).where(Node.name == MASTER_NODE)).scalar_one()
        assert node.token_hash == hash_token(cfg.token) and node.enabled
    # Again: the same token; a changed setting is kept.
    path.write_text(path.read_text(encoding="utf-8").replace("slots = 0", "slots = 2"), encoding="utf-8")
    assert NodeConfig.load(m.prepare_master_node()).token == cfg.token
    assert NodeConfig.load(path).slots == 2
    children = m.children()
    assert list(children) == ["main", "node"] and children["node"][-2:] == ["--child", "--local-node"]
    assert any(c.name == "El maestro calcula" and c.ok for c in m.checks())
    # Turned off: only the master.
    m.set_setting("master_computes", "false")
    assert not m.master_computes() and list(m.children()) == ["main"]
