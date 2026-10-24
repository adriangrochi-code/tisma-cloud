"""Personal data encrypted in the database (AES-256-GCM), with the key outside of it: a copy of the database alone (a
backup, a dump, a stolen disk of the database) does not show the e-mails of the users, the names of their printers
and files, or their store tokens.

The key: TISMA_DATA_KEY (32 bytes in base64), or the file data.key in the storage folder, made on the first start.
It must be kept (a copy apart from the backups of the database): without it the encrypted data cannot be read and
nobody can log in with an e-mail. The e-mail is found by its index: an HMAC of it with a key derived from the same
key, so that equal e-mails can be looked up without storing them in clear.

A value written before the encryption (in clear) is still read, and encrypted when the master starts (migrate).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text, inspect, text
from sqlalchemy.types import TypeDecorator

log = logging.getLogger("tisma_cloud")

PREFIX = "enc1:"
_cipher: AESGCM | None = None
_index_key: bytes | None = None


class KeyMissing(RuntimeError):
    pass


class WrongKey(RuntimeError):
    pass


def configure(settings, home: Path | None = None) -> None:  # noqa: ANN001
    """Loads the key of the master (TISMA_DATA_KEY, or storage_dir/data.key, made when missing). A relative storage_dir
    is taken from home when given (the folder the service runs in)."""
    global _cipher, _index_key
    raw = os.environ.get("TISMA_DATA_KEY", "").strip()
    if raw:
        key = base64.b64decode(raw)
    else:
        path = Path(settings.storage_dir) / "data.key"
        if home is not None and not path.is_absolute():
            path = home / path
        if path.exists():
            key = base64.b64decode(path.read_text().strip())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(base64.b64encode(key).decode() + "\n")
            log.warning("New key of the personal data made in %s: keep a copy of it apart from the backups of the "
                        "database (without it the data cannot be read)", path)
    if len(key) != 32:
        raise ValueError("The key of the personal data must be 32 bytes (base64)")
    _cipher = AESGCM(key)
    _index_key = hmac.new(key, b"tisma-cloud email index v1", hashlib.sha256).digest()


def _require() -> AESGCM:
    if _cipher is None:
        raise KeyMissing("The key of the personal data is not loaded (crypto.configure)")
    return _cipher


def encrypt(value: str) -> str:
    nonce = secrets.token_bytes(12)
    return PREFIX + base64.b64encode(nonce + _require().encrypt(nonce, value.encode("utf-8"), None)).decode("ascii")


def decrypt(value: str) -> str:
    """The value in clear (one written before the encryption is returned as it is). Raises ValueError when the
    encrypted value was changed or the key is another one."""
    if not value.startswith(PREFIX):
        return value
    raw = base64.b64decode(value[len(PREFIX):])
    try:
        return _require().decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except Exception as ex:  # noqa: BLE001 (InvalidTag: changed data or another key)
        raise ValueError("Encrypted data that cannot be read with this key") from ex


def index(email: str) -> str:
    """The index of an e-mail (normalized): HMAC-SHA256 with the index key."""
    if _index_key is None:
        raise KeyMissing("The key of the personal data is not loaded (crypto.configure)")
    return hmac.new(_index_key, email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


class Encrypted(TypeDecorator):
    """A text column stored encrypted (TEXT: an encrypted value is longer than the text)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        # Always encrypted, also a text that starts like an encrypted one (a name "enc1:..." would not be read back).
        if value is None or value == "":
            return value
        return encrypt(value)

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None or value == "":
            return value
        return decrypt(value)


def migrate(db) -> int:  # noqa: ANN001
    """Widens the columns of personal data made shorter by older versions (PostgreSQL), encrypts the values written
    in clear and fills the e-mail index. Returns how many values were encrypted."""
    from . import models

    with db.session() as s:
        sample = s.execute(text(f"SELECT email FROM users WHERE email LIKE '{PREFIX}%' LIMIT 1")).scalar()
    if sample is not None:
        try:
            decrypt(sample)
        except ValueError:
            # Another key (another storage folder, or TISMA_DATA_KEY not set): stop before writing with it.
            raise WrongKey("The key of the personal data is not the one this database was encrypted with: use the "
                           "data.key of the service (TISMA_STORAGE_DIR) or its TISMA_DATA_KEY") from None
    fields = [(models.User, "email"), (models.ApiToken, "label"), (models.Bridge, "name"), (models.Bridge, "printers"),
              (models.Job, "input_name"), (models.Job, "result_filename"), (models.Payment, "note"),
              (models.PlaySubscription, "purchase_token"), (models.PlaySubscription, "order_id"),
              (models.RemoteCommand, "file_name")]
    if not db.is_sqlite:
        existing = inspect(db.engine)
        with db.engine.begin() as conn:
            for model, name in fields:
                table = model.__tablename__
                column = next((c for c in existing.get_columns(table) if c["name"] == name), None)
                if column is not None and getattr(column["type"], "length", None):
                    conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE TEXT"))
    count = 0
    with db.session() as s:
        for model, name in fields:
            table, key = model.__tablename__, inspect(model).primary_key[0].name
            rows = s.execute(text(f"SELECT {key}, {name} FROM {table} WHERE {name} IS NOT NULL AND {name} != '' "
                                  f"AND {name} NOT LIKE '{PREFIX}%'")).all()
            if rows:
                # One statement for all the rows (executemany).
                s.execute(text(f"UPDATE {table} SET {name} = :value WHERE {key} = :id"),
                          [{"value": encrypt(value), "id": row_id} for row_id, value in rows])
            count += len(rows)
        rows = s.execute(text("SELECT id, email FROM users WHERE email_index IS NULL")).all()
        if rows:
            s.execute(text("UPDATE users SET email_index = :i WHERE id = :id"),
                      [{"i": index(decrypt(email)), "id": row_id} for row_id, email in rows])
    # The column added to an older database (Database._add_new_columns) gets the index create_all gives a new one.
    with db.engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_index ON users (email_index)"))
    if count:
        log.warning("%d values of personal data written in clear were encrypted", count)
    return count
