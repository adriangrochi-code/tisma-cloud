"""The master server: the API of the users (Tisma, the app), of the computing nodes and of the administrator."""

import asyncio
import base64
import contextlib
import errno
import multiprocessing
import os
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import shutil
import threading
import uuid
import zipfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from . import (__version__, alerts, apple, backup, billing, capacity, codes, crypto, google_play, library, microsoft,
               pairing, peers, remote, reports, scans, scheduler, stores, triage)
from .google_auth import GoogleError, GoogleVerifier
from .mail import code_message, exists_message, mailer_for, reward_message
from .db import Database
from .jobs import (ESTIMATE_MAX_TRIANGLES, JOB_CLASS_DESCRIPTIONS, JOB_CLASSES, JobError, count_triangles,
                   estimate_seconds, plan_job)
from .models import (AccountFile, AccountFileTaken, Alert, ApiToken, AppleSubscription, Bridge, BugReport, EmailCode,
                     HourAdjustment, Job, LibraryBlock, LibraryModel, LibraryPeerKey, LibraryReport, MicrosoftKey,
                     MicrosoftSubscription, Node, Payment, Plan, PlaySubscription, RemoteCommand, ScanUpload, Usage, User, month_of, utcnow)
from .guard import Guard
from .presence import Presence
from .publicip import CHECK_S as PUBLIC_ADDRESS_CHECK_S, PublicAddress
from .ratelimit import RateLimiter
from .security import DUMMY_HASH, Busy, hash_password, hash_token, needs_rehash, new_token, verify_password
from .settings import Settings
from .storage import ALLOWED_RESULTS, Storage, TooLarge, safe_filename
from .translate import TranslationError, Translator
from .threats import PARALLEL_BYTES, Clamd, ScanUnavailable, Threat, ThreatLog, check_gcode, check_project
from .twins import Twins
from .updates import CHECK_S as UPDATES_CHECK_S, UpdateMirror
from .wakeup import Wakeup

log = logging.getLogger("tisma_cloud")

# Keys of the long polls (wakeup.py): the nodes wait for any job, a bridge for its own commands.
JOBS = "jobs"
# Machines asking to be nodes (pairing.py): how often they ask for the answer [s], and requests per minute of an
# address (the request and its polls).
JOIN_POLL_S = 5
JOIN_PER_MINUTE = 30
# Wrong passwords an hour for one account, from any address (guessing it from many addresses), and e-mail codes a day
# to one address.
LOGIN_FAILURES_PER_HOUR = 20
CODES_PER_DAY = 10
# Free space a scan leaves on the master's disk once it is there (results, the database, the copies) [bytes].
SCAN_ROOM_LEFT = 2 << 30
# The library (library.py): models published and reports sent by one account in a day.
LIBRARY_PUBLISH_PER_DAY = 20
LIBRARY_REPORTS_PER_DAY = 20
LIBRARY_RENAMES_PER_DAY = 5
# Apps (peer keys) of one account, and announcements of an account per minute (all its apps).
LIBRARY_PEER_KEYS_PER_ACCOUNT = 10
LIBRARY_ANNOUNCES_PER_MINUTE = 30


def bridge_key(bridge_id: str) -> str:
    return "bridge:" + bridge_id


# The last seen of the bridges and their states kept in memory are written this often [s].
FLUSH_S = 5.


class TokenCache:
    """The accounts of the access tokens used lately, so that the requests of a bridge (one every few seconds) do not
    ask the database each time. An entry lives TTL_S; a session closed or an account disabled through this master is
    dropped at once (by the administration program, another process, within TTL_S)."""

    TTL_S = 60.

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[int, float]] = {}

    def get(self, token_hash: str) -> int | None:
        with self._lock:
            item = self._items.get(token_hash)
            if item is None:
                return None
            if time.monotonic() > item[1]:
                del self._items[token_hash]
                return None
            return item[0]

    def put(self, token_hash: str, user_id: int) -> None:
        with self._lock:
            if len(self._items) > 100_000:
                self._items.clear()
            self._items[token_hash] = (user_id, time.monotonic() + self.TTL_S)

    def drop(self, token_hash: str) -> None:
        with self._lock:
            self._items.pop(token_hash, None)

    def drop_user(self, user_id: int) -> None:
        with self._lock:
            for key in [k for k, (u, _) in self._items.items() if u == user_id]:
                del self._items[key]


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # The personal data is encrypted in the database (crypto.py); the values of older versions are encrypted now.
        crypto.configure(settings)
        self.db = Database(settings.database_url)
        self.db.create_all()
        crypto.migrate(self.db)
        self.sync_internal_accounts()
        self.storage = Storage(settings.storage_dir)
        # The address of the master on internet when TISMA_PUBLIC_URL is empty, and the dynamic DNS name.
        self.public_address = PublicAddress(settings, Path(settings.storage_dir) / "public-address.json")
        self.remote_files = remote.RemoteFiles(settings.storage_dir)
        # The bridges, their state and the camera images in memory (presence.py, remote.Images).
        self.presence = Presence()
        self.images = remote.Images()
        self.tokens = TokenCache()
        # Uploads checked before they are kept (threats.py), and the ones refused lately for the alerts.
        self.clamd = Clamd(settings.clamd) if settings.clamd else None
        self.refused = ThreatLog()
        self.twins = Twins()
        self.updates = UpdateMirror(settings.updates_url, Path(settings.storage_dir) / "updates")
        # The library shared between users (library.py): its own signing key and the signed index and blocklist.
        # Who shares which model, in memory only (peers.py).
        self.peers = peers.Announcements()
        key = library.LibraryKey(Path(settings.storage_dir))
        if key.made_now:
            with self.db.session() as s:
                published = s.execute(select(func.count()).select_from(LibraryModel)).scalar_one()
            if published:
                # A master restored without its library.key: the lists are not served until the new key is certified.
                log.error("The library key was made now, but %d models were published with another one (a master "
                          "restored without storage/library.key?): the library answers 503 until the owner certifies "
                          "the new key (tisma-cloud admin library-key, then library-cert); put the old one in "
                          "TISMA_LIBRARY_REVOKED_KEYS if it was lost or stolen", published)
        self.library = library.Signed(key,
                                      tuple(k.strip() for k in settings.library_revoked_keys.split(",") if k.strip()))
        # The last daily backup that failed (backup.py), for the alerts; None when the last one was good.
        self.backup_failed: str | None = None
        self._backup_lock = threading.Lock()
        # Processes that read large G-code in parts at the same time (made when first needed).
        self._scan_pool: ProcessPoolExecutor | None = None
        self._scan_pool_lock = threading.Lock()
        self.limiter = RateLimiter()
        # The long polls of the bridges and the nodes, woken by the commands and jobs created here.
        self.wakeup = Wakeup()
        # A slower window for the e-mails sent to one address (at most a few per hour).
        self.hourly = RateLimiter(3600.)
        # And a day: the e-mail codes to one address (each reset code allows a few guesses).
        self.daily = RateLimiter(86400.)
        self.mailer = mailer_for(settings)
        # The translator of the problem reports, on our own servers (none: they are kept as written).
        self.translator = Translator(settings.translate_url, settings.translate_to) if settings.translate_url else None
        self.google = GoogleVerifier([c.strip() for c in settings.google_client_ids.split(",") if c.strip()])
        self.play = google_play.store_for(settings.play_package, settings.play_credentials, settings.play_products)
        self.apple = apple.store_for(settings.apple_bundle_id, settings.apple_app_id, settings.apple_environment,
                                     settings.apple_products, settings.apple_root_certs, settings.apple_online_checks)
        self.microsoft = microsoft.store_for(settings.microsoft_tenant, settings.microsoft_client_id,
                                             settings.microsoft_client_secret, settings.microsoft_products)
        self._microsoft_checked: float | None = None
        self.play_push = (google_play.PushVerifier(settings.play_push_audience, settings.play_push_account)
                          if settings.play_push_audience and settings.play_push_account else None)
        # When the alerts were last checked (time.monotonic(), None: never). Not 0: the monotonic clock counts from the
        # start of the machine, and a machine started less than a minute ago would skip the first check.
        self._alerts_checked: float | None = None
        # When the old data was last forgotten (once an hour).
        self._forgot_at: float | None = None
        self._alerts_lock = threading.Lock()

    def sync_internal_accounts(self) -> None:
        """The accounts with an e-mail of TISMA_INTERNAL_EMAILS are internal (unlimited). Removing an address from the
        list does not take it back: `tisma-cloud-admin user-set EMAIL --limited` does."""
        if not self.settings.internal_emails.strip():
            return
        with self.db.session() as s:
            for user in s.execute(select(User).where(User.unlimited.is_(False))).scalars():
                if self.settings.is_internal_email(user.email):
                    user.unlimited = True

    def refresh_microsoft(self, now: datetime | None = None) -> int:
        """The Microsoft Store sends no notifications: the subscriptions of the accounts with a key are asked again once
        a day (microsoft.py). Returns how many accounts were asked."""
        if self.microsoft is None:
            return 0
        with self.db.session() as s:
            due = self.microsoft.due(s, now)
        for user_id in due:
            try:
                with self.db.session() as s:
                    user = s.get(User, user_id)
                    if user is not None and self.microsoft.refresh(s, user, now) is None:
                        log.info("Microsoft Store key of account %s refused by Microsoft: dropped", user_id)
            except microsoft.MicrosoftUnavailable as ex:
                log.warning("Microsoft Store not reachable: %s", ex)
                break
            except Exception:  # noqa: BLE001 (one account must not stop the others; asked again next time)
                log.exception("Microsoft Store check of account %s failed", user_id)
        return len(due)

    def sweep(self) -> None:
        """Requeues the jobs of lost nodes, removes the files of old jobs and fails the remote commands not taken."""
        with self.db.session() as s:
            remote.expire(s, self.remote_files, self.settings.remote_command_minutes)
            # The bridges with commands waiting (after a restart, or made by another process).
            self.presence.set_queued(set(s.execute(
                select(RemoteCommand.bridge_id).where(RemoteCommand.status == "queued").distinct()).scalars()))
        self.images.drop_old()
        self.presence.drop_old_looks()
        with self.db.session() as s:
            requeued = scheduler.requeue_stale(s, self.settings.node_timeout_s, self.settings.max_attempts)
            pairing.drop_old(s)
            # Scans being sent and given up (scans.py): their pieces and their row. A complete one is the job's.
            stale = utcnow() - timedelta(hours=scans.STALE_H)
            for up in s.execute(select(ScanUpload).where(ScanUpload.updated_at < stale)).scalars().all():
                if up.job_id is None:
                    scans.upload_path(self.storage.root, up.id).unlink(missing_ok=True)
                s.delete(up)
            # Jobs ended by the sweep itself (nodes lost): the recent ones are enough, the others were cleaned before.
            ended = s.execute(select(Job.id).where(Job.status.in_(scheduler.FINAL), Job.files_removed.is_(False),
                                                   Job.finished_at > utcnow() - timedelta(hours=1))).scalars()
            for job_id in ended:
                self.storage.remove_input(job_id)
        if requeued:
            self.wakeup.notify(JOBS)
        limit = utcnow() - timedelta(hours=self.settings.retention_h)
        with self.db.session() as s:
            old = s.execute(
                select(Job).where(Job.status.in_(scheduler.FINAL), Job.finished_at < limit, Job.files_removed.is_(False))
            ).scalars().all()
            for job in old:
                self.storage.remove_job(job.id)
                job.files_removed = True
        if self._forgot_at is None or time.monotonic() - self._forgot_at >= 3600:
            self._forgot_at = time.monotonic()
            try:
                self.forget_old_data()
            except Exception:  # noqa: BLE001 (the alerts below must still run)
                log.exception("Forgetting old data failed")
        # The Microsoft Store subscriptions, looked at every 10 minutes (each account once a day).
        if self.microsoft is not None and (self._microsoft_checked is None
                                           or time.monotonic() - self._microsoft_checked >= 600):
            self._microsoft_checked = time.monotonic()
            try:
                self.refresh_microsoft()
            except Exception:  # noqa: BLE001 (the rest of the sweep, the alerts too, goes on)
                log.exception("Checking the Microsoft Store subscriptions failed")
        # The alerts of the administrators, once a minute.
        if self._alerts_checked is None or time.monotonic() - self._alerts_checked >= 60:
            self._alerts_checked = time.monotonic()
            self.check_alerts()

    def translate_reports(self, limit: int = 5) -> int:
        """Translates the texts of the reports that wait for it (translate.py) and guesses their kind of error again on
        the translated words. Returns how many were done; stops at the first failure of the translator (it is tried
        again later)."""
        translator = self.translator
        if translator is None:
            return 0
        with self.db.session() as s:
            pending = s.execute(select(BugReport).where(BugReport.translated_at.is_(None))
                                .order_by(BugReport.created_at).limit(limit)).scalars().all()
            work = [(r.id, {k: getattr(r, k) for k in reports.TEXT_FIELDS}) for r in pending]
        done = 0
        for report_id, fields in work:
            translated, languages = {}, {}
            try:
                for key, text in fields.items():
                    if text.strip():
                        out, language = translator.translate(text)
                        languages[key] = language
                        if language and language != translator.target and out.strip() != text.strip():
                            translated[key] = out
            except TranslationError as ex:
                log.warning("The translator does not answer: %s", ex)
                break
            # The language of the longest text.
            longest = max((k for k in languages), key=lambda k: len(fields[k]), default=None)
            with self.db.session() as s:
                r = s.get(BugReport, report_id)
                if r is None:
                    continue
                r.language = languages.get(longest, "") if longest else ""
                r.translation = json.dumps(translated, ensure_ascii=False) if translated else ""
                r.translated_at = utcnow()
                if translated and not r.category_by_hand:
                    # On the words as written and translated: a translator can miss the words of 3D printing.
                    texts = reports.texts_of(r)
                    body = ("description", "steps", "expected", "actual")
                    r.category = triage.category(r.kind, f"{r.title} {texts['title']}", "\n".join(
                        [*(getattr(r, k) for k in body), *(texts[k] for k in body)]), r.log)
            done += 1
        return done

    def forget_old_data(self, now: datetime | None = None) -> None:
        """Keeps only what the service needs (privacy): the jobs ended long ago (their files went before; the hours used
        stay in the monthly usage), the remote commands ended, the e-mail codes expired, the sessions not used for
        session_days, the bridges not seen for remote.FORGET_BRIDGE_DAYS and the problem reports older than
        report_days."""
        now = now or utcnow()
        sessions = now - timedelta(days=self.settings.session_days)
        with self.db.session() as s:
            s.execute(delete(Job).where(Job.status.in_(scheduler.FINAL), Job.files_removed.is_(True),
                                        Job.finished_at < now - timedelta(days=self.settings.job_history_days)))
            s.execute(delete(RemoteCommand).where(RemoteCommand.status.in_(("done", "failed")),
                                                  RemoteCommand.updated_at < now - timedelta(days=1)))
            s.execute(delete(EmailCode).where(EmailCode.expires_at < now))
            s.execute(delete(ApiToken).where(func.coalesce(ApiToken.last_used, ApiToken.created_at) < sessions))
            s.execute(delete(BugReport).where(BugReport.created_at < now - timedelta(days=self.settings.report_days)))
            stale = s.execute(select(Bridge.id).where(
                func.coalesce(Bridge.last_seen, Bridge.created_at) < now - timedelta(days=remote.FORGET_BRIDGE_DAYS))
            ).scalars().all()
            if stale:
                s.execute(delete(Bridge).where(Bridge.id.in_(stale)))
        for bridge_id in stale:
            self.presence.forget(bridge_id)

    def delete_account(self, user_id: int) -> None:
        """Removes everything of the account but its payments (see the endpoint POST /v1/me/delete)."""
        with self.db.session() as s:
            user = s.get(User, user_id)
            jobs = s.execute(select(Job.id).where(Job.user_id == user_id)).scalars().all()
            bridges = s.execute(select(Bridge.id).where(Bridge.user_id == user_id)).scalars().all()
            commands = s.execute(select(RemoteCommand.id).where(RemoteCommand.bridge_id.in_(bridges))).scalars().all() \
                if bridges else []
            files = s.execute(select(AccountFile.id).where(AccountFile.user_id == user_id)).scalars().all()
            # Scans half sent: their pieces on the disk are photos of the person's things.
            uploads = s.execute(select(ScanUpload.id).where(ScanUpload.user_id == user_id,
                                                            ScanUpload.job_id.is_(None))).scalars().all()
            thumbs = set(s.execute(select(LibraryModel.thumbnail_sha256).where(LibraryModel.user_id == user_id)).scalars())
            if files:
                s.execute(delete(AccountFileTaken).where(AccountFileTaken.file_id.in_(files)))
            for model in (ApiToken, EmailCode, Usage, PlaySubscription, AppleSubscription, MicrosoftSubscription,
                          MicrosoftKey, Job, Bridge, BugReport, AccountFile, LibraryModel, LibraryPeerKey, ScanUpload,
                          HourAdjustment):
                s.execute(delete(model).where(model.user_id == user_id))
            # Its reports of others' models stay for the review, without the account.
            s.execute(update(LibraryReport).where(LibraryReport.user_id == user_id).values(user_id=None))
            # Its published models leave the index, and their images the disk (unless another model uses the same).
            library.bump(s, "index")
            s.flush()
            shared = set(s.execute(select(LibraryModel.thumbnail_sha256)
                                   .where(LibraryModel.thumbnail_sha256.in_(thumbs))).scalars()) if thumbs else set()
            if commands:
                s.execute(delete(RemoteCommand).where(RemoteCommand.id.in_(commands)))
            user.email = f"deleted-{user.id}@deleted.invalid"
            user.password_hash, user.google_sub, user.play_account, user.external_id = "", None, None, None
            user.apple_account, user.microsoft_account, user.library_name = None, None, None
            user.enabled = user.email_verified = user.unlimited = False
            user.extra_printers = 0
        for job_id in jobs:
            self.storage.remove_job(job_id)
        for file_id in files:
            shutil.rmtree(Path(self.settings.storage_dir) / "files" / file_id, ignore_errors=True)
        for upload_id in uploads:
            scans.upload_path(self.storage.root, upload_id).unlink(missing_ok=True)
        for digest in thumbs - shared:
            (Path(self.settings.storage_dir) / "library" / "thumbs" / f"{digest}.webp").unlink(missing_ok=True)
        for command_id in commands:
            self.remote_files.remove_command(command_id)
        for bridge_id in bridges:
            self.presence.forget(bridge_id)
            self.images.drop_bridge(bridge_id)
        self.tokens.drop_user(user_id)

    def flush_presence(self) -> None:
        """Writes the last seen of the bridges and the states kept in memory (presence.py)."""
        seen, states = self.presence.take_dirty()
        if not seen and not states:
            return
        table = Bridge.__table__
        with self.db.session() as s:
            # Not the ORM's update by primary key: a bridge removed meanwhile must not fail the others.
            rows = [{"bridge": b, "seen": t} for b, t in seen.items() if b not in states]
            if rows:
                s.execute(update(table).where(table.c.id == bindparam("bridge")).values(last_seen=bindparam("seen")),
                          rows)
            for bridge_id, (name, printers) in states.items():
                s.execute(update(Bridge).where(Bridge.id == bridge_id).values(
                    name=name, printers=json.dumps(printers), last_seen=seen.get(bridge_id) or utcnow()))

    def scan_pool(self) -> ProcessPoolExecutor:
        with self._scan_pool_lock:
            if self._scan_pool is None:
                self._scan_pool = ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1),
                                                      mp_context=multiprocessing.get_context("spawn"))
            return self._scan_pool

    def close(self) -> None:
        if self._scan_pool is not None:
            self._scan_pool.shutdown(wait=False, cancel_futures=True)
        self.db.engine.dispose()

    def inspect(self, path: Path, kind: str, text_gcode: bool = True) -> list:
        """Checks an uploaded file: "project" (3MF), "gcode" (sent to a printer; text_gcode: ASCII, read) or "result"
        (of a node), and with ClamAV when set. Raises Threat or ScanUnavailable (both counted for the alerts); returns
        the commands of a G-code that are only reported."""
        findings: list = []
        try:
            if kind == "project":
                check_project(path)
            elif kind == "gcode" and text_gcode:
                large = path.stat().st_size > PARALLEL_BYTES
                findings = check_gcode(path, pool=self.scan_pool() if large else None)
            if self.clamd is not None:
                found = self.clamd.scan(path)
                if found:
                    raise Threat(f"The antivirus found {found} in the file", "virus")
        except Threat as ex:
            self.refused.add(ex.kind)
            log.warning("Upload refused (%s): %s", ex.kind, ex)
            raise
        except ScanUnavailable:
            self.refused.add("scanner")
            log.exception("The antivirus did not check an upload")
            raise
        return findings

    def backup_if_due(self, now: datetime | None = None) -> backup.Backup | None:
        """The daily backup of the database, when it is due (backup.py). A failure is logged and alerted."""
        dest = backup.backup_dir(self.settings)
        now = now or utcnow()
        if dest is None or not self._backup_lock.acquire(blocking=False):
            return None
        try:
            if not backup.due(self.settings, dest, now):
                return None
            made = backup.make(self.settings, dest, now)
            self.backup_failed = None
            return made
        except (backup.BackupError, OSError, ValueError) as ex:
            if self.backup_failed is None:
                log.error("The daily backup failed: %s", ex)
            self.backup_failed = str(ex)
            return None
        finally:
            self._backup_lock.release()

    def check_alerts(self, now: datetime | None = None) -> list[Alert]:
        """Opens, repeats and closes the alerts (one check at a time, so that an alert is never opened twice)."""
        with self._alerts_lock, self.db.session() as s:
            return alerts.update(s, self.settings, self.storage.root, self.mailer, now, self.refused.recent(),
                                 self.twins.twins(now or utcnow()), self.backup_failed)


