"""The bridges in the memory of the master, so that a bridge waiting for commands (most of them, most of the time) costs
no query to the database:

- whose each bridge is (a bridge never changes account);
- which bridges may have commands waiting (set when a command is made, read by the bridge's next look);
- when each bridge was last seen and its last state, written to the database in batches (flush());
- who is looking: the phone listing an account's printers (the bridges then send their state often) and the cameras
  being watched.

The database stays the record: after a restart the master reads the commands waiting from it, and what is in memory
only makes the database be asked less. One master process (as deployed); a second one would see the bridges of the
first through the database, a few seconds late."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

# The bridge sends its state this often while someone looks at its printers, and otherwise.
LIVE_STATE_S = 3
IDLE_STATE_S = 30
# Someone looks while the phone listed the printers (or asked a camera image) this recently.
LOOK_S = 30
# A state that only changes the readings (temperatures, progress) is written at most this often; one that changes the
# printers (added, removed, renamed, other actions) at once.
STATE_WRITE_S = 60


@dataclass
class _Bridge:
    user_id: int
    seen: datetime | None = None
    # Seen since the last flush.
    dirty_seen: bool = False
    # The cleaned printers (remote.clean_printers) and name last sent, and whether they still have to be written.
    printers: list[dict] | None = None
    name: str | None = None
    dirty_state: bool = False
    structure: tuple | None = None
    written_at: float = 0.
    # Printer id -> until (time.monotonic()) someone looks at its camera.
    watch: dict[str, float] = field(default_factory=dict)
    # The bridge was asked to send its state now (someone started looking).
    state_wanted: bool = False


def structure_of(printers: list[dict]) -> tuple:
    """What of the printers is not a reading: which ones, their names, kinds, actions and camera."""
    return tuple((p["id"], p["name"], p["kind"], p["camera"], tuple(p["actions"])) for p in printers)


class Presence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bridges: dict[str, _Bridge] = {}
        self._queued: set[str] = set()
        # Account -> until (time.monotonic()) someone looks at its printers.
        self._looking: dict[int, float] = {}

    # ---- owner
    def owner(self, bridge_id: str) -> int | None:
        with self._lock:
            b = self._bridges.get(bridge_id)
            return b.user_id if b else None

    def known(self, bridge_id: str, user_id: int) -> None:
        with self._lock:
            if bridge_id not in self._bridges:
                self._bridges[bridge_id] = _Bridge(user_id)

    def forget(self, bridge_id: str) -> None:
        with self._lock:
            self._bridges.pop(bridge_id, None)
            self._queued.discard(bridge_id)

    # ---- seen and state
    def seen(self, bridge_id: str, now: datetime) -> None:
        with self._lock:
            b = self._bridges.get(bridge_id)
            if b is not None:
                b.seen, b.dirty_seen = now, True

    def last_seen(self, bridge_id: str) -> datetime | None:
        with self._lock:
            b = self._bridges.get(bridge_id)
            return b.seen if b else None

    def set_state(self, bridge_id: str, name: str, printers: list[dict], now: datetime) -> bool:
        """Keeps the state sent by the bridge. True when it has to be written now (the printers changed, or it was
        not written for STATE_WRITE_S); otherwise flush() writes it later."""
        structure = structure_of(printers)
        with self._lock:
            b = self._bridges[bridge_id]
            b.seen, b.printers, b.name, b.state_wanted = now, printers, name, False
            write = structure != b.structure or time.monotonic() - b.written_at >= STATE_WRITE_S
            if write:
                b.structure, b.written_at, b.dirty_state, b.dirty_seen = structure, time.monotonic(), False, False
            else:
                b.dirty_state = b.dirty_seen = True
            return write

    def printers(self, bridge_id: str) -> list[dict] | None:
        """The last printers sent by the bridge, newer than the database's when known."""
        with self._lock:
            b = self._bridges.get(bridge_id)
            return b.printers if b else None

    def take_dirty(self) -> tuple[dict[str, datetime], dict[str, tuple[str, list[dict]]]]:
        """What flush() has to write: last seen of the bridges, and the states not written yet."""
        seen: dict[str, datetime] = {}
        states: dict[str, tuple[str, list[dict]]] = {}
        with self._lock:
            for bridge_id, b in self._bridges.items():
                if b.dirty_seen and b.seen is not None:
                    seen[bridge_id] = b.seen
                    b.dirty_seen = False
                if b.dirty_state and b.printers is not None:
                    states[bridge_id] = (b.name or "", b.printers)
                    b.dirty_state, b.written_at = False, time.monotonic()
        return seen, states

    # ---- commands
    def mark_queued(self, bridge_id: str) -> None:
        with self._lock:
            self._queued.add(bridge_id)

    def take_queued(self, bridge_id: str) -> bool:
        """Whether the bridge may have commands waiting; the mark is cleared before the database is read, so that a
        command made meanwhile marks it again."""
        with self._lock:
            if bridge_id in self._queued:
                self._queued.discard(bridge_id)
                return True
            return False

    def set_queued(self, bridge_ids: set[str]) -> None:
        """The bridges with commands waiting according to the database (at start and now and then)."""
        with self._lock:
            self._queued |= bridge_ids

    # ---- who looks
    def looking_at(self, user_id: int) -> list[str]:
        """The phone lists the account's printers: its bridges send their state often for a while. Returns the bridges
        that were sending it slowly (to be woken now)."""
        now = time.monotonic()
        with self._lock:
            was = self._looking.get(user_id, 0.) > now
            self._looking[user_id] = now + LOOK_S
            if was:
                return []
            wake = [bridge_id for bridge_id, b in self._bridges.items() if b.user_id == user_id]
            for bridge_id in wake:
                self._bridges[bridge_id].state_wanted = True
            return wake

    def state_every(self, bridge_id: str) -> int:
        """How often the bridge should send its state now [s]."""
        now = time.monotonic()
        with self._lock:
            b = self._bridges.get(bridge_id)
            if b is None:
                return IDLE_STATE_S
            live = self._looking.get(b.user_id, 0.) > now or any(until > now for until in b.watch.values())
            return LIVE_STATE_S if live else IDLE_STATE_S

    def take_state_wanted(self, bridge_id: str) -> bool:
        with self._lock:
            b = self._bridges.get(bridge_id)
            if b is None or not b.state_wanted:
                return False
            b.state_wanted = False
            return True

    def watch(self, bridge_id: str, printer_id: str, seconds: float) -> bool:
        """Someone looks at the camera of the printer: the bridge sends its images for a while. True when nobody was
        looking at it (the bridge is to be told now)."""
        now = time.monotonic()
        with self._lock:
            b = self._bridges.get(bridge_id)
            if b is None:
                return False
            b.watch = {p: until for p, until in b.watch.items() if until > now}
            new = printer_id not in b.watch
            b.watch[printer_id] = now + seconds
            if new:
                b.state_wanted = True
            return new

    def watched(self, bridge_id: str) -> list[str]:
        now = time.monotonic()
        with self._lock:
            b = self._bridges.get(bridge_id)
            return sorted(p for p, until in b.watch.items() if until > now) if b else []

    def sizes(self) -> dict[str, int]:
        with self._lock:
            return {"bridges": len(self._bridges), "queued": len(self._queued), "looking": len(self._looking)}

    def drop_old_looks(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._looking = {u: until for u, until in self._looking.items() if until > now}
