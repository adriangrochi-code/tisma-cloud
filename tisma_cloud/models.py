"""Tables: plans, users (accounts of the web site, with their payment state), access tokens, monthly usage, nodes
(computing servers) and jobs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def month_of(when: datetime) -> str:
    return when.strftime("%Y-%m")


class Plan(Base):
    """A monthly plan: hours of computing per calendar month and the limits that protect the servers."""

    __tablename__ = "plans"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    monthly_hours: Mapped[float] = mapped_column(Float, default=1.)
    # Jobs of the user waiting or running at the same time.
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1)
    # Requests to the API per minute.
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    # Jobs of plans with a higher priority are taken first.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # Monthly price in cents of the currency, and whether users can choose it on the account page.
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    public: Mapped[bool] = mapped_column(Boolean, default=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    # Id of the account on the web site, which creates the users and records their payments.
    external_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # scrypt (see security.py); empty: no password login (token only).
    password_hash: Mapped[str] = mapped_column(String(300), default="")
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    # The subscription is paid until then (the web site moves it forward with each payment).
    paid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Administrators are not limited by plans or payments.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # The user proved the e-mail is theirs (a code sent to it, or Google for its own addresses). Accounts made by the
    # administrator or the web site are trusted; self-registered ones start unverified.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    # Google account linked for "Sign in with Google" (the "sub" of its ID tokens).
    google_sub: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    plan: Mapped[Plan] = relationship(lazy="joined")


class Payment(Base):
    """A payment of a plan: through a payment provider (the account page) or recorded by the administrator."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    months: Mapped[int] = mapped_column(Integer, default=1)
    # The amount expected (set by the master from the plan, never by the buyer) and its currency.
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # test, manual, or the name of the provider.
    provider: Mapped[str] = mapped_column(String(30))
    # Id of the payment at the provider (each one applied once).
    provider_ref: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # pending, approved, rejected
    status: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EmailCode(Base):
    """A one-time code sent by e-mail: to confirm the address (verify) or to choose a new password (reset)."""

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(10))
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ApiToken(Base):
    """A session of a device (Tisma on a PC, the app): created at login, stored as SHA-256."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Usage(Base):
    """Seconds of computing used by a user in a calendar month (UTC)."""

    __tablename__ = "usage"
    __table_args__ = (UniqueConstraint("user_id", "month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7))
    seconds: Mapped[float] = mapped_column(Float, default=0.)


class Node(Base):
    """A computing server. It takes the job classes of its priority list, in that order."""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Job classes in order of priority, comma separated (see jobs.JOB_CLASSES).
    priorities: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # What the node reports about itself (processor, graphics card, memory, slots), JSON.
    info: Mapped[str] = mapped_column(Text, default="{}")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def priority_list(self) -> list[str]:
        return [c for c in (p.strip() for p in self.priorities.split(",")) if c]

    @property
    def info_dict(self) -> dict:
        try:
            return json.loads(self.info or "{}")
        except ValueError:
            return {}


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # slice | fea
    kind: Mapped[str] = mapped_column(String(20))
    # The queue of the job: slice, slice_large, fea, fea_optimize (see jobs.JOB_CLASSES).
    job_class: Mapped[str] = mapped_column(String(20), index=True)
    params: Mapped[str] = mapped_column(Text, default="{}")
    # queued, running, done, failed, cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # Priority of the plan of the user when it was submitted (order of the queue).
    priority: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Computing time charged to the plan [s] (done, or stopped by the user while running).
    seconds: Mapped[float] = mapped_column(Float, default=0.)
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    input_name: Mapped[str] = mapped_column(String(200), default="")
    result_filename: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="{}")
    # The project and the result were deleted (retention_h after the job ended).
    files_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    node: Mapped[Node | None] = relationship()

    @property
    def params_dict(self) -> dict:
        return json.loads(self.params or "{}")

    @property
    def summary_dict(self) -> dict:
        try:
            return json.loads(self.summary or "{}")
        except ValueError:
            return {}
