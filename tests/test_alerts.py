"""Alerts of the administrators: problems found, e-mailed once, repeated, closed; shown by /v1/admin/alerts."""

from __future__ import annotations

from collections import namedtuple
from datetime import timedelta

from sqlalchemy import select

from tisma_cloud import alerts
from tisma_cloud.models import Job, Node, User, utcnow


def run(client, now=None):
    return client.app.state.services.check_alerts(now)


def sent(client):
    return client.app.state.services.mailer.sent


def set_node_seen(client, name, when):
    with client.app.state.services.db.session() as s:
        s.execute(select(Node).where(Node.name == name)).scalar_one().last_seen = when


def test_node_down_is_mailed_once_repeated_and_resolved(client, f):
    f.user("jefe@x.com", admin=True)
    f.node("i5")
    f.node("nuevo")                                     # never connected: not installed yet, no alert
    now = utcnow()
    set_node_seen(client, "i5", now - timedelta(minutes=1))
    assert run(client, now) == [] and sent(client) == []

    set_node_seen(client, "i5", now - timedelta(minutes=10))
    open_ = run(client, now)
    assert [a.key for a in open_] == [f"node:{open_[0].key.split(':')[1]}"] and "i5" in open_[0].message
    assert len(sent(client)) == 1 and sent(client)[0][0] == "jefe@x.com" and "no responde" in sent(client)[0][1]

    run(client, now + timedelta(minutes=5))             # still down: no new e-mail
    assert len(sent(client)) == 1
    run(client, now + timedelta(hours=13))              # reminder after alert_repeat_h
    assert len(sent(client)) == 2 and "sigue" in sent(client)[1][1]

    set_node_seen(client, "i5", now + timedelta(hours=13))
    assert run(client, now + timedelta(hours=13, minutes=1)) == []
    assert len(sent(client)) == 3 and "resuelto" in sent(client)[2][1]


def test_queue_waiting_too_long(client, f):
    user = f.user("ana@x.com")
    f.user("jefe@x.com", admin=True)
    f.submit(user)
    now = utcnow()
    assert run(client, now) == []
    with client.app.state.services.db.session() as s:
        s.execute(select(Job)).scalar_one().created_at = now - timedelta(minutes=45)
    open_ = run(client, now)
    assert [a.key for a in open_] == ["queue"] and "1 trabajo" in open_[0].message


def test_disk_low(client, f, monkeypatch):
    f.user("jefe@x.com", admin=True)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda _p: usage(500e9, 498e9, 2e9))
    open_ = run(client)
    assert [a.key for a in open_] == ["disk"] and "2.0 GB" in open_[0].message


def test_recipients_from_settings_and_unreachable_mail(client, f):
    services = client.app.state.services
    services.settings.alert_emails = "ops@tisma.com.uy, otro@tisma.com.uy"
    f.node("i5")
    set_node_seen(client, "i5", utcnow() - timedelta(hours=1))

    def broken(*_a):
        raise OSError("SMTP down")
    services.mailer.send = broken                       # an e-mail that fails does not stop the sweep
    assert len(run(client)) == 1


def test_admin_alerts_api_and_sweep(client, f):
    admin = f.user("jefe@x.com", admin=True)
    f.node("i5")
    set_node_seen(client, "i5", utcnow() - timedelta(hours=1))
    services = client.app.state.services
    services._alerts_checked = None                     # the sweep at start-up already checked: once a minute
    services.sweep()                                    # the periodic sweep runs the check
    alerts_ = client.get("/v1/admin/alerts", headers=admin).json()["alerts"]
    assert len(alerts_) == 1 and alerts_[0]["resolved_at"] is None and "i5" in alerts_[0]["message"]
    # Users are not administrators.
    assert client.get("/v1/admin/alerts", headers=f.user("ana@x.com")).status_code == 403
    with client.app.state.services.db.session() as s:
        assert s.execute(select(User).where(User.is_admin.is_(True))).scalar_one().email == "jefe@x.com"
