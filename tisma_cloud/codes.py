"""One-time codes sent by e-mail: 6 digits, stored as a hash, valid 15 minutes and for 5 attempts, one at a time per
purpose (a new code replaces the previous one)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import EmailCode, User, utcnow

VALIDITY = timedelta(minutes=15)
MAX_ATTEMPTS = 5


def _hash(user_id: int, purpose: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{purpose}:{code}".encode()).hexdigest()


def issue(session: Session, user: User, purpose: str) -> str:
    session.execute(delete(EmailCode).where(EmailCode.user_id == user.id, EmailCode.purpose == purpose))
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(EmailCode(user_id=user.id, purpose=purpose, code_hash=_hash(user.id, purpose, code),
                          expires_at=utcnow() + VALIDITY))
    session.flush()
    return code


def check(session: Session, user: User, purpose: str, code: str) -> bool:
    """True once for the right code (it is then used up). Wrong attempts count; after MAX_ATTEMPTS the code dies."""
    row = session.execute(select(EmailCode).where(EmailCode.user_id == user.id, EmailCode.purpose == purpose)
                          ).scalar_one_or_none()
    if row is None or row.expires_at < utcnow() or row.attempts >= MAX_ATTEMPTS:
        return False
    if not hmac.compare_digest(row.code_hash, _hash(user.id, purpose, code.strip())):
        row.attempts += 1
        return False
    session.delete(row)
    return True
