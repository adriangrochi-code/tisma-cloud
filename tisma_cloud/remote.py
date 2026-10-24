"""Remote printers: a computer at home (Tisma Slicer left open, the "bridge") relays the printers of its local network
to the account, so that the phone sees and commands them from anywhere, without opening ports at home.

- The bridge sends its printers and their state every few seconds (PUT /v1/bridges/{id}) and waits for commands
  (GET /v1/bridges/{id}/commands, a long poll); it downloads the file of a print, runs the command on the printer and
  tells how it ended.
- The phone lists the printers of its bridges (GET /v1/remote/printers), sends a G-code to print, pauses, resumes or
  stops, and looks at the camera (the bridge sends images only while someone looks, and they are kept in memory only).
- The bridge sends its state every 3 s while someone looks at its printers and every 30 s otherwise (state_s in the
  answers); when the phone starts looking, the bridge's wait for commands ends at once to send it (presence.py).
- A command the bridge does not take within a few minutes fails: a print never starts long after it was asked for.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Bridge, RemoteCommand, User, utcnow

ACTIONS = ("print", "pause", "resume", "stop")
# Ids made by the bridge (its own, those of its printers): plain, so that they can name files.
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PRINTERS = 32
MAX_BRIDGES = 10
# The files a printer prints (Bambu Lab printers get the G-code packed by the bridge).
PRINT_EXTENSIONS = (".gcode", ".bgcode")
# A bridge is online while it asked for commands or sent its state this recently.
ONLINE_S = 60
# A bridge not seen for this long is forgotten (with its printers); it is made again if it comes back [days].
FORGET_BRIDGE_DAYS = 180
# The camera: the bridge sends images while someone looked at it this recently, and an image is shown this long.
WATCH_S = 30
MAX_IMAGE_BYTES = 2 << 20

# The state of a printer, as the phone shows it (the same fields as its own printers, PrinterLink.kt).
_STATE_TEXT = {"state": 20, "job_name": 200}
_STATE_NUMBERS = ("percent", "remaining_min", "nozzle_temp", "nozzle_target", "bed_temp", "bed_target", "layer",
                  "total_layers")


def clean_printers(printers: object) -> list[dict]:
    """The printers sent by a bridge, keeping only known fields of the right kind and size."""
    out: list[dict] = []
    if not isinstance(printers, list):
        return out
    for p in printers[:MAX_PRINTERS]:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not ID.match(p["id"]):
            continue
        state_in = p.get("state") if isinstance(p.get("state"), dict) else {}
        state: dict = {}
        for key, size in _STATE_TEXT.items():
            if isinstance(state_in.get(key), str):
                state[key] = state_in[key][:size]
        for key in _STATE_NUMBERS:
            value = state_in.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                state[key] = value
        actions = [a for a in p.get("actions", []) if a in ACTIONS] if isinstance(p.get("actions"), list) else []
        out.append({"id": p["id"], "name": str(p.get("name", ""))[:100], "kind": str(p.get("kind", ""))[:30],
                    "camera": bool(p.get("camera")), "actions": actions, "state": state})
    return out


def printer_allowance(user: User, paid: bool) -> int:
    """How many printers of the account can be used from afar: those of the plan and the ones paid for on top, while
    the plan is paid (a free plan: its own). Administrators and internal accounts: all of them."""
    if user.is_unlimited:
        return MAX_PRINTERS * MAX_BRIDGES
    if user.plan.price_cents <= 0:
        return user.plan.remote_printers
    return user.plan.remote_printers + user.extra_printers if paid else 0


def allowed_printers(session: Session, user: User, allowance: int) -> set[tuple[str, str]]:
    """The (bridge, printer) pairs that can be used from afar: the first ones of the oldest bridges."""
    out: set[tuple[str, str]] = set()
    for bridge in session.execute(select(Bridge).where(Bridge.user_id == user.id).order_by(Bridge.created_at)).scalars():
        for printer in bridge.printer_list:
            if len(out) >= allowance:
                return out
            out.add((bridge.id, printer["id"]))
    return out


def is_online(last_seen: datetime | None, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return last_seen is not None and (now - last_seen).total_seconds() < ONLINE_S


def bridge_view(bridge: Bridge, now: datetime | None = None, allowed: set[tuple[str, str]] | None = None,
                printers: list[dict] | None = None, last_seen: datetime | None = None) -> dict:
    """The bridge as the phone shows it; with allowed, the printers beyond the plan are locked. printers and last_seen:
    newer than the database's (presence.py)."""
    printers = bridge.printer_list if printers is None else printers
    last_seen = max(d for d in (bridge.last_seen, last_seen) if d is not None) \
        if bridge.last_seen or last_seen else None
    if allowed is not None:
        printers = [{**p, "locked": (bridge.id, p["id"]) not in allowed} for p in printers]
    return {"id": bridge.id, "name": bridge.name, "online": is_online(last_seen, now),
            "last_seen": last_seen.isoformat() + "Z" if last_seen else None,
            "printers": printers}


