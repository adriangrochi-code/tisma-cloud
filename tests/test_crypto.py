"""Personal data encrypted in the database (crypto.py): what a copy of the database shows, older data, the key."""

from __future__ import annotations

import base64
import os
import stat

import pytest
from conftest import new_app
from sqlalchemy import text

from tisma_cloud import crypto
from tisma_cloud.api import create_app
from tisma_cloud.models import Bridge, User
from tisma_cloud.security import hash_password

PERSONAL = [("users", "email"), ("api_tokens", "label"), ("bridges", "name"), ("bridges", "printers"),
            ("jobs", "input_name"), ("remote_commands", "file_name")]


def raw(db, table: str, column: str) -> list[str]:
    with db.engine.connect() as conn:
        return [v for v in conn.execute(text(f"SELECT {column} FROM {table}")).scalars() if v]


def test_a_copy_of_the_database_does_not_show_personal_data(client, f):
    f.user("ana@correo.com")
    with f.db.session() as s:
        s.query(User).one().password_hash = hash_password("una clave larga")
    r = client.post("/v1/auth/login", json={"email": "Ana@Correo.com", "password": "una clave larga",
                                            "device": "Teléfono de Ana"})
    ana = {"Authorization": f"Bearer {r.json()['token']}"}
    f.submit(ana)
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "Taller de Ana", "printers": [
        {"id": "p", "name": "MK4 de Ana", "actions": ["print"]}]})
    client.get("/v1/bridges/pc-1/commands?wait=0", headers=ana)
    assert client.post("/v1/remote/bridges/pc-1/printers/p/print", headers=ana,
                       files={"file": ("llavero de Ana.gcode", b"G28\n")}).status_code == 201
    services = client.app.state.services
    # The state of the bridge written by the flush of the presence (a Core UPDATE, not the ORM).
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "Taller de Ana", "printers": [
        {"id": "p", "name": "MK4 de Ana", "actions": ["print"], "state": "printing"}]})
    services.flush_presence()
    db = services.db
    for table, column in PERSONAL:
        values = raw(db, table, column)
        assert values and all(v.startswith(crypto.PREFIX) for v in values), (table, column, values)
        assert not any("Ana" in v or "ana@" in v for v in values)
    if db.is_sqlite:
        # Nowhere in the file either (a stolen disk, a backup).
        path = db.engine.url.database
        with db.engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(FULL)"))
        data = open(path, "rb").read()
        assert b"Ana" not in data and b"ana@" not in data.lower()
    # The service reads them as before.
    me = client.get("/v1/me", headers=ana).json()
    assert me["email"] == "ana@correo.com"
    assert client.get("/v1/remote/printers", headers=ana).json()["bridges"][0]["name"] == "Taller de Ana"
    # And the e-mail is found (login, register of a known address) by its index.
    assert client.post("/v1/auth/login", json={"email": "ANA@correo.com ", "password": "una clave larga"}).status_code \
        == 200
    with db.session() as s:
        assert s.query(User).filter_by(email_index=crypto.index("ana@correo.com")).one().email == "ana@correo.com"


def test_data_written_in_clear_by_an_older_version_is_encrypted(client, f, settings):
    ana = f.user("ana@x.com")
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "Taller", "printers": [{"id": "p", "name": "MK4"}]})
    services = client.app.state.services
    db = services.db
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE users SET email = 'ana@x.com', email_index = NULL"))
        conn.execute(text("UPDATE bridges SET name = 'Taller', printers = '[{\"id\": \"p\", \"name\": \"MK4\"}]'"))
    # Still read as it is...
    with db.session() as s:
        assert s.get(Bridge, "pc-1").name == "Taller"
    # ...and encrypted when the master starts.
    assert crypto.migrate(db) == 3
    assert all(v.startswith(crypto.PREFIX) for t, c in [("users", "email"), ("bridges", "name"),
                                                        ("bridges", "printers")] for v in raw(db, t, c))
    with db.session() as s:
        user = s.query(User).filter_by(email_index=crypto.index("ana@x.com")).one()
        assert user.email == "ana@x.com"
    assert crypto.migrate(db) == 0
    assert client.get("/v1/me", headers=ana).json()["email"] == "ana@x.com"


def test_a_changed_value_is_not_read(client, f):
    f.user("ana@x.com")
    db = client.app.state.services.db
    stored = raw(db, "users", "email")[0]
    body = bytearray(base64.b64decode(stored[len(crypto.PREFIX):]))
    body[-1] ^= 1
    with pytest.raises(ValueError):
        crypto.decrypt(crypto.PREFIX + base64.b64encode(bytes(body)).decode())
    # Two encryptions of the same value differ (a random nonce each time): equal values are not seen as equal.
    assert crypto.encrypt("ana@x.com") != crypto.encrypt("ana@x.com")


def test_the_key(settings, tmp_path, monkeypatch):
    # Made on the first start, readable only by the service.
    app = new_app(settings)
    key_file = settings.storage_dir / "data.key"
    assert key_file.exists()
    if os.name != "nt":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    with app.state.services.db.session() as s:
        s.add(User(email="ana@x.com", plan_name="trial"))
    # The same key on the next start.
    create_app(settings)
    # Another key (another storage folder, or a wrong TISMA_DATA_KEY) is refused before anything is written with it.
    monkeypatch.setenv("TISMA_DATA_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(crypto.WrongKey):
        create_app(settings)
    # The key of the file given as TISMA_DATA_KEY (a secret of the container instead of a file) works.
    monkeypatch.setenv("TISMA_DATA_KEY", key_file.read_text().strip())
    create_app(settings)
    monkeypatch.setenv("TISMA_DATA_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        create_app(settings)


def test_a_name_that_looks_encrypted_is_encrypted_too(client, f):
    ana = f.user("ana@x.com")
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "enc1:AAAA", "printers": []})
    db = client.app.state.services.db
    assert raw(db, "bridges", "name")[0] != "enc1:AAAA"
    with db.session() as s:
        assert s.get(Bridge, "pc-1").name == "enc1:AAAA"
