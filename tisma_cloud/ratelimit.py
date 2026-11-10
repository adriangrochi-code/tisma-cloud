"""Limits of requests per minute, in the memory of the master process (one process of the API; with several, each
one counts its own requests)."""

from __future__ import annotations

import threading
import time
from collections import deque

CLEANUP_S = 600.


class RateLimiter:
    def __init__(self, window_s: float = 60.) -> None:
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        """Counts a request of the key; False when it already made limit requests in the window."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            # The keys whose window ended are forgotten every ten windows, at most every CLEANUP_S (a daily limit
            # per e-mail, a key anyone can make up, would otherwise keep them ten days).
            if now - self._last_cleanup > min(self.window_s * 10, CLEANUP_S):
                self._cleanup(now)
            return True

    def used(self, key: str, now: float | None = None) -> int:
        """How many requests of the key are in the window now, without counting one more."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            return sum(1 for h in hits if h > now - self.window_s) if hits else 0

    def _cleanup(self, now: float) -> None:
        self._last_cleanup = now
        for key in [k for k, h in self._hits.items() if not h or h[-1] <= now - self.window_s]:
            del self._hits[key]