def job_view(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "job_class": job.job_class,
        "params": job.params_dict,
        "status": job.status,
        "progress": job.progress,
        "seconds": round(job.seconds, 1),
        "error": job.error,
        "summary": job.summary_dict,
        "result_filename": job.result_filename if job.status == "done" and not job.files_removed else "",
        "created_at": job.created_at.isoformat() + "Z",
        "started_at": job.started_at.isoformat() + "Z" if job.started_at else None,
        "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
    }


def node_view(node: Node, running: int = 0, timeout_s: int = 90) -> dict[str, Any]:
    return {
        "name": node.name,
        "priorities": node.priority_list,
        "enabled": node.enabled,
        "info": node.info_dict,
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        # Seen recently: the node is waiting for jobs or running them (it claims or beats every few seconds).
        "online": node.last_seen is not None and utcnow() - node.last_seen < timedelta(seconds=timeout_s),
        "running": running,
    }


# The pages of the master load nothing from other sites, are not shown inside other pages and are not cached.
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src {scripts}; style-src 'unsafe-inline'; img-src data:; "
                               "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
}


def script_hashes(page: str) -> str:
    """The SHA-256 of each script of the page, for its Content Security Policy: only those scripts run, so that a
    script slipped into the page (a name with HTML in it) would not."""
    return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode() + "'"
                    for script in re.findall(r"<script>(.*?)</script>", page, re.S))


def static_page(name: str) -> tuple[str, dict[str, str]]:
    page = resources.files("tisma_cloud").joinpath(f"static/{name}").read_text(encoding="utf-8")
    headers = dict(PAGE_HEADERS)
    headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace("{scripts}", script_hashes(page))
    return page, headers


def user_view(user: User) -> dict[str, Any]:
    return {"email": user.email, "external_id": user.external_id, "plan": user.plan_name,
            "paid_until": user.paid_until.isoformat() + "Z" if user.paid_until else None,
            "store_until": user.store_until.isoformat() + "Z" if user.store_until else None,
            "access_until": user.access_until.isoformat() + "Z" if user.access_until else None, "enabled": user.enabled,
            "is_admin": user.is_admin, "unlimited": user.unlimited}


def adjustment_view(a: HourAdjustment) -> dict[str, Any]:
    return {"id": a.id, "month": a.month, "hours": a.hours, "note": a.note,
            "created_at": a.created_at.isoformat() + "Z"}


def plan_view(plan: Plan) -> dict[str, Any]:
    return {"name": plan.name, "monthly_hours": plan.monthly_hours, "max_concurrent": plan.max_concurrent,
            "requests_per_minute": plan.requests_per_minute, "priority": plan.priority,
            "price_cents": plan.price_cents, "currency": plan.currency, "public": plan.public,
            "remote_printers": plan.remote_printers, "extra_printer_cents": plan.extra_printer_cents,
            "classes": [c for c in JOB_CLASSES if plan.allows(c)],
            "cpu_cores": plan.cpu_cores or 1, "cuda_cores": plan.cuda_cores or 0}


def payment_view(p: Payment) -> dict[str, Any]:
    return {"id": p.id, "plan": p.plan_name, "months": p.months, "extra_printers": p.extra_printers,
            "amount_cents": p.amount_cents, "currency": p.currency,
            "provider": p.provider, "status": p.status, "note": p.note,
            "created_at": p.created_at.isoformat() + "Z", "paid_at": p.paid_at.isoformat() + "Z" if p.paid_at else None}


EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


def parse_networks(text: str) -> list:
    return [ipaddress.ip_network(n.strip(), strict=False) for n in text.split(",") if n.strip()]


def address_in(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing access token", headers={"WWW-Authenticate": "Bearer"})
    return authorization[len("Bearer "):].strip()


def check_priorities(priorities: list[str]) -> str:
    unknown = [p for p in priorities if p not in JOB_CLASSES]
    if unknown:
        raise HTTPException(422, f"Unknown job classes: {', '.join(unknown)} (known: {', '.join(JOB_CLASSES)})")
    if len(set(priorities)) != len(priorities):
        raise HTTPException(422, "A job class is repeated")
    return ",".join(priorities)


class BridgeBody(BaseModel):
    """What a bridge sends every few seconds: its name and its printers with their state (see remote.clean_printers)."""
    name: str = Field(default="", max_length=100)
    printers: list[Any] = Field(default_factory=list)


class CommandResultBody(BaseModel):
    ok: bool
    error: str = Field(default="", max_length=2000)


class ClaimBody(BaseModel):
    info: dict[str, Any] = Field(default_factory=dict)
    # Job classes the node cannot take now (it runs as many analyses as it can).
    exclude: list[str] = Field(default_factory=list, max_length=len(JOB_CLASSES))


# A job claimed this recently may not be in the heartbeat of its node yet (claimed between two heartbeats) [s].
ORPHAN_GRACE_S = 60.


class ScanStartBody(BaseModel):
    size: int = Field(gt=0)
    name: str = Field(default="", max_length=200)


# The jobs a node may say it runs in one heartbeat (with the engine, one per core).
MAX_RUNNING = 1024


class HeartbeatBody(BaseModel):
    running: list[str] = Field(default_factory=list, max_length=MAX_RUNNING)
    # A random id of this run of the node program (twins.py); empty from an older node.
    instance: str = Field(default="", max_length=64)
    info: dict[str, Any] = Field(default_factory=dict)


class ProgressBody(BaseModel):
    progress: int = Field(ge=0, le=100)


class ReportBody(BaseModel):
    """A problem reported from Tisma (pc), the app (android) or the web site."""
    app: str = Field(pattern="^(" + "|".join(reports.APPS) + ")$")
    kind: str = Field(default="bug", pattern="^(" + "|".join(reports.KINDS) + ")$")
    title: str = Field(min_length=3, max_length=reports.TITLE_MAX)
    description: str = Field(default="", max_length=reports.TEXT_MAX)
    steps: str = Field(default="", max_length=reports.TEXT_MAX)
    expected: str = Field(default="", max_length=reports.TEXT_MAX)
    actual: str = Field(default="", max_length=reports.TEXT_MAX)
    app_version: str = Field(default="", max_length=40)
    os: str = Field(default="", max_length=80)
    log: str = Field(default="", max_length=reports.LOG_MAX)


class ReportUpdateBody(BaseModel):
    status: str | None = Field(default=None, pattern="^(" + "|".join(reports.STATUSES) + ")$")
    note: str | None = Field(default=None, max_length=2000)
    # The kind of error chosen by the team (triage.CATEGORIES); it is not guessed again.
    category: str | None = Field(default=None, pattern="^(" + "|".join(k for k, _n, _s in triage.CATEGORIES) + ")$")


class RewardBody(BaseModel):
    # Days of the plan; the default of the service (TISMA_REPORT_REWARD_DAYS) without them.
    days: int | None = Field(default=None, ge=1, le=90)


def report_view(r: BugReport, texts: bool = False) -> dict[str, Any]:
    out = {"id": r.id, "app": r.app, "kind": r.kind, "title": r.title, "status": r.status, "signature": r.signature,
           "category": r.category, "category_name": triage.CATEGORY_NAMES.get(r.category, r.category),
           "language": r.language,
           "app_version": r.app_version, "os": r.os, "created_at": r.created_at.isoformat() + "Z",
           "updated_at": r.updated_at.isoformat() + "Z",
           "rewarded_at": r.rewarded_at.isoformat() + "Z" if r.rewarded_at else None, "reward_days": r.reward_days}
    if texts:
        out.update(description=r.description, steps=r.steps, expected=r.expected, actual=r.actual, log=r.log,
                   translation=reports.translation_of(r))
    return out


class FailBody(BaseModel):
    error: str = Field(max_length=4000)
    cancelled: bool = False
    # From the engine: its stable error "code" and "error_es", the text in Spanish (cloud.md, rule 18).
    details: dict[str, str] = Field(default_factory=dict)
    # The node's own trouble (a full disk, an error of the node program), not the job's: back to the queue.
    node_error: bool = False


class JoinBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    info: dict[str, Any] = Field(default_factory=dict)


class AcceptBody(BaseModel):
    name: str = Field(default="", max_length=100)
    priorities: list[str] | None = None


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    priorities: list[str]


class NodeUpdate(BaseModel):
    priorities: list[str] | None = None
    enabled: bool | None = None


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)
    # Name of the device, to tell the sessions apart ("Tisma on the office PC").
    device: str = Field(default="", max_length=100)


