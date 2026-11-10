"""The library shared between users (p2p_library.md of tisma_engine): the master keeps and signs only an index of the
published models (what they are and the SHA-256 of each of their files) and the list of what is blocked; the files go
from app to app through a peer network and never through the master.

Both lists are JSON documents served byte for byte with a detached Ed25519 signature of their exact bytes (64 bytes, as
updates.json of app_updates.md, rule 13). They are signed by the library's own key, storage_dir/library.key, made on
the first start: the key of the project lives only on the owner's USB (app_updates.md, rule 14) and cannot sign lists
that change with every publication. The owner signs the public library key once with the project key (the bytes
CERT_PREFIX + the 32 bytes of the key) and installs that certificate (`tisma-cloud admin library-cert`); the apps
carry only the project key and accept a list signed by a certified library key.

The index and the blocklist are built again when something changes (their sequence number grows) and the blocklist
also every RESIGN_S, so that its "expires" moves forward (the apps stop sharing with a list older than 7 days)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import LibraryBlock, LibraryCounter, LibraryModel, LibraryPeerKey, User, utcnow

INDEX_FORMAT = "tisma-library-index"
BLOCKLIST_FORMAT = "tisma-library-blocklist"
FORMAT_VERSION = 1
CERT_PREFIX = b"tisma-library-key/1\n"
# The blocklist is signed again this often even without changes, and it is valid this long (rule 6).
RESIGN_S = 6 * 3600
BLOCKLIST_VALID = timedelta(days=7)
# Licences a model may be published under (SPDX identifiers).
LICENSES = ("CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-ND-4.0",
            "CC-BY-NC-ND-4.0", "MIT", "GPL-3.0-or-later", "LicenseRef-All-Rights-Reserved")
ROLES = ("project", "gcode", "analysis", "photo", "profile", "other")
BLOCK_KINDS = ("file", "account", "peer", "address")
MAX_FILES = 50
MAX_FILE_BYTES = 2 << 30
MAX_THUMBNAIL_BYTES = 64 << 10
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PATH = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]{1,120}$")


class LibraryError(ValueError):
    """What was sent cannot be published (the message says why, for the person)."""


# ---- the keys
def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: str, size: int | None = None) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise LibraryError("not base64") from None
    if size is not None and len(data) != size:
        raise LibraryError(f"{size} bytes expected, not {len(data)}")
    return data


def public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def verify(public_key: bytes, signature: bytes, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


class LibraryKey:
    """The library's Ed25519 key (storage_dir/library.key, the 32 bytes of the private key in base64) and its
    certificate from the project key (storage_dir/library.cert, 64 bytes in base64), when installed."""

    def __init__(self, folder: Path) -> None:
        self.path = Path(folder) / "library.key"
        self.cert_path = Path(folder) / "library.cert"
        if self.path.exists():
            raw = unb64(self.path.read_text().strip(), 32)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = os.urandom(32)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(b64(raw) + "\n")
        self._private = Ed25519PrivateKey.from_private_bytes(raw)
        self.public = public_bytes(self._private.public_key())

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)

    def certificate(self) -> bytes | None:
        if not self.cert_path.exists():
            return None
        try:
            return unb64(self.cert_path.read_text().strip(), 64)
        except LibraryError:
            return None

    def install_certificate(self, certificate_b64: str, project_key_b64: str) -> None:
        """Keeps the owner's signature of this key after checking it with the project key."""
        if not project_key_b64:
            raise LibraryError("Falta TISMA_LIBRARY_PROJECT_KEY, la clave pública del proyecto, para comprobarlo")
        cert = unb64(certificate_b64.strip(), 64)
        if not verify(unb64(project_key_b64, 32), cert, CERT_PREFIX + self.public):
            raise LibraryError("El certificado no es una firma de esta clave de la biblioteca con la clave del proyecto")
        self.cert_path.write_text(b64(cert) + "\n")


# ---- what the index says of the people and their files
def account_id(user_id: int) -> str:
    """The public id of an account in the lists: an HMAC of its number with the key of the personal data (never its
    e-mail), stable while the key is the same."""
    from . import crypto

    return "ua_" + crypto.index(f"library-account:{user_id}")[:24]


