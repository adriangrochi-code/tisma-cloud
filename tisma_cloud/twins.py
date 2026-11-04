"""Two node programs with one token (a copied node.toml, an old process left running): they fight over the jobs, each
one's heartbeat saying the other's jobs are not running. Each node program sends a random instance id with its
heartbeat; two instances that keep answering at the same time are twins. A restart is not: the old instance stops
before the new one starts (or at most one heartbeat later).

Kept in memory, like presence.py: one master process."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

# How long a silent instance is remembered, and how long twins stay marked after their last overlap.
WINDOW = timedelta(seconds=120)
# A new instance is trusted after this long (more than two heartbeats of 15 s): an older one still alive has answered
# by then, and they are known to be twins.
SETTLE = timedelta(seconds=35)


class Twins:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # node id -> instance -> (first seen, last seen)
        self._seen: dict[int, dict[str, tuple[datetime, datetime]]] = {}
        # node id -> marked as twins until
        self._until: dict[int, datetime] = {}

    def seen(self, node_id: int, instance: str, now: datetime) -> tuple[bool, bool]:
        """Notes a heartbeat of an instance; returns whether the node has twins now, and whether this instance is
        settled (seen for SETTLE: what its heartbeat says it does not run can be trusted)."""
        with self._lock:
            instances = self._seen.setdefault(node_id, {})
            for other, (_, last) in list(instances.items()):
                if last < now - WINDOW:
                    del instances[other]
            first, _ = instances.get(instance, (now, now))
            instances[instance] = (first, now)
            # Two instances both seen after the later of them started: they run at the same time.
            if len(instances) > 1:
                start = max(f for f, _ in instances.values())
                if sum(1 for _, last in instances.values() if last > start) > 1:
                    self._until[node_id] = now + WINDOW
            until = self._until.get(node_id)
            if until is not None and until < now:
                del self._until[node_id]
                until = None
            return until is not None, now - first >= SETTLE

    def twins(self, now: datetime) -> set[int]:
        with self._lock:
            return {n for n, until in self._until.items() if until >= now}
