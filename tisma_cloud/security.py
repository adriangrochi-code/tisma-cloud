"""Access tokens (random, shown once, stored as SHA-256) and passwords (scrypt, from the standard library)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# scrypt parameters (RFC 7914): 2^14 iterations, 8 blocks of 1 KiB, 16 MiB of memory per hash.
_N, _R, _P = 1 << 14, 8, 1


def new_token(prefix: str) -> str:
    """A new token, e.g. "tu_..." for users and "tn_..." for nodes."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A hash to compare against when the user does not exist, so that the time of a failed login does not tell whether
# the e-mail has an account.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