def command_view(c: RemoteCommand) -> dict:
    return {"id": c.id, "bridge_id": c.bridge_id, "printer_id": c.printer_id, "action": c.action,
            "file_name": c.file_name, "status": c.status, "error": c.error,
            "created_at": c.created_at.isoformat() + "Z", "updated_at": c.updated_at.isoformat() + "Z"}


class Images:
    """The last image of each camera someone looks at, in memory only: images of the homes of the users are never
    written to disk (nor to the backups), and are gone WATCH_S after they arrived. At most budget bytes in all (the
    oldest go first)."""

    def __init__(self, budget: int = 256 << 20) -> None:
        self.budget = budget
        self._lock = threading.Lock()
        self._images: dict[tuple[str, str], tuple[float, bytes]] = {}
        self._bytes = 0

    def put(self, bridge_id: str, printer_id: str, data: bytes) -> None:
        with self._lock:
            old = self._images.pop((bridge_id, printer_id), None)
            self._bytes -= len(old[1]) if old else 0
            self._images[(bridge_id, printer_id)] = (time.monotonic(), data)
            self._bytes += len(data)
            # Insertion order is age order: the oldest images go first.
            while self._bytes > self.budget and self._images:
                key = next(iter(self._images))
                self._bytes -= len(self._images.pop(key)[1])

    def get(self, bridge_id: str, printer_id: str) -> bytes | None:
        with self._lock:
            item = self._images.get((bridge_id, printer_id))
        return item[1] if item and time.monotonic() - item[0] <= WATCH_S else None

    def drop_bridge(self, bridge_id: str) -> None:
        with self._lock:
            for key in [k for k in self._images if k[0] == bridge_id]:
                self._bytes -= len(self._images.pop(key)[1])

    def drop_old(self) -> None:
        limit = time.monotonic() - WATCH_S
        with self._lock:
            for key in [k for k, (t, _) in self._images.items() if t < limit]:
                self._bytes -= len(self._images.pop(key)[1])

    def size(self) -> int:
        with self._lock:
            return self._bytes


class RemoteFiles:
    """The files to print, in storage_dir/remote, until the bridge took them."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "remote"
        self.root.mkdir(parents=True, exist_ok=True)

    def print_file(self, command_id: str) -> Path:
        # Command ids are UUIDs made by the server.
        return self.root / f"{command_id}.print"

    def remove_command(self, command_id: str) -> None:
        self.print_file(command_id).unlink(missing_ok=True)


def expire(session: Session, files: RemoteFiles, minutes: int, now: datetime | None = None) -> int:
    """The commands not taken (or not answered) by their bridge in time fail, and their files go."""
    now = now or utcnow()
    late = session.execute(select(RemoteCommand).where(
        RemoteCommand.status.in_(("queued", "sent")),
        RemoteCommand.updated_at < now - timedelta(minutes=minutes))).scalars().all()
    for c in late:
        c.error = ("The bridge did not take the command in time (is the computer off, or Tisma closed?)"
                   if c.status == "queued" else "The bridge did not tell how the command ended")
        c.status = "failed"
        c.updated_at = now
        files.remove_command(c.id)
    return len(late)
