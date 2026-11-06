"""Protection of the master against floods and abuse, before FastAPI reads anything (an ASGI middleware).

FastAPI reads a whole upload before it checks the token, so without this an address without account could make the
master receive and store large files over and over. Here, before the body is read:

- uploads (projects of the users, results of the nodes) need a valid token, a Content-Length within the limit and a free
  place (a few at a time in all and per account);
- the pages without account (login, registration, payment notifications...) are limited per address;
- an address that collects refused requests (bad tokens, unknown pages, limits) is blocked for a while: scanners and
  floods are answered with a short 403 without touching the database.

The local network (the computing nodes, the administrator) is never blocked. This protects the application; a flood
larger than the internet link can only be stopped by the provider of the link (see README.md).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from .ratelimit import RateLimiter

Scope = dict
Receive = Callable
Send = Callable

# Uploads: (method, path) -> kind of token.
UPLOADS = (
    ("POST", re.compile(r"^/v1/jobs$"), "user"),
    ("POST", re.compile(r"^/v1/node/jobs/[^/]+/result$"), "node"),
    # Remote printers: a G-code to print sent by the phone, an image of a camera sent by the bridge.
    ("POST", re.compile(r"^/v1/remote/bridges/[^/]+/printers/[^/]+/print$"), "user"),
    ("POST", re.compile(r"^/v1/bridges/[^/]+/printers/[^/]+/camera$"), "user"),
    # A piece of a 3D scan (scans.py).
    ("PUT", re.compile(r"^/v1/scans/[^/]+/data$"), "scan"),
)
# Pages that need no account (limited per address).
ANONYMOUS = re.compile(r"^/(v1/(health|plans|auth/.*|billing/webhook/.*|billing/play/notify|billing/test/.*|reports)|cuenta.*|admin)?$")
# Small bodies that need no account: (path, largest body [KB]).
SMALL = ((re.compile(r"^/v1/reports$"), 512), (re.compile(r"^/v1/node/join$"), 16))
# Answers that count against the address that caused them.
REFUSED = {400, 401, 403, 404, 405, 411, 413, 422, 429}


class Bans:
    def __init__(self, threshold: int, minutes: int) -> None:
        self.threshold = threshold
        self.seconds = minutes * 60
        self._refused = RateLimiter(60.)
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def banned(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            until = self._until.get(ip)
            if until is None:
                return False
            if until <= now:
                del self._until[ip]
                return False
            return True

    def refused(self, ip: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        # The refusal number `threshold` within a minute blocks the address.
        if not self._refused.allow(ip, max(0, self.threshold - 1), now):
            with self._lock:
                self._until[ip] = now + self.seconds

    def active(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for u in self._until.values() if u > now)


class Uploads:
    """Places for uploads, in all and per account."""

    def __init__(self, total: int, per_account: int) -> None:
        self.total, self.per_account = total, per_account
        self._all = 0
        self._by: dict[str, int] = {}
        self._lock = threading.Lock()

    def take(self, who: str) -> bool:
        with self._lock:
            if self._all >= self.total or self._by.get(who, 0) >= self.per_account:
                return False
            self._all += 1
            self._by[who] = self._by.get(who, 0) + 1
            return True

    def give(self, who: str) -> None:
        with self._lock:
            self._all -= 1
            n = self._by.get(who, 1) - 1
            if n:
                self._by[who] = n
            else:
                self._by.pop(who, None)


async def _answer(send: Send, status: int, text: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = ('{"detail":"' + text + '"}').encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")] + (headers or [])})
    await send({"type": "http.response.body", "body": body})


def _nosniff(send: Send) -> Send:
    """Every answer with X-Content-Type-Options: nosniff (a browser never takes a download or JSON for a page)."""
    async def wrapped(message: dict) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            if not any(k.lower() == b"x-content-type-options" for k, _ in headers):
                headers.append((b"x-content-type-options", b"nosniff"))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


class Guard:
    def __init__(self, app, *, settings, client_ip: Callable[[Scope], str], is_local: Callable[[str], bool],
                 token_owner: Callable[[str, str], str | None], run_sync: Callable) -> None:
        self.app = app
        self.settings = settings
        self.client_ip = client_ip
        self.is_local = is_local
        # The account ("u:<id>") or node ("n:<id>") of a valid token, None otherwise.
        self.token_owner = token_owner
        self.run_sync = run_sync
        self.bans = Bans(settings.ban_threshold, settings.ban_minutes)
        self.uploads = Uploads(settings.max_uploads, settings.max_uploads_per_account)
        self.anonymous = RateLimiter(60.)

    async def _limited(self, scope: Scope, receive: Receive, send: Send, refuse_counted: Callable[[int], None],
                       limit: int) -> None:
        """Any other request: its body, declared or sent in parts, at most `limit` bytes (413 beyond)."""
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length", b"").decode("latin-1")
        if length.isdigit() and int(length) > limit:
            refuse_counted(413)
            await _answer(send, 413, "The request is too large")
            return
        state = {"read": 0, "over": False, "started": False}

        async def limited_receive() -> dict:
            message = await receive()
            if message["type"] == "http.request" and not state["over"]:
                state["read"] += len(message.get("body", b""))
                if state["read"] > limit:
                    # The application sees the client gone and stops reading; the answer is given here.
                    state["over"] = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message: dict) -> None:
            if state["over"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not state["over"]:
                raise
        if state["over"] and not state["started"]:
            refuse_counted(413)
            await _answer(send, 413, "The request is too large")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = _nosniff(send)
        ip = self.client_ip(scope)
        local = self.is_local(ip)
        if not local and self.bans.banned(ip):
            await _answer(send, 403, "Blocked for a while: too many refused requests")
            return

        def refuse_counted(status: int) -> None:
            if not local and status in REFUSED:
                self.bans.refused(ip)

        async def counted_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                refuse_counted(message["status"])
            await send(message)

        path, method = scope.get("path", ""), scope.get("method", "")
        if not local and ANONYMOUS.match(path) and not self.anonymous.allow(ip, self.settings.anonymous_per_minute):
            refuse_counted(429)
            await _answer(send, 429, "Too many requests: wait a minute", [(b"retry-after", b"60")])
            return

        upload = next((kind for m, rx, kind in UPLOADS if method == m and rx.match(path)), None)
        small = next((kb for rx, kb in SMALL if method == "POST" and rx.match(path)), None)
        if upload is None and small is None:
            await self._limited(scope, receive, counted_send, refuse_counted, self.settings.max_body_kb << 10)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if upload is None:
            length = headers.get("content-length", "")
            if not length.isdigit():
                refuse_counted(411)
                await _answer(send, 411, "Content-Length is required")
                return
            if int(length) > small << 10:
                refuse_counted(413)
                await _answer(send, 413, "The request is too large")
                return
            await self.app(scope, receive, counted_send)
            return
        limit = {"user": self.settings.max_upload_mb, "scan": self.settings.scan_chunk_mb}.get(
            upload, self.settings.max_result_mb) << 20
        length = headers.get("content-length", "")
        if not length.isdigit():
            refuse_counted(411)
            await _answer(send, 411, "Content-Length is required")
            return
        if int(length) > limit + (1 << 20):
            refuse_counted(413)
            await _answer(send, 413, "The upload is too large")
            return
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        who = await self.run_sync(self.token_owner, "node" if upload == "node" else "user", token) if token else None
        if who is None:
            refuse_counted(401)
            await _answer(send, 401, "Invalid access token", [(b"www-authenticate", b"Bearer")])
            return
        # The result of a node is not one of the uploads of the users: it never waits for their places (a result
        # refused under load would fail a job already computed). A node sends as many as its slots.
        if upload == "node":
            await self.app(scope, receive, counted_send)
            return
        if not self.uploads.take(who):
            refuse_counted(429)
            await _answer(send, 429, "Too many uploads at the same time: try again in a moment", [(b"retry-after", b"5")])
            return
        try:
            await self.app(scope, receive, counted_send)
        finally:
            self.uploads.give(who)
