"""A new key for the personal data (crypto.rotate, «tisma-cloud rotate-key»)."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select, text

from tisma_cloud import crypto, library
from tisma_cloud.models import Job, LibraryBlock, User


def _setup(client, f):
    services = client.app.state.services
    f.user("ana@x.com")
    f.user("beto@x.com")
    f.submit(f.user("carla@x.com"))
    with services.db.session() as s:
        beto = s.execute(select(User).where(User.email_index == crypto.index("beto@x.com"))).scalar_one()
        s.add(LibraryBlock(kind="account", value=library.account_id(beto.id), reason="spam"))
        before = (library.sequence(s, "index"), library.sequence(s, "blocklist"))
    return services, beto.id, before


def _raw_emails(db):
    with db.session() as s:
        return [e for (e,) in s.execute(text("SELECT email FROM users ORDER BY id"))]


def test_every_value_is_written_again_with_the_new_key(client, f):
    services, beto, before = _setup(client, f)
    path = crypto.key_path(services.settings)
    old_key = path.read_text()
    raw_before = _raw_emails(services.db)

    done = crypto.rotate(services.db, path, "20261113-060000")
    assert not done["finished"] and done["users"] == 3 and done["blocked_accounts"] == 1 and done["values"] >= 4
    old_file = path.with_name("data.key.old-20261113-060000")
    assert old_file.read_text() == old_key and path.read_text() != old_key
    assert not path.with_name("data.key.new").exists()
    assert _raw_emails(services.db) != raw_before

    crypto.configure(services.settings)   # the new key, as the service at its next start
    with services.db.session() as s:
        assert {u.email for u in s.execute(select(User)).scalars()} == {"ana@x.com", "beto@x.com", "carla@x.com"}
        # Found by e-mail (the index with the new key): the logins keep working.
        assert s.execute(select(User).where(User.email_index == crypto.index("beto@x.com"))).scalar_one().id == beto
        assert s.execute(select(Job)).scalar_one().input_name.endswith(".3mf")
        # The suspended account under its new public id; both lists signed again.
        assert s.execute(select(LibraryBlock.value)).scalar_one() == library.account_id(beto)
        assert (library.sequence(s, "index"), library.sequence(s, "blocklist")) == (before[0] + 1, before[1] + 1)
    # The old key does not read the database any more.
    assert crypto._readable_with(services.db, crypto.AESGCM(base64.b64decode(old_key.strip()))) is False


def test_a_cut_between_the_database_and_the_files_is_finished_by_the_next_run(client, f, monkeypatch):
    services, _beto, _before = _setup(client, f)
    path = crypto.key_path(services.settings)
    real_replace = crypto.os.replace

    def cut(*_args):
        raise OSError("power cut")

    monkeypatch.setattr(crypto.os, "replace", cut)
    with pytest.raises(OSError, match="power cut"):
        crypto.rotate(services.db, path, "20261113-060000")
    new = path.with_name("data.key.new")
    assert new.exists()
    monkeypatch.setattr(crypto.os, "replace", real_replace)
    done = crypto.rotate(services.db, path, "20261113-060100")
    assert done["finished"] and not new.exists()
    crypto.configure(services.settings)
    with services.db.session() as s:
        assert s.execute(select(User).where(User.email_index == crypto.index("ana@x.com"))).scalar_one()


def test_a_new_key_left_without_changing_the_database_is_dropped(client, f):
    services, _beto, _before = _setup(client, f)
    path = crypto.key_path(services.settings)
    crypto._write_key(path.with_name("data.key.new"), b"\x01" * 32)   # a run cut before the database changed
    done = crypto.rotate(services.db, path, "20261113-060000")
    assert not done["finished"] and done["users"] == 3
    crypto.configure(services.settings)
    with services.db.session() as s:
        assert len(list(s.execute(select(User)).scalars())) == 3


def test_another_key_is_refused_before_changing_anything(client, f):
    services, _beto, _before = _setup(client, f)
    path = crypto.key_path(services.settings)
    raw = _raw_emails(services.db)
    path.write_text(base64.b64encode(b"\x02" * 32).decode() + "\n")
    with pytest.raises(crypto.RotationError, match="not the key of this database"):
        crypto.rotate(services.db, path, "20261113-060000")
    assert _raw_emails(services.db) == raw and not path.with_name("data.key.new").exists()


def test_the_command_makes_a_backup_first_and_puts_the_new_key_in_place(tmp_path, monkeypatch, capsys):
    """«tisma-cloud rotate-key» on a master's folder: a backup before, the key changed, the old one kept beside it."""
    from tisma_cloud import cli
    from tisma_cloud.manager import Manager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TISMA_HOME", str(tmp_path))
    for var in ("TISMA_DATABASE_URL", "TISMA_STORAGE_DIR", "TISMA_BACKUP_DIR", "TISMA_DATA_KEY"):
        monkeypatch.delenv(var, raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    db, settings = m._database()
    db.create_all()
    crypto.migrate(db)
    with db.session() as s:
        from tisma_cloud.models import Plan

        s.add(User(email="ana@x.com", plan_name=s.execute(select(Plan.name)).scalars().first()))
    db.engine.dispose()
    key = tmp_path / "storage" / "data.key"
    old = key.read_text()

    assert cli.main(["rotate-key"]) == 0
    out = capsys.readouterr().out
    assert "Copia de antes:" in out and "Clave nueva en su lugar" in out
    assert key.read_text() != old and [p.read_text() for p in key.parent.glob("data.key.old-*")] == [old]
    assert list((tmp_path / "backups").rglob("*.db"))
    db, _settings = m._database()   # loads the new key, as the service would
    with db.session() as s:
        assert s.execute(select(User).where(User.email_index == crypto.index("ana@x.com"))).scalar_one()
    db.engine.dispose()

    # With the key in TISMA_DATA_KEY there is no file to change.
    monkeypatch.setenv("TISMA_DATA_KEY", base64.b64encode(b"\x03" * 32).decode())
    assert cli.main(["rotate-key"]) == 1
    assert "TISMA_DATA_KEY" in capsys.readouterr().err
