"""The log of the service (logs/tisma-cloud.log): one line per event, with the date in UTC, the part of the service
that wrote it and what happened (the owner, 23-11: «que me haga logs de todo intento»):

    2026-11-23T19:10:04Z [servicio] inicio pedido por botón: en marcha (pid 4120)
    2026-11-23T19:10:09Z [nodo] Master not reachable: «tismaservidores.net» no existe en el DNS (ConnectError: ...)

The supervisor of the service is its only steady writer: the lines of its children (the master, the nodes, the relay
and the seeder) come to it through pipes and it writes them, so that it can rotate the file by size (10 MB, 5 old
files: tisma-cloud.log.1 ... .5). The commands and the window add a line now and then (who asked for a start or a
stop). Tokens, passwords and keys never reach the file: they are replaced by «***» on the way."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

MAX_BYTES = 10 * 2 ** 20
BACKUPS = 5

# Tokens of Tisma Cloud (tu_ accounts, tn_ nodes, ...), bearer tokens and named secrets of a URL or a setting.
_SECRETS = [
    (re.compile(r"\b(t[a-z]_)[A-Za-z0-9_\-]{16,}"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\1***"),
    (re.compile(r"(?i)\b((?:token|password|passwd|secret|api_key|apikey|ticket|x-join-secret|[a-z_]*_key|"
                r"[a-z_]*_token|[a-z_]*_password|[a-z_]*_secret)\s*[=:]\s*)(?!\*\*\*)[^\s&,;\"']+"), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def stamp(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def line(component: str, message: str) -> str:
    """One line of the log: the UTC date, the component, the message (one line, without secrets)."""
    text = " ".join(redact(message).split("\n")).rstrip()
    return f"{stamp()} [{component}] {text}"


_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[([^\]]+)\] ")


def parse(text: str) -> tuple[str, str]:
    """The component and the message of a line of the log ("" and the whole line for one of an older version)."""
    found = _PREFIX.match(text)
    return (found.group(1), text[found.end():]) if found else ("", text)


class RotatingLog:
    """The file of the log, kept by one process (the supervisor); thread-safe. When it passes MAX_BYTES it becomes
    tisma-cloud.log.1 (the older ones move up to .5, the oldest goes). On Windows a file open in another process
    cannot be renamed: then it is tried again at the next line."""

    def __init__(self, path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (open for the life of the service)

    def write(self, component: str, message: str) -> None:
        self.write_line(line(component, message))

    def write_line(self, text: str) -> None:
        with self.lock:
            try:
                self.file.write(text + "\n")
                if self.file.tell() >= self.max_bytes:
                    self._rotate()
            except (OSError, ValueError):
                pass   # a full disk: the service goes on without its log

    def _rotate(self) -> None:
        self.file.close()
        try:
            rotate_files(self.path, self.backups)
        except OSError:
            pass
        self.file = open(self.path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    def close(self) -> None:
        with self.lock:
            self.file.close()


def rotate_files(path: Path, backups: int = BACKUPS) -> None:
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for n in range(backups - 1, 0, -1):
        older = path.with_name(f"{path.name}.{n}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def event(path: Path, component: str, message: str) -> None:
    """A line added by a process other than the service (a command, the window): never fails."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line(component, message) + "\n")
    except OSError:
        pass


class Writer:
    """sys.stdout and sys.stderr of the supervisor: what it prints goes to the log as lines of one component."""

    def __init__(self, log: RotatingLog, component: str) -> None:
        self.log = log
        self.component = component
        self.pending = ""
        self.encoding = "utf-8"

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            first, self.pending = self.pending.split("\n", 1)
            if first.strip():
                self.log.write(self.component, first.removeprefix("[tisma-cloud] "))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def pump(stream, log: RotatingLog, component: str) -> None:
    """The lines of a child's output to the log, until the child closes it (a thread per child)."""
    for raw in iter(stream.readline, b""):
        text = raw.decode("utf-8", errors="replace").rstrip()
        if text:
            log.write(component, text)
    stream.close()
