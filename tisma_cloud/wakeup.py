"""Long polls woken as soon as there is something for them (a command for a bridge, a job for the nodes) instead of each
one asking the database every second: a waiting bridge or node costs an idle task, not a query per second.

The answer is still read from the database: a notification only says "look now". A wait looks when it starts and when it
is notified, not in between (measured: the looks every few seconds of 1000 waiting bridges kept a core busy). What another
process writes (several masters on one database) or what is written without a notification is seen by the next wait,
which starts as soon as the previous one ends (at most claim_wait_s or 25 s later)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

T = TypeVar("T")


class Wakeup:
    """Waiters by key ("bridge:<id>", "jobs"). notify() can be called from any thread (the endpoints that are not async
    run in a pool of threads), after the change is committed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    @contextmanager
    def waiter(self, key: str) -> Iterator[asyncio.Event]:
        entry = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiting.setdefault(key, set()).add(entry)
        try:
            yield entry[1]
        finally:
            with self._lock:
                entries = self._waiting.get(key)
                if entries is not None:
                    entries.discard(entry)
                    if not entries:
                        del self._waiting[key]

    def notify(self, key: str) -> None:
        with self._lock:
            entries = list(self._waiting.get(key, ()))
        for loop, event in entries:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # The loop of that request was closed: nobody waits there any more.

    def waiting(self) -> int:
        with self._lock:
            return sum(len(e) for e in self._waiting.values())

    async def poll(self, key: str, check: Callable[[], Awaitable[T]], wait_s: float,
                   recheck_s: float | None = None) -> T:
        """check() until it gives something or wait_s passes (then its last answer). It runs at once and after each
        notification of key (and every recheck_s, if given)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0., wait_s)
        # Registered before the first look, and cleared before each one: a notification never falls between the look
        # and the wait.
        with self.waiter(key) as event:
            while True:
                event.clear()
                result = await check()
                left = deadline - loop.time()
                if result or left <= 0:
                    return result
                try:
                    await asyncio.wait_for(event.wait(), left if recheck_s is None else min(left, recheck_s))
                except asyncio.TimeoutError:
                    if recheck_s is None or loop.time() >= deadline:
                        return result
