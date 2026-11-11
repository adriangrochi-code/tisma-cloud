"""The access an account has from the app stores (Google Play, the App Store, the Microsoft Store): that of its subscription that lasts
longest in any store; its plan becomes the plan of the account (cloud.md 5b: the plan lives on the account and applies
on every device)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppleSubscription, MicrosoftSubscription, Plan, PlaySubscription, User, utcnow


class OtherAccount(Exception):
    """The purchase belongs to another account of Tisma Cloud (a purchase is worth one account only)."""


STORES = {PlaySubscription: "google_play", AppleSubscription: "app_store", MicrosoftSubscription: "microsoft_store"}


def best_subscription(session: Session, user: User, now: datetime | None = None):  # noqa: ANN201
    """The subscription of the account that gives the longest access now, of any store (or None)."""
    now = now or utcnow()
    best = None
    for table in STORES:
        sub = session.execute(select(table).where(
            table.user_id == user.id, table.entitled_until.is_not(None), table.entitled_until > now)
            .order_by(table.entitled_until.desc())).scalars().first()
        if sub is not None and (best is None or sub.entitled_until > best.entitled_until):
            best = sub
    return best


def subscription_view(session: Session, user: User) -> dict | None:
    """«subscription» of GET /v1/me (cloud.md 5c): the store, its product, when it ends and whether it renews."""
    sub = best_subscription(session, user)
    if sub is None:
        return None
    return {"store": STORES[type(sub)], "product_id": sub.product_id,
            "expires": sub.entitled_until.isoformat() + "Z", "auto_renew": sub.auto_renew}


def free_plan(session: Session) -> Plan | None:
    """The plan an account goes back to when its store subscription ends: the cheapest public free plan, or trial."""
    return session.execute(select(Plan).where(Plan.public.is_(True), Plan.price_cents == 0)
                           .order_by(Plan.monthly_hours.desc())).scalars().first() or session.get(Plan, "trial")


def update_access(session: Session, user: User) -> None:
    now = utcnow()
    best = best_subscription(session, user, now)
    had_store = user.store_until is not None
    user.store_until = best.entitled_until if best else None
    if best is not None:
        user.plan = session.get(Plan, best.plan_name)
    elif had_store and not (user.paid_until and user.paid_until > now):
        # The store's plan ended (expired, cancelled, refunded) and nothing else pays it: the free plan again
        # (cloud.md 5c, CC23).
        plan = free_plan(session)
        if plan is not None:
            user.plan = plan
