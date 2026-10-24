"""Access tokens (random, shown once, stored as SHA-256) and passwords (scrypt, from the standard library)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading

# scrypt parameters (RFC 7914): N=2^17, r=8 (blocks of 1 KiB), p=1, 128 MiB of memory per hash: the first setting of
# the OWASP Password Storage Cheat Sheet. About half a second of a core (Xeon 2.1 GHz). Hashes made with other
# parameters (2^14 before) still verify and are made again at the next login (needs_rehash).
_N, _R, _P = 1 << 17, 8, 1
# Hashes computed at the same time: each takes 128 MiB, so that a flood of logins cannot take the memory of the
# server. A login that waits longer than _WAIT_S is refused (Busy).
_SLOTS = threading.BoundedSemaphore(2)
_WAIT_S = 10.


class Busy(Exception):
    """Too many passwords being checked right now."""


def new_token(prefix: str) -> str:
    """A new token, e.g. "tu_..." for users and "tn_..." for nodes."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    if not _SLOTS.acquire(timeout=_WAIT_S):
        raise Busy("Too many logins right now: try again in a moment")
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                              maxmem=128 * r * n + (1 << 20))
    finally:
        _SLOTS.release()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, _N, _R, _P, 32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt" or not (1 < int(n) <= _N and 0 < int(r) <= _R and 0 < int(p) <= 16):
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """The hash was made with other parameters than the current ones."""
    return not stored.startswith(f"scrypt${_N}${_R}${_P}$")


# A hash to compare against when the user does not exist, so that the time of a failed login does not tell whether
# the e-mail has an account.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
