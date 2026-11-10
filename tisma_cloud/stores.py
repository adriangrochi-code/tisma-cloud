"""The access an account has from the app stores (Google Play, the App Store): that of its subscription that lasts
longest in any store; its plan becomes the plan of the account (cloud.md 5b: the plan lives on the account and applies
on every device)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppleSubscription, Plan, PlaySubscription, User, utcnow


def update_access(session: Session, user: User) -> None:
    now = utcnow()
    best = None
    for table in (PlaySubscription, AppleSubscription):
        sub = session.execute(select(table).where(
            table.user_id == user.id, table.entitled_until.is_not(None), table.entitled_until > now)
            .order_by(table.entitled_until.desc())).scalars().first()
        if sub is not None and (best is None or sub.entitled_until > best.entitled_until):
            best = sub
    user.store_until = best.entitled_until if best else None
    if best is not None:
        user.plan = session.get(Plan, best.plan_name)