def canonical(value: object) -> bytes:
    """The bytes a peer signs: JSON with sorted keys, no spaces, UTF-8."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def manifest(files: list[dict]) -> bytes:
    """What the peer key of the author signs: its files, by path, with their sizes and SHA-256."""
    return canonical({"files": sorted(files, key=lambda f: f["path"])})


@dataclass
class Publication:
    name: str
    description: str
    tags: list[str]
    license: str
    author_name: str
    files: list[dict]
    analysis: dict | None
    swarm: dict | None
    peer_key: str
    manifest_signature: str


def _text(body: dict, key: str, most: int, needed: bool = True) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise LibraryError(f"'{key}' is text")
    value = value.strip()
    if needed and not value:
        raise LibraryError(f"'{key}' is needed")
    if len(value) > most:
        raise LibraryError(f"'{key}' is at most {most} characters")
    return value


def parse_publication(body: dict) -> Publication:
    """The model's data as the app sends it to publish (no files: their paths, sizes and SHA-256)."""
    if not isinstance(body, dict):
        raise LibraryError("A JSON object is expected")
    license_ = _text(body, "license", 40)
    if license_ not in LICENSES:
        raise LibraryError(f"'license' is one of {', '.join(LICENSES)}")
    tags = body.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 10 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 30
                                                                for t in tags):
        raise LibraryError("'tags': at most 10 texts of up to 30 characters")
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise LibraryError(f"'files': from 1 to {MAX_FILES} files")
    clean: list[dict] = []
    for f in files:
        if not isinstance(f, dict):
            raise LibraryError("Each file is an object")
        path, role, size, sha = f.get("path"), f.get("role"), f.get("size"), f.get("sha256")
        if not isinstance(path, str) or not _PATH.match(path) or path.strip(" .") != path:
            raise LibraryError(f"Bad file name: {path!r}")
        if role not in ROLES:
            raise LibraryError(f"'role' is one of {', '.join(ROLES)}")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE_BYTES:
            raise LibraryError(f"{path}: 'size' from 1 byte to {MAX_FILE_BYTES >> 30} GB")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise LibraryError(f"{path}: 'sha256' is 64 hexadecimal digits in lower case")
        item = {"path": path, "role": role, "size": size, "sha256": sha}
        if isinstance(f.get("printer"), str) and f["printer"].strip():
            item["printer"] = f["printer"].strip()[:80]
        clean.append(item)
    if len({f["path"] for f in clean}) != len(clean):
        raise LibraryError("Two files with the same name")
    if not any(f["role"] == "project" for f in clean):
        raise LibraryError("A model has its project (a file with role 'project')")
    analysis = body.get("analysis")
    swarm = body.get("swarm")
    for key, value in (("analysis", analysis), ("swarm", swarm)):
        if value is not None and (not isinstance(value, dict) or len(canonical(value)) > 4096):
            raise LibraryError(f"'{key}' is an object of at most 4 KB")
    peer_key = _text(body, "peer_key", 64)
    signature = _text(body, "manifest_signature", 100)
    if not verify(unb64(peer_key, 32), unb64(signature, 64), manifest(clean)):
        raise LibraryError("'manifest_signature' is not the signature of the files with 'peer_key'")
    return Publication(name=_text(body, "name", 120), description=_text(body, "description", 500, needed=False),
                       tags=[t.strip() for t in tags], license=license_, author_name=_text(body, "author_name", 60),
                       files=sorted(clean, key=lambda f: f["path"]), analysis=analysis, swarm=swarm, peer_key=peer_key,
                       manifest_signature=signature)


def thumbnail_ok(data: bytes) -> bool:
    """A WebP image (RIFF....WEBP) of at most MAX_THUMBNAIL_BYTES."""
    return 12 <= len(data) <= MAX_THUMBNAIL_BYTES and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


# ---- the lists, with their sequence numbers
def bump(session: Session, name: str) -> int:
    """Moves a list's sequence forward (something in it changed)."""
    row = session.get(LibraryCounter, name, with_for_update=True)
    if row is None:
        row = LibraryCounter(name=name, value=0)
        session.add(row)
    row.value += 1
    session.flush()
    return row.value


def sequence(session: Session, name: str) -> int:
    row = session.get(LibraryCounter, name)
    return row.value if row else 0


