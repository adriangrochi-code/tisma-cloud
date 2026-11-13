from __future__ import annotations

from sqlalchemy import create_engine, text

from tisma_cloud.db import Database


def test_new_columns_are_added_to_an_existing_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    # A database of the first version: plans without price, users and jobs.
    old = create_engine(url)
    with old.begin() as c:
        c.execute(text("CREATE TABLE plans (name VARCHAR(50) PRIMARY KEY, monthly_hours FLOAT NOT NULL, "
                       "max_concurrent INTEGER NOT NULL, requests_per_minute INTEGER NOT NULL, priority INTEGER NOT NULL)"))
        c.execute(text("INSERT INTO plans VALUES ('basico', 10, 2, 60, 1)"))
    db = Database(url)
    db.create_all()
    with db.engine.connect() as c:
        row = c.execute(text("SELECT name, price_cents, currency, public FROM plans")).one()
    assert tuple(row) == ("basico", 0, "USD", 0)
    db.create_all()   # again: nothing to add


def test_new_indexes_are_added_to_an_existing_database(tmp_path):
    """An index added to a model in a new version reaches the tables of an older database (the thumbnails of the
    library are looked for by their SHA-256 at each request)."""
    from sqlalchemy import inspect

    url = f"sqlite:///{tmp_path / 'old.db'}"
    db = Database(url)
    db.create_all()
    with db.engine.begin() as c:
        c.execute(text("DROP INDEX ix_library_models_thumbnail_sha256"))
    assert "ix_library_models_thumbnail_sha256" not in {i["name"] for i in inspect(db.engine).get_indexes("library_models")}
    db.engine.dispose()
    db = Database(url)
    db.create_all()
    assert "ix_library_models_thumbnail_sha256" in {i["name"] for i in inspect(db.engine).get_indexes("library_models")}
    db.create_all()   # again: nothing to add
