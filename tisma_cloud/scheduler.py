"""The queue: jobs wait in the database and the nodes take them (they are not pushed), so a node that is switched
off does not stop the others.

A node takes the first queued job of the first class of its priority list that has one; within a class, the jobs of
the plans with a higher priority first, then the oldest. On PostgreSQL the row is locked with FOR UPDATE SKIP LOCKED,
so that nodes claiming at the same time do not wait for each other; the conditional UPDATE (status still queued)
makes the claim safe on any database.

A user may submit a job when the account is enabled, the plan is paid (with a few days of grace), the hours of the
month are not used up and fewer than max_concurrent of its jobs are waiting or running. The time a job runs on a node
is charged to the month when it is done (failed jobs are not charged).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import Job, Node, Usage, User, month_of, utcnow

FINAL = ("done", "failed", "cancelled")
ACTIVE = ("queued", "running")


@dataclass
class Account:
    """State of an account for the client (Tisma, the app) and for the admission of jobs."""

    plan: str
    paid_until: datetime | None
    paid: bool
    hours_included: float
    hours_used: float
    active_jobs: int
    max_concurrent: int
    requests_per_minute: int

    @property
    def hours_left(self) -> float:
        return max(0., self.hours_included - self.hours_used)

    def as_dict(self) -> dict:
        return {
            "plan": self.plan,
            "paid_until": self.paid_until.isoformat() + "Z" if self.paid_until else None,
            "paid": self.paid,
            "hours_included": round(self.hours_included, 3),
            "hours_used": round(self.hours_used, 3),
            "hours_left": round(self.hours_left, 3),
            "active_jobs": self.active_jobs,
            "max_concurrent": self.max_concurrent,
        }


def used_seconds(session: Session, user_id: int, month: str) -> float:
    value = session.execute(select(Usage.seconds).where(Usage.user_id == user_id, Usage.month == month)).scalar()
    return float(value or 0.)


def account(session: Session, user: User, grace_days: int, now: datetime | None = None) -> Account:
    now = now or utcnow()
    plan = user.plan
    active = session.execute(
        select(func.count()).select_from(Job).where(Job.user_id == user.id, Job.status.in_(ACTIVE))
    ).scalar_one()
    until = user.access_until
    paid = user.is_admin or (until is not None and now <= until + timedelta(days=grace_days))
    return Account(plan=plan.name, paid_until=until, paid=paid, hours_included=plan.monthly_hours,
                   hours_used=used_seconds(session, user.id, month_of(now)) / 3600., active_jobs=active,
                   max_concurrent=plan.max_concurrent, requests_per_minute=plan.requests_per_minute)


class Refused(Exception):
    """A job the account may not submit now: status is the HTTP code (402 payment, 429 limits)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def admit(session: Session, user: User, grace_days: int) -> None:
    """Raises Refused when the user may not submit a job now. The user row is locked (PostgreSQL) so that parallel
    submissions of one user are counted one after the other."""
    session.execute(select(User.id).where(User.id == user.id).with_for_update())
    if user.is_admin:
        return
    acc = account(session, user, grace_days)
    if not acc.paid:
        raise Refused(402, "The subscription is not paid: renew it on the web site")
    if acc.hours_left <= 0.:
        raise Refused(402, f"The {acc.hours_included:g} hours of computing of this month are used up")
    if acc.active_jobs >= acc.max_concurrent:
        raise Refused(429, f"You already have {acc.active_jobs} jobs waiting or running (the plan allows "
                           f"{acc.max_concurrent} at a time): wait until one ends")


def charge_time(session: Session, job: Job) -> None:
    """Adds the time the job ran to the usage of the month it ended in."""
    if job.started_at is None or job.finished_at is None:
        return
    seconds = max(0., (job.finished_at - job.started_at).total_seconds())
    job.seconds = seconds
    month = month_of(job.finished_at)
    updated = session.execute(
        update(Usage).where(Usage.user_id == job.user_id, Usage.month == month).values(seconds=Usage.seconds + seconds)
    )
    if updated.rowcount == 0:
        session.add(Usage(user_id=job.user_id, month=month, seconds=seconds))
        session.flush()


def claim(session: Session, node: Node, is_sqlite: bool) -> Job | None:
    """The next job for the node, already marked as running on it, or None."""
    for job_class in node.priority_list:
        while True:
            query = (
                select(Job.id)
                .where(Job.status == "queued", Job.job_class == job_class)
                .order_by(Job.priority.desc(), Job.created_at, Job.id)
                .limit(1)
            )
            if not is_sqlite:
                query = query.with_for_update(skip_locked=True)
            job_id = session.execute(query).scalar_one_or_none()
            if job_id is None:
                break
            taken = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="running", node_id=node.id, started_at=utcnow(), progress=0, attempts=Job.attempts + 1)
            )
            if taken.rowcount == 1:
                session.flush()
                return session.get(Job, job_id, populate_existing=True)
            # Another node took it between the select and the update: try the next one.
    return None


def set_progress(session: Session, job: Job, progress: int) -> None:
    job.progress = max(0, min(100, int(progress)))


def finish(session: Session, job: Job, result_filename: str, summary: dict) -> None:
    job.status = "done"
    job.progress = 100
    job.result_filename = result_filename
    job.summary = json.dumps(summary)
    job.finished_at = utcnow()
    charge_time(session, job)


def fail(session: Session, job: Job, error: str) -> None:
    job.status = "failed"
    job.error = error[:4000]
    job.finished_at = utcnow()


def cancel(session: Session, job: Job) -> None:
    """Cancels a job of the user: a queued one at once; a running one is stopped by its node (which sees
    cancel_requested), and the time it ran is charged."""
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = utcnow()
    elif job.status == "running":
        job.cancel_requested = True


def stopped_by_node(session: Session, job: Job) -> None:
    """The node stopped a job whose cancellation was requested."""
    job.status = "cancelled"
    job.finished_at = utcnow()
    charge_time(session, job)


def requeue_stale(session: Session, timeout_s: int, max_attempts: int, now: datetime | None = None) -> int:
    """Running jobs of nodes not seen for timeout_s go back to the queue (or fail after max_attempts). Returns how
    many jobs were requeued or ended."""
    now = now or utcnow()
    limit = now - timedelta(seconds=timeout_s)
    stale = session.execute(
        select(Job)
        .join(Node, Node.id == Job.node_id)
        .where(Job.status == "running", (Node.last_seen.is_(None)) | (Node.last_seen < limit))
    ).scalars().all()
    for job in stale:
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = now
        elif job.attempts >= max_attempts:
            fail(session, job, "The computing servers that ran the job stopped answering")
        else:
            job.status = "queued"
            job.node_id = None
            job.progress = 0
            job.started_at = None
    return len(stale)
