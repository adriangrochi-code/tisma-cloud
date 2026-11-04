"""Daily backups of the database of the master, made by the master itself: the tisma-cloud program on Windows or Linux
has no backup service (with Docker, deploy/backup.sh does it with pg_dump).

- SQLite (the database of the program by default): an online copy with SQLite's backup API, checked with
  PRAGMA integrity_check before it counts.
- PostgreSQL: pg_dump in custom format, checked with pg_restore --list, when those programs are installed and
  TISMA_BACKUP_DIR is set (with Docker the backup service already does it).

As backup.sh: written aside and renamed when complete and checked; the last backup_days daily copies and one per week
(Sunday) for backup_weeks weeks. The job files and the key of the personal data (data.key) are not copied: the files
are deleted after a few days anyway, and a stolen copy is useless without the key, which is kept apart once."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("tisma_cloud")

PREFIX = "tisma-"
# tisma-YYYYMMDD-HHMMSS.ext (UTC); the first ones had no seconds.
NAME = re.compile(r"^tisma-\d{8}-\d{4}(\d{2})?\.(db|dump)$")


class BackupError(Exception):
    pass


@dataclass
class Backup:
    path: Path
    size: int


def sqlite_path(database_url: str) -> Path | None:
    """The file of a SQLite database URL (relative to the current folder, as the master opens it), or None."""
    if not database_url.startswith("sqlite:///"):
        return None
    rest = database_url[len("sqlite:///"):].split("?", 1)[0]
    if not rest or rest == ":memory:":
        return None
    return Path(rest)


def backup_dir(settings) -> Path | None:  # noqa: ANN001
    """Where the backups go: TISMA_BACKUP_DIR; "off" for none; empty: next to the files of the master with SQLite,
    none with PostgreSQL (the Docker backup service makes them)."""
    value = settings.backup_dir.strip()
    if value.lower() == "off":
        return None
    if value:
        return Path(value)
    if sqlite_path(settings.database_url) is not None:
        return Path(settings.storage_dir).parent / "backups"
    return None


def _ext(database_url: str) -> str:
    return ".db" if sqlite_path(database_url) is not None else ".dump"


def latest(dest: Path) -> Path | None:
    files = sorted((f for f in (dest / "daily").iterdir() if NAME.match(f.name)), key=_order) \
        if (dest / "daily").is_dir() else []
    return files[-1] if files else None


def stamp_of(path: Path) -> datetime | None:
    """The time in the name of a backup (tisma-YYYYMMDD-HHMMSS.ext, UTC)."""
    if not NAME.match(path.name):
        return None
    text = path.stem[len(PREFIX):]
    try:
        return datetime.strptime(text, "%Y%m%d-%H%M%S" if len(text) == 15 else "%Y%m%d-%H%M")
    except ValueError:
        return None


def _order(path: Path) -> tuple[datetime, str]:
    # By the time in the name, not the name: names with and without seconds sort together.
    return stamp_of(path) or datetime.min, path.name


def check_sqlite(path: Path) -> None:
    if not path.is_file():
        raise BackupError(f"{path} does not exist")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    except sqlite3.DatabaseError as ex:
        raise BackupError(f"{path.name} is not a good SQLite database: {ex}") from ex
    finally:
        con.close()
    if not result or result[0] != "ok":
        raise BackupError(f"{path.name} failed the integrity check: {result[0] if result else '?'}")
    if tables == 0:
        raise BackupError(f"{path.name} has no tables")


def _postgres_url(database_url: str) -> str:
    # pg_dump takes a libpq URL: without the driver of SQLAlchemy.
    scheme, _, rest = database_url.partition("://")
    return "postgresql://" + rest if scheme.startswith("postgresql") else database_url


def _dump(database_url: str, part: Path) -> None:
    db_file = sqlite_path(database_url)
    if db_file is not None:
        if not db_file.is_file():
            raise BackupError(f"The database {db_file} does not exist")
        src = sqlite3.connect(str(db_file))
        dst = sqlite3.connect(str(part))
        try:
            # Online: the master keeps writing while it copies (SQLite's backup API restarts on changes).
            src.backup(dst)
            # The database of the master is in WAL mode, and so is the copy: back to one file, or part of it would
            # stay in a -wal file next to it.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
        check_sqlite(part)
        return
    pg_dump, pg_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise BackupError("pg_dump and pg_restore are needed to back up PostgreSQL (the PostgreSQL client programs)")
    url = _postgres_url(database_url)
    run = subprocess.run([pg_dump, "--format=custom", "--compress=6", f"--file={part}", f"--dbname={url}"],
                         capture_output=True, text=True, timeout=3600)
    if run.returncode != 0:
        raise BackupError(f"pg_dump failed: {run.stderr.strip()[-500:]}")
    check = subprocess.run([pg_restore, "--list", str(part)], capture_output=True, text=True, timeout=600)
    if check.returncode != 0:
        raise BackupError(f"The dump does not read back: {check.stderr.strip()[-500:]}")


def prune(folder: Path, keep: int) -> None:
    """Keeps the newest `keep` backups of the folder (the names sort by date) and removes what a cut run left."""
    if not folder.is_dir():
        return
    for part in folder.glob("*.part*"):
        part.unlink(missing_ok=True)
    files = sorted((f for f in folder.iterdir() if stamp_of(f) is not None), key=_order)
    for old in files[:max(0, len(files) - keep)]:
        old.unlink(missing_ok=True)
        log.info("Backup removed: %s", old)


def make(settings, dest: Path, now: datetime) -> Backup:  # noqa: ANN001
    """One backup now, checked, with the old ones pruned."""
    daily, weekly = dest / "daily", dest / "weekly"
    daily.mkdir(parents=True, exist_ok=True)
    weekly.mkdir(parents=True, exist_ok=True)
    final = daily / f"{PREFIX}{now:%Y%m%d-%H%M%S}{_ext(settings.database_url)}"
    # Two in one second (a backup by hand right after the daily one): the second waits a little.
    while final.exists():
        now += timedelta(seconds=1)
        final = daily / f"{PREFIX}{now:%Y%m%d-%H%M%S}{_ext(settings.database_url)}"
    part = final.with_name(final.name + ".part")

    def clear() -> None:
        for leftover in (part, Path(f"{part}-wal"), Path(f"{part}-shm"), Path(f"{part}-journal")):
            leftover.unlink(missing_ok=True)

    clear()
    try:
        _dump(settings.database_url, part)
    except BaseException:
        clear()
        raise
    os.replace(part, final)
    log.info("Backup written: %s (%d KB)", final, final.stat().st_size // 1024)
    if now.isoweekday() == 7:
        shutil.copy2(final, weekly / final.name)
    prune(daily, settings.backup_days)
    prune(weekly, settings.backup_weeks)
    return Backup(final, final.stat().st_size)


def due(settings, dest: Path, now: datetime) -> bool:  # noqa: ANN001
    """Whether today's backup is to be made: past backup_hour (UTC) and none yet today."""
    if now.hour < settings.backup_hour:
        return False
    last = latest(dest)
    when = stamp_of(last) if last else None
    return when is None or when.date() < now.date()


def restore_sqlite(backup: Path, database_url: str) -> Path:
    """Puts a SQLite backup in place of the database (the master must be stopped). The database it replaces is kept
    next to it as .before-restore. Returns the path of the database."""
    db_file = sqlite_path(database_url)
    if db_file is None:
        raise BackupError("Restore by hand with pg_restore for PostgreSQL (deploy/restore.sh)")
    check_sqlite(backup)
    if db_file.exists():
        # With what is still in its -wal file: SQLite's backup API reads the database as it is.
        before = db_file.with_name(db_file.name + ".before-restore")
        before.unlink(missing_ok=True)
        src, dst = sqlite3.connect(str(db_file)), sqlite3.connect(str(before))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
    for extra in ("-wal", "-shm", "-journal"):
        Path(str(db_file) + extra).unlink(missing_ok=True)
    tmp = db_file.with_name(db_file.name + ".restoring")
    shutil.copy2(backup, tmp)
    os.replace(tmp, db_file)
    return db_file
