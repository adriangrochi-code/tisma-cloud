"""Database: SQLAlchemy engine and sessions. PostgreSQL in production, SQLite for tests and trials."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            # The API answers from several threads.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (registers the tables)

        Base.metadata.create_all(self.engine)
        self._add_new_columns()
        # A first plan, so that users can be created before the web site sets the real ones.
        with self.session() as s:
            if s.query(models.Plan).count() == 0:
                s.add(models.Plan(name="trial", monthly_hours=1., max_concurrent=1, requests_per_minute=60, priority=0))

    def _add_new_columns(self) -> None:
        """Additive migration: the columns added to the models in a new version are added to the tables of an existing
        database, with their default value (create_all only creates missing tables). Columns are never removed or
        changed here."""
        existing = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if not existing.has_table(table.name):
                    continue
                present = {c["name"] for c in existing.get_columns(table.name)}
                for column in table.columns:
                    if column.name in present:
                        continue
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(self.engine.dialect)}'
                    default = column.default.arg if column.default is not None and column.default.is_scalar else None
                    if default is not None:
                        literal = ("TRUE" if default else "FALSE") if isinstance(default, bool) and not self.is_sqlite else \
                            str(int(default)) if isinstance(default, bool) else \
                            repr(default) if isinstance(default, (int, float)) else "'" + str(default).replace("'", "''") + "'"
                        ddl += f" DEFAULT {literal} NOT NULL"
                    conn.execute(text(ddl))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
