"""Uploads checked before the master keeps them: a project (3MF) must be only a project, a G-code sent to a printer
from afar must not change the printer itself, and every file goes through ClamAV when TISMA_CLAMD is set.

What is refused raises Threat, with a reason for the person who sent it; the administrators get an alert (alerts.py).
The nodes still treat every project as hostile (node/runners.py: no post-processing scripts, size limits, a sandbox).
"""

from __future__ import annotations

import re
import socket
import struct
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class Threat(Exception):
    """The file is refused (the message says why, for the person who sent it)."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class ScanUnavailable(Exception):
    """The antivirus did not answer: the file is not accepted unchecked."""


# ---- projects (3MF: a zip with XML parts, thumbnails and the settings of the slicer)
MAX_ENTRIES = 10_000
# Decompressed / compressed size of one part beyond which it is a "zip bomb" (when it decompresses to more than
# BOMB_MIN_BYTES; small parts of repeated text compress very well and are fine).
MAX_RATIO = 1000
BOMB_MIN_BYTES = 50 << 20
# Parts read as XML: declarations of entities (XXE, "billion laughs") are refused; Tisma never writes them.
XML_PARTS = (".model", ".rels", ".xml", ".config")
XML_DANGER = re.compile(rb"<!(DOCTYPE|ENTITY)", re.I)
# Programs: executables of Windows (MZ), Linux (ELF), macOS (Mach-O, fat) and scripts (#!).
PROGRAM_MAGIC = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                 b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"#!")
PROGRAM_EXTENSIONS = (".exe", ".dll", ".scr", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                      ".hta", ".msi", ".lnk", ".jar", ".sh", ".py", ".pl", ".so", ".dylib")


def check_project(path: Path) -> None:
    """Raises Threat unless the file is a zip with only the parts of a project."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise Threat("The project is not a 3MF file", "project") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise Threat("The project has too many files inside", "project")
        for info in infos:
            name = info.filename
            lower = name.lower()
            if name.startswith("/") or "\\" in name or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
                raise Threat("A file inside the project has a dangerous path", "project")
            if info.flag_bits & 0x1:
                raise Threat("The project has encrypted files inside", "project")
            if info.file_size > BOMB_MIN_BYTES and info.file_size > MAX_RATIO * max(1, info.compress_size):
                raise Threat("The project decompresses to much more than its size (a zip bomb)", "project")
            if info.is_dir():
                continue
            if lower.endswith(PROGRAM_EXTENSIONS):
                raise Threat("The project has a program inside", "project")
            try:
                with archive.open(info) as part:
                    head = part.read(64 << 10 if lower.endswith(XML_PARTS) else 8)
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
                raise Threat("The project is damaged", "project") from None
            if head.startswith(PROGRAM_MAGIC):
                raise Threat("The project has a program inside", "project")
            if lower.endswith(XML_PARTS) and XML_DANGER.search(head):
                raise Threat("The project has XML with declarations Tisma never writes", "project")


# ---- G-code sent to a printer from afar
@dataclass(frozen=True)
class Command:
    what: str      # for people (English; the clients translate the reason)
    blocked: bool  # refused; otherwise only reported


# Commands that change the printer itself rather than print. Sources: the G-code documentation of Marlin
# (https://marlinfw.org/docs/gcode/), Klipper (https://www.klipper3d.org/G-Codes.html) and the G-Code Shell Command
# extension of Klipper (https://docs.kalico.gg/G-Code_Shell_Command.html). Refused: those no profile of a printer uses
# (checked on the vendor profiles of Tisma). Reported only: those some vendors put in their start G-code (M500 in
# HCTECH, Trimaker and Longer; M502 in MakerGear; SET_KINEMATIC_POSITION in InfiMech and FlyingBear).
GCODE_COMMANDS = {
    "M997": Command("updates the firmware of the printer", True),
    "M28": Command("writes a file on the card of the printer", True),
    "M29": Command("writes a file on the card of the printer", True),
    "M30": Command("deletes a file from the card of the printer", True),
    "M32": Command("runs another file of the card of the printer", True),
    "SAVE_CONFIG": Command("overwrites the configuration of the printer and restarts it", True),
    "FORCE_MOVE": Command("moves a motor without checking the limits", True),
    "RUN_SHELL_COMMAND": Command("runs a program on the computer of the printer", True),
    "M500": Command("saves the settings of the printer", False),
    "M502": Command("resets the settings of the printer to the factory ones", False),
    "SET_KINEMATIC_POSITION": Command("sets the position without homing", False),
    "RESTART": Command("restarts the printer", False),
    "FIRMWARE_RESTART": Command("restarts the printer", False),
}
# A command at the start of a line (after an optional line number "N123"), not in a comment.
_COMMAND = re.compile(rb"^[ \t]*(?:N\d+[ \t]+)?(" + b"|".join(re.escape(c.encode()) for c in GCODE_COMMANDS)
                      + rb")(?![A-Za-z0-9_.])", re.M | re.I)
