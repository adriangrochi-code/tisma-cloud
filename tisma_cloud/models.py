"""Tables: plans, users (accounts of the web site, with their payment state), access tokens, monthly usage, nodes
(computing servers) and the machines that ask to be one, jobs, the bridges that relay the printers of a home to the
account with their commands, and the problems reported from Tisma and the app."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from .crypto import Encrypted, index
from .db import Base
from .settings import usage_offset


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def month_of(when: datetime) -> str:
    """The month of the plans' hours of a time in UTC: calendar months of TISMA_USAGE_UTC_OFFSET (UTC by default)."""
    return (when + usage_offset()).strftime("%Y-%m")


def month_start(when: datetime) -> datetime:
    """The start, in UTC, of the month of the plans' hours that holds a time in UTC."""
    offset = usage_offset()
    return (when + offset).replace(day=1, hour=0, minute=0, second=0, microsecond=0) - offset


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
    # The share of a node each job of the plan takes (capacity.py): processor cores (the engine runs with that many
    # threads) and CUDA cores of the graphics card (0: none counted).
    cpu_cores: Mapped[int | None] = mapped_column(Integer, default=1, nullable=True)
    cuda_cores: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    # The job classes (jobs.JOB_CLASSES) the plan lets submit, separated by commas; empty or NULL: all of them.
    classes: Mapped[str | None] = mapped_column(String(200), default="", nullable=True)

    def allows(self, job_class: str) -> bool:
        """Whether the plan lets submit jobs of this class."""
        allowed = [c for c in (self.classes or "").split(",") if c]
        return not allowed or job_class in allowed


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
    # The name the account shows as author in the library (one for all its publications; never its e-mail).
    library_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Random id of the account given to the App Store with each purchase (appAccountToken, a UUID), to know whose it is.
    apple_account: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    # The publisherUserId given to the Microsoft Store (microsoft.py): random, made the first time.
    microsoft_account: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
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
    # Whether the store renews it at its end (None: the store did not say).
    auto_renew: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    """Seconds of computing used by a user in a month of the plans (month_of: UTC unless TISMA_USAGE_UTC_OFFSET)."""

    __tablename__ = "usage"
    __table_args__ = (UniqueConstraint("user_id", "month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7))
    seconds: Mapped[float] = mapped_column(Float, default=0.)


class HourAdjustment(Base):
    """Hours of computing the administration gave (positive) or took (negative) for one calendar month (UTC) of a user:
    a reward or a penalty on top of the hours of the plan. Kept as a history, never edited."""

    __tablename__ = "hour_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    hours: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Encrypted, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    # pc, android, ios, web
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


class ScanUpload(Base):
    """A 3D scan of Tisma Scanner being sent in pieces (scans.py): too large for one request (Cloudflare cuts requests
    of more than 100 MB) and sent from a phone, whose connection may drop. It becomes a job when complete."""
    __tablename__ = "scan_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Encrypted, default="")
    # Bytes (several GB: a 64-bit integer also in PostgreSQL).
    size: Mapped[int] = mapped_column(BigInteger)
    received: Mapped[int] = mapped_column(BigInteger, default=0)
    # The job it became (when complete).
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---- the library shared between users (library.py; p2p_library.md of tisma_engine): only what is published about each
# model and how to recognise its files; the files themselves go from app to app, never through the master.
class LibraryPeerKey(Base):
    """The Ed25519 key of an app of an account (its peer identity, rule 5), bound when it first publishes."""
    __tablename__ = "library_peer_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # 32 bytes in base64.
    public_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryModel(Base):
    """One revision of a published model: what the index says of it. The newest revision of a model not removed is the
    one listed."""
    __tablename__ = "library_models"
    __table_args__ = (UniqueConstraint("model_id", "revision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[str] = mapped_column(String(40), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="[]")
    license: Mapped[str] = mapped_column(String(40))
    author_name: Mapped[str] = mapped_column(String(60))
    # Looked for at each request of a thumbnail (only a model still published shows it).
    thumbnail_sha256: Mapped[str] = mapped_column(String(64), index=True)
    thumbnail_size: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(BigInteger)
    # JSON: the files ([{path, role, size, sha256, ...}]), the summary of the analyses, the swarm of the peer network.
    files: Mapped[str] = mapped_column(Text)
    analysis: Mapped[str] = mapped_column(Text, default="null")
    swarm: Mapped[str] = mapped_column(Text, default="null")
    peer_key: Mapped[str] = mapped_column(String(64))
    manifest_signature: Mapped[str] = mapped_column(String(100))
    # A seeder downloaded and checked its files (ClamAV and the checks of p2p_library.md 3 octies).
    checked: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Removed by its author or by the review (with the reason): out of the index.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_reason: Mapped[str] = mapped_column(String(300), default="")


class LibraryBlock(Base):
    """What the apps refuse (rule 4): a file by its SHA-256, an account, a peer key, or an address for a short time."""
    __tablename__ = "library_blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # file, account, peer or address.
    kind: Mapped[str] = mapped_column(String(10), index=True)
    value: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(300), default="")
    # An address is blocked only until then (rule 5) and its row is deleted when it ends.
    until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryReport(Base):
    """«Denunciar»: a file or a model someone reported, until a person reviews it in the panel (rule 7)."""
    __tablename__ = "library_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    model_id: Mapped[str] = mapped_column(String(40), index=True)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Reviewed: the model was removed, or the report dismissed.
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), default="")


class LibraryCounter(Base):
    """The sequence numbers of the signed index and blocklist: they only grow (an app refuses an older one)."""
    __tablename__ = "library_counters"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class AccountFile(Base):
    """A file kept in the account, private (p2p_library.md 7 bis): the part of a scan, for «Mi biblioteca» of Tisma on
    the phone and the computer, and to publish in the library from it. Its files are in storage_dir/files/<id>/."""
    __tablename__ = "account_files"
    # One item per job of an account, also when the app retries «Imprimir» while the first request still runs.
    __table_args__ = (UniqueConstraint("user_id", "source_job"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(Encrypted, default="")
    kind: Mapped[str] = mapped_column(String(20), default="scan")
    # The job it came from (one item per job).
    source_job: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    # JSON: [{path, size, sha256}].
    files: Mapped[str] = mapped_column(Text, default="[]")
    published_model: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AccountFileTaken(Base):
    """A device of the account already took the file into its «Mi biblioteca» (each device takes it once)."""
    __tablename__ = "account_files_taken"
    __table_args__ = (UniqueConstraint("file_id", "device"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("account_files.id", ondelete="CASCADE"), index=True)
    device: Mapped[str] = mapped_column(String(100))
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibrarySetting(Base):
    """Settings of the library changed from the panel (the cap of the relays per hour and per account, 8 quater)."""
    __tablename__ = "library_settings"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class AppleSubscription(Base):
    """An App Store auto-renewable subscription (its original transaction) and the access it gives, as Apple signed it
    in its last transaction (apple.py)."""
    __tablename__ = "apple_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    original_transaction_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    transaction_id: Mapped[str] = mapped_column(String(64), default="")
    product_id: Mapped[str] = mapped_column(String(100))
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    environment: Mapped[str] = mapped_column(String(20), default="")
    # Apple's expiresDate while not revoked (refunded), none otherwise.
    entitled_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # The time of the transaction applied last: an older one arriving late does not undo a newer one.
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_renew: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MicrosoftKey(Base):
    """The Microsoft Store ID key (purchase API) the app of an account sent, kept encrypted to ask Microsoft again and
    renewed before it ends (microsoft.py)."""
    __tablename__ = "microsoft_keys"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(Encrypted)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MicrosoftSubscription(Base):
    """A Microsoft Store subscription add-on and the access it gives, as the purchase API answered last."""
    __tablename__ = "microsoft_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    product_id: Mapped[str] = mapped_column(String(100), default="")
    plan_name: Mapped[str] = mapped_column(ForeignKey("plans.name"))
    state: Mapped[str] = mapped_column(String(20), default="")
    # expirationTimeWithGrace (or expirationTime) while Active or InDunning, none otherwise.
    entitled_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_renew: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