class PlanBody(BaseModel):
    monthly_hours: float = Field(ge=0)
    max_concurrent: int = Field(ge=1, le=100)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)
    priority: int = 0
    price_cents: int = Field(default=0, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    public: bool = False
    remote_printers: int = Field(default=0, ge=0, le=remote.MAX_PRINTERS * remote.MAX_BRIDGES)
    extra_printer_cents: int = Field(default=0, ge=0)
    # The share of a node each job takes (capacity.py).
    cpu_cores: int = Field(default=1, ge=1, le=256)
    cuda_cores: int = Field(default=0, ge=0, le=100_000)
    # Job classes the plan includes (jobs.JOB_CLASSES); empty: all of them.
    classes: list[str] = Field(default_factory=list, max_length=len(JOB_CLASSES))


class RegisterBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    plan: str | None = None


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class DeleteAccountBody(BaseModel):
    """The password of the account, or (accounts without one: Google) the code sent by e-mail."""
    password: str | None = Field(default=None, max_length=200)
    code: str | None = Field(default=None, min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class CodeBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    device: str = Field(default="", max_length=100)


class ResetBody(CodeBody):
    password: str = Field(min_length=8, max_length=200)


class GoogleBody(BaseModel):
    id_token: str = Field(min_length=20, max_length=8000)
    device: str = Field(default="", max_length=100)


class CheckoutBody(BaseModel):
    plan: str = Field(min_length=1, max_length=50)
    months: int = Field(default=1, ge=1, le=12)
    # Remote printers on top of those of the plan.
    extra_printers: int = Field(default=0, ge=0, le=100)


class PlayPurchaseBody(BaseModel):
    purchase_token: str = Field(min_length=1, max_length=2000)


class ManualPaymentBody(BaseModel):
    plan: str | None = None
    months: int = Field(default=1, ge=1, le=24)
    amount_cents: int = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    note: str = Field(default="", max_length=200)
    extra_printers: int = Field(default=0, ge=0, le=100)


class UnlimitedBody(BaseModel):
    unlimited: bool


class HoursBody(BaseModel):
    # Hours of computing given (positive, a reward) or taken (negative, a penalty) for the current month.
    hours: float = Field(ge=-10000, le=10000)
    note: str = Field(default="", max_length=200)


class EnabledBody(BaseModel):
    enabled: bool


class SiteUser(BaseModel):
    """A user as the web site sends it (create or update; omitted fields keep their value)."""

    email: str | None = Field(default=None, min_length=3, max_length=200)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    plan: str | None = None
    paid_until: datetime | None = None
    enabled: bool | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    services = Services(settings or Settings())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The loops end by themselves when the server stops, after the sweep or flush in flight (a cancel would not
        # stop its thread: the database would be closed under it, and the tests would drop the tables under it).
        stopping = asyncio.Event()

        async def pause(seconds: float) -> None:
            try:
                await asyncio.wait_for(stopping.wait(), seconds)
            except asyncio.TimeoutError:
                pass

        async def sweeper() -> None:
            while not stopping.is_set():
                try:
                    await run_in_threadpool(services.sweep)
                except Exception:  # noqa: BLE001 (keep sweeping)
                    log.exception("Sweep failed")
                try:
                    await run_in_threadpool(services.backup_if_due)
                except Exception:  # noqa: BLE001 (keep sweeping)
                    log.exception("Backup failed")
                await pause(30)

        async def flusher() -> None:
            while not stopping.is_set():
                await pause(FLUSH_S)
                try:
                    await run_in_threadpool(services.flush_presence)
                except Exception:  # noqa: BLE001 (keep flushing)
                    log.exception("Writing the bridges failed")

        async def translator() -> None:
            while not stopping.is_set():
                try:
                    done = await run_in_threadpool(services.translate_reports)
                except Exception:  # noqa: BLE001 (keep translating)
                    log.exception("Translating the problem reports failed")
                    done = 0
                # Right away while there are reports waiting, else every half a minute.
                await pause(0.1 if done else 30)

        async def addresser() -> None:
            while not stopping.is_set():
                try:
                    await run_in_threadpool(services.public_address.refresh)
                except Exception:  # noqa: BLE001 (keep checking)
                    log.exception("Checking the public address failed")
                await pause(PUBLIC_ADDRESS_CHECK_S)

        async def updater() -> None:
            while not stopping.is_set():
                try:
                    await run_in_threadpool(services.updates.refresh)
                except Exception:  # noqa: BLE001 (keep checking)
                    log.exception("Updating the list of versions failed")
                await pause(UPDATES_CHECK_S)

        tasks = [asyncio.create_task(sweeper()), asyncio.create_task(flusher())]
        if services.updates.needed:
            tasks.append(asyncio.create_task(updater()))
        if services.translator is not None:
            tasks.append(asyncio.create_task(translator()))
        if services.public_address.needed:
            tasks.append(asyncio.create_task(addresser()))
        try:
            yield
        finally:
            stopping.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await run_in_threadpool(services.flush_presence)
            # The processes that read G-code and the connections to the database end with the server.
            services.close()

    # The interactive documentation lists every route, the administration's and the nodes' too: it is served only to
    # the local network (local_only, below), not by FastAPI to everyone.
    app = FastAPI(title="Tisma Cloud", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(Busy)
    async def busy(_request: Request, ex: Busy) -> Response:
        return JSONResponse({"detail": str(ex)}, status_code=503, headers={"Retry-After": "5"})

    @app.exception_handler(OSError)
    async def disk_trouble(request: Request, ex: OSError) -> Response:
        # A full disk of the master (a project, a scan or a result that does not fit) is said as such, with a time to
        # try again, not as an error of the server; the alert of the disk tells the owner (alerts.py).
        if ex.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            log.error("No room on the master's disk for %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "The server has no room for files right now: try again later"},
                                status_code=507, headers={"Retry-After": "300"})
        raise ex
    app.state.services = services
    db = services.db
    cfg = services.settings

    limiter = services.limiter
    # Problem reports, per hour.
    report_limiter = RateLimiter(3600.)

    admin_networks = parse_networks(cfg.admin_networks)
    site_networks = parse_networks(cfg.site_networks)

    trusted_proxies = parse_networks(cfg.trusted_proxies)
    ip_header = cfg.client_ip_header.encode("latin-1")

    def scope_ip(scope: dict) -> str:
        # Behind a proxy the address of the user is in cfg.client_ip_header: X-Forwarded-For behind Caddy (which replaces
        # what the user sends), CF-Connecting-IP behind a Cloudflare Tunnel. Without it, the address of the connection
        # (the local network reaches port 8000 directly). The header counts only on a connection from a proxy
        # (cfg.trusted_proxies): uvicorn runs without --proxy-headers, so scope["client"] is the real connection.
        client = scope.get("client")
        peer = client[0] if client else "?"
        if not address_in(peer, trusted_proxies):
            return peer
        for k, v in scope.get("headers", []):
            if k == ip_header:
                first = v.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
        return peer

    def client_key(request: Request) -> str:
        return scope_ip(request.scope)

    owners: dict[str, tuple[str, float]] = {}

    def token_owner(kind: str, token: str) -> str | None:
        """The account or node of an upload token (valid ones remembered for a minute)."""
        key = kind + hash_token(token)
        hit = owners.get(key)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        with db.session() as s:
            if kind == "user":
                row = s.execute(select(User.id).join(ApiToken, ApiToken.user_id == User.id)
                                .where(ApiToken.token_hash == hash_token(token), User.enabled.is_(True))).first()
                owner = f"u:{row[0]}" if row else None
            else:
                row = s.execute(select(Node.id).where(Node.token_hash == hash_token(token), Node.enabled.is_(True))).first()
                owner = f"n:{row[0]}" if row else None
        if owner:
            if len(owners) > 10000:
                owners.clear()
            owners[key] = (owner, now + 60.)
        return owner

    app.add_middleware(Guard, settings=cfg, client_ip=scope_ip, is_local=lambda ip: address_in(ip, admin_networks),
                       token_owner=token_owner, run_sync=run_in_threadpool)

    def local_only(request: Request) -> None:
        """The administration exists only for the local network: from internet it answers as an unknown page."""
        if not address_in(client_key(request), admin_networks):
            raise HTTPException(404, "Not Found")

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(local_only)])
    def openapi_schema() -> dict:
        return app.openapi()

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(local_only)])
    def api_docs() -> Response:
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/openapi.json", title="Tisma Cloud API")

    # ---- authentication
    def session_expired(api_token: ApiToken, now: datetime) -> bool:
        """A session not used for session_days is closed: a lost or forgotten device does not stay logged in."""
        return (api_token.last_used or api_token.created_at) < now - timedelta(days=cfg.session_days)

    def current_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User:
        token_hash = hash_token(bearer(authorization))
        expired = False
        with db.session() as s:
            row = s.execute(
                select(ApiToken, User).join(User, User.id == ApiToken.user_id).where(ApiToken.token_hash == token_hash)
            ).one_or_none()
            if row is None or not row[1].enabled:
                raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"})
            api_token, user = row
            now = utcnow()
            if session_expired(api_token, now):
                s.delete(api_token)
                expired = True
            elif api_token.last_used is None or now - api_token.last_used > timedelta(minutes=5):
                api_token.last_used = now
            limit = 10**6 if user.is_unlimited else user.plan.requests_per_minute
        if expired:
            raise HTTPException(401, "The session expired: log in again", headers={"WWW-Authenticate": "Bearer"})
        if not limiter.allow(f"user:{user.id}", limit):
            raise HTTPException(429, "Too many requests: wait a minute", headers={"Retry-After": "60"})
        request.state.token_hash = token_hash
        return user

    def optional_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User | None:
        """The account of the token when one is sent (a wrong one is refused, as everywhere), none without one."""
        return current_user(request, authorization) if authorization else None

    def current_admin(_local: Annotated[None, Depends(local_only)], user: Annotated[User, Depends(current_user)]) -> User:
        if not user.is_admin:
            raise HTTPException(403, "Only for administrators")
        return user

    def current_node(authorization: Annotated[str | None, Header()] = None) -> Node:
        token = bearer(authorization)
        with db.session() as s:
            node = s.execute(select(Node).where(Node.token_hash == hash_token(token))).scalar_one_or_none()
            if node is None:
                raise HTTPException(401, "Invalid node token", headers={"WWW-Authenticate": "Bearer"})
            # A disabled node waits (an unknown token makes the machine ask to join again).
            if not node.enabled:
                raise HTTPException(403, "This node is disabled")
            node.last_seen = utcnow()
        if not limiter.allow(f"node:{node.id}", cfg.node_requests_per_minute):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "10"})
        return node

    def current_site(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        if site_networks and not address_in(client_key(request), site_networks):
            raise HTTPException(404, "Not Found")
        token = bearer(authorization)
        if not cfg.site_key or not hmac.compare_digest(token.encode(), cfg.site_key.encode()):
            raise HTTPException(401, "Invalid site key", headers={"WWW-Authenticate": "Bearer"})

    async def current_remote_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> int:
        """The id of the account, for its remote printers: limited apart from the plan (a bridge asks every few
        seconds). The accounts of the tokens are kept TokenCache.TTL_S in memory: most of these requests do not ask
        the database."""
        token_hash = hash_token(bearer(authorization))
        user_id = services.tokens.get(token_hash)
        if user_id is None:
            def load() -> int | None:
                with db.session() as s:
                    row = s.execute(select(ApiToken, User.enabled).join(User, User.id == ApiToken.user_id)
                                    .where(ApiToken.token_hash == token_hash)).one_or_none()
                    if row is None or not row[1]:
                        return None
                    api_token, now = row[0], utcnow()
                    if session_expired(api_token, now):
                        s.delete(api_token)
                        return None
                    if api_token.last_used is None or now - api_token.last_used > timedelta(minutes=5):
                        api_token.last_used = now
                    return api_token.user_id

            user_id = await run_in_threadpool(load)
            if user_id is None:
                raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"})
            services.tokens.put(token_hash, user_id)
        if not limiter.allow(f"remote:{user_id}", cfg.remote_requests_per_minute):
            raise HTTPException(429, "Too many requests: wait a minute", headers={"Retry-After": "60"})
        return user_id

    UserDep = Annotated[User, Depends(current_user)]
    RemoteDep = Annotated[int, Depends(current_remote_user)]
    AdminDep = Annotated[User, Depends(current_admin)]
    NodeDep = Annotated[Node, Depends(current_node)]
    SiteDep = Annotated[None, Depends(current_site)]

    def inspected(path: Path, kind: str, cleanup, text_gcode: bool = True) -> list:  # noqa: ANN001
        """services.inspect, answered as HTTP errors (cleanup() removes the file when it is refused)."""
        try:
            return services.inspect(path, kind, text_gcode)
        except Threat as ex:
            cleanup()
            raise HTTPException(422, str(ex)) from None
        except ScanUnavailable:
            cleanup()
            raise HTTPException(503, "The file could not be checked for viruses: try again in a few minutes",
                                headers={"Retry-After": "60"}) from None

    def own_job(s, user: User, job_id: str) -> Job:  # noqa: ANN001
        job = s.get(Job, job_id)
        if job is None or job.user_id != user.id:
            raise HTTPException(404, "No such job")
        return job

    # One lock per job whose result is being received, with how many requests use it (forgotten when none does).
    result_locks: dict[str, list] = {}
    result_locks_guard = threading.Lock()

    @contextlib.contextmanager
    def result_lock(job_id: str):  # noqa: ANN202
        with result_locks_guard:
            entry = result_locks.setdefault(job_id, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with result_locks_guard:
                entry[1] -= 1
                if entry[1] == 0:
                    result_locks.pop(job_id, None)

    def node_job(s, node: Node, job_id: str, lock: bool = False) -> Job:  # noqa: ANN001
        # lock: the row is held until the session ends (PostgreSQL; SQLite writes one at a time anyway).
        job = s.get(Job, job_id, with_for_update=lock)
        if job is None or job.node_id != node.id or job.status != "running":
            raise HTTPException(404, "No such running job on this node")
        return job

    # ---- users
    @app.get("/v1/health")
    def health() -> dict:
        public_url = services.public_address.url()
        billing_url = cfg.billing_url or (public_url + "/cuenta" if public_url else "")
        return {"ok": True, "service": "tisma-cloud", "version": __version__, "billing_url": billing_url,
                "payments": bool(provider),
                # The web client ID of "Sign in with Google" (the first one), for the account page.
                "google_client_id": services.google.client_ids[0] if services.google.client_ids else "",
                "play_payments": services.play is not None, "apple_payments": services.apple is not None,
                "microsoft_payments": services.microsoft is not None}

    # The signed list of versions, byte for byte (updates.py): a second address for the programs and the app.
    def update_file(index: int, media_type: str) -> Response:
        pair = services.updates.current()
        if pair is None:
            raise HTTPException(503, "The list of versions is not available yet", headers={"Retry-After": "60"})
        return Response(pair[index], media_type=media_type, headers={"Cache-Control": "no-cache"})

    # ---- the library shared between users (library.py; p2p_library.md): an index and a blocklist, signed; the files
    # themselves never come here.
    def library_file(name: str, signature: bool, request: Request) -> Response:
        if services.library.key.certificate() is None and not cfg.library_uncertified:
            # The apps use only lists of a certified key (p2p_library.md 3 quinquies).
            raise HTTPException(503, "The library key is not certified yet (tisma-cloud admin library-cert)",
                                headers={"Retry-After": "3600"})
        with db.session() as s:
            data, sig, etag = services.library.get(s, name)
        if signature:
            return Response(sig, media_type="application/octet-stream", headers={"Cache-Control": "no-cache"})
        headers = {"Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        # The index of thousands of models is megabytes of JSON: sent compressed to whoever accepts it (the apps undo
        # it before checking the signature, which is of the plain bytes).
        packed = services.library.compressed(name)
        if packed is not None and "gzip" in request.headers.get("accept-encoding", "") and len(packed) < len(data):
            return Response(packed, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
        return Response(data, media_type="application/json", headers=headers)

    @app.get("/v1/library/index.json")
    def library_index(request: Request) -> Response:
        return library_file("index", False, request)

    @app.get("/v1/library/index.json.sig")
    def library_index_signature(request: Request) -> Response:
        return library_file("index", True, request)

    @app.get("/v1/library/blocklist.json")
    def library_blocklist(request: Request) -> Response:
        return library_file("blocklist", False, request)

    @app.get("/v1/library/blocklist.json.sig")
    def library_blocklist_signature(request: Request) -> Response:
        return library_file("blocklist", True, request)

    @app.get("/v1/library/key")
    def library_key() -> dict:
        cert = services.library.key.certificate()
        return {"public_key": library.b64(services.library.key.public),
                "certificate": library.b64(cert) if cert else None}

    def thumbnail_path(sha256: str) -> Path:
        return Path(cfg.storage_dir) / "library" / "thumbs" / f"{sha256}.webp"

    @app.get("/v1/library/thumbs/{name}")
    def library_thumbnail(name: str) -> Response:
        sha256 = name.removesuffix(".webp")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not thumbnail_path(sha256).is_file():
            raise HTTPException(404, "No such image")
        # Only the image of a model still published: a model removed in the review (an offensive image) or by its
        # author is not shown by its address either.
        with db.session() as s:
            used = s.execute(select(LibraryModel.id).where(LibraryModel.thumbnail_sha256 == sha256,
                                                           LibraryModel.removed_at.is_(None)).limit(1)).first()
        if used is None:
            raise HTTPException(404, "No such image")
        return Response(thumbnail_path(sha256).read_bytes(), media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.post("/v1/library/peer-key")
    def library_peer_key(user: UserDep, body: dict = Body(...)) -> dict:
        with db.session() as s:
            try:
                library.bind_peer_key(s, s.get(User, user.id), str(body.get("public_key", "")),
                                      LIBRARY_PEER_KEYS_PER_ACCOUNT)
            except library.LibraryError as ex:
                raise HTTPException(422 if "base64" in str(ex) or "bytes" in str(ex) else 409, str(ex)) from None
        return {"ok": True, "account": library.account_id(user.id)}

    def publish(user: User, body: dict, model_id: str | None) -> dict:
        if not services.daily.allow(f"library-publish:{user.id}", LIBRARY_PUBLISH_PER_DAY):
            raise HTTPException(429, "Too many publications today", headers={"Retry-After": "3600"})
        try:
            pub = library.parse_publication(body)
            thumb = library.unb64(str(body.get("thumbnail_webp", "")))
        except library.LibraryError as ex:
            raise HTTPException(422, str(ex)) from None
        if not library.thumbnail_ok(thumb):
            raise HTTPException(422, f"'thumbnail_webp' is a WebP image of at most {library.MAX_THUMBNAIL_BYTES >> 10} KB")
        # Two revisions of a model at once take the same number (or two first publications create the same counter):
        # the one that loses is tried again with the next number.
        for _attempt in range(5):
            try:
                return publish_once(user, body, model_id, pub, thumb)
            except IntegrityError:
                continue
        raise HTTPException(409, "The model is being changed at the same time: try again", headers={"Retry-After": "2"})

    def publish_once(user: User, body: dict, model_id: str | None, pub: library.Publication, thumb: bytes) -> dict:
        with db.session() as s:
            u = s.get(User, user.id)
            source: AccountFile | None = None
            if body.get("from_file"):
                # Published from a file kept in the account (a scanned part): the files are those, the same bytes.
                source = s.get(AccountFile, str(body["from_file"]))
                if source is None or source.user_id != u.id:
                    raise HTTPException(404, "No such file in the account")
                kept = {(x["path"], x["size"], x["sha256"]) for x in json.loads(source.files)}
                if any((f["path"], f["size"], f["sha256"]) not in kept for f in pub.files):
                    raise HTTPException(422, "The files are not those of the account's file")
            if not library.owns_key(s, u, pub.peer_key):
                raise HTTPException(409, "'peer_key' is not a key of this account (POST /v1/library/peer-key)")
            # The author is the account (spec 967f7a3): one name for all its publications, set the first time.
            if not u.library_name:
                if not pub.author_name:
                    raise HTTPException(422, "'author_name' is needed the first time the account publishes "
                                             "(or PUT /v1/me/library-name)")
                u.library_name = pub.author_name
            if library.blocked(s, "account", library.account_id(u.id)) or library.blocked(s, "peer", pub.peer_key):
                raise HTTPException(403, "This account cannot publish in the library")
            if model_id is not None and library.blocked(s, "model", model_id):
                raise HTTPException(403, "The model was removed from the library")
            refused = [f["path"] for f in pub.files if library.blocked(s, "file", f["sha256"])]
            if refused:
                raise HTTPException(422, f"Removed from the library: {', '.join(refused)}")
            revision = 1
            if model_id is not None:
                last = s.execute(select(LibraryModel).where(LibraryModel.model_id == model_id)
                                 .order_by(LibraryModel.revision.desc())).scalars().first()
                if last is None or last.user_id != u.id:
                    raise HTTPException(404, "No such model")
                if last.removed_at is not None and last.removed_reason != "":
                    raise HTTPException(403, "The model was removed from the library")
                revision = last.revision + 1
            else:
                model_id = "lm_" + uuid.uuid4().hex
            digest = hashlib.sha256(thumb).hexdigest()
            path = thumbnail_path(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(thumb)
            row = LibraryModel(model_id=model_id, revision=revision, user_id=u.id, name=pub.name,
                               description=pub.description, tags=json.dumps(pub.tags, ensure_ascii=False),
                               license=pub.license, author_name=u.library_name, thumbnail_sha256=digest,
                               thumbnail_size=len(thumb), size=sum(f["size"] for f in pub.files),
                               files=json.dumps(pub.files), analysis=json.dumps(pub.analysis),
                               swarm=json.dumps(pub.swarm), peer_key=pub.peer_key,
                               manifest_signature=pub.manifest_signature)
            s.add(row)
            s.flush()
            if source is not None:
                source.published_model = model_id
            library.bump(s, "index")
            return library.model_entry(row)

    # ---- the peers of the library (peers.py): relay tickets, announcements and who shares a model
    relays = [r.strip() for r in cfg.library_relays.split(",") if r.strip()]

    def peer_allowed(s, user: User, peer_key: str, request: Request) -> None:  # noqa: ANN001
        if not library.owns_key(s, user, peer_key):
            raise HTTPException(409, "'peer_key' is not a key of this account (POST /v1/library/peer-key)")
        try:
            address = str(ipaddress.ip_address(client_key(request)))
        except ValueError:
            address = client_key(request)
        if (library.blocked(s, "account", library.account_id(user.id)) or library.blocked(s, "peer", peer_key)
                or library.blocked(s, "address", address)):
            raise HTTPException(403, "Blocked in the library")

    @app.post("/v1/library/relay-ticket")
    def library_relay_ticket(user: UserDep, request: Request, body: dict = Body(...)) -> dict:
        peer_key = str(body.get("peer_key", ""))
        with db.session() as s:
            peer_allowed(s, user, peer_key, request)
            cap = library.settings_view(s)["relay_mb_per_hour_per_account"]
        made = peers.ticket(services.library.key, library.account_id(user.id), library.unb64(peer_key, 32), cap)
        return {**made, "relays": relays}

    @app.post("/v1/library/peers/announce")
    def library_announce(user: UserDep, request: Request, body: dict = Body(...)) -> dict:
        if not limiter.allow(f"library-announce:{user.id}", LIBRARY_ANNOUNCES_PER_MINUTE):
            raise HTTPException(429, "Too many announcements: once every 10 minutes is enough",
                                headers={"Retry-After": "60"})
        peer_key = str(body.get("peer_key", ""))
        models, addrs, direct, when = body.get("models"), body.get("addrs"), body.get("direct"), body.get("time")
        if not (isinstance(models, list) and len(models) <= peers.MAX_MODELS and all(isinstance(m, str) for m in models)
                and isinstance(addrs, list) and len(addrs) <= peers.MAX_ADDRS
                and all(isinstance(a, str) and a.startswith("/") and len(a) <= 300 for a in addrs)
                and isinstance(direct, bool) and isinstance(when, int) and not isinstance(when, bool)):
            raise HTTPException(422, "'models' (ids), 'addrs' (multiaddrs), 'direct' and 'time' (unix seconds)")
        if abs(time.time() - when) > peers.CLOCK_SKEW_S:
            raise HTTPException(422, "'time' is more than 5 minutes off: check the clock")
        try:
            ok = library.verify(library.unb64(peer_key, 32), library.unb64(str(body.get("signature", "")), 64),
                                peers.announcement_bytes(body))
        except library.LibraryError:
            ok = False
        if not ok:
            raise HTTPException(422, "'signature' is not the peer key's signature of the announcement")
        if direct:
            # Only its own address: announcing another machine's would send every downloader to it.
            own = client_key(request)
            for addr in addrs:
                if peers.address_of(addr) != peers.normal_ip(own):
                    raise HTTPException(422, "'addrs' are this app's own public address (/ip4 or /ip6), the one it "
                                             "announces from")
        with db.session() as s:
            peer_allowed(s, user, peer_key, request)
        services.peers.announce(peers.Peer(peer_key=peer_key, account=library.account_id(user.id),
                                           models=frozenset(models), addrs=tuple(addrs) if direct else (), direct=direct))
        return {"ok": True, "again_in_s": peers.ANNOUNCE_TTL_S // 3}

    @app.get("/v1/library/peers")
    def library_peers(user: UserDep, model: str, peer_key: str = "") -> dict:
        asker = services.peers.get(peer_key) if peer_key else None
        asker_direct = bool(asker and asker.direct and asker.account == library.account_id(user.id))
        with db.session() as s:
            found = []
            for p in services.peers.providers(model):
                if p.peer_key == peer_key or library.blocked(s, "peer", p.peer_key) or library.blocked(
                        s, "account", p.account):
                    continue
                addrs = peers.addresses_for(p, asker_direct, relays)
                if addrs:
                    found.append({"peer_id": p.id, "addrs": addrs})
        return {"peers": found}

    @app.post("/v1/library/models", status_code=201)
    def library_publish(user: UserDep, body: dict = Body(...)) -> dict:
        return publish(user, body, None)

    # ---- the files kept in the account (p2p_library.md 7 bis): the part of a scan, for «Mi biblioteca» of Tisma and
    # to publish from it. Private: only its account sees them.
    SCAN_KEPT = ("pieza.stl", "pieza_informe.json")

    def files_dir(file_id: str) -> Path:
        return Path(cfg.storage_dir) / "files" / file_id

    def file_view(f: AccountFile) -> dict:
        return {"id": f.id, "name": f.name, "kind": f.kind, "source_job": f.source_job,
                "created_at": f.created_at.isoformat(timespec="seconds") + "Z", "size": f.size,
                "files": json.loads(f.files), "published_model": f.published_model}

    def own_file(s, user: User, file_id: str) -> AccountFile:  # noqa: ANN001
        f = s.get(AccountFile, file_id)
        if f is None or f.user_id != user.id:
            raise HTTPException(404, "No such file")
        return f

    @app.post("/v1/files", status_code=201)
    def keep_file(user: UserDep, response: Response, body: dict = Body(...)) -> dict:
        job_id = str(body.get("scan_job", "")).strip()
        name = str(body.get("name", "")).strip()[:120]
        with db.session() as s:
            job = s.get(Job, job_id) if job_id else None
            if job is None or job.user_id != user.id or job.kind != "scan":
                raise HTTPException(404, "No such scan job")
            kept = s.execute(select(AccountFile).where(AccountFile.user_id == user.id,
                                                       AccountFile.source_job == job.id)).scalars().first()
            if kept is not None:
                response.status_code = 200
                return file_view(kept)
            if job.status != "done":
                raise HTTPException(409, "The scan is not done")
            result = services.storage.find_result(job.id)
            if result is None or result.suffix != ".zip":
                raise HTTPException(410, f"The result of the scan was removed ({cfg.retention_h} h after it ended)")
            used = s.execute(select(func.coalesce(func.sum(AccountFile.size), 0))
                             .where(AccountFile.user_id == user.id)).scalar_one()
            file_id = "af_" + uuid.uuid4().hex
            folder = files_dir(file_id)
            listed: list[dict] = []
            try:
                with zipfile.ZipFile(result) as z:
                    names = {Path(n).name: n for n in z.namelist() if Path(n).name in SCAN_KEPT}
                    if "pieza.stl" not in names:
                        raise HTTPException(410, "The result of the scan has no part")
                    sizes = sum(z.getinfo(n).file_size for n in names.values())
                    if used + sizes > cfg.account_files_gb << 30:
                        raise HTTPException(413, f"The files of the account are over {cfg.account_files_gb} GB: "
                                                 "delete some first")
                    folder.mkdir(parents=True, exist_ok=True)
                    for kept_name in SCAN_KEPT:
                        if kept_name not in names:
                            continue
                        digest = hashlib.sha256()
                        size = 0
                        with z.open(names[kept_name]) as src, open(folder / kept_name, "wb") as out:
                            for chunk in iter(lambda: src.read(1 << 20), b""):
                                digest.update(chunk)
                                size += len(chunk)
                                out.write(chunk)
                        listed.append({"path": kept_name, "size": size, "sha256": digest.hexdigest()})
            except (zipfile.BadZipFile, OSError):
                shutil.rmtree(folder, ignore_errors=True)
                raise
            except HTTPException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            item = AccountFile(id=file_id, user_id=user.id, name=name or (job.input_name or "pieza").rsplit(".", 1)[0],
                               kind="scan", source_job=job.id, size=sum(f["size"] for f in listed),
                               files=json.dumps(listed))
            source_job = job.id
        try:
            with db.session() as s:
                s.add(item)
                s.flush()
                return file_view(item)
        except IntegrityError:
            # Another request kept the same job meanwhile: that one is the item.
            shutil.rmtree(folder, ignore_errors=True)
            with db.session() as s:
                kept = s.execute(select(AccountFile).where(AccountFile.user_id == user.id,
                                                           AccountFile.source_job == source_job)).scalars().first()
                if kept is None:
                    raise
                response.status_code = 200
                return file_view(kept)

    @app.get("/v1/files")
    def list_files(user: UserDep) -> dict:
        with db.session() as s:
            rows = s.execute(select(AccountFile).where(AccountFile.user_id == user.id)
                             .order_by(AccountFile.created_at.desc())).scalars().all()
            return {"files": [file_view(f) for f in rows]}

    @app.get("/v1/files/{file_id}/{path}")
    def get_file(user: UserDep, file_id: str, path: str) -> Response:
        with db.session() as s:
            f = own_file(s, user, file_id)
            if path not in {x["path"] for x in json.loads(f.files)}:
                raise HTTPException(404, "No such file")
            name = f.name
        target = files_dir(file_id) / path
        if not target.is_file():
            raise HTTPException(404, "No such file")
        download = safe_filename(f"{name}{Path(path).suffix}" if path == "pieza.stl" else path, path)
        return FileResponse(target, media_type="application/octet-stream", filename=download)

    @app.patch("/v1/files/{file_id}")
    def rename_file(user: UserDep, file_id: str, body: dict = Body(...)) -> dict:
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 120:
            raise HTTPException(422, "'name' from 1 to 120 characters")
        with db.session() as s:
            f = own_file(s, user, file_id)
            f.name = name
            return file_view(f)

    # The same parts as the Tisma apps read them for «Mi biblioteca» (Android's contract, 2026-11-10): the scans kept
    # with «Imprimir» that this device has not taken yet. Each device takes them once (the phone and the computer both).
    def scan_entry(f: AccountFile) -> dict | None:
        mesh = next((x for x in json.loads(f.files) if x["path"] == "pieza.stl"), None)
        if mesh is None:
            return None
        return {"id": f.id, "name": f.name, "file": safe_filename(f"{f.name}.stl", "pieza.stl"), "size": mesh["size"],
                "sha256": mesh["sha256"], "created": f.created_at.isoformat(timespec="seconds") + "Z"}

    @app.get("/v1/me/scans")
    def my_scans(user: UserDep, device: str = "") -> dict:
        device = device.strip()[:100]
        with db.session() as s:
            query = select(AccountFile).where(AccountFile.user_id == user.id, AccountFile.kind == "scan")
            if device:
                taken = select(AccountFileTaken.file_id).where(AccountFileTaken.device == device)
                query = query.where(AccountFile.id.not_in(taken))
            rows = s.execute(query.order_by(AccountFile.created_at.desc())).scalars().all()
            return {"scans": [e for e in (scan_entry(f) for f in rows) if e]}

    @app.get("/v1/me/scans/{file_id}/mesh")
    def my_scan_mesh(user: UserDep, file_id: str) -> Response:
        return get_file(user, file_id, "pieza.stl")

    @app.post("/v1/me/scans/{file_id}/taken")
    def my_scan_taken(user: UserDep, file_id: str, body: dict = Body(...)) -> dict:
        device = str(body.get("device", "")).strip()[:100]
        if not device:
            raise HTTPException(422, "'device' is needed")
        with db.session() as s:
            own_file(s, user, file_id)
            exists = s.execute(select(AccountFileTaken).where(AccountFileTaken.file_id == file_id,
                                                              AccountFileTaken.device == device)).scalars().first()
            if exists is None:
                s.add(AccountFileTaken(file_id=file_id, device=device))
        return {"ok": True}

    @app.delete("/v1/files/{file_id}")
    def delete_file(user: UserDep, file_id: str) -> dict:
        with db.session() as s:
            s.execute(delete(AccountFileTaken).where(AccountFileTaken.file_id == own_file(s, user, file_id).id))
            s.delete(own_file(s, user, file_id))
        shutil.rmtree(files_dir(file_id), ignore_errors=True)
        return {"ok": True}

    @app.post("/v1/library/models/{model_id}/revisions", status_code=201)
    def library_revision(user: UserDep, model_id: str, body: dict = Body(...)) -> dict:
        return publish(user, body, model_id)

    @app.get("/v1/library/mine")
    def library_mine(user: UserDep) -> dict:
        with db.session() as s:
            rows = s.execute(select(LibraryModel).where(LibraryModel.user_id == user.id)
                             .order_by(LibraryModel.published_at.desc())).scalars().all()
            return {"models": [{**library.model_entry(m), "removed": m.removed_at is not None,
                                "removed_reason": m.removed_reason} for m in rows]}

    @app.delete("/v1/library/models/{model_id}")
    def library_unpublish(user: UserDep, model_id: str) -> dict:
        with db.session() as s:
            rows = s.execute(select(LibraryModel).where(LibraryModel.model_id == model_id,
                                                        LibraryModel.user_id == user.id)).scalars().all()
            if not rows:
                raise HTTPException(404, "No such model")
            for m in rows:
                if m.removed_at is None:
                    m.removed_at = utcnow()
            library.bump(s, "index")
        return {"ok": True}

    @app.post("/v1/library/reports", status_code=201)
    def library_report(user: UserDep, body: dict = Body(...)) -> dict:
        model_id, sha256 = str(body.get("model", "")).strip(), str(body.get("sha256", "")).strip().lower()
        reason = str(body.get("reason", "")).strip()
        if not reason or len(reason) > 2000:
            raise HTTPException(422, "'reason' is needed (at most 2000 characters)")
        if not model_id and not sha256:
            raise HTTPException(422, "'model' or 'sha256' is needed")
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise HTTPException(422, "'sha256' is 64 hexadecimal digits")
        if not services.daily.allow(f"library-report:{user.id}", LIBRARY_REPORTS_PER_DAY):
            raise HTTPException(429, "Too many reports today", headers={"Retry-After": "3600"})
        with db.session() as s:
            query = select(LibraryModel)
            query = query.where(LibraryModel.model_id == model_id) if model_id else query.where(
                LibraryModel.files.contains(sha256 or "-"))
            found = s.execute(query.order_by(LibraryModel.revision.desc())).scalars().first()
            if found is None or (sha256 and sha256 not in found.files):
                raise HTTPException(404, "No such model or file in the library")
            s.add(LibraryReport(user_id=user.id, model_id=found.model_id, sha256=sha256, reason=reason))
        return {"ok": True}

    @app.get("/v1/updates")
    def updates_list() -> Response:
        return update_file(0, "application/json")

    @app.get("/v1/updates.sig")
    def updates_signature() -> Response:
        return update_file(1, "application/octet-stream")

    @app.post("/v1/auth/login")
    def login(request: Request, body: LoginBody) -> dict:
        email = body.email.strip().lower()
        if not (limiter.allow(f"login-ip:{client_key(request)}", cfg.login_per_minute)
                and limiter.allow(f"login-email:{email}", cfg.login_per_minute)):
            raise HTTPException(429, "Too many login attempts: wait a minute", headers={"Retry-After": "60"})
        # Guessing a password from many network addresses: at most LOGIN_FAILURES_PER_HOUR wrong passwords an hour
        # for one account. Right ones never count, and a new password by e-mail code still works.
        if services.hourly.used(f"login-fail:{email}") >= LOGIN_FAILURES_PER_HOUR:
            raise HTTPException(429, "Too many wrong passwords for this account: wait an hour, or choose a new "
                                     "password with a code by e-mail", headers={"Retry-After": "3600"})
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            stored = user.password_hash if user and user.password_hash else DUMMY_HASH
            ok = verify_password(body.password, stored)
            if ok and needs_rehash(stored):
                # Made with weaker parameters: made again now that the password is known.
                user.password_hash = hash_password(body.password)
            elif not ok and needs_rehash(stored):
                # As long as a failed login on an unknown e-mail, so that the time does not tell the account exists.
                verify_password(body.password, DUMMY_HASH)
            if user is None or not ok or not user.enabled:
                services.hourly.allow(f"login-fail:{email}", LOGIN_FAILURES_PER_HOUR + 1)
                raise HTTPException(401, "Wrong e-mail or password")
            if not user.email_verified:
                raise HTTPException(403, "E-mail not verified: enter the code we sent to it")
            return {"token": open_session(s, user, body.device), "email": user.email}

    @app.post("/v1/auth/logout")
    def logout(user: UserDep, request: Request) -> dict:
        with db.session() as s:
            s.execute(ApiToken.__table__.delete().where(ApiToken.token_hash == request.state.token_hash))
        services.tokens.drop(request.state.token_hash)
        return {"ok": True}

    # ---- the account's own data: sessions, password, a copy of everything, deletion
    @app.get("/v1/me/sessions")
    def my_sessions(user: UserDep, request: Request) -> dict:
        """The devices logged in to the account (to close a lost one)."""
        with db.session() as s:
            rows = s.execute(select(ApiToken).where(ApiToken.user_id == user.id)
                             .order_by(ApiToken.created_at.desc())).scalars().all()
            return {"sessions": [{"id": t.id, "device": t.label, "created_at": t.created_at.isoformat() + "Z",
                                  "last_used": t.last_used.isoformat() + "Z" if t.last_used else None,
                                  "current": t.token_hash == request.state.token_hash} for t in rows]}

    @app.delete("/v1/me/sessions/{session_id}")
    def close_session(user: UserDep, session_id: int) -> dict:
        with db.session() as s:
            token = s.get(ApiToken, session_id)
            if token is None or token.user_id != user.id:
                raise HTTPException(404, "No such session")
            services.tokens.drop(token.token_hash)
            s.delete(token)
        return {"ok": True}

    @app.post("/v1/me/sessions/close-others")
    def close_other_sessions(user: UserDep, request: Request) -> dict:
        with db.session() as s:
            closed = s.execute(delete(ApiToken).where(ApiToken.user_id == user.id,
                                                      ApiToken.token_hash != request.state.token_hash)).rowcount
        services.tokens.drop_user(user.id)
        return {"closed": closed}

    @app.post("/v1/me/password")
    def change_password(user: UserDep, request: Request, body: PasswordBody) -> dict:
        """A new password, knowing the current one; the other devices are logged out."""
        if not limiter.allow(f"password:{user.id}", cfg.login_per_minute):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        with db.session() as s:
            u = s.get(User, user.id)
            if not u.password_hash:
                raise HTTPException(409, "The account has no password: choose one with \"Forgot my password\"")
            ok = verify_password(body.current_password, u.password_hash)
        if not ok:
            raise HTTPException(403, "Wrong password")
        new_hash = hash_password(body.new_password)
        with db.session() as s:
            s.get(User, user.id).password_hash = new_hash
            s.execute(delete(ApiToken).where(ApiToken.user_id == user.id,
                                             ApiToken.token_hash != request.state.token_hash))
        services.tokens.drop_user(user.id)
        return {"ok": True}

    @app.get("/v1/me/export")
    def export_my_data(user: UserDep) -> Response:
        """Everything the service keeps about the account, as JSON (the projects and results are not kept)."""
        with db.session() as s:
            u = s.get(User, user.id)
            data = {
                "account": {"email": u.email, "created_at": u.created_at.isoformat() + "Z", "plan": u.plan_name,
                            "paid_until": u.paid_until.isoformat() + "Z" if u.paid_until else None,
                            "store_until": u.store_until.isoformat() + "Z" if u.store_until else None,
                            "email_verified": u.email_verified, "google_linked": u.google_sub is not None,
                            "has_password": bool(u.password_hash), "extra_printers": u.extra_printers,
                            "library_name": u.library_name},
                "sessions": [{"device": t.label, "created_at": t.created_at.isoformat() + "Z",
                              "last_used": t.last_used.isoformat() + "Z" if t.last_used else None}
                             for t in s.execute(select(ApiToken).where(ApiToken.user_id == u.id)).scalars()],
                "payments": [payment_view(p) for p in
                             s.execute(select(Payment).where(Payment.user_id == u.id)).scalars()],
                "store_subscriptions": [
                    {"store": stores.STORES[table], "product": p.product_id, "plan": p.plan_name,
                     "state": getattr(p, "state", "revoked" if getattr(p, "revoked", False) else ""),
                     "order_id": getattr(p, "order_id", None) or getattr(p, "original_transaction_id", None)
                     or getattr(p, "subscription_id", None),
                     "until": p.entitled_until.isoformat() + "Z" if p.entitled_until else None}
                    for table in stores.STORES
                    for p in s.execute(select(table).where(table.user_id == u.id)).scalars()],
                "hour_adjustments": [adjustment_view(a) for a in
                                     s.execute(select(HourAdjustment).where(HourAdjustment.user_id == u.id)).scalars()],
                "scans_being_sent": [{"name": x.name, "size": x.size, "received": x.received,
                                      "created_at": x.created_at.isoformat() + "Z"}
                                     for x in s.execute(select(ScanUpload).where(ScanUpload.user_id == u.id,
                                                                                 ScanUpload.job_id.is_(None))).scalars()],
                "account_files": [file_view(x) for x in
                                  s.execute(select(AccountFile).where(AccountFile.user_id == u.id)).scalars()],
                "library": {
                    "publications": [{**library.model_entry(m), "removed": m.removed_at is not None}
                                     for m in s.execute(select(LibraryModel).where(LibraryModel.user_id == u.id))
                                     .scalars()],
                    "peer_keys": [{"public_key": k.public_key, "created_at": k.created_at.isoformat() + "Z"}
                                  for k in s.execute(select(LibraryPeerKey).where(LibraryPeerKey.user_id == u.id))
                                  .scalars()],
                    "reports": [{"model": r.model_id, "sha256": r.sha256, "reason": r.reason,
                                 "created_at": r.created_at.isoformat() + "Z", "outcome": r.outcome}
                                for r in s.execute(select(LibraryReport).where(LibraryReport.user_id == u.id))
                                .scalars()],
                },
                "usage": [{"month": x.month, "seconds": round(x.seconds, 1)}
                          for x in s.execute(select(Usage).where(Usage.user_id == u.id)).scalars()],
                "jobs": [job_view(j) for j in s.execute(select(Job).where(Job.user_id == u.id)).scalars()],
                "bridges": [{"id": b.id, "name": b.name, "printers": b.printer_list,
                             "created_at": b.created_at.isoformat() + "Z",
                             "last_seen": b.last_seen.isoformat() + "Z" if b.last_seen else None}
                            for b in s.execute(select(Bridge).where(Bridge.user_id == u.id)).scalars()],
                "problem_reports": [report_view(r, texts=True) for r in
                                    s.execute(select(BugReport).where(BugReport.user_id == u.id)).scalars()],
            }
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="tisma-cloud-mis-datos.json"',
                                           "Cache-Control": "no-store"})

    @app.post("/v1/me/delete/code")
    def delete_account_code(user: UserDep, request: Request) -> dict:
        """A code by e-mail to delete the account (for the accounts without a password)."""
        if not mail_allowed(request, user.email):
            raise HTTPException(429, "Wait a minute before asking another code", headers={"Retry-After": "60"})
        with db.session() as s:
            code = codes.issue(s, s.get(User, user.id), "delete")
        send_code(user.email, "delete", code)
        return {"ok": True}

    @app.post("/v1/me/delete")
    def delete_account(user: UserDep, request: Request, body: DeleteAccountBody) -> dict:
        """Deletes the account and its data: sessions, jobs and their files, bridges, commands, store purchases,
        usage, problem reports. Only the payments stay, without anything personal (the account is left as "deleted-<id>"), for the
        accounting of the service."""
        if not code_attempt_allowed(request, user.email):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        with db.session() as s:
            u = s.get(User, user.id)
            if u.is_admin:
                raise HTTPException(409, "An administrator account is removed by another administrator")
            if body.code:
                ok = codes.check(s, u, "delete", body.code)
            else:
                ok = bool(u.password_hash and body.password) and verify_password(body.password, u.password_hash)
        if not ok:
            raise HTTPException(403, "Wrong password or code")
        services.delete_account(user.id)
        return {"ok": True}

    @app.get("/v1/me")
    def me(user: UserDep) -> dict:
        with db.session() as s:
            u = s.get(User, user.id)
            acc = scheduler.account(s, u, cfg.grace_days)
            return {"email": u.email, **acc.as_dict(), "remote_printers": remote.printer_allowance(u, acc.paid),
                    "extra_printers": u.extra_printers, "extra_printer_cents": u.plan.extra_printer_cents,
                    "has_password": bool(u.password_hash), "library_name": u.library_name,
                    "subscription": stores.subscription_view(s, u),
                    # What the app passes to its store with a purchase (cloud.md 5c): obfuscatedAccountId of Google
                    # Play, appAccountToken of StoreKit (a UUID); random, nothing personal in it.
                    "purchase_account_token": apple.apple_account(s, u)}

    @app.put("/v1/me/library-name")
    def set_library_name(user: UserDep, body: dict = Body(...)) -> dict:
        """The name the account shows as author in the library (its publications, the old ones too)."""
        try:
            name = library._text(body, "name", 60)
        except library.LibraryError as ex:
            raise HTTPException(422, str(ex)) from None
        # Each rename rebuilds the signed index every app downloads.
        if not services.daily.allow(f"library-rename:{user.id}", LIBRARY_RENAMES_PER_DAY):
            raise HTTPException(429, "Too many name changes today", headers={"Retry-After": "3600"})
        with db.session() as s:
            u = s.get(User, user.id)
            u.library_name = name
            s.execute(update(LibraryModel).where(LibraryModel.user_id == u.id).values(author_name=name))
            library.bump(s, "index")
        return {"library_name": name}

    # ---- problem reports (reports.py)
    @app.post("/v1/reports", status_code=201)
    def send_report(request: Request, body: ReportBody, user: Annotated[User | None, Depends(optional_user)]) -> dict:
        """A problem reported from Tisma, the app or the web site, with or without an account. What could identify the
        person is removed from the texts and the log before they are kept; the address is not kept."""
        if user is not None:
            allowed = report_limiter.allow(f"user:{user.id}", cfg.reports_per_hour)
        else:
            allowed = report_limiter.allow(f"ip:{client_key(request)}", cfg.anonymous_reports_per_hour)
        if not allowed:
            raise HTTPException(429, "Too many reports: try again in an hour", headers={"Retry-After": "3600"})
        fields = reports.clean(**body.model_dump())
        with db.session() as s:
            report = BugReport(user_id=user.id if user is not None else None, **fields)
            s.add(report)
            s.flush()
            similar = s.execute(select(func.count()).select_from(BugReport)
                                .where(BugReport.signature == report.signature, BugReport.id != report.id)).scalar()
            return {"id": report.id, "signature": report.signature, "similar": similar}

    @app.get("/v1/me/reports")
    def my_reports(user: UserDep) -> dict:
        """The problems reported with the account and their state."""
        with db.session() as s:
            rows = s.execute(select(BugReport).where(BugReport.user_id == user.id)
                             .order_by(BugReport.created_at.desc()).limit(100)).scalars().all()
            return {"reports": [report_view(r) for r in rows]}

    @app.get("/v1/plans")
    def plans() -> dict:
        """The plans users can choose (the account page)."""
        with db.session() as s:
            rows = s.execute(select(Plan).where(Plan.public.is_(True)).order_by(Plan.price_cents)).scalars().all()
            # The product of each store that sells the plan (cloud.md 5c); play_product stays for the older apps.
            sellers = {"google_play": services.play, "app_store": services.apple, "microsoft_store": services.microsoft}
            by_plan: dict[str, dict[str, str]] = {}
            for name, store in sellers.items():
                for product, plan in (store.products.items() if store else ()):
                    by_plan.setdefault(plan, {}).setdefault(name, product)
            return {"plans": [{**plan_view(p), "store_products": by_plan.get(p.name, {}),
                               "play_product": by_plan.get(p.name, {}).get("google_play")} for p in rows],
                    "classes": JOB_CLASS_DESCRIPTIONS}

    # ---- accounts and payments on the master
    # Paying on the web site only when turned on (TISMA_WEB_PAYMENTS): the plans are sold through the app stores.
    provider = billing.provider_for(cfg.billing_provider, cfg.public_url) if cfg.web_payments else None

    @app.post("/v1/auth/register", status_code=201)
    def register(request: Request, body: RegisterBody) -> dict:
        """The same answer whether the address has an account or not (the page must not tell who is a user): a new
        account gets its code, an existing one an e-mail saying so."""
        email = body.email.strip().lower()
        if not limiter.allow(f"register-ip:{client_key(request)}", cfg.login_per_minute):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        if not EMAIL.match(email):
            raise HTTPException(422, "Not an e-mail address")
        # Hashed in both cases, so that the time does not tell either.
        password_hash = hash_password(body.password)
        with db.session() as s:
            if s.execute(select(User.id).where(User.email_index == crypto.index(email))).first():
                code = None
            else:
                code = new_account(s, email, body.plan, password_hash)
        if code is None:
            # At most as often as the codes (silently: a refusal would tell the address has an account).
            if mail_allowed(request, email):
                subject, text = exists_message()
                try:
                    services.mailer.send(email, subject, text)
                except Exception:  # noqa: BLE001 (nothing to tell the visitor: the answer is the same)
                    log.exception("Could not send the existing account notice")
        else:
            send_code(email, "verify", code)
        return {"verification_required": True, "email": email}

    def new_account(s, email: str, plan_name: str | None, password_hash: str) -> str:  # noqa: ANN001
        """A self-registered account (not verified nor paid yet: the code sent to the address confirms it; then the
        account can pay). Returns the code."""
        plan = s.get(Plan, plan_name) if plan_name else None
        if plan is None or not plan.public:
            plan = s.execute(select(Plan).where(Plan.public.is_(True)).order_by(Plan.price_cents)).scalars().first() \
                or s.get(Plan, "trial")
        user = User(email=email, plan_name=plan.name, password_hash=password_hash,
                    email_verified=False, unlimited=cfg.is_internal_email(email))
        s.add(user)
        s.flush()
        return codes.issue(s, user, "verify")

    def open_session(s, user: User, device: str) -> str:  # noqa: ANN001
        token = new_token("tu")
        s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label=device[:100]))
        return token

    def send_code(email: str, purpose: str, code: str) -> None:
        subject, text = code_message(purpose, code)
        try:
            services.mailer.send(email, subject, text)
        except Exception:  # noqa: BLE001 (the account exists; the user can ask the code again)
            log.exception("Could not send the %s code to %s", purpose, email)
            raise HTTPException(503, "The e-mail could not be sent: try again in a few minutes") from None

    def mail_allowed(request: Request, email: str) -> bool:
        """At most one code per minute, 5 per hour and CODES_PER_DAY per day to an address, and 20 per hour from a
        network address. With 5 attempts per code, at most 50 guesses of a 6-digit code a day."""
        return (limiter.allow(f"mail-min:{email}", 1) and services.hourly.allow(f"mail-hour:{email}", 5)
                and services.daily.allow(f"mail-day:{email}", CODES_PER_DAY)
                and services.hourly.allow(f"mail-ip:{client_key(request)}", 20))

    def code_attempt_allowed(request: Request, email: str) -> bool:
        return (limiter.allow(f"code-ip:{client_key(request)}", cfg.login_per_minute)
                and limiter.allow(f"code-email:{email}", cfg.login_per_minute))

    @app.post("/v1/auth/verify")
    def verify_email(request: Request, body: CodeBody) -> dict:
        email = body.email.strip().lower()
        if not code_attempt_allowed(request, email):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        # The wrong attempt is saved before the answer (an exception inside the session would undo it).
        token = None
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if user is not None and user.enabled and codes.check(s, user, "verify", body.code):
                user.email_verified = True
                token = open_session(s, user, body.device)
        if token is None:
            raise HTTPException(400, "Wrong or expired code")
        return {"token": token, "email": email}

    @app.post("/v1/auth/resend")
    def resend_code(request: Request, body: EmailBody) -> dict:
        """A new confirmation code. The answer is the same whether the address has an account or not."""
        email = body.email.strip().lower()
        if not mail_allowed(request, email):
            raise HTTPException(429, "Wait a minute before asking another code", headers={"Retry-After": "60"})
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            code = codes.issue(s, user, "verify") if user is not None and user.enabled and not user.email_verified else None
        if code:
            send_code(email, "verify", code)
        return {"ok": True}

    @app.post("/v1/auth/password/forgot")
    def forgot_password(request: Request, body: EmailBody) -> dict:
        """A code to choose a new password. The answer is the same whether the address has an account or not."""
        email = body.email.strip().lower()
        if not mail_allowed(request, email):
            raise HTTPException(429, "Wait a minute before asking another code", headers={"Retry-After": "60"})
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            code = codes.issue(s, user, "reset") if user is not None and user.enabled else None
        if code:
            send_code(email, "reset", code)
        return {"ok": True}

    @app.post("/v1/auth/password/reset")
    def reset_password(request: Request, body: ResetBody) -> dict:
        email = body.email.strip().lower()
        if not code_attempt_allowed(request, email):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        token = None
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if user is not None and user.enabled and codes.check(s, user, "reset", body.code):
                user.password_hash = hash_password(body.password)
                # The code reached the inbox: the address is theirs. The sessions of the devices are closed.
                user.email_verified = True
                s.execute(ApiToken.__table__.delete().where(ApiToken.user_id == user.id))
                services.tokens.drop_user(user.id)
                token = open_session(s, user, body.device)
        if token is None:
            raise HTTPException(400, "Wrong or expired code")
        return {"token": token, "email": email}

    @app.post("/v1/auth/google")
    def google_login(request: Request, body: GoogleBody) -> dict:
        if not limiter.allow(f"google-ip:{client_key(request)}", cfg.login_per_minute):
            raise HTTPException(429, "Too many attempts: wait a minute", headers={"Retry-After": "60"})
        try:
            who = services.google.verify(body.id_token)
        except GoogleError as ex:
            raise HTTPException(401, str(ex)) from None
        code = None
        with db.session() as s:
            user = s.execute(select(User).where(User.google_sub == who.sub)).scalar_one_or_none()
            if user is None:
                user = s.execute(select(User).where(User.email_index == crypto.index(who.email))).scalar_one_or_none()
                if user is not None:
                    # Linking needs Google to be the authority on the address, or the account could be taken.
                    if not who.authoritative or user.google_sub:
                        raise HTTPException(409, "An account with this e-mail exists: log in with its password")
                    if not user.email_verified:
                        # Its password was never confirmed by the owner of the address: anyone could have registered
                        # it first («pre-account hijacking»). It does not open the account any more, nor its sessions.
                        user.password_hash = ""
                        s.execute(ApiToken.__table__.delete().where(ApiToken.user_id == user.id))
                        services.tokens.drop_user(user.id)
                    user.google_sub = who.sub
                    user.email_verified = True
                else:
                    plan = s.execute(select(Plan).where(Plan.public.is_(True)).order_by(Plan.price_cents)).scalars().first() \
                        or s.get(Plan, "trial")
                    user = User(email=who.email, plan_name=plan.name, google_sub=who.sub,
                                email_verified=who.authoritative, unlimited=cfg.is_internal_email(who.email))
                    s.add(user)
                    s.flush()
            if not user.enabled:
                raise HTTPException(401, "The account is disabled")
            if not user.email_verified:
                code = codes.issue(s, user, "verify")
                email = user.email
            else:
                return {"token": open_session(s, user, body.device), "email": user.email}
        # An address Google does not vouch for (not Gmail nor Workspace): confirmed with our code like any other.
        send_code(email, "verify", code)
        return {"verification_required": True, "email": email}

    @app.post("/v1/billing/checkout")
    def checkout(user: UserDep, body: CheckoutBody) -> dict:
        if provider is None:
            raise HTTPException(503, "Online payments are not open yet")
        with db.session() as s:
            plan = s.get(Plan, body.plan)
            if plan is None or not plan.public or plan.price_cents <= 0:
                raise HTTPException(422, "No such plan")
            u = s.get(User, user.id)
            try:
                payment = billing.start_payment(s, u, plan, provider.name, body.months, body.extra_printers)
            except billing.BillingError as ex:
                raise HTTPException(422, str(ex)) from None
            return_url = services.public_address.url() + "/cuenta"
            return {"payment": payment_view(payment), "url": provider.checkout(payment, plan, u, return_url)}

    @app.get("/v1/billing/payments")
    def my_payments(user: UserDep) -> dict:
        with db.session() as s:
            rows = s.execute(select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc())
                             .limit(50)).scalars().all()
            return {"payments": [payment_view(p) for p in rows]}

    @app.post("/v1/billing/webhook/{name}")
    async def webhook(name: str, request: Request) -> dict:
        if provider is None or name != provider.name:
            raise HTTPException(404, "Not Found")
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(413, "Too large")
        try:
            notices = provider.notices({k.lower(): v for k, v in request.headers.items()}, dict(request.query_params), body)
        except billing.BillingError as ex:
            raise HTTPException(400, str(ex)) from None

        def apply_all() -> None:
            with db.session() as s:
                for n in notices:
                    billing.apply(s, n, provider.name)
        try:
            await run_in_threadpool(apply_all)
        except billing.BillingError as ex:
            log.warning("Payment notice refused: %s", ex)
            raise HTTPException(400, str(ex)) from None
        return {"ok": True}

    # ---- Google Play subscriptions (phone app)
    def play_store() -> google_play.Store:
        if services.play is None:
            raise HTTPException(503, "Google Play payments are not enabled")
        return services.play

    @app.get("/v1/billing/play/account")
    def play_account(user: UserDep) -> dict:
        """The obfuscatedAccountId the app passes to Google Play with each purchase."""
        play_store()
        with db.session() as s:
            return {"account_id": google_play.play_account(s, s.get(User, user.id))}

    def play_once_more(apply):  # noqa: ANN001, ANN202
        """The app and Google's notification report a new purchase at about the same time, and both make its row: the
        one that loses (the same purchase token, unique) applies it again over the row the other made."""
        try:
            return apply()
        except IntegrityError:
            return apply()

    # ---- App Store subscriptions (iPhone and Mac; apple.py)
    class ApplePurchaseBody(BaseModel):
        signed_transaction: str = Field(min_length=10, max_length=20000)

    def apple_store() -> apple.Store:
        if services.apple is None:
            raise HTTPException(404, "App Store payments are not set up")
        return services.apple

    @app.get("/v1/billing/apple/account")
    def apple_account(user: UserDep) -> dict:
        """The appAccountToken the app passes to StoreKit with each purchase (a random UUID of the account)."""
        apple_store()
        with db.session() as s:
            return {"app_account_token": apple.apple_account(s, s.get(User, user.id))}

    @app.post("/v1/billing/apple/purchase")
    def apple_purchase(user: UserDep, body: ApplePurchaseBody) -> dict:
        """A purchase made in the app: Apple's signed transaction (Transaction.jwsRepresentation), applied."""
        store = apple_store()

        def link() -> dict:
            with db.session() as s:
                u = s.get(User, user.id)
                sub = store.link(s, u, body.signed_transaction)
                return {"plan": sub.plan_name, "revoked": sub.revoked,
                        **scheduler.account(s, u, cfg.grace_days).as_dict()}

        try:
            return play_once_more(link)
        except apple.AppleError as ex:
            raise HTTPException(422, str(ex)) from None

    # ---- Microsoft Store subscriptions (Windows; microsoft.py)
    class MicrosoftPurchaseBody(BaseModel):
        purchase_key: str = Field(min_length=20, max_length=20000)

    def microsoft_store() -> microsoft.Store:
        if services.microsoft is None:
            raise HTTPException(404, "Microsoft Store payments are not set up")
        return services.microsoft

    @app.get("/v1/billing/microsoft/ticket")
    def microsoft_ticket(user: UserDep) -> dict:
        """What the app passes to StoreContext.GetCustomerPurchaseIdAsync: the service ticket and the account's
        publisherUserId (a random id)."""
        store = microsoft_store()
        try:
            ticket = store.service_ticket()
        except microsoft.MicrosoftUnavailable as ex:
            log.warning("Microsoft Store ticket: %s", ex)
            raise HTTPException(503, "The Microsoft Store cannot be reached now", headers={"Retry-After": "60"}) from None
        with db.session() as s:
            return {**ticket, "publisher_user_id": microsoft.microsoft_account(s, s.get(User, user.id))}

    @app.post("/v1/billing/microsoft/purchase")
    def microsoft_purchase(user: UserDep, body: MicrosoftPurchaseBody) -> dict:
        """The Microsoft Store ID key the app made after a purchase (or at start): its subscriptions, applied."""
        store = microsoft_store()

        def link() -> dict:
            with db.session() as s:
                u = s.get(User, user.id)
                subs = store.link(s, u, body.purchase_key)
                return {"subscriptions": [{"plan": x.plan_name, "state": x.state} for x in subs],
                        **scheduler.account(s, u, cfg.grace_days).as_dict()}

        try:
            return play_once_more(link)
        except microsoft.MicrosoftError as ex:
            raise HTTPException(422, str(ex)) from None
        except microsoft.MicrosoftUnavailable as ex:
            log.warning("Microsoft Store: %s", ex)
            raise HTTPException(503, "The Microsoft Store cannot be reached now", headers={"Retry-After": "60"}) from None

    # ---- the purchase of any store (cloud.md 5c)
    class PurchaseBody(BaseModel):
        store: str = Field(pattern="^(google_play|app_store|microsoft_store)$")
        product_id: str = Field(min_length=1, max_length=200)
        purchase_token: str | None = Field(default=None, min_length=1, max_length=2000)
        signed_transaction: str | None = Field(default=None, min_length=10, max_length=20000)
        store_id_key: str | None = Field(default=None, min_length=20, max_length=20000)

    NOT_CONFIRMED = "The store did not confirm this purchase"

    @app.post("/v1/me/purchases")
    def purchase(user: UserDep, body: PurchaseBody) -> dict:
        """A purchase made in the app with its store: verified with the store, then the account as GET /v1/me. The
        same purchase sent again gives the same answer; one of another account is refused (409)."""
        proof = {"google_play": body.purchase_token, "app_store": body.signed_transaction,
                 "microsoft_store": body.store_id_key}[body.store]
        if not proof:
            field_name = {"google_play": "purchase_token", "app_store": "signed_transaction",
                          "microsoft_store": "store_id_key"}[body.store]
            raise HTTPException(422, f"'{field_name}' is needed for {body.store}")
        store = {"google_play": services.play, "app_store": services.apple,
                 "microsoft_store": services.microsoft}[body.store]
        if store is None:
            raise HTTPException(404, f"{body.store} is not set up on this server")

        def link() -> None:
            with db.session() as s:
                u = s.get(User, user.id)
                if body.store == "microsoft_store":
                    products = {x.product_id for x in store.link(s, u, proof)}
                else:
                    products = {store.link(s, u, proof).product_id}
                if body.product_id not in products:
                    # Verified, but not the product the app says it bought: nothing is confirmed of that one.
                    raise HTTPException(402, NOT_CONFIRMED)

        try:
            play_once_more(link)
        except (google_play.PlayOtherAccount, apple.AppleOtherAccount, microsoft.MicrosoftOtherAccount):
            raise HTTPException(409, "This purchase belongs to another account") from None
        except (google_play.PlayError, apple.AppleError, microsoft.MicrosoftError) as ex:
            log.info("Purchase not confirmed by %s: %s", body.store, ex)
            raise HTTPException(402, NOT_CONFIRMED) from None
        except (google_play.PlayUnavailable, microsoft.MicrosoftUnavailable) as ex:
            log.warning("%s: %s", body.store, ex)
            raise HTTPException(503, "The store cannot be reached now", headers={"Retry-After": "60"}) from None
        return me(user)

    @app.post("/v1/billing/apple/notify")
    async def apple_notify(request: Request) -> dict:
        """App Store Server Notifications v2: {"signedPayload": JWS signed by Apple}. Answered 200 once applied; an error
        makes Apple send it again."""
        store = apple_store()
        body = await request.body()
        if len(body) > 256 * 1024:
            raise HTTPException(413, "Too large")
        try:
            payload = json.loads(body).get("signedPayload")
        except (ValueError, AttributeError):
            payload = None
        if not isinstance(payload, str):
            raise HTTPException(400, "'signedPayload' is needed")

        def apply() -> None:
            with db.session() as s:
                if store.notify(s, payload) is None:
                    log.info("App Store notification without a transaction of an account")

        try:
            await run_in_threadpool(play_once_more, apply)
        except apple.AppleError as ex:
            # Not signed by Apple for this app, or not ours: nothing for Apple to send again.
            log.warning("App Store notification refused: %s", ex)
            raise HTTPException(400, str(ex)) from None
        return {"ok": True}

    @app.post("/v1/billing/play/purchase")
    def play_purchase(user: UserDep, body: PlayPurchaseBody) -> dict:
        """A purchase made in the app: verified with Google, acknowledged and applied to the account."""
        store = play_store()

        def link() -> dict:
            with db.session() as s:
                u = s.get(User, user.id)
                sub = store.link(s, u, body.purchase_token)
                return {"state": sub.state, "plan": sub.plan_name,
                        **scheduler.account(s, u, cfg.grace_days).as_dict()}

        try:
            result = play_once_more(link)
        except google_play.PlayError as ex:
            raise HTTPException(422, str(ex)) from None
        except google_play.PlayUnavailable as ex:
            log.warning("Google Play: %s", ex)
            raise HTTPException(503, "Google Play is not available now, try again") from None
        return result

    @app.post("/v1/billing/play/notify")
    async def play_notify(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict:
        """Real-time developer notifications of Google Play (Pub/Sub push). An error answer makes Pub/Sub send it again."""
        if services.play is None or services.play_push is None:
            raise HTTPException(404, "Not Found")
        try:
            await run_in_threadpool(services.play_push.verify, authorization)
        except google_play.PlayError as ex:
            raise HTTPException(401, str(ex)) from None
        body = await request.body()
        if len(body) > 64 * 1024:
            raise HTTPException(413, "Too large")
        try:
            message_id, token = google_play.notification_token(body)
        except google_play.PlayError as ex:
            raise HTTPException(400, str(ex)) from None
        if token is None:
            return {"ok": True}

        def refresh() -> None:
            with db.session() as s:
                if services.play.refresh(s, token) is None:
                    log.info("Google Play notification %s for a purchase of no account", message_id)
        try:
            await run_in_threadpool(play_once_more, refresh)
        except google_play.PlayError as ex:
            # Not ours (another product, an unknown token): nothing to retry.
            log.warning("Google Play notification %s refused: %s", message_id, ex)
        except google_play.PlayUnavailable as ex:
            log.warning("Google Play notification %s: %s", message_id, ex)
            raise HTTPException(503, "Try again") from None
        return {"ok": True}

    if provider is not None and provider.name == "test":
        @app.get("/billing/test/{payment_id}", response_class=HTMLResponse, include_in_schema=False)
        def test_checkout(payment_id: str) -> HTMLResponse:
            with db.session() as s:
                p = s.get(Payment, payment_id)
                if p is None or p.provider != "test":
                    raise HTTPException(404, "Not Found")
                amount = f"{p.currency} {p.amount_cents / 100:.2f}"
                page = (f"<!doctype html><meta charset=utf-8><title>Pago de prueba</title><body style='font:16px system-ui;"
                        f"max-width:420px;margin:60px auto'><h1>Pago de prueba</h1><p>Plan {p.plan_name}, {p.months} mes(es): "
                        f"<b>{amount}</b>. No se cobra nada: es el proveedor de prueba.</p>"
                        f"<form method=post action='/billing/test/{p.id}/approved'><button>Aprobar</button></form><br>"
                        f"<form method=post action='/billing/test/{p.id}/rejected'><button>Rechazar</button></form></body>")
            return HTMLResponse(page, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                                           "form-action 'self'"})

        @app.post("/billing/test/{payment_id}/{status}", include_in_schema=False)
        def test_pay(payment_id: str, status: str) -> Response:
            if status not in ("approved", "rejected"):
                raise HTTPException(404, "Not Found")
            with db.session() as s:
                p = s.get(Payment, payment_id)
                if p is None or p.provider != "test":
                    raise HTTPException(404, "Not Found")
                billing.apply(s, billing.Notice(p.id, "test-" + p.id, status, p.amount_cents, p.currency), "test")
            return Response(status_code=303, headers={"Location": "/cuenta?pago=" + status})

    @app.get("/cuenta", response_class=HTMLResponse, include_in_schema=False)
    def account_page() -> HTMLResponse:
        page, headers = static_page("cuenta.html")
        if services.google.client_ids:
            # The Google button (Google Identity Services), as Google documents its Content Security Policy.
            headers["Content-Security-Policy"] = (
                f"default-src 'self'; script-src {script_hashes(page)} https://accounts.google.com/gsi/client; "
                "style-src 'unsafe-inline' https://accounts.google.com/gsi/style; frame-src https://accounts.google.com/gsi/; "
                "connect-src 'self' https://accounts.google.com/gsi/; img-src data: https:; frame-ancestors 'none'; "
                "form-action 'self'; base-uri 'none'")
        return HTMLResponse(page, headers=headers)

    @app.post("/v1/jobs", status_code=201)
    def submit(
        user: UserDep,
        request: Request,
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        params: Annotated[str, Form()] = "{}",
    ) -> dict:
        max_bytes = cfg.max_upload_mb << 20
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes + (1 << 20):
            raise HTTPException(413, f"The project is larger than {cfg.max_upload_mb} MB")
        try:
            parsed = json.loads(params)
        except ValueError:
            raise HTTPException(422, "'params' is not JSON") from None
        if kind == "scan":
            raise HTTPException(422, "A 3D scan is sent in pieces to /v1/scans")
        try:
            file.file.seek(0)
            # The triangles choose the queue of slicing and estimate its duration; for an analysis it only checks
            # that it is a 3MF project.
            triangles = count_triangles(file.file, max(cfg.large_slice_triangles, ESTIMATE_MAX_TRIANGLES)
                                        if kind == "slice" else 0)
            plan = plan_job(kind, parsed, triangles, cfg.large_slice_triangles)
            estimate = estimate_seconds(plan, triangles)
        except JobError as ex:
            raise HTTPException(422, str(ex)) from None
        with db.session() as s:
            u = s.get(User, user.id)
            try:
                scheduler.admit(s, u, cfg.grace_days, plan.job_class)
            except scheduler.Refused as ex:
                raise HTTPException(ex.status, str(ex)) from None
            now = utcnow()
            job = Job(user_id=u.id, kind=plan.kind, job_class=plan.job_class, params=json.dumps(plan.params),
                      priority=u.plan.priority, input_name=safe_filename(file.filename or "", "project.3mf"),
                      created_at=now, order_at=now + timedelta(seconds=min(estimate, cfg.queue_size_wait_max_s)))
            s.add(job)
            s.flush()
            try:
                file.file.seek(0)
                services.storage.save_stream(file.file, services.storage.input_path(job.id), max_bytes)
            except TooLarge:
                services.storage.remove_job(job.id)
                raise HTTPException(413, f"The project is larger than {cfg.max_upload_mb} MB") from None
            except OSError:
                services.storage.remove_job(job.id)
                raise
            # Checked before it is queued (the job is not kept when it is refused).
            inspected(services.storage.input_path(job.id), "project", lambda: services.storage.remove_job(job.id))
            view = job_view(job)
        # After the commit: a node woken before it would not see the job.
        services.wakeup.notify(JOBS)
        return view

    @app.get("/v1/jobs")
    def list_jobs(user: UserDep, limit: int = 50) -> dict:
        limit = max(1, min(limit, 200))
        with db.session() as s:
            jobs = s.execute(
                select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(limit)
            ).scalars().all()
            return {"jobs": [job_view(j) for j in jobs]}

    @app.get("/v1/jobs/{job_id}")
    def get_job(user: UserDep, job_id: str) -> dict:
        with db.session() as s:
            return job_view(own_job(s, user, job_id))

    @app.get("/v1/jobs/{job_id}/result")
    def get_result(user: UserDep, job_id: str) -> FileResponse:
        with db.session() as s:
            job = own_job(s, user, job_id)
            if job.status != "done" or job.files_removed:
                raise HTTPException(409, "The job has no result")
            path = services.storage.find_result(job.id)
            if path is None:
                raise HTTPException(410, "The result was removed")
            # Text G-code as text and the analysis results with their own type, so that the proxy compresses them (Caddy
            # "encode"); binary G-code is already compressed.
            media = {".gcode": "text/x.gcode", ".tfea": "application/x-tisma-fea"}.get(path.suffix, "application/octet-stream")
            return FileResponse(path, filename=job.result_filename or path.name, media_type=media)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(user: UserDep, job_id: str) -> dict:
        with db.session() as s:
            job = own_job(s, user, job_id)
            scheduler.cancel(s, job)
            if job.status in scheduler.FINAL:
                services.storage.remove_input(job.id)
            return job_view(job)

    # ---- machines of the local network that ask to be computing nodes (pairing.py)
    def join_allowed(request: Request) -> None:
        if not limiter.allow(f"join-ip:{client_key(request)}", JOIN_PER_MINUTE):
            raise HTTPException(429, "Too many requests", headers={"Retry-After": "10"})

    # ---- 3D scans of Tisma Scanner, sent in pieces (scans.py)
    def scan_view(up: ScanUpload) -> dict:
        return {"id": up.id, "size": up.size, "received": up.received, "chunk_size": cfg.scan_chunk_mb << 20,
                "job_id": up.job_id}

    def own_scan(s, user: User, upload_id: str) -> ScanUpload:  # noqa: ANN001
        up = s.get(ScanUpload, upload_id)
        if up is None or up.user_id != user.id:
            raise HTTPException(404, "No such scan")
        if up.job_id is None:
            on_disk(up)
        return up

    def on_disk(up: ScanUpload) -> None:
        """What arrived is what is on the disk: after a power cut the database may count a piece whose bytes were
        never written, or the file may be gone. The phone then sends again from there (409 with "received")."""
        path = scans.upload_path(services.storage.root, up.id)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            size = 0
        if size < up.received:
            log.warning("Scan %s: %d bytes counted, %d on the disk; asked again from there", up.id, up.received, size)
            up.received = size
            # Written now: a caller may take the row out of its session before the commit.
            object_session(up).flush()

    @app.post("/v1/scans", status_code=201)
    def scan_start(user: UserDep, body: ScanStartBody) -> dict:
        if body.size > cfg.max_scan_gb << 30:
            raise HTTPException(413, f"The scan is larger than {cfg.max_scan_gb} GB")
        with db.session() as s:
            u = s.get(User, user.id)
            try:
                # Refused now, before gigabytes are sent: the plan does not include scans, it is not paid...
                scheduler.admit(s, u, cfg.grace_days, "scan")
            except scheduler.Refused as ex:
                raise HTTPException(ex.status, str(ex)) from None
            if s.execute(select(func.count()).select_from(ScanUpload).where(
                    ScanUpload.user_id == u.id, ScanUpload.job_id.is_(None))).scalar_one() >= 3:
                raise HTTPException(429, "Three scans are being sent already: finish or delete one")
            # Refused before it is sent when it does not fit (with room left for the results and the database).
            folder = services.storage.root
            if shutil.disk_usage(folder).free < body.size + SCAN_ROOM_LEFT:
                raise OSError(errno.ENOSPC, "No room for the scan", str(folder))
            up = ScanUpload(user_id=u.id, name=safe_filename(body.name, "scan.zip"), size=body.size)
            s.add(up)
            s.flush()
            scans.upload_path(services.storage.root, up.id).parent.mkdir(parents=True, exist_ok=True)
            scans.upload_path(services.storage.root, up.id).touch()
            return scan_view(up)

    @app.put("/v1/scans/{upload_id}/data")
    async def scan_piece(user: UserDep, request: Request, upload_id: str, offset: int) -> Any:
        chunk = cfg.scan_chunk_mb << 20

        def where() -> ScanUpload:
            with db.session() as s:
                up = own_scan(s, user, upload_id)
                s.expunge(up)
                return up

        up = await run_in_threadpool(where)
        if up.job_id:
            raise HTTPException(409, "The scan is complete already")
        if offset != up.received:
            return JSONResponse({"detail": "The piece does not go there", "received": up.received}, status_code=409)
        path = scans.upload_path(services.storage.root, up.id)
        data = bytearray()
        async for part in request.stream():
            data += part
            if len(data) > chunk or offset + len(data) > up.size:
                raise HTTPException(413, f"A piece is at most {cfg.scan_chunk_mb} MB and the scan {up.size} bytes")
        if not data:
            raise HTTPException(422, "The piece is empty")

        def write() -> tuple[bool, int]:
            """(written, received): whether this piece was written, and what has arrived."""
            with db.session() as s:
                row = s.get(ScanUpload, up.id, with_for_update=True)
                if row is None or row.received != offset:
                    return False, -1 if row is None else row.received
                with open(path, "r+b") as f:
                    f.seek(offset)
                    f.write(data)
                    f.truncate()
                    # On the disk before the database counts it (a power cut would lose it otherwise).
                    f.flush()
                    os.fsync(f.fileno())
                row.received = offset + len(data)
                row.updated_at = utcnow()
                return True, row.received

        written, received = await run_in_threadpool(write)
        if received < 0:
            raise HTTPException(404, "No such scan")
        # Not written (another request moved it meanwhile): even when what arrived happens to end where this piece
        # would have, this piece is not there.
        if not written:
            return JSONResponse({"detail": "The piece does not go there", "received": received}, status_code=409)
        return {"received": received}

    @app.get("/v1/scans/{upload_id}")
    def scan_state(user: UserDep, upload_id: str) -> dict:
        with db.session() as s:
            return scan_view(own_scan(s, user, upload_id))

    @app.post("/v1/scans/{upload_id}/complete", status_code=201)
    def scan_complete(user: UserDep, upload_id: str) -> dict:
        refused: str | None = None
        with db.session() as s:
            up = own_scan(s, user, upload_id)
            if not up.job_id and up.received == up.size:
                try:
                    scans.check_scan(scans.upload_path(services.storage.root, up.id), cfg.max_scan_unzipped_gb << 30)
                except scans.ScanError as ex:
                    # Not kept: removed in this transaction, the answer said after it.
                    refused = str(ex)
                    scans.upload_path(services.storage.root, up.id).unlink(missing_ok=True)
                    s.delete(up)
        if refused is not None:
            raise HTTPException(422, refused)
        with db.session() as s:
            up = own_scan(s, user, upload_id)
            if up.job_id:
                # Asked again (the answer was lost): the same job.
                job = s.get(Job, up.job_id)
                if job is not None:
                    return job_view(job)
            if up.received != up.size:
                raise HTTPException(409, f"The scan is not complete: {up.received} of {up.size} bytes")
            path = scans.upload_path(services.storage.root, up.id)
            u = s.get(User, user.id)
            try:
                scheduler.admit(s, u, cfg.grace_days, "scan")
            except scheduler.Refused as ex:
                raise HTTPException(ex.status, str(ex)) from None
            plan = plan_job("scan", {}, 0, cfg.large_slice_triangles)
            now = utcnow()
            estimate = estimate_seconds(plan, up.size)
            job = Job(user_id=u.id, kind=plan.kind, job_class=plan.job_class, params=json.dumps(plan.params),
                      priority=u.plan.priority, input_name=up.name, created_at=now,
                      order_at=now + timedelta(seconds=min(estimate, cfg.queue_size_wait_max_s)))
            s.add(job)
            s.flush()
            target = services.storage.input_path(job.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            up.job_id = job.id
            view = job_view(job)
        services.wakeup.notify(JOBS)
        return view

    @app.delete("/v1/scans/{upload_id}")
    def scan_delete(user: UserDep, upload_id: str) -> dict:
        with db.session() as s:
            up = own_scan(s, user, upload_id)
            scans.upload_path(services.storage.root, up.id).unlink(missing_ok=True)
            s.delete(up)
        return {"deleted": upload_id}

    @app.post("/v1/node/join", status_code=201, dependencies=[Depends(local_only), Depends(join_allowed)])
    def node_join(request: Request, body: JoinBody) -> dict:
        with db.session() as s:
            try:
                request_id, secret = pairing.request_join(s, body.name, client_key(request), body.info)
            except pairing.PairingError as e:
                raise HTTPException(429, str(e), headers={"Retry-After": "600"}) from e
        log.info("The machine %s (%s) asks to be a computing node", pairing.clean_name(body.name), client_key(request))
        return {"id": request_id, "secret": secret, "poll_s": JOIN_POLL_S}

    @app.get("/v1/node/join/{request_id}", dependencies=[Depends(local_only), Depends(join_allowed)])
    def node_join_state(request_id: str, x_join_secret: Annotated[str, Header()] = "") -> dict:
        with db.session() as s:
            answer = pairing.join_state(s, request_id, x_join_secret)
        if answer is None:
            raise HTTPException(404, "No such request")
        return answer

    # ---- computing nodes
    def try_claim(node_id: int, exclude: list[str]) -> dict | None:
        with db.session() as s:
            node = s.get(Node, node_id)
            if node is None or not node.enabled:
                return None
            job = scheduler.claim(s, node, db.is_sqlite, exclude)
            if job is None:
                return None
            owner = s.get(User, job.user_id)
            plan = owner.plan if owner is not None else None
            return {"id": job.id, "kind": job.kind, "job_class": job.job_class, "params": job.params_dict,
                    "input_name": job.input_name, "cpu_cores": (plan.cpu_cores or 1) if plan else 1}

    @app.post("/v1/node/claim")
    async def node_claim(node: NodeDep, body: ClaimBody, wait: int | None = None) -> Any:
        if body.info:
            def save_info() -> None:
                with db.session() as s:
                    n = s.get(Node, node.id)
                    if n is not None:
                        n.info = json.dumps(body.info)[:4000]
            await run_in_threadpool(save_info)
        wait_s = cfg.claim_wait_s if wait is None else max(0, min(wait, cfg.claim_wait_s))
        job = await services.wakeup.poll(JOBS, lambda: run_in_threadpool(try_claim, node.id, body.exclude), wait_s)
        if job is None:
            return Response(status_code=204)
        return {"job": job, "priorities": node.priority_list}

    @app.post("/v1/node/heartbeat")
    def node_heartbeat(node: NodeDep, body: HeartbeatBody) -> dict:
        with db.session() as s:
            if body.info:
                n = s.get(Node, node.id)
                n.info = json.dumps(body.info)[:4000]
            cancel = []
            if body.running:
                # All of them (a node of more than 64 cores runs more than 64), in parts for SQLite's parameters.
                jobs = [j for i in range(0, len(body.running), 500) for j in s.execute(
                    select(Job).where(Job.id.in_(body.running[i:i + 500]), Job.node_id == node.id)).scalars()]
                # Jobs to stop: cancelled by the user, or no longer this node's (requeued after a timeout).
                known = {j.id for j in jobs if j.status == "running"}
                cancel = [j.id for j in jobs if j.cancel_requested] + [i for i in body.running if i not in known]
            # Jobs this node had and no longer runs (it restarted while the master kept them as running): back to the
            # queue now, not never (the node is seen, so requeue_stale does not take them). Only from a node that sends
            # its whole list.
            requeued = 0
            # Two programs with this token: neither one's list says what the other runs, so nothing is requeued here,
            # nor from a program that started a moment ago (an older one may still be alive).
            twins, settled = services.twins.seen(node.id, body.instance, utcnow()) if body.instance else (False, True)
            if twins:
                log.warning("Node %s: two programs answer with its token (a copied node.toml or an old process)",
                            node.name)
            elif settled and "running" in body.model_fields_set:
                requeued = scheduler.requeue_orphans(s, node.id, body.running, cfg.max_attempts, ORPHAN_GRACE_S)
        if requeued:
            services.wakeup.notify(JOBS)
        return {"cancel": sorted(set(cancel)), "priorities": node.priority_list}

    @app.get("/v1/node/jobs/{job_id}/input")
    def node_input(node: NodeDep, job_id: str) -> FileResponse:
        with db.session() as s:
            job = node_job(s, node, job_id)
            path = services.storage.input_path(job.id)
        if not path.exists():
            raise HTTPException(410, "The project was removed")
        return FileResponse(path, media_type="application/octet-stream", filename="input.3mf")

    @app.post("/v1/node/jobs/{job_id}/progress")
    def node_progress(node: NodeDep, job_id: str, body: ProgressBody) -> dict:
        with db.session() as s:
            job = node_job(s, node, job_id)
            scheduler.set_progress(s, job, body.progress)
            return {"cancel": job.cancel_requested}

    @app.post("/v1/node/jobs/{job_id}/result")
    def node_result(
        node: NodeDep,
        job_id: str,
        file: Annotated[UploadFile, File()],
        summary: Annotated[str, Form()] = "{}",
    ) -> dict:
        try:
            summary_dict = json.loads(summary)
            if not isinstance(summary_dict, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "'summary' must be a JSON object") from None
        name = safe_filename(file.filename or "", "result")
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_RESULTS:
            raise HTTPException(422, f"Results are {', '.join(ALLOWED_RESULTS)} files")
        refused: HTTPException | None = None
        # A node that timed out sends its result again while the first is still being received: the second waits for
        # the first and then finds the job done (404), instead of writing the same file and finishing it twice.
        with result_lock(job_id), db.session() as s:
            job = node_job(s, node, job_id, lock=True)
            path = services.storage.result_path(job.id, ext)
            try:
                services.storage.save_stream(file.file, path, cfg.max_result_mb << 20)
                # A node is ours, but what it gives the users is checked too (with ClamAV, when set).
                inspected(path, "result", lambda: path.unlink(missing_ok=True))
            except TooLarge:
                refused = HTTPException(413, "The result is too large")
                scheduler.fail(s, job, f"The result is larger than {cfg.max_result_mb} MB")
            except HTTPException as ex:
                refused = ex
                scheduler.fail(s, job, "The result was refused: " + str(ex.detail))
            else:
                stem = job.input_name.rsplit(".", 1)[0] if job.input_name else "result"
                scheduler.finish(s, job, safe_filename(stem + ext, "result" + ext), summary_dict)
            services.storage.remove_input(job.id)
        # Raised once the session is committed: the job stays failed.
        if refused is not None:
            raise refused
        return {"ok": True}

    @app.post("/v1/node/jobs/{job_id}/fail")
    def node_fail(node: NodeDep, job_id: str, body: FailBody) -> dict:
        with db.session() as s:
            job = node_job(s, node, job_id)
            if job.cancel_requested:
                # The person cancelled it: whatever the node says (it stopped it, or failed before it learnt it), the
                # job is cancelled, not failed with an error the person did not cause, nor sent to another node.
                scheduler.stopped_by_node(s, job)
            elif body.node_error and not job.cancel_requested and job.attempts < cfg.max_attempts:
                # Another node takes it: its project stays.
                log.warning("Job %s back to the queue: node %s could not run it (%s)", job.id, node.name,
                            body.error[:300])
                scheduler.release(s, job, cfg.max_attempts)
                return {"ok": True, "requeued": True}
            else:
                scheduler.fail(s, job, body.error, body.details)
            services.storage.remove_input(job.id)
            return {"ok": True}

    # ---- remote printers: a bridge at home (Tisma left open) relays them, the phone sees and commands them (remote.py).
    # What a bridge does most of the time (waiting for commands, sending its state) is answered from memory
    # (presence.py); the database is asked when there is something to read or to keep.
    presence = services.presence

    def own_bridge(s, user_id: int, bridge_id: str) -> Bridge:  # noqa: ANN001
        bridge = s.get(Bridge, bridge_id) if remote.ID.match(bridge_id) else None
        if bridge is None or bridge.user_id != user_id:
            raise HTTPException(404, "No such bridge")
        return bridge

    async def known_bridge(user_id: int, bridge_id: str, state: tuple[str, list[dict], datetime] | None = None) -> bool:
        """The bridge is the account's; from memory when known. With state (what the bridge sends: name, printers,
        time), a bridge not in memory is read, or made, and its state written in the same session; True then."""
        if not remote.ID.match(bridge_id):
            raise HTTPException(404, "No such bridge")
        owner, written = presence.owner(bridge_id), False
        if owner is None:
            def load() -> int | None:
                with db.session() as s:
                    bridge = s.get(Bridge, bridge_id)
                    if bridge is None:
                        if state is None:
                            return None
                        if s.execute(select(func.count()).select_from(Bridge)
                                     .where(Bridge.user_id == user_id)).scalar_one() >= remote.MAX_BRIDGES:
                            raise HTTPException(409, "Too many bridges: remove an old one")
                        bridge = Bridge(id=bridge_id, user_id=user_id)
                        s.add(bridge)
                    if state is not None and bridge.user_id == user_id:
                        bridge.name, bridge.printers, bridge.last_seen = state[0], json.dumps(state[1]), state[2]
                    return bridge.user_id

            try:
                owner = await run_in_threadpool(load)
            except IntegrityError:
                # Made at the same time by another request of the bridge.
                owner = await run_in_threadpool(load)
            if owner is not None:
                presence.known(bridge_id, owner)
            written = state is not None
        if owner != user_id:
            raise HTTPException(404, "No such bridge")
        return written

    def bridge_printers(bridge: Bridge) -> list[dict]:
        printers = presence.printers(bridge.id)
        return bridge.printer_list if printers is None else printers

    def bridge_printer(bridge: Bridge, printer_id: str, action: str | None = None) -> dict:
        printer = next((p for p in bridge_printers(bridge) if p["id"] == printer_id), None)
        if printer is None:
            raise HTTPException(404, "No such printer")
        if action is not None and action not in printer["actions"]:
            raise HTTPException(422, f"The printer cannot {action} from afar")
        return printer

    def bridge_online(bridge: Bridge) -> bool:
        seen = [d for d in (bridge.last_seen, presence.last_seen(bridge.id)) if d is not None]
        return remote.is_online(max(seen) if seen else None)

    def remote_allowance(s, user_id: int) -> tuple[int, set[tuple[str, str]]]:  # noqa: ANN001
        """How many printers the plan lets use from afar, and which ones."""
        u = s.get(User, user_id)
        allowance = remote.printer_allowance(u, scheduler.account(s, u, cfg.grace_days).paid)
        return allowance, remote.allowed_printers(s, u, allowance)

    def check_allowed(s, user_id: int, bridge: Bridge, printer_id: str) -> None:  # noqa: ANN001
        allowance, allowed = remote_allowance(s, user_id)
        if (bridge.id, printer_id) not in allowed:
            raise HTTPException(402, "Remote printers are not in your plan: choose one on the account page" if allowance == 0
                                else f"Your plan includes {allowance} remote printers: add more on the account page")

    def new_command(s, bridge: Bridge, printer_id: str, action: str, file_name: str = "") -> RemoteCommand:  # noqa: ANN001
        if not bridge_online(bridge):
            raise HTTPException(409, "The bridge is offline: open Tisma on the computer at home")
        command = RemoteCommand(bridge_id=bridge.id, printer_id=printer_id, action=action, file_name=file_name)
        s.add(command)
        s.flush()
        return command

    def command_made(bridge_id: str) -> None:
        """After the commit of a command: the bridge's wait ends and reads it."""
        presence.mark_queued(bridge_id)
        services.wakeup.notify(bridge_key(bridge_id))

    def wake_for_state(bridge_ids: list[str]) -> None:
        for bridge_id in bridge_ids:
            services.wakeup.notify(bridge_key(bridge_id))

    # The bridge.
    @app.put("/v1/bridges/{bridge_id}")
    async def bridge_state(user_id: RemoteDep, bridge_id: str, body: BridgeBody) -> dict:
        name, printers, now = body.name.strip()[:100], remote.clean_printers(body.printers), utcnow()
        written = await known_bridge(user_id, bridge_id, (name, printers, now))
        if presence.set_state(bridge_id, name, printers, now) and not written:
            def write() -> None:
                with db.session() as s:
                    s.execute(update(Bridge).where(Bridge.id == bridge_id).values(
                        name=name, printers=json.dumps(printers), last_seen=now))

            await run_in_threadpool(write)
        # The printers whose camera someone is looking at (the bridge sends their images), and how often to send the
        # state.
        return {"watch": presence.watched(bridge_id), "state_s": presence.state_every(bridge_id)}

    @app.get("/v1/bridges/{bridge_id}/commands")
    async def bridge_commands(user_id: RemoteDep, bridge_id: str, wait: int = 20) -> dict:
        await known_bridge(user_id, bridge_id)
        presence.seen(bridge_id, utcnow())

        def take() -> list[dict]:
            with db.session() as s:
                queued = s.execute(select(RemoteCommand).where(RemoteCommand.bridge_id == bridge_id,
                                                               RemoteCommand.status == "queued")
                                   .order_by(RemoteCommand.created_at)).scalars().all()
                taken = []
                for c in queued:
                    # Two waits of the bridge at once (a program restarted while its last wait was open) read the
                    # same queued commands: each goes to the wait that turns it «sent», so a print reaches the printer
                    # once.
                    won = s.execute(update(RemoteCommand).where(RemoteCommand.id == c.id,
                                                                RemoteCommand.status == "queued")
                                    .values(status="sent", updated_at=utcnow())
                                    .execution_options(synchronize_session=False)).rowcount
                    if won == 1:
                        s.refresh(c)
                        taken.append(remote.command_view(c))
                return taken

        async def check() -> dict | None:
            commands: list[dict] = []
            # Only when a command may be waiting: an idle bridge costs no query.
            if presence.take_queued(bridge_id):
                try:
                    commands = await run_in_threadpool(take)
                except Exception:
                    presence.mark_queued(bridge_id)
                    raise
            if commands or presence.take_state_wanted(bridge_id):
                return {"commands": commands}
            return None

        answer = await services.wakeup.poll(bridge_key(bridge_id), check, min(wait, 25)) or {"commands": []}
        return {**answer, "state_s": presence.state_every(bridge_id)}

    @app.get("/v1/bridges/{bridge_id}/commands/{command_id}/file")
    def bridge_command_file(user_id: RemoteDep, bridge_id: str, command_id: str) -> FileResponse:
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            command = s.get(RemoteCommand, command_id)
            if command is None or command.bridge_id != bridge.id or command.action != "print" or command.status != "sent":
                raise HTTPException(404, "No such command")
        path = services.remote_files.print_file(command.id)
        if not path.exists():
            raise HTTPException(410, "The file was removed")
        return FileResponse(path, media_type="application/octet-stream", filename=command.file_name)

    @app.post("/v1/bridges/{bridge_id}/commands/{command_id}")
    def bridge_command_result(user_id: RemoteDep, bridge_id: str, command_id: str, body: CommandResultBody) -> dict:
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            command = s.get(RemoteCommand, command_id)
            if command is None or command.bridge_id != bridge.id or command.status != "sent":
                raise HTTPException(404, "No such command")
            command.status = "done" if body.ok else "failed"
            command.error = "" if body.ok else (body.error or "The printer refused the command")
            command.updated_at = utcnow()
            services.remote_files.remove_command(command.id)
            return remote.command_view(command)

    @app.post("/v1/bridges/{bridge_id}/printers/{printer_id}/camera")
    async def bridge_camera(user_id: RemoteDep, bridge_id: str, printer_id: str, request: Request) -> dict:
        await known_bridge(user_id, bridge_id)
        printers = presence.printers(bridge_id)
        if printers is None:
            def load() -> list[dict]:
                with db.session() as s:
                    return own_bridge(s, user_id, bridge_id).printer_list

            printers = await run_in_threadpool(load)
        if not any(p["id"] == printer_id for p in printers):
            raise HTTPException(404, "No such printer")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > remote.MAX_IMAGE_BYTES:
                raise HTTPException(413, "The image is too large")
        if not data.startswith(b"\xff\xd8"):
            raise HTTPException(422, "Not a JPEG image")
        services.images.put(bridge_id, printer_id, bytes(data))
        return {"ok": True}

    # The phone.
    @app.get("/v1/remote/printers")
    def remote_printers(user_id: RemoteDep) -> dict:
        now = utcnow()
        # Someone looks: the bridges that sent their state slowly send it now, and then often.
        wake_for_state(presence.looking_at(user_id))
        with db.session() as s:
            allowance, allowed = remote_allowance(s, user_id)
            bridges = s.execute(select(Bridge).where(Bridge.user_id == user_id).order_by(Bridge.created_at)).scalars()
            return {"allowance": allowance,
                    "bridges": [remote.bridge_view(b, now, allowed, presence.printers(b.id), presence.last_seen(b.id))
                                for b in bridges]}

    @app.delete("/v1/remote/bridges/{bridge_id}")
    def remote_remove_bridge(user_id: RemoteDep, bridge_id: str) -> dict:
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            for c in s.execute(select(RemoteCommand).where(RemoteCommand.bridge_id == bridge.id)).scalars():
                services.remote_files.remove_command(c.id)
            s.delete(bridge)
        presence.forget(bridge_id)
        services.images.drop_bridge(bridge_id)
        return {"ok": True}

    @app.post("/v1/remote/bridges/{bridge_id}/printers/{printer_id}/print", status_code=201)
    def remote_print(user_id: RemoteDep, request: Request, bridge_id: str, printer_id: str,
                     file: Annotated[UploadFile, File()]) -> dict:
        max_bytes = cfg.max_upload_mb << 20
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes + (1 << 20):
            raise HTTPException(413, f"The file is larger than {cfg.max_upload_mb} MB")
        name = safe_filename(file.filename or "", "tisma.gcode")
        if not name.lower().endswith(remote.PRINT_EXTENSIONS):
            raise HTTPException(422, "Send a G-code (.gcode or .bgcode)")
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            bridge_printer(bridge, printer_id, "print")
            check_allowed(s, user_id, bridge, printer_id)
            command = new_command(s, bridge, printer_id, "print", name)
            # Saved before the command is visible to the bridge (the session commits at the end).
            try:
                file.file.seek(0)
                services.storage.save_stream(file.file, services.remote_files.print_file(command.id), max_bytes)
            except TooLarge:
                services.remote_files.remove_command(command.id)
                raise HTTPException(413, f"The file is larger than {cfg.max_upload_mb} MB") from None
            # The commands that change the printer itself are refused; some only reported (threats.py).
            findings = inspected(services.remote_files.print_file(command.id), "gcode",
                                 lambda: services.remote_files.remove_command(command.id),
                                 text_gcode=name.lower().endswith(".gcode"))
            view = remote.command_view(command)
            view["warnings"] = [{"command": x.command, "line": x.line, "what": x.what} for x in findings]
        command_made(bridge_id)
        return view

    @app.post("/v1/remote/bridges/{bridge_id}/printers/{printer_id}/{action}", status_code=201)
    def remote_action(user_id: RemoteDep, bridge_id: str, printer_id: str, action: str) -> dict:
        if action not in ("pause", "resume", "stop"):
            raise HTTPException(404, "No such action")
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            bridge_printer(bridge, printer_id, action)
            check_allowed(s, user_id, bridge, printer_id)
            view = remote.command_view(new_command(s, bridge, printer_id, action))
        command_made(bridge_id)
        return view

    @app.get("/v1/remote/commands/{command_id}")
    def remote_command(user_id: RemoteDep, command_id: str) -> dict:
        with db.session() as s:
            command = s.get(RemoteCommand, command_id)
            bridge = s.get(Bridge, command.bridge_id) if command else None
            if command is None or bridge is None or bridge.user_id != user_id:
                raise HTTPException(404, "No such command")
            return remote.command_view(command)

    @app.get("/v1/remote/bridges/{bridge_id}/printers/{printer_id}/camera")
    def remote_camera(user_id: RemoteDep, bridge_id: str, printer_id: str) -> Response:
        """The last image of the camera (and the bridge is asked for the next ones); 204 while there is none recent."""
        with db.session() as s:
            bridge = own_bridge(s, user_id, bridge_id)
            printer = bridge_printer(bridge, printer_id)
            if not printer["camera"]:
                raise HTTPException(409, "The printer has no camera the bridge can relay")
            check_allowed(s, user_id, bridge, printer_id)
        presence.known(bridge_id, user_id)
        if presence.watch(bridge_id, printer_id, remote.WATCH_S):
            # Nobody was looking: the bridge is told now instead of at its next state.
            wake_for_state([bridge_id])
        image = services.images.get(bridge_id, printer_id)
        if image is None:
            return Response(status_code=204)
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---- administrator
    # ---- the review of the library (rule 7): the reports, removing a model, and the blocks
    @app.get("/v1/admin/library/reports")
    def admin_library_reports(_admin: AdminDep, done: bool = False) -> dict:
        with db.session() as s:
            query = select(LibraryReport).order_by(LibraryReport.created_at.desc()).limit(500)
            query = query.where(LibraryReport.handled_at.is_not(None) if done else LibraryReport.handled_at.is_(None))
            groups: dict[str, dict] = {}
            for r in s.execute(query).scalars():
                g = groups.get(r.model_id)
                if g is None:
                    m = s.execute(select(LibraryModel).where(LibraryModel.model_id == r.model_id)
                                  .order_by(LibraryModel.revision.desc())).scalars().first()
                    g = groups[r.model_id] = {
                        "model": library.model_entry(m) if m else {"id": r.model_id},
                        "removed": bool(m and m.removed_at), "reports": []}
                g["reports"].append({"id": r.id, "sha256": r.sha256, "reason": r.reason,
                                     "created_at": r.created_at.isoformat(timespec="seconds") + "Z", "outcome": r.outcome})
            return {"models": sorted(groups.values(), key=lambda g: -len(g["reports"]))}

    @app.post("/v1/admin/library/models/{model_id}/remove")
    def admin_library_remove(_admin: AdminDep, model_id: str, body: dict = Body(default={})) -> dict:
        """Takes the model out of the index and blocks its files: the apps stop sharing them and delete their copy."""
        reason = str(body.get("reason", "")).strip()[:300] or "retirado por la revisión"
        with db.session() as s:
            rows = s.execute(select(LibraryModel).where(LibraryModel.model_id == model_id)).scalars().all()
            if not rows:
                raise HTTPException(404, "No such model")
            hashes = {f["sha256"] for m in rows for f in json.loads(m.files)}
            for m in rows:
                m.removed_at = m.removed_at or utcnow()
                m.removed_reason = reason
            for h in sorted(hashes):
                if not library.blocked(s, "file", h):
                    library.add_block(s, "file", h, reason)
            library.bump(s, "index")
            for r in s.execute(select(LibraryReport).where(LibraryReport.model_id == model_id,
                                                           LibraryReport.handled_at.is_(None))).scalars():
                r.handled_at, r.outcome = utcnow(), "removed"
            return {"ok": True, "blocked_files": len(hashes)}

    @app.post("/v1/admin/library/reports/{report_id}/dismiss")
    def admin_library_dismiss(_admin: AdminDep, report_id: int) -> dict:
        with db.session() as s:
            r = s.get(LibraryReport, report_id)
            if r is None:
                raise HTTPException(404, "No such report")
            r.handled_at, r.outcome = utcnow(), "dismissed"
        return {"ok": True}

    @app.get("/v1/admin/library/settings")
    def admin_library_settings(_admin: AdminDep) -> dict:
        with db.session() as s:
            return library.settings_view(s)

    @app.put("/v1/admin/library/settings")
    def admin_library_change_settings(_admin: AdminDep, body: dict = Body(...)) -> dict:
        with db.session() as s:
            try:
                return library.change_settings(s, body)
            except library.LibraryError as ex:
                raise HTTPException(422, str(ex)) from None

    @app.get("/v1/admin/library/blocks")
    def admin_library_blocks(_admin: AdminDep) -> dict:
        with db.session() as s:
            library.purge_addresses(s)
            return {"blocks": [{"id": b.id, "kind": b.kind, "value": b.value, "reason": b.reason,
                                "until": b.until.isoformat(timespec="seconds") + "Z" if b.until else None,
                                "created_at": b.created_at.isoformat(timespec="seconds") + "Z"}
                               for b in s.execute(select(LibraryBlock).order_by(LibraryBlock.id.desc())).scalars()]}

    @app.post("/v1/admin/library/blocks", status_code=201)
    def admin_library_block(_admin: AdminDep, body: dict = Body(...)) -> dict:
        hours = body.get("hours")
        if hours is not None and (not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 30):
            raise HTTPException(422, "'hours' from more than 0 to 720")
        kind = str(body.get("kind", ""))
        if kind == "address" and hours is None:
            hours = cfg.library_address_block_h
        with db.session() as s:
            try:
                b = library.add_block(s, kind, str(body.get("value", "")), str(body.get("reason", "")),
                                      float(hours) if hours else None)
            except library.LibraryError as ex:
                raise HTTPException(422, str(ex)) from None
            s.flush()
            return {"id": b.id, "kind": b.kind, "value": b.value,
                    "until": b.until.isoformat(timespec="seconds") + "Z" if b.until else None}

    @app.delete("/v1/admin/library/blocks/{block_id}")
    def admin_library_unblock(_admin: AdminDep, block_id: int) -> dict:
        with db.session() as s:
            try:
                library.remove_block(s, block_id)
            except library.LibraryError as ex:
                raise HTTPException(404, str(ex)) from None
        return {"ok": True}

    @app.get("/v1/admin/nodes")
    def admin_nodes(_admin: AdminDep) -> dict:
        with db.session() as s:
            running = dict(s.execute(
                select(Job.node_id, func.count()).where(Job.status == "running").group_by(Job.node_id)
            ).all())
            nodes = s.execute(select(Node).order_by(Node.name)).scalars().all()
            return {"nodes": [node_view(n, running.get(n.id, 0), cfg.node_timeout_s) for n in nodes],
                    "classes": list(JOB_CLASSES), "descriptions": JOB_CLASS_DESCRIPTIONS}

    @app.post("/v1/admin/nodes", status_code=201)
    def admin_add_node(_admin: AdminDep, body: NodeCreate) -> dict:
        priorities = check_priorities(body.priorities)
        token = new_token("tn")
        with db.session() as s:
            if s.execute(select(Node).where(Node.name == body.name)).scalar_one_or_none():
                raise HTTPException(409, "A node with that name exists")
            node = Node(name=body.name, token_hash=hash_token(token), priorities=priorities)
            s.add(node)
            s.flush()
            return {"node": node_view(node, 0, cfg.node_timeout_s), "token": token}

    @app.delete("/v1/admin/nodes/{name}")
    def admin_delete_node(_admin: AdminDep, name: str) -> dict:
        with db.session() as s:
            try:
                requeued = pairing.delete_node(s, name)
            except pairing.PairingError as e:
                raise HTTPException(404, str(e)) from e
        if requeued:
            services.wakeup.notify(JOBS)
        return {"deleted": name, "requeued": requeued}

    @app.get("/v1/admin/node-requests")
    def admin_node_requests(_admin: AdminDep) -> dict:
        with db.session() as s:
            return {"requests": pairing.pending(s)}

    @app.post("/v1/admin/node-requests/{request_id}/accept")
    def admin_accept_node(_admin: AdminDep, request_id: str, body: AcceptBody) -> dict:
        with db.session() as s:
            try:
                node = pairing.accept(s, request_id, body.name, body.priorities)
            except pairing.PairingError as e:
                raise HTTPException(404 if "No such" in str(e) else 422, str(e)) from e
            return {"node": node_view(node, 0, cfg.node_timeout_s)}

    @app.post("/v1/admin/node-requests/{request_id}/reject")
    def admin_reject_node(_admin: AdminDep, request_id: str) -> dict:
        with db.session() as s:
            try:
                pairing.reject(s, request_id)
            except pairing.PairingError as e:
                raise HTTPException(404, str(e)) from e
        return {"rejected": request_id}

    @app.patch("/v1/admin/nodes/{name}")
    def admin_update_node(_admin: AdminDep, name: str, body: NodeUpdate) -> dict:
        with db.session() as s:
            node = s.execute(select(Node).where(Node.name == name)).scalar_one_or_none()
            if node is None:
                raise HTTPException(404, "No such node")
            if body.priorities is not None:
                node.priorities = check_priorities(body.priorities)
            if body.enabled is not None:
                node.enabled = body.enabled
            return {"node": node_view(node, 0, cfg.node_timeout_s)}

    @app.get("/v1/admin/users")
    def admin_users(_admin: AdminDep) -> dict:
        with db.session() as s:
            # Sorted here: the e-mails are encrypted in the database.
            users = sorted(s.execute(select(User)).scalars(), key=lambda u: u.email)
            return {"users": [{**user_view(u), "account": scheduler.account(s, u, cfg.grace_days).as_dict()}
                              for u in users]}

    @app.put("/v1/admin/users/{email}/unlimited")
    def admin_unlimited(_admin: AdminDep, email: str, body: UnlimitedBody) -> dict:
        """Makes an account internal (unlimited computing, never charged) or a normal one again."""
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if user is None:
                raise HTTPException(404, "No such user")
            user.unlimited = body.unlimited
            s.flush()
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    def admin_user(s, email: str) -> User:  # noqa: ANN001
        user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
        if user is None:
            raise HTTPException(404, "No such user")
        return user

    @app.post("/v1/admin/users/{email}/hours", status_code=201)
    def admin_adjust_hours(_admin: AdminDep, email: str, body: HoursBody) -> dict:
        """Gives (positive) or takes (negative) hours of computing of the current month: a reward or a penalty."""
        if body.hours == 0:
            raise HTTPException(422, "The hours cannot be 0")
        with db.session() as s:
            user = admin_user(s, email)
            adj = HourAdjustment(user_id=user.id, month=month_of(utcnow()), hours=body.hours, note=body.note.strip())
            s.add(adj)
            s.flush()
            return {"adjustment": adjustment_view(adj), "user": user_view(user),
                    "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/admin/users/{email}/hours")
    def admin_hours(_admin: AdminDep, email: str) -> dict:
        """The hours given and taken from the user, newest first."""
        with db.session() as s:
            user = admin_user(s, email)
            rows = s.execute(select(HourAdjustment).where(HourAdjustment.user_id == user.id)
                             .order_by(HourAdjustment.created_at.desc(), HourAdjustment.id.desc())).scalars()
            return {"adjustments": [adjustment_view(a) for a in rows]}

    @app.put("/v1/admin/users/{email}/enabled")
    def admin_enable_user(_admin: AdminDep, email: str, body: EnabledBody) -> dict:
        """Disables an account (it cannot log in, and its devices are refused) or enables it again."""
        with db.session() as s:
            user = admin_user(s, email)
            if user.is_admin and not body.enabled:
                raise HTTPException(422, "An administrator cannot be disabled here")
            user.enabled = body.enabled
            if not body.enabled:
                # Its bridges' tokens are kept a minute in memory (TokenCache): refused from now on.
                services.tokens.drop_user(user.id)
                # And its jobs are not computed any more.
                for job_id in scheduler.stop_user_jobs(s, user.id):
                    services.storage.remove_input(job_id)
            s.flush()
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/admin/plans")
    def admin_plans(_admin: AdminDep) -> dict:
        """Every plan, public or not, for editing them."""
        with db.session() as s:
            return {"plans": [plan_view(p) for p in s.execute(select(Plan).order_by(Plan.price_cents, Plan.name)).scalars()]}

    @app.post("/v1/admin/users/{email}/payments", status_code=201)
    def admin_manual_payment(_admin: AdminDep, email: str, body: ManualPaymentBody) -> dict:
        """A payment received outside the provider (transfer, cash): the plan is paid body.months more."""
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if user is None:
                raise HTTPException(404, "No such user")
            plan = s.get(Plan, body.plan or user.plan_name)
            if plan is None:
                raise HTTPException(422, "No such plan")
            try:
                payment = billing.record_manual(s, user, plan, body.months, body.amount_cents, body.currency, body.note,
                                                body.extra_printers)
            except billing.BillingError as ex:
                raise HTTPException(422, str(ex)) from None
            return {"payment": payment_view(payment), "user": user_view(user)}

    @app.get("/v1/admin/payments")
    def admin_payments(_admin: AdminDep, limit: int = 50) -> dict:
        with db.session() as s:
            rows = s.execute(select(Payment, User.email).join(User, User.id == Payment.user_id)
                             .order_by(Payment.created_at.desc()).limit(max(1, min(limit, 500)))).all()
            return {"payments": [{**payment_view(p), "user": email} for p, email in rows]}

    # ---- the web site: creates the users, sets their plan and records their payments
    def upsert_plan(s, name: str, body: PlanBody) -> Plan:  # noqa: ANN001
        plan = s.get(Plan, name)
        if plan is None:
            plan = Plan(name=name)
            s.add(plan)
        plan.monthly_hours = body.monthly_hours
        plan.max_concurrent = body.max_concurrent
        plan.requests_per_minute = body.requests_per_minute
        plan.priority = body.priority
        plan.price_cents = body.price_cents
        plan.currency = body.currency.upper()
        plan.public = body.public
        plan.remote_printers = body.remote_printers
        plan.extra_printer_cents = body.extra_printer_cents
        plan.cpu_cores = body.cpu_cores
        plan.cuda_cores = body.cuda_cores
        unknown = [c for c in body.classes if c not in JOB_CLASSES]
        if unknown:
            raise HTTPException(422, f"Unknown job classes: {', '.join(unknown)} (known: {', '.join(JOB_CLASSES)})")
        plan.classes = ",".join(c for c in JOB_CLASSES if c in body.classes)
        return plan

    @app.put("/v1/site/plans/{name}")
    def site_plan(_site: SiteDep, name: str, body: PlanBody) -> dict:
        with db.session() as s:
            return {"plan": plan_view(upsert_plan(s, name, body))}

    @app.get("/v1/admin/capacity")
    def admin_capacity(_admin: AdminDep) -> dict:
        """How many users of each plan the enabled nodes can serve (capacity.py)."""
        with db.session() as s:
            nodes = s.execute(select(Node)).scalars().all()
            plans = s.execute(select(Plan).order_by(Plan.price_cents)).scalars().all()
            return capacity.report(nodes, plans)

    @app.put("/v1/admin/plans/{name}")
    def admin_plan(_admin: AdminDep, name: str, body: PlanBody) -> dict:
        with db.session() as s:
            return {"plan": plan_view(upsert_plan(s, name, body))}

    @app.put("/v1/site/users/{external_id}")
    def site_user(_site: SiteDep, external_id: str, body: SiteUser) -> dict:
        with db.session() as s:
            user = s.execute(select(User).where(User.external_id == external_id)).scalar_one_or_none()
            email = body.email.strip().lower() if body.email else None
            if email and s.execute(select(User).where(User.email_index == crypto.index(email), User.external_id != external_id)).first():
                raise HTTPException(409, "Another user has that e-mail")
            if body.plan is not None and s.get(Plan, body.plan) is None:
                raise HTTPException(422, f"No plan {body.plan}")
            if user is None:
                if not email or not body.plan:
                    raise HTTPException(422, "A new user needs an e-mail and a plan")
                user = User(external_id=external_id, email=email, plan_name=body.plan,
                            unlimited=cfg.is_internal_email(email))
                s.add(user)
            if email:
                user.email = email
            if body.password is not None:
                user.password_hash = hash_password(body.password)
                # A new password closes the sessions of the devices.
                if user.id is not None:
                    s.execute(ApiToken.__table__.delete().where(ApiToken.user_id == user.id))
            if body.plan is not None:
                user.plan_name = body.plan
            if body.paid_until is not None:
                # Stored in UTC without time zone; a date without zone is taken as UTC.
                paid = body.paid_until
                user.paid_until = paid.astimezone(timezone.utc).replace(tzinfo=None) if paid.tzinfo else paid
            if body.enabled is not None:
                user.enabled = body.enabled
                if not body.enabled and user.id is not None:
                    services.tokens.drop_user(user.id)
                    for job_id in scheduler.stop_user_jobs(s, user.id):
                        services.storage.remove_input(job_id)
            s.flush()
            s.refresh(user)
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/site/users/{external_id}")
    def site_get_user(_site: SiteDep, external_id: str) -> dict:
        with db.session() as s:
            user = s.execute(select(User).where(User.external_id == external_id)).scalar_one_or_none()
            if user is None:
                raise HTTPException(404, "No such user")
            return {"user": user_view(user), "account": scheduler.account(s, user, cfg.grace_days).as_dict()}

    @app.get("/v1/admin/alerts")
    def admin_alerts(_admin: AdminDep) -> dict:
        """The open alerts and the ones solved in the last week."""
        with db.session() as s:
            rows = s.execute(select(Alert).where((Alert.resolved_at.is_(None)) |
                                                 (Alert.resolved_at > utcnow() - timedelta(days=7)))
                             .order_by(Alert.started_at.desc()).limit(50)).scalars().all()
            return {"alerts": [{"key": a.key, "message": a.message, "started_at": a.started_at.isoformat() + "Z",
                                "resolved_at": a.resolved_at.isoformat() + "Z" if a.resolved_at else None}
                               for a in rows]}

    @app.get("/v1/admin/queue")
    def admin_queue(_admin: AdminDep) -> dict:
        with db.session() as s:
            rows = s.execute(
                select(Job.job_class, Job.status, func.count()).where(Job.status.in_(("queued", "running")))
                .group_by(Job.job_class, Job.status)
            ).all()
        queue: dict[str, dict[str, int]] = {c: {"queued": 0, "running": 0} for c in JOB_CLASSES}
        for job_class, status, count in rows:
            queue.setdefault(job_class, {"queued": 0, "running": 0})[status] = count
        return {"queue": queue}

    @app.get("/v1/admin/jobs")
    def admin_jobs(_admin: AdminDep, limit: int = 30) -> dict:
        """The jobs waiting and running, and the last ones that ended."""
        limit = max(1, min(limit, 200))
        with db.session() as s:
            active = s.execute(
                select(Job, User.email, Node.name).join(User, User.id == Job.user_id)
                .outerjoin(Node, Node.id == Job.node_id)
                .where(Job.status.in_(scheduler.ACTIVE)).order_by(Job.created_at).limit(200)
            ).all()
            ended = s.execute(
                select(Job, User.email, Node.name).join(User, User.id == Job.user_id)
                .outerjoin(Node, Node.id == Job.node_id)
                .where(Job.status.in_(scheduler.FINAL)).order_by(Job.finished_at.desc()).limit(limit)
            ).all()

            def view(row) -> dict:  # noqa: ANN001
                job, email, node = row
                return {**job_view(job), "user": email, "node": node}

            return {"active": [view(r) for r in active], "ended": [view(r) for r in ended]}

    class ReportFilters(BaseModel):
        """The filters of the lists of reports (query parameters)."""
        status: str = ""
        app: str = ""
        category: str = ""
        kind: str = ""
        version: str = ""
        # The words of the search: free ones and of given fields ("impresora:kobra", reports.SEARCH_FIELDS).
        q: str = ""
        language: str = ""
        printer: str = ""
        system: str = ""
        since: datetime | None = None
        until: datetime | None = None
        # "1": only the reports sent with an account / only the rewarded ones.
        account: str = ""
        rewarded: str = ""

    FiltersDep = Annotated[ReportFilters, Depends()]

    def filtered_rows(s, f: ReportFilters):  # noqa: ANN001, ANN202
        rows = report_rows(s, f.status, f.app, f.category, f.kind, f.version, f.q)
        if f.language:
            rows = [r for r in rows if r.language == f.language]
        if f.printer:
            rows = [r for r in rows if triage.normalize(f.printer) in reports.field_text(r, "printer")]
        if f.system:
            rows = [r for r in rows if reports.system_of(r.os) == f.system]
        if f.since:
            since = f.since.replace(tzinfo=None)
            rows = [r for r in rows if r.created_at >= since]
        if f.until:
            # A date alone includes its whole day.
            until = f.until.replace(tzinfo=None)
            if until.time() == datetime.min.time():
                until += timedelta(days=1)
            rows = [r for r in rows if r.created_at < until]
        if f.account == "1":
            rows = [r for r in rows if r.user_id is not None]
        if f.rewarded == "1":
            rows = [r for r in rows if r.rewarded_at is not None]
        return rows

    def report_rows(s, status: str, app_name: str, category: str = "", kind: str = "", version: str = "",  # noqa: ANN001, ANN202
                    q: str = ""):
        """The reports of the filters (the open ones by default: status=new,confirmed), the newest first; q: words
        that must all be in their texts, as written or translated, or in the fields they name."""
        query = select(BugReport)
        statuses = [x for x in status.split(",") if x] if status else ["new", "confirmed"]
        if any(x not in reports.STATUSES for x in statuses):
            raise HTTPException(422, "Unknown status")
        query = query.where(BugReport.status.in_(statuses))
        if app_name:
            query = query.where(BugReport.app == app_name)
        if category:
            query = query.where(BugReport.category == category)
        if kind:
            query = query.where(BugReport.kind == kind)
        if version:
            query = query.where(BugReport.app_version == version)
        rows = s.execute(query.order_by(BugReport.created_at.desc()).limit(5000)).scalars().all()
        # The texts are encrypted: the words are looked for here.
        return [r for r in rows if reports.matches(r, q)] if q.strip() else rows

    def report_corpus(s) -> triage.Corpus:  # noqa: ANN001
        """The words of all the reports kept, to weigh those of each one."""
        return reports.corpus_of(s.execute(select(BugReport)).scalars().all())

    @app.get("/v1/admin/reports")
    def admin_reports(_admin: AdminDep, filters: FiltersDep) -> dict:
        """The problem reports of the filters grouped, the most frequent first, with their kind of error, key words and
        summary; and the values of all the reports for the filters (kinds of error, versions, apps, printers, systems,
        languages)."""
        with db.session() as s:
            rows = filtered_rows(s, filters)
            everything = s.execute(select(BugReport)).scalars().all()
            groups = reports.catalog(rows, corpus=reports.corpus_of(everything))
            counts = Counter(r.category for r in everything)
            facets = {
                "versions": sorted({r.app_version for r in everything if r.app_version}),
                "apps": sorted({r.app for r in everything}),
                "printers": sorted({p for r in everything if (p := triage.printer_of(r.log or ""))}),
                "systems": sorted({x for r in everything if (x := reports.system_of(r.os))}),
                "languages": sorted({r.language for r in everything if r.language}),
            }
        return {"groups": [g.as_dict() for g in groups], "reports": len(rows),
                "categories": [{"key": k, "name": n, "reports": counts[k]} for k, n, _s in triage.CATEGORIES], **facets}

    @app.get("/v1/admin/reports/list")
    def admin_reports_list(_admin: AdminDep, filters: FiltersDep, limit: int = 50, offset: int = 0) -> dict:
        """Every report of the filters (not grouped), the newest first, with its summary, key words and texts
        (translated when they were in another language)."""
        limit, offset = max(1, min(limit, 200)), max(0, offset)
        with db.session() as s:
            rows = filtered_rows(s, filters)
            corpus = report_corpus(s)
            page = [{**report_view(r, texts=True), **reports.digest(r, corpus), "note": r.note}
                    for r in rows[offset:offset + limit]]
        return {"total": len(rows), "reports": page}

    @app.get("/v1/admin/reports/catalog")
    def admin_reports_catalog(_admin: AdminDep, filters: FiltersDep, format: str = "md") -> Response:
        """The catalog of the problems for the programmers (and the AI that programs with them): Markdown, or JSON lines
        (format=jsonl), one group per line."""
        if format not in ("md", "jsonl"):
            raise HTTPException(422, "format is md or jsonl")
        with db.session() as s:
            groups = reports.catalog(filtered_rows(s, filters), corpus=report_corpus(s))
        if format == "jsonl":
            return PlainTextResponse(reports.to_jsonl(groups), media_type="application/x-ndjson",
                                     headers={"Content-Disposition": 'attachment; filename="tisma-problems.jsonl"'})
        return PlainTextResponse(reports.to_markdown(groups), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="tisma-problems.md"'})

    @app.get("/v1/admin/reports/{report_id}")
    def admin_report(_admin: AdminDep, report_id: str) -> dict:
        with db.session() as s:
            r = s.get(BugReport, report_id)
            if r is None:
                raise HTTPException(404, "No such report")
            return {**report_view(r, texts=True), "note": r.note}

    @app.patch("/v1/admin/reports/{report_id}")
    def admin_update_report(_admin: AdminDep, report_id: str, body: ReportUpdateBody) -> dict:
        """The state of a report (confirmed, fixed...) and a note of the programmers."""
        with db.session() as s:
            r = s.get(BugReport, report_id)
            if r is None:
                raise HTTPException(404, "No such report")
            if body.status is not None:
                r.status = body.status
            if body.note is not None:
                r.note = body.note
            if body.category is not None:
                r.category, r.category_by_hand = body.category, True
            r.updated_at = utcnow()
            return {**report_view(r), "note": r.note}

    def reward_report(s, r: BugReport, days: int | None) -> dict:  # noqa: ANN001
        """Days of the plan for the author of a useful report: by hand, once per report, a few times a year per account
        (the limit keeps the reports from being a way to get the plan free)."""
        if r.user_id is None:
            raise HTTPException(409, "The report was sent without an account: there is nobody to reward")
        # The report locked and read again: another reward of it may have committed since it was loaded.
        s.execute(select(BugReport.id).where(BugReport.id == r.id).with_for_update())
        s.refresh(r)
        if r.rewarded_at is not None:
            raise HTTPException(409, "The report was rewarded already")
        now = utcnow()
        # One reward at a time per account (the row is locked in PostgreSQL), counted with the others of the year.
        s.execute(select(User.id).where(User.id == r.user_id).with_for_update())
        user = s.get(User, r.user_id)
        given = s.execute(select(func.count()).select_from(BugReport).where(
            BugReport.user_id == user.id, BugReport.rewarded_at > now - timedelta(days=365))).scalar()
        if given >= cfg.report_rewards_per_year:
            raise HTTPException(409, f"The account was rewarded {given} times this year (the limit of "
                                     "TISMA_REPORT_REWARDS_PER_YEAR)")
        days = days or cfg.report_reward_days
        start = user.paid_until if user.paid_until and user.paid_until > now else now
        user.paid_until = start + timedelta(days=days)
        r.rewarded_at, r.reward_days, r.updated_at = now, days, now
        if r.status == "new":
            r.status = "confirmed"
        subject, text = reward_message(r.title, days, user.paid_until.strftime("%d/%m/%Y"))
        try:
            services.mailer.send(user.email, subject, text)
        except Exception:  # noqa: BLE001 (the reward stays; the e-mail is only a notice)
            log.exception("Could not send the notice of a reward")
        return {"report": r.id, "days": days, "paid_until": user.paid_until.isoformat() + "Z"}

    @app.post("/v1/admin/reports/{report_id}/reward")
    def admin_reward_report(_admin: AdminDep, report_id: str, body: RewardBody) -> dict:
        """The team found the report useful: days of the plan for its author."""
        with db.session() as s:
            r = s.get(BugReport, report_id)
            if r is None:
                raise HTTPException(404, "No such report")
            return reward_report(s, r, body.days)

    @app.post("/v1/admin/report-groups/{signature}/reward")
    def admin_reward_report_group(_admin: AdminDep, signature: str, body: RewardBody) -> dict:
        """The same for the first report of a problem that came with an account and was not rewarded (the first one
        who told it)."""
        with db.session() as s:
            r = s.execute(select(BugReport).where(BugReport.signature == signature, BugReport.user_id.is_not(None),
                                                  BugReport.rewarded_at.is_(None))
                          .order_by(BugReport.created_at).limit(1)).scalar_one_or_none()
            if r is None:
                raise HTTPException(404, "No report of this problem with an account left to reward")
            return reward_report(s, r, body.days)

    @app.patch("/v1/admin/report-groups/{signature}")
    def admin_update_report_group(_admin: AdminDep, signature: str, body: ReportUpdateBody) -> dict:
        """The same for every report of a problem (all the reports with its signature)."""
        values: dict[str, Any] = {"updated_at": utcnow()}
        if body.status is not None:
            values["status"] = body.status
        if body.note is not None:
            values["note"] = body.note
        if body.category is not None:
            values["category"], values["category_by_hand"] = body.category, True
        with db.session() as s:
            count = s.execute(update(BugReport).where(BugReport.signature == signature).values(**values)).rowcount
        if not count:
            raise HTTPException(404, "No reports with this signature")
        return {"updated": count}

    # ---- the administration panel (a single page that uses the API above)
    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_panel(_local: Annotated[None, Depends(local_only)]) -> HTMLResponse:
        page, headers = static_page("admin.html")
        return HTMLResponse(page, headers=headers)

    return app
