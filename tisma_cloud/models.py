"""Tables: plans, users (accounts of the web site, with their payment state), access tokens, monthly usage, nodes
(computing servers) and the machines that ask to be one, jobs, the bridges that relay the printers of a home to the
account with their commands, and the problems reported from Tisma and the app."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .crypto import Encrypted, index
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
    # Remote printers (remote.py): how many printers of the account the plan lets use from afar, and the monthly price
    # in cents of each one more (0: the plan has no more).
    remote_printers: Mapped[int] = mapped_column(Integer, default=0)
    extra_printer_cents: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Encrypted (crypto.py); found by email_index, an HMAC of it set with it.
    email: Mapped[str] = mapped_column(Encrypted)
    email_index: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
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
    # Internal accounts (the team, TISMA_INTERNAL_EMAILS): not limited by plans or payments and never charged, without
    # the rights of an administrator.
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    # The user proved the e-mail is theirs (a code sent to it, or Google for its own addresses). Accounts made by the
    # administrator or the web site are trusted; self-registered ones start unverified.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    # Google account linked for "Sign in with Google" (the "sub" of its ID tokens).
    google_sub: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # Subscription bought in an app store (Google Play): access until then, apart from the payments of paid_until (a
    # refund in the store takes back only this).
    store_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Random id of the account given to Google Play with each purchase (obfuscatedAccountId), to know whose it is.
    play_account: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # Remote printers paid for on top of those of the plan (set by the last payment).
    extra_printers: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    plan: Mapped[Plan] = relationship(lazy="joined")

    @validates("email")
    def _index_email(self, _key: str, value: str) -> str:
        self.email_index = index(value)
        return value

    @property
    def is_unlimited(self) -> bool:
        """Not limited by plans or payments: administrators and internal accounts with their e-mail confirmed."""
        return self.is_admin or (self.unlimited and self.email_verified)

    @property
    def access_until(self) -> datetime | None:
        """End of the paid access: the later of the payments and the store subscription."""
        dates = [d for d in (self.paid_until, self.store_until) if d is not None]
        return max(dates) if dates else None


class Payment(Base):
    """A payment of a plan: through a payment provider (the account page) or recorded by the administrator."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    months: Mapped[int] = mapped_column(Integer, default=1)
    # Remote printers paid for on top of those of the plan.
    extra_printers: Mapped[int] = mapped_column(Integer, default=0)
    # The amount expected (set by the master from the plan, never by the buyer) and its currency.
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # test, manual, or the name of the provider.
    provider: Mapped[str] = mapped_column(String(30))
    # Id of the payment at the provider (each one applied once).
    provider_ref: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # pending, approved, rejected
    status: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str] = mapped_column(Encrypted, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlaySubscription(Base):
    """A Google Play subscription (a purchase token) and its state as Google last reported it."""

    __tablename__ = "play_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # SHA-256 of the purchase token (the index; tokens are long) and the token itself (needed to ask Google).
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purchase_token: Mapped[str] = mapped_column(Encrypted)
    product_id: Mapped[str] = mapped_column(String(100))
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    # subscriptionState of Google (SUBSCRIPTION_STATE_ACTIVE...), or REPLACED by a newer token (upgrade, resubscribe).
    state: Mapped[str] = mapped_column(String(50), default="")
    # The access it gives (expiryTime of Google while the state gives access), none otherwise.
    entitled_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_id: Mapped[str] = mapped_column(Encrypted, default="")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    """A problem of the service for its administrators (alerts.py): open while resolved_at is empty."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # node:<id>, queue, disk
    key: Mapped[str] = mapped_column(String(50), index=True)
    message: Mapped[str] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


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
    label: Mapped[str] = mapped_column(Encrypted, default="")
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


class NodeRequest(Base):
    """A machine of the local network that asks to be a computing node (pairing.py). The administrator accepts it (it
    becomes a node, and takes its token the next time it asks) or rejects it."""

    __tablename__ = "node_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # The name it asks for (the name of the machine) and the address it asked from.
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(64), default="")
    # What the machine reports about itself (as Node.info), JSON.
    info: Mapped[str] = mapped_column(Text, default="{}")
    # Only the machine that asked knows the secret of its request.
    secret_hash: Mapped[str] = mapped_column(String(64))
    # pending, accepted, rejected
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    # Accepted: the node made and its token, kept until the machine takes it (the request is deleted then).
    node_name: Mapped[str] = mapped_column(String(100), default="")
    token: Mapped[str] = mapped_column(Encrypted, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    @property
    def info_dict(self) -> dict:
        try:
            value = json.loads(self.info or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


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
    input_name: Mapped[str] = mapped_column(Encrypted, default="")
    result_filename: Mapped[str] = mapped_column(Encrypted, default="")
    summary: Mapped[str] = mapped_column(Text, default="{}")
    # The project and the result were deleted (retention_h after the job ended).
    files_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Its place in the queue: when it was submitted plus its estimated duration (capped), so that short jobs pass
    # long ones submitted a little earlier but a long one never waits much more than that (scheduler.claim()).
    # Empty for the jobs of older versions (their created_at counts).
    order_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
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


class Bridge(Base):
    """A computer at home (Tisma Slicer left open) that relays the printers of its local network to the account: their
    state goes up, the commands sent from the phone come down (see remote.py)."""

    __tablename__ = "bridges"

    # Made by the computer once (a UUID), so that it is the same bridge after a restart.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Encrypted, default="")
    # The printers and their state, a JSON list (remote.clean_printers).
    printers: Mapped[str] = mapped_column(Encrypted, default="[]")
    # The printers whose camera is being looked at: JSON {printer id: until (ISO 8601)}.
    watching: Mapped[str] = mapped_column(Text, default="{}")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def printer_list(self) -> list[dict]:
        try:
            value = json.loads(self.printers or "[]")
        except ValueError:
            return []
        return value if isinstance(value, list) else []

    @property
    def watching_dict(self) -> dict:
        try:
            value = json.loads(self.watching or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


class RemoteCommand(Base):
    """A command for a printer of a bridge: print a file, pause, resume or stop."""

    __tablename__ = "remote_commands"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bridge_id: Mapped[str] = mapped_column(ForeignKey("bridges.id", ondelete="CASCADE"), index=True)
    printer_id: Mapped[str] = mapped_column(String(64))
    # print, pause, resume, stop
    action: Mapped[str] = mapped_column(String(10))
    # print: the name of the file for the printer (the file is in storage_dir/remote/<id>).
    file_name: Mapped[str] = mapped_column(Encrypted, default="")
    # queued (waiting for the bridge), sent (the bridge took it), done, failed
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BugReport(Base):
    """A problem reported from Tisma on a computer or from the app (reports.py): its texts and an excerpt of the log
    without anything personal (removed before they are kept, and encrypted besides), the account that sent it (none
    without an account) and the signature that groups the reports of the same problem."""

    __tablename__ = "bug_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    # pc, android, web
    app: Mapped[str] = mapped_column(String(10))
    # bug, crash, suggestion
    kind: Mapped[str] = mapped_column(String(10))
    app_version: Mapped[str] = mapped_column(String(40), default="")
    os: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(Encrypted, default="")
    description: Mapped[str] = mapped_column(Encrypted, default="")
    steps: Mapped[str] = mapped_column(Encrypted, default="")
    expected: Mapped[str] = mapped_column(Encrypted, default="")
    actual: Mapped[str] = mapped_column(Encrypted, default="")
    log: Mapped[str] = mapped_column(Encrypted, default="")
    signature: Mapped[str] = mapped_column(String(16), index=True)
    # new, confirmed, fixed, wontfix, duplicate (reports.STATUSES)
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)
    # Kind of error (triage.CATEGORIES): found from its words, or set by the team (category_by_hand).
    category: Mapped[str] = mapped_column(String(20), default="other", index=True)
    category_by_hand: Mapped[bool] = mapped_column(Boolean, default=False)
    # The language it was written in and its texts in the language of the team (translate.py): JSON {field: text} of
    # the fields that were in another language. translated_at is empty while it waits for the translator.
    language: Mapped[str] = mapped_column(String(8), default="")
    translation: Mapped[str] = mapped_column(Encrypted, default="")
    translated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Of the programmers: what was found, the commit that fixed it.
    note: Mapped[str] = mapped_column(Text, default="")
    # The team found the report useful and rewarded its author with days of their plan (by hand, once per report).
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    reward_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