# The fast way: each command searched as bytes (bytes.find, in C) in the block made upper case, and only its hits
# checked in Python. A block with more hits than this (a file made to slow the check down) is read with _COMMAND.
_TOKENS = [(c.encode(), c) for c in GCODE_COMMANDS]
_MAX_HITS = 5000
_NAME_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")


def _at_line_start(data: bytes, pos: int) -> bool:
    """data[pos:] starts its line, after blanks or a line number ("N123 "): not a comment nor part of a word."""
    i = pos - 1
    while i >= 0 and data[i] in (32, 9):
        i -= 1
    if i >= 0 and data[i] != 10:
        if i == pos - 1:
            return False
        j = i
        while j >= 0 and 48 <= data[j] <= 57:
            j -= 1
        if j == i or j < 0 or data[j] != 78:  # "N" and its digits
            return False
        i = j - 1
        while i >= 0 and data[i] in (32, 9):
            i -= 1
    return i < 0 or data[i] == 10


def _commands_in(data: bytes) -> list[tuple[int, str]]:
    """(position, command) of the commands of GCODE_COMMANDS in a block of whole lines, in order."""
    upper = data.upper()
    hits: list[tuple[int, str]] = []
    candidates = 0
    for token, name in _TOKENS:
        pos = upper.find(token)
        while pos >= 0:
            candidates += 1
            if candidates > _MAX_HITS:
                return [(m.start(1), m.group(1).decode().upper()) for m in _COMMAND.finditer(data)]
            end = pos + len(token)
            if (end == len(upper) or upper[end] not in _NAME_CHARS) and _at_line_start(upper, pos):
                hits.append((pos, name))
            pos = upper.find(token, pos + 1)
    return sorted(hits)


@dataclass
class Finding:
    command: str
    line: int
    what: str
    blocked: bool


# A line longer than this is not a command (it is skipped, not kept in memory).
LONG_LINE = 1 << 20
# A G-code larger than this is read by several processes at once, one part each (check_gcode).
PARALLEL_BYTES = 32 << 20


def scan_range(path: str, start: int, end: int, skip_first: bool = False,
               max_findings: int = 20) -> tuple[list[Finding], int]:
    """The commands of GCODE_COMMANDS in the bytes [start, end) of a G-code, start being the start of a line (or, with
    skip_first, inside a line too long to be a command). Line numbers counted from start; returns them with the
    newlines of the range. Stops at the first refused command. Runs in another process for large files."""
    findings: list[Finding] = []
    lines = 0             # newlines before the block being read
    pending = b""         # the start of a line not ended yet
    skipping = skip_first  # inside a line too long to be a command

    def scan(data: bytes) -> bool:
        for pos, name in _commands_in(data):
            info = GCODE_COMMANDS[name]
            findings.append(Finding(name, lines + data.count(b"\n", 0, pos) + 1, info.what, info.blocked))
            if info.blocked:
                return True
            if len(findings) >= max_findings:
                findings.pop()
        return False

    with open(path, "rb") as f:
        f.seek(start)
        left = end - start
        while left > 0:
            chunk = f.read(min(4 << 20, left))
            if not chunk:
                break
            left -= len(chunk)
            data = pending + chunk
            pending = b""
            if skipping:
                nl = data.find(b"\n")
                if nl < 0:
                    continue
                lines += 1
                data, skipping = data[nl + 1:], False
            cut = data.rfind(b"\n") + 1
            data, pending = data[:cut], data[cut:]
            if len(pending) > LONG_LINE:
                pending, skipping = b"", True
            if scan(data):
                return findings, lines
            lines += data.count(b"\n")
    if pending and not skipping:
        scan(pending)
    return findings, lines


