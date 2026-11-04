"""Daily backups made by the master itself (backup.py): the tisma-cloud program has no backup service."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta

import pytest
from conftest import new_app
from tisma_cloud import backup, cli
from tisma_cloud.manager import Manager
from tisma_cloud.models import Plan


def _sqlite(settings) -> bool:
    return backup.sqlite_path(settings.database_url) is not None


def test_daily_backup_of_sqlite(settings, tmp_path):
    if not _sqlite(settings):
        pytest.skip("SQLite only")
    on = dataclasses.replace(settings, backup_dir=str(tmp_path / "copies"), backup_days=2, backup_weeks=1)
    # The services without the server: its own sweep would make a backup on the real clock.
    services = new_app(on).state.services
    if True:
        with services.db.session() as s:
            s.add(Plan(name="pro", monthly_hours=1, max_concurrent=1))
        monday = datetime(2026, 11, 2, 3, 30)
        # Before the hour nothing; then one, checked, and no second one the same day.
        assert services.backup_if_due(monday.replace(hour=2)) is None
        made = services.backup_if_due(monday)
        assert made is not None and made.path.name == "tisma-20261102-033000.db"
        backup.check_sqlite(made.path)
        con = sqlite3.connect(f"file:{made.path}?mode=ro", uri=True)
        assert con.execute("select name from plans").fetchone()[0] == "pro"
        assert con.execute("pragma journal_mode").fetchone()[0] == "delete"
        con.close()
        # One file per copy: nothing left beside it (the master's database is in WAL mode).
        assert [p.name for p in (tmp_path / "copies" / "daily").iterdir()] == [made.path.name]
        assert services.backup_if_due(monday + timedelta(hours=5)) is None
        # Three more days: two daily copies kept; Sunday's is also kept as the weekly one.
        for day in (1, 2, 6):
            assert services.backup_if_due(monday + timedelta(days=day)) is not None
        daily = sorted(p.name for p in (tmp_path / "copies" / "daily").iterdir())
        assert daily == ["tisma-20261104-033000.db", "tisma-20261108-033000.db"]
        assert [p.name for p in (tmp_path / "copies" / "weekly").iterdir()] == ["tisma-20261108-033000.db"]


def test_failed_backup_is_alerted(settings, tmp_path):
    if not _sqlite(settings):
        pytest.skip("SQLite only")
    (tmp_path / "a-file").write_text("x")
    broken = dataclasses.replace(settings, backup_dir=str(tmp_path / "a-file"))
    services = new_app(broken).state.services
    if True:
        assert services.backup_if_due(datetime(2026, 11, 2, 4)) is None
        assert services.backup_failed
        assert any(a.key == "backup" for a in services.check_alerts())


def test_backup_dir_default():
    from types import SimpleNamespace as NS

    assert backup.backup_dir(NS(backup_dir="", database_url="sqlite:///./tisma.db", storage_dir="/srv/t/storage")) \
        .as_posix() == "/srv/t/backups"
    assert backup.backup_dir(NS(backup_dir="", database_url="postgresql+psycopg://u@h/db", storage_dir="/s")) is None
    assert backup.backup_dir(NS(backup_dir="off", database_url="sqlite:///t.db", storage_dir="/s")) is None
    assert backup.backup_dir(NS(backup_dir="/b", database_url="postgresql://u@h/db", storage_dir="/s")).as_posix() \
        == "/b"


def test_commands_backup_and_restore(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TISMA_HOME", str(tmp_path))
    for var in ("TISMA_DATABASE_URL", "TISMA_STORAGE_DIR", "TISMA_BACKUP_DIR"):
        monkeypatch.delenv(var, raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    con = sqlite3.connect(tmp_path / "tisma.db")
    con.execute("create table t (v)")
    con.execute("insert into t values ('before')")
    con.commit()
    con.close()
    assert cli.main(["backup"]) == 0
    made = capsys.readouterr().out.split(" (")[0]
    assert made.startswith(str(tmp_path / "storage" / ".." / "backups")) or "backups" in made
    con = sqlite3.connect(tmp_path / "tisma.db")
    con.execute("update t set v = 'after'")
    con.commit()
    con.close()
    assert any(c.name == "Copias de seguridad" and c.ok for c in m.checks())
    assert cli.main(["restore", made]) == 0
    con = sqlite3.connect(tmp_path / "tisma.db")
    assert con.execute("select v from t").fetchone()[0] == "before"
    con.close()
    assert (tmp_path / "tisma.db.before-restore").exists()
    # A file that is not a good database is refused, and the database stays.
    (tmp_path / "bad.db").write_bytes(b"not a database at all" * 100)
    assert cli.main(["restore", str(tmp_path / "bad.db")]) == 1
    con = sqlite3.connect(tmp_path / "tisma.db")
    assert con.execute("select v from t").fetchone()[0] == "before"
    con.close()


def test_two_backups_in_one_second_are_both_kept(tmp_path):
    from types import SimpleNamespace as NS

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("create table t (v)")
    con.commit()
    con.close()
    st = NS(database_url=f"sqlite:///{db}", backup_days=14, backup_weeks=8)
    when = datetime(2026, 11, 4, 13, 29, 5)
    first = backup.make(st, tmp_path / "b", when)
    second = backup.make(st, tmp_path / "b", when)
    assert first.path != second.path and first.path.exists() and second.path.exists()
    assert backup.latest(tmp_path / "b") == second.path
    # The names of the first version, without seconds, still count.
    assert backup.stamp_of(tmp_path / "tisma-20261104-1329.db") == datetime(2026, 11, 4, 13, 29)


def test_concurrent_backups_get_their_own_names(tmp_path):
    import threading
    from types import SimpleNamespace as NS

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("create table t (v)")
    con.execute("insert into t values (randomblob(200000))")
    con.commit()
    con.close()
    st = NS(database_url=f"sqlite:///{db}", backup_days=14, backup_weeks=8)
    when = datetime(2026, 11, 4, 13, 47, 6)
    made, errors = [], []

    def run() -> None:
        try:
            made.append(backup.make(st, tmp_path / "b", when).path)
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(made)) == 4
    for path in made:
        backup.check_sqlite(path)
    assert sorted(p.name for p in (tmp_path / "b" / "daily").iterdir()) == sorted(p.name for p in made)