def blocked(session: Session, kind: str, value: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return session.execute(select(func.count()).select_from(LibraryBlock).where(
        LibraryBlock.kind == kind, LibraryBlock.value == value,
        (LibraryBlock.until.is_(None)) | (LibraryBlock.until > now))).scalar_one() > 0


def add_block(session: Session, kind: str, value: str, reason: str = "", hours: float | None = None) -> LibraryBlock:
    """Blocks a file (its SHA-256), an account (its public id), a peer key or, for `hours` only, an address."""
    if kind not in BLOCK_KINDS:
        raise LibraryError(f"kind is one of {', '.join(BLOCK_KINDS)}")
    value = value.strip()
    if kind == "file" and not _SHA256.match(value):
        raise LibraryError("A file is blocked by its SHA-256")
    if kind == "address":
        import ipaddress

        try:
            value = str(ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise LibraryError(f"Not an address: {value}") from None
        hours = hours or 24.
    block = LibraryBlock(kind=kind, value=value[:100], reason=reason[:300],
                         until=utcnow() + timedelta(hours=hours) if hours else None)
    session.add(block)
    if kind != "address":
        # The models of a blocked account, peer or file leave the index too.
        bump(session, "index")
    bump(session, "blocklist")
    return block


def remove_block(session: Session, block_id: int) -> None:
    block = session.get(LibraryBlock, block_id)
    if block is None:
        raise LibraryError("No such block")
    if block.kind != "address":
        bump(session, "index")
    session.delete(block)
    bump(session, "blocklist")


def purge_addresses(session: Session, now: datetime | None = None) -> int:
    """Deletes the blocks that ended (an address is a personal datum: kept no longer than its block, rule 5)."""
    gone = session.execute(delete(LibraryBlock).where(LibraryBlock.until.is_not(None),
                                                      LibraryBlock.until <= (now or utcnow())))
    if gone.rowcount:
        bump(session, "blocklist")
    return gone.rowcount or 0


def _stamp(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat() + "Z"


def listed_models(session: Session) -> list[LibraryModel]:
    """The newest revision of each model not removed, of accounts not suspended nor disabled."""
    newest = (select(LibraryModel.model_id, func.max(LibraryModel.revision).label("revision"))
              .group_by(LibraryModel.model_id).subquery())
    rows = session.execute(
        select(LibraryModel).join(newest, (LibraryModel.model_id == newest.c.model_id)
                                  & (LibraryModel.revision == newest.c.revision))
        .join(User, User.id == LibraryModel.user_id)
        .where(LibraryModel.removed_at.is_(None), User.enabled.is_(True))
        .order_by(LibraryModel.published_at.desc())).scalars().all()
    suspended = set(session.execute(select(LibraryBlock.value).where(LibraryBlock.kind == "account")).scalars())
    files = set(session.execute(select(LibraryBlock.value).where(LibraryBlock.kind == "file")).scalars())
    peers = set(session.execute(select(LibraryBlock.value).where(LibraryBlock.kind == "peer")).scalars())
    return [m for m in rows if account_id(m.user_id) not in suspended and m.peer_key not in peers
            and not any(f["sha256"] in files for f in json.loads(m.files))]


def model_entry(m: LibraryModel) -> dict:
    return {"id": m.model_id, "revision": m.revision, "published": _stamp(m.published_at), "name": m.name,
            "description": m.description, "tags": json.loads(m.tags), "license": m.license,
            "author": {"account": account_id(m.user_id), "name": m.author_name},
            "thumbnail": {"sha256": m.thumbnail_sha256, "size": m.thumbnail_size, "type": "image/webp"},
            "size": m.size, "files": json.loads(m.files), "analysis": json.loads(m.analysis),
            "peer_key": m.peer_key, "manifest_signature": m.manifest_signature, "swarm": json.loads(m.swarm)}


def build_index(session: Session, now: datetime | None = None) -> bytes:
    now = now or utcnow()
    doc = {"format": INDEX_FORMAT, "version": FORMAT_VERSION, "generated": _stamp(now),
           "sequence": sequence(session, "index"), "models": [model_entry(m) for m in listed_models(session)]}
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_blocklist(session: Session, now: datetime | None = None) -> bytes:
    now = now or utcnow()
    purge_addresses(session, now)
    rows = session.execute(select(LibraryBlock).order_by(LibraryBlock.id)).scalars().all()
    doc = {"format": BLOCKLIST_FORMAT, "version": FORMAT_VERSION, "generated": _stamp(now),
           "sequence": sequence(session, "blocklist"), "expires": _stamp(now + BLOCKLIST_VALID),
           "files": [{"sha256": b.value, "reason": b.reason} for b in rows if b.kind == "file"],
           "accounts": [b.value for b in rows if b.kind == "account"],
           "peers": [b.value for b in rows if b.kind == "peer"],
           "addresses": [{"ip": b.value, "until": _stamp(b.until)} for b in rows if b.kind == "address" and b.until],
           "revoked_keys": []}
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Signed:
    """The two signed lists, kept in memory and built again when their sequence changes (or, the blocklist, every
    RESIGN_S): (bytes, signature, etag)."""

    def __init__(self, key: LibraryKey) -> None:
        self.key = key
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, float, bytes, bytes, str]] = {}

    def get(self, session: Session, name: str) -> tuple[bytes, bytes, str]:
        if name == "blocklist":
            # An address whose block ended leaves the list at once (its sequence moves).
            purge_addresses(session)
        seq = sequence(session, name)
        with self._lock:
            cached = self._cache.get(name)
            fresh = cached and cached[0] == seq and (name == "index" or time.monotonic() - cached[1] < RESIGN_S)
            if not fresh:
                data = build_index(session) if name == "index" else build_blocklist(session)
                sig = self.key.sign(data)
                etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
                cached = (seq, time.monotonic(), data, sig, etag)
                self._cache[name] = cached
            return cached[2], cached[3], cached[4]


def bind_peer_key(session: Session, user: User, public_key_b64: str) -> None:
    """Ties an app's key to the account (rule 5): a key belongs to one account only."""
    unb64(public_key_b64, 32)
    row = session.execute(select(LibraryPeerKey).where(LibraryPeerKey.public_key == public_key_b64)).scalar_one_or_none()
    if row is not None and row.user_id != user.id:
        raise LibraryError("This key is of another account")
    if blocked(session, "peer", public_key_b64):
        raise LibraryError("This app is blocked in the library")
    if row is None:
        session.add(LibraryPeerKey(user_id=user.id, public_key=public_key_b64))


def owns_key(session: Session, user: User, public_key_b64: str) -> bool:
    return session.execute(select(LibraryPeerKey).where(LibraryPeerKey.public_key == public_key_b64,
                                                        LibraryPeerKey.user_id == user.id)).scalar_one_or_none() is not None