def split_lines(path: Path, parts: int) -> list[tuple[int, int, bool]]:
    """[start, end) ranges of about the same size cut at the ends of lines (skip_first when a cut fell inside a line
    longer than LONG_LINE)."""
    size = path.stat().st_size
    ranges: list[tuple[int, int, bool]] = []
    start, skip = 0, False
    with open(path, "rb") as f:
        for i in range(1, parts):
            f.seek(max(start, size * i // parts))
            line = f.readline(LONG_LINE)
            cut = f.tell()
            if cut <= start or cut >= size:
                continue
            ranges.append((start, cut, skip))
            start, skip = cut, not line.endswith(b"\n")
    ranges.append((start, size, skip))
    return ranges


def check_gcode(path: Path, max_findings: int = 20, pool=None) -> list[Finding]:  # noqa: ANN001
    """The commands of GCODE_COMMANDS in an ASCII G-code. Raises Threat when one is refused. With pool (a process
    pool), a file larger than PARALLEL_BYTES is read in parts at the same time, one per process: the master's own
    process is not held, and 4 cores read it about 4 times faster. A binary G-code (.bgcode) is compressed: it is not
    read here (only ClamAV checks it)."""
    if pool is not None and path.stat().st_size > PARALLEL_BYTES:
        ranges = split_lines(path, getattr(pool, "_max_workers", 4))
        parts = [f.result() for f in [pool.submit(scan_range, str(path), a, b, skip) for a, b, skip in ranges]]
    else:
        parts = [scan_range(str(path), 0, path.stat().st_size, False, max_findings)]
    findings: list[Finding] = []
    lines_before = 0
    for part, newlines in parts:
        for x in part:
            x = Finding(x.command, x.line + lines_before, x.what, x.blocked)
            if x.blocked:
                raise Threat(f"The G-code has {x.command} at line {x.line}, which {x.what}: it is not sent to the "
                             "printer", "gcode")
            if len(findings) < max_findings:
                findings.append(x)
        lines_before += newlines
    return findings


# ---- ClamAV (clamd, a separate service: https://docs.clamav.net/manual/Usage/ClamdProtocol.html)
class Clamd:
    """Sends a file to clamd (INSTREAM over TCP) and reads its verdict."""

    def __init__(self, address: str, timeout: float = 60.) -> None:
        host, _, port = address.rpartition(":")
        self.host, self.port, self.timeout = host or "127.0.0.1", int(port or 3310), timeout

    def scan(self, path: Path) -> str | None:
        """None when clean, the name of what was found otherwise. Raises ScanUnavailable when clamd cannot tell."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(b"zINSTREAM\0")
                with open(path, "rb") as f:
                    while chunk := f.read(1 << 20):
                        sock.sendall(struct.pack(">I", len(chunk)) + chunk)
                sock.sendall(struct.pack(">I", 0))
                reply = b""
                while not reply.endswith(b"\0"):
                    part = sock.recv(4096)
                    if not part:
                        break
                    reply += part
        except OSError as ex:
            raise ScanUnavailable(f"ClamAV did not answer: {ex}") from None
        text = reply.rstrip(b"\0").decode("utf-8", errors="replace").strip()
        if text.endswith(": OK"):
            return None
        if text.endswith(" FOUND"):
            return text.split(": ", 1)[-1][:-len(" FOUND")]
        raise ScanUnavailable(f"ClamAV could not check the file: {text}")


class ThreatLog:
    """The files refused lately, by kind, for the alerts of the administrators (only kinds and times: no files, no
    accounts, no addresses)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[tuple[float, str]] = deque(maxlen=10_000)

    def add(self, kind: str) -> None:
        with self._lock:
            self._events.append((time.monotonic(), kind))

    def recent(self, seconds: float = 3600.) -> dict[str, int]:
        limit = time.monotonic() - seconds
        counts: dict[str, int] = {}
        with self._lock:
            for t, kind in self._events:
                if t >= limit:
                    counts[kind] = counts.get(kind, 0) + 1
        return counts
