"""The service on one machine: its role (the master or a computing node), its settings and its process. Used by the
window on the screen of the server (gui.py) and by the commands and the console over SSH (cli.py).

Everything is kept in one folder, the home (TISMA_HOME, else the folder of the executable, else the current folder):

    tisma-cloud.toml   role ("master" or "node"), port of the master, the master computes too (master_computes)
    .env               settings of the master (TISMA_* variables, see .env.example)
    node.toml          settings of a node (see deploy/node.example.toml)
    master-node.toml   settings of the node of the master (made by the service when the master computes)
    logs/tisma-cloud.log, tisma-cloud.pid

The service runs as a small supervisor ("run") which starts the master or the node ("run --child") and starts it again
if it stops with an error, so that a crash does not leave the machine without service. When the master computes too,
the supervisor also runs a node of its own ("run --child --local-node", node MASTER_NODE of the master) connected to
the master of the same machine.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import system

ROLES = ("master", "node")
ROLE_NAMES = {"master": "maestro (servidor central)", "node": "nodo de cómputo"}
TASK_NAME = "Tisma Cloud"
SERVICE_NAME = "tisma-cloud"

# Settings of the master shown by the window and the console: variable, label, secret.
MASTER_FIELDS = [
    ("TISMA_PUBLIC_URL", "Dirección pública (vacía: la IP pública, detectada)", False),
    ("TISMA_DDNS_HOST", "DNS dinámico: nombre (No-IP, ej. tisma.ddns.net)", False),
    ("TISMA_DDNS_USER", "DNS dinámico: usuario (correo de No-IP)", False),
    ("TISMA_DDNS_PASSWORD", "DNS dinámico: contraseña", True),
    ("TISMA_DATABASE_URL", "Base de datos", False),
    ("TISMA_STORAGE_DIR", "Carpeta de los archivos", False),
    ("TISMA_INTERNAL_EMAILS", "Cuentas internas sin cobro (correos o @dominio)", False),
    ("TISMA_SITE_KEY", "Clave de la página web", True),
    ("TISMA_ALERT_EMAILS", "Correos de los avisos", False),
    ("TISMA_SMTP_HOST", "Servidor de correo (SMTP)", False),
    ("TISMA_SMTP_USER", "Usuario SMTP", False),
    ("TISMA_SMTP_PASSWORD", "Contraseña SMTP", True),
    ("TISMA_SMTP_FROM", "Remitente de los correos", False),
]
# Settings of a node: key, label, type, secret.
NODE_FIELDS = [
    ("master_url", "Dirección del maestro (http://IP:8000)", str, False),
    ("token", "Token del nodo (vacío: pide unirse al maestro)", str, True),
    ("tisma_slicer", "Programa tisma-slicer (consola)", str, False),
    ("tisma_fea", "Programa tisma-fea", str, False),
    ("work_dir", "Carpeta de trabajo", str, False),
    ("slots", "Trabajos a la vez (0 = automático)", int, False),
    ("fea_slots", "Análisis estructurales a la vez", int, False),
    ("timeout_s", "Duración máxima de un trabajo [s]", int, False),
    ("gpu", "Usar la placa de video (análisis estructural y laminado)", bool, False),
    ("verify_tls", "Verificar el certificado HTTPS", bool, False),
    ("name", "Nombre con el que pide unirse (vacío: el de la máquina)", str, False),
]
# What a node can do (the job classes of jobs.py), as the window and the console name them.
JOB_CLASS_NAMES = {
    "slice": "Laminar",
    "slice_large": "Laminar placas grandes",
    "fea": "Análisis estructural",
    "fea_optimize": "Relleno mínimo / paredes de resina (muchos análisis)",
}
NODE_DEFAULTS = {"master_url": "http://192.168.1.10:8000", "token": "", "tisma_slicer": "", "tisma_fea": "",
                 "work_dir": "node-work", "slots": 0, "fea_slots": 1, "timeout_s": 3600, "gpu": True,
                 "verify_tls": True}
# The node of the master when it computes too (registered in its database by the service).
MASTER_NODE = "maestro"


def default_home() -> Path:
    if os.environ.get("TISMA_HOME"):
        return Path(os.environ["TISMA_HOME"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def run_command(home: Path | None = None, *args: str) -> list[str]:
    """The command that runs the service in the foreground ("run"): the executable, or this Python. With the folder of
    the service when it is given (a scheduled task or a child process do not get TISMA_HOME)."""
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "tisma_cloud.cli"]
    return base + (["--home", str(home)] if home else []) + ["run", *args]


@dataclass
class Check:
    """One item of the diagnosis of the machine: ok (True), wrong (False) or only a warning (None), what was found,
    and the key of the fix (Manager.fix) when Tisma Cloud can fix it."""
    name: str
    ok: bool | None
    detail: str
    fix: str | None = None


@dataclass
class Status:
    role: str
    running: bool
    pid: int | None
    healthy: bool | None  # the master answers /v1/health (None: not checked)
    detail: str


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Manager:
    def __init__(self, home: str | os.PathLike | None = None) -> None:
        # Absolute: the commands change to this folder, a relative path would then point elsewhere.
        self.home = (Path(home) if home else default_home()).resolve()
        self.home.mkdir(parents=True, exist_ok=True)

    # ---- files
    @property
    def settings_path(self) -> Path:
        return self.home / "tisma-cloud.toml"

    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def node_path(self) -> Path:
        return self.home / "node.toml"

    @property
    def master_node_path(self) -> Path:
        return self.home / "master-node.toml"

    @property
    def pid_path(self) -> Path:
        return self.home / "tisma-cloud.pid"

    @property
    def log_path(self) -> Path:
        return self.home / "logs" / "tisma-cloud.log"

    # ---- role and port (tisma-cloud.toml)
    def _settings(self) -> dict:
        if not self.settings_path.is_file():
            return {}
        with open(self.settings_path, "rb") as f:
            return tomllib.load(f)

    def _save_settings(self, data: dict) -> None:
        lines = ["# Tisma Cloud on this machine (tisma-cloud role / the window). The settings of the master are in",
                 "# .env, those of a node in node.toml."]
        lines += [f"{k} = {_toml_value(v)}" for k, v in data.items()]
        self.settings_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def role(self) -> str:
        role = str(self._settings().get("role", ""))
        return role if role in ROLES else ""

    def set_role(self, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"The role is master or node, not {role!r}")
        if self.status().running and role != self.role():
            raise RuntimeError("Stop the service before changing its role")
        data = self._settings()
        data["role"] = role
        self._save_settings(data)
        if role == "master" and not self.env_path.exists():
            self.write_env({"TISMA_DATABASE_URL": "sqlite:///./tisma.db", "TISMA_STORAGE_DIR": "./storage"})
        if role == "node" and not self.node_path.exists():
            self.write_node(dict(NODE_DEFAULTS))
            self.detect_programs()

    def port(self) -> int:
        return int(self._settings().get("port", 8000))

    def master_computes(self) -> bool:
        """The master also runs jobs, as a node of its own (on unless it was turned off)."""
        return bool(self._settings().get("master_computes", True))

    def set_master_computes(self, on: bool) -> None:
        data = self._settings()
        data["master_computes"] = bool(on)
        self._save_settings(data)

    def prepare_master_node(self) -> Path | None:
        """The node of the master: registered in its database (a new token when it has none that works) and its
        master-node.toml, connected to the master of this machine. None when Tisma Slicer is not on this machine."""
        from sqlalchemy import select

        from .jobs import JOB_CLASSES
        from .models import Node
        from .security import hash_token, new_token

        slicer, fea = system.find_tisma_programs()
        if not slicer or not fea:
            return None
        current = {}
        if self.master_node_path.is_file():
            with open(self.master_node_path, "rb") as f:
                current = tomllib.load(f)
        token = str(current.get("token") or "")
        db, _settings = self._database()
        with db.session() as s:
            node = s.execute(select(Node).where(Node.name == MASTER_NODE)).scalar_one_or_none()
            if node is None:
                token = new_token("tn")
                s.add(Node(name=MASTER_NODE, token_hash=hash_token(token), priorities=",".join(JOB_CLASSES)))
            elif not token or hash_token(token) != node.token_hash:
                token = new_token("tn")
                node.token_hash = hash_token(token)
        data = dict(NODE_DEFAULTS, master_url=f"http://127.0.0.1:{self.port()}", token=token, tisma_slicer=slicer,
                    tisma_fea=fea, work_dir="master-work")
        # The settings changed by hand (jobs at a time...) are kept.
        data.update({k: v for k, v in current.items() if k in ("slots", "fea_slots", "timeout_s", "gpu")})
        lines = ["# The node of the master (made by Tisma Cloud when the master computes too; master_computes in",
                 "# tisma-cloud.toml). Jobs at a time, structural analyses at a time, longest job and GPU can be changed."]
        lines += [f"{k} = {_toml_value(v)}" for k, v in data.items()]
        self.master_node_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.master_node_path

    def public_address(self) -> dict:
        """What the master found of its public address and dynamic DNS (publicip.py), from its storage folder."""
        import json

        from .cli import load_env
        from .settings import Settings

        load_env(self.env_path)
        folder = Path(Settings().storage_dir)
        folder = folder if folder.is_absolute() else self.home / folder
        try:
            return json.loads((folder / "public-address.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_port(self, port: int) -> None:
        if not 1 <= int(port) <= 65535:
            raise ValueError(f"Bad port {port}")
        data = self._settings()
        data["port"] = int(port)
        self._save_settings(data)
        # The firewall rule follows the port (it needs an administrator: the diagnosis shows it if it did not).
        if system.firewall_port() is not None:
            try:
                system.set_firewall(int(port))
            except RuntimeError:
                pass

    # ---- settings of the master (.env)
    def read_env(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if not self.env_path.is_file():
            return values
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
        return values

    def write_env(self, updates: dict[str, str]) -> None:
        """Sets the variables in .env, keeping its other lines and comments."""
        lines = self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.is_file() else []
        pending = dict(updates)
        for i, line in enumerate(lines):
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(1) in pending:
                lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
        lines += [f"{k}={v}" for k, v in pending.items()]
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- settings of a node (node.toml)
    def read_node(self) -> dict:
        data = dict(NODE_DEFAULTS)
        if self.node_path.is_file():
            with open(self.node_path, "rb") as f:
                data.update(tomllib.load(f))
        return data

    def write_node(self, values: dict) -> None:
        data = self.read_node() if self.node_path.is_file() else dict(NODE_DEFAULTS)
        for key, value in values.items():
            kind = next((f[2] for f in NODE_FIELDS if f[0] == key), None)
            if kind is None:
                raise ValueError(f"Unknown setting of a node: {key}")
            if kind is bool and isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
            data[key] = kind(value)
        lines = ["# Settings of this computing node (see deploy/node.example.toml)."]
        lines += [f"{k} = {_toml_value(v)}" for k, v in data.items()]
        self.node_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_setting(self, key: str, value: str) -> None:
        """One setting of the role of this machine, by name (port, a TISMA_* variable or a key of node.toml)."""
        if key == "port":
            self.set_port(int(value))
        elif key == "master_computes":
            self.set_master_computes(value.strip().lower() in ("1", "true", "yes", "si", "sí", "on"))
        elif key.startswith("TISMA_"):
            self.write_env({key: value})
        else:
            self.write_node({key: value})

    # ---- the nodes known by the master: what each one does
    def _database(self):
        from . import crypto
        from .cli import load_env
        from .db import Database
        from .settings import Settings

        load_env(self.env_path)
        settings = Settings()
        # The key of the personal data is in the storage folder of the service (relative to the home).
        crypto.configure(settings, self.home)
        url = settings.database_url
        # A relative SQLite file is in the home, as for the service (run() works in the home).
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////") and not Path(url[10:]).is_absolute():
            url = "sqlite:///" + str((self.home / url[10:]).resolve())
        db = Database(url)
        db.create_all()
        crypto.migrate(db)
        return db, settings

    def nodes(self) -> list[dict]:
        """The nodes registered in the master: name, job classes in order, enabled, online, last signal."""
        from sqlalchemy import select

        from .models import Node, utcnow

        db, settings = self._database()
        now = utcnow()
        with db.session() as s:
            return [{"name": n.name, "priorities": [p for p in n.priorities.split(",") if p], "enabled": n.enabled,
                     "online": n.last_seen is not None and (now - n.last_seen).total_seconds() < settings.node_timeout_s,
                     "last_seen": n.last_seen.isoformat(timespec="seconds") if n.last_seen else ""}
                    for n in s.execute(select(Node).order_by(Node.name)).scalars()]

    def set_node(self, name: str, priorities: list[str] | None = None, enabled: bool | None = None) -> None:
        """Changes what a node does (job classes in order of priority) or enables / disables it. It is used from its
        next job."""
        from sqlalchemy import select

        from .jobs import JOB_CLASSES
        from .models import Node

        if priorities is not None:
            unknown = [p for p in priorities if p not in JOB_CLASSES]
            if not priorities or unknown or len(set(priorities)) != len(priorities):
                raise ValueError(f"A node does at least one of {', '.join(JOB_CLASSES)}, each once")
        db, _settings = self._database()
        with db.session() as s:
            node = s.execute(select(Node).where(Node.name == name)).scalar_one_or_none()
            if node is None:
                raise ValueError(f"No node {name}")
            if priorities is not None:
                node.priorities = ",".join(priorities)
            if enabled is not None:
                node.enabled = enabled

    # ---- the machines that ask to be nodes, and the removal of a node (pairing.py)
    def join_requests(self) -> list[dict]:
        from . import pairing

        db, _settings = self._database()
        with db.session() as s:
            return pairing.pending(s)

    def accept_join(self, request_id: str, name: str = "", priorities: list[str] | None = None) -> str:
        """Makes the node a machine asked for; returns its name. The machine takes its token by itself."""
        from . import pairing

        db, _settings = self._database()
        with db.session() as s:
            return pairing.accept(s, request_id, name, priorities).name

    def add_node(self, name: str, priorities: list[str] | None = None) -> str:
        """A node made by hand (all the job classes unless priorities); returns its token, shown only now."""
        from sqlalchemy import select

        from .jobs import JOB_CLASSES
        from .models import Node
        from .pairing import clean_name
        from .security import hash_token, new_token

        name = clean_name(name)
        db, _settings = self._database()
        with db.session() as s:
            if s.execute(select(Node).where(Node.name == name)).scalar_one_or_none():
                raise ValueError(f"Ya hay un nodo {name}")
            token = new_token("tn")
            s.add(Node(name=name, token_hash=hash_token(token), priorities=",".join(priorities or JOB_CLASSES)))
        return token

    def reject_join(self, request_id: str) -> None:
        from . import pairing

        db, _settings = self._database()
        with db.session() as s:
            pairing.reject(s, request_id)

    def delete_node(self, name: str) -> int:
        """Removes a node; returns how many of its running jobs went back to the queue. The node of the master is not
        removed while the master computes (the service would make it again): turn that off instead."""
        from . import pairing

        if name == MASTER_NODE and self.role() == "master" and self.master_computes():
            raise ValueError("Es el nodo del propio maestro: apagá «El maestro también calcula» en Configuración")
        db, _settings = self._database()
        with db.session() as s:
            return pairing.delete_node(s, name)

    # ---- what the master is doing: the window shows it, read from the database (also while the service is stopped)
    def dashboard(self, last_jobs: int = 12) -> dict:
        """The state of the master: its nodes (with the jobs each one runs and its machine), the jobs (waiting,
        running, done and failed today, the last ones), the users, the hours computed this month, the payments of
        the month and the open alerts."""
        from datetime import timedelta

        from sqlalchemy import func, select

        from . import pairing
        from .models import Alert, Job, Node, Payment, Usage, User, month_of, utcnow
        from .scheduler import ACTIVE

        db, settings = self._database()
        now = utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with db.session() as s:
            running = dict(s.execute(select(Job.node_id, func.count()).where(Job.status == "running")
                                     .group_by(Job.node_id)).all())
            nodes = [{"name": n.name, "priorities": n.priority_list, "enabled": n.enabled, "info": n.info_dict,
                      "online": n.last_seen is not None and (now - n.last_seen).total_seconds() < settings.node_timeout_s,
                      "last_seen": n.last_seen.isoformat(timespec="seconds") if n.last_seen else "",
                      "running": running.get(n.id, 0)}
                     for n in s.execute(select(Node).order_by(Node.name)).scalars()]
            active = dict(s.execute(select(Job.status, func.count()).where(Job.status.in_(ACTIVE))
                                    .group_by(Job.status)).all())
            ended = dict(s.execute(select(Job.status, func.count()).where(Job.finished_at >= today)
                                   .group_by(Job.status)).all())
            jobs = [{"id": j.id, "kind": j.kind, "job_class": j.job_class, "status": j.status, "progress": j.progress,
                     "user": email, "node": node or "", "input": j.input_name, "seconds": round(j.seconds, 1),
                     "created_at": j.created_at.isoformat(timespec="seconds"), "error": j.error}
                    for j, email, node in s.execute(
                        select(Job, User.email, Node.name).join(User, User.id == Job.user_id)
                        .outerjoin(Node, Node.id == Job.node_id).order_by(Job.created_at.desc()).limit(last_jobs)).all()]
            users = s.execute(select(func.count()).select_from(User)).scalar_one()
            new_users = s.execute(select(func.count()).select_from(User)
                                  .where(User.created_at >= now - timedelta(days=7))).scalar_one()
            paying = s.execute(select(func.count()).select_from(User).where(User.paid_until > now)).scalar_one()
            seconds = s.execute(select(func.coalesce(func.sum(Usage.seconds), 0.))
                                .where(Usage.month == month_of(now))).scalar_one()
            payments = [{"currency": currency, "count": count, "amount_cents": int(amount or 0)}
                        for currency, count, amount in s.execute(
                            select(Payment.currency, func.count(), func.sum(Payment.amount_cents))
                            .where(Payment.status == "approved", Payment.paid_at >= today.replace(day=1))
                            .group_by(Payment.currency)).all()]
            requests = pairing.pending(s)
            alerts = [{"message": a.message, "started_at": a.started_at.isoformat(timespec="seconds")}
                      for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None))
                                         .order_by(Alert.started_at.desc())).scalars()]
            # The jobs that ended on each of the last 7 days (UTC), the oldest first.
            first = today - timedelta(days=6)
            days = {(first + timedelta(days=i)).date().isoformat(): {"done": 0, "failed": 0} for i in range(7)}
            for status, finished in s.execute(select(Job.status, Job.finished_at).where(
                    Job.finished_at >= first, Job.status.in_(("done", "failed")))).all():
                day = days.get(finished.date().isoformat())
                if day is not None:
                    day[status] += 1
        return {"nodes": nodes, "requests": requests, "queued": active.get("queued", 0), "running": active.get("running", 0),
                "done_today": ended.get("done", 0), "failed_today": ended.get("failed", 0), "jobs": jobs,
                "users": users, "new_users": new_users, "paying_users": paying, "hours_month": round(seconds / 3600, 2),
                "payments_month": payments, "alerts": alerts,
                "days": [{"day": day, **counts} for day, counts in days.items()]}

    def users(self, limit: int = 500) -> list[dict]:
        """The accounts of the master, the newest first: plan, paid until, hours computed this month and what they are
        (administrator, internal without charge, disabled)."""
        from sqlalchemy import select

        from .models import Usage, User, month_of, utcnow

        db, _settings = self._database()
        now = utcnow()
        with db.session() as s:
            hours = dict(s.execute(select(Usage.user_id, Usage.seconds).where(Usage.month == month_of(now))).all())
            return [{"email": u.email, "plan": u.plan_name, "admin": u.is_admin, "unlimited": u.unlimited,
                     "enabled": u.enabled, "paid_until": u.paid_until.date().isoformat() if u.paid_until else "",
                     "paid": u.paid_until is not None and u.paid_until > now,
                     "hours_month": round(hours.get(u.id, 0.) / 3600, 2),
                     "created_at": u.created_at.date().isoformat()}
                    for u in s.execute(select(User).order_by(User.created_at.desc()).limit(limit)).scalars()]

    def resources(self) -> dict:
        """This machine: processors, load, memory and the disk of the home."""
        return system.resources(self.home)

    # ---- the administration panel of the master, opened by the program
    PANEL_TOKEN_LABEL = "panel (tisma-cloud)"

    def has_admin(self) -> bool:
        from sqlalchemy import select

        from .models import User

        db, _settings = self._database()
        with db.session() as s:
            return s.execute(select(User.id).where(User.is_admin.is_(True), User.enabled.is_(True))).first() is not None

    def create_admin(self, email: str, password: str) -> None:
        """The first administrator of the master (the panel, the alerts), with the trial plan."""
        from sqlalchemy import select

        from . import crypto
        from .models import Plan, User
        from .security import hash_password

        email = email.strip().lower()
        if "@" not in email or len(password) < 8:
            raise ValueError("Correo válido y contraseña de 8 caracteres o más")
        db, _settings = self._database()
        with db.session() as s:
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if user is None:
                plan = s.execute(select(Plan.name).order_by(Plan.monthly_hours)).scalars().first() or "trial"
                user = User(email=email, plan_name=plan)
                s.add(user)
            user.is_admin, user.enabled, user.email_verified = True, True, True
            user.password_hash = hash_password(password)

    def panel_token(self) -> str:
        """A session of the first administrator for the panel opened by the program (it replaces the previous one)."""
        from sqlalchemy import select

        from .models import ApiToken, User
        from .security import hash_token, new_token

        db, _settings = self._database()
        with db.session() as s:
            admin = s.execute(select(User).where(User.is_admin.is_(True), User.enabled.is_(True))
                              .order_by(User.id)).scalars().first()
            if admin is None:
                raise RuntimeError("No administrator: create one first")
            # The labels are encrypted in the database: compared here.
            for old in s.execute(select(ApiToken).where(ApiToken.user_id == admin.id)).scalars():
                if old.label == self.PANEL_TOKEN_LABEL:
                    s.delete(old)
            token = new_token("tu")
            s.add(ApiToken(user_id=admin.id, token_hash=hash_token(token), label=self.PANEL_TOKEN_LABEL))
            return token

    def panel_url(self) -> str:
        return f"http://127.0.0.1:{self.port()}/admin#token={self.panel_token()}"

    # ---- the process
    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def health(self, timeout: float = 2.) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port()}/v1/health", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def status(self) -> Status:
        role = self.role()
        pid = self._read_pid()
        running = pid is not None and _pid_alive(pid)
        healthy = self.health() if running and role == "master" else None
        if not role:
            detail = "Sin rol: elegí maestro o nodo"
        elif not running:
            detail = "Detenido"
        elif role == "master":
            detail = (f"En marcha (pid {pid}), puerto {self.port()}" if healthy else
                      f"Arrancando (pid {pid}), puerto {self.port()}")
        else:
            detail = f"En marcha (pid {pid}), maestro {self.read_node().get('master_url')}"
        return Status(role, running, pid if running else None, healthy, detail)

    def start(self, wait_s: float = 20.) -> Status:
        if not self.role():
            raise RuntimeError("Choose the role first (master or node)")
        if self.status().running:
            return self.status()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and self.autostart_enabled():
            # The scheduled task runs apart from the session (an SSH session closes its processes when it ends).
            system.run_task(TASK_NAME)
        else:
            kwargs: dict = {"cwd": str(self.home), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.DEVNULL, "close_fds": True}
            env = system.own_env(TISMA_HOME=str(self.home))
            if os.name == "nt":
                flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_PROCESS_GROUP, NO_WINDOW
                try:
                    subprocess.Popen(run_command(self.home), env=env, creationflags=flags | 0x01000000, **kwargs)  # BREAKAWAY
                except OSError:
                    subprocess.Popen(run_command(self.home), env=env, creationflags=flags, **kwargs)
            else:
                subprocess.Popen(run_command(self.home), env=env, start_new_session=True, **kwargs)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            st = self.status()
            if st.running and (st.role != "master" or st.healthy):
                return st
            time.sleep(0.3)
        return self.status()

    def stop(self, wait_s: float = 15.) -> Status:
        pid = self._read_pid()
        if pid is None or not _pid_alive(pid):
            self.pid_path.unlink(missing_ok=True)
            return self.status()
        if os.name == "nt":
            # The supervisor and the master or node it runs.
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
                               env=system.program_env())
            if r.returncode != 0 and _pid_alive(pid):
                raise RuntimeError("No se pudo detener el servicio (si arranca con la máquina corre como SYSTEM: "
                                   "ejecutá como administrador). " + (r.stderr or r.stdout).strip())
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + wait_s
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.pid_path.unlink(missing_ok=True)
        return self.status()

    def restart(self) -> Status:
        self.stop()
        return self.start()

    def logs(self, lines: int = 40) -> list[str]:
        if not self.log_path.is_file():
            return []
        with open(self.log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            return f.read().decode("utf-8", errors="replace").splitlines()[-lines:]

    # ---- start with the machine
    def _systemd_unit(self) -> tuple[Path, list[str]]:
        system = hasattr(os, "geteuid") and os.geteuid() == 0
        folder = Path("/etc/systemd/system") if system else Path.home() / ".config" / "systemd" / "user"
        return folder / f"{SERVICE_NAME}.service", ([] if system else ["--user"])

    def autostart_enabled(self) -> bool:
        if os.name == "nt":
            return system.task_definition(TASK_NAME) is not None
        unit, _ = self._systemd_unit()
        return unit.is_file()

    def autostart_problems(self) -> list[str]:
        """Windows: what is wrong in the scheduled task (one made by an older version); empty when it is right."""
        if os.name != "nt":
            return []
        xml = system.task_definition(TASK_NAME)
        return system.task_problems(xml) if xml else []

    def set_autostart(self, enabled: bool) -> None:
        """Windows: a scheduled task at the start of the machine (as SYSTEM, no time limit, started again if it fails;
        needs an administrator). Linux: a systemd service (of the system as root, of the user otherwise)."""
        cmd = run_command(self.home)
        if os.name == "nt":
            if enabled:
                system.create_task(TASK_NAME, cmd, self.home, "Tisma Cloud: el maestro o el nodo de cómputo de esta máquina")
            else:
                system.delete_task(TASK_NAME)
            return
        unit, scope = self._systemd_unit()
        if enabled:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("\n".join([
                "[Unit]", "Description=Tisma Cloud", "After=network-online.target", "",
                "[Service]", f"Environment=TISMA_HOME={self.home}", f"WorkingDirectory={self.home}",
                f"ExecStart={subprocess.list2cmdline(cmd)}", "Restart=on-failure", "",
                "[Install]", "WantedBy=" + ("multi-user.target" if not scope else "default.target"), ""]),
                encoding="utf-8")
            subprocess.run(["systemctl", *scope, "daemon-reload"], capture_output=True, env=system.program_env())
            subprocess.run(["systemctl", *scope, "enable", SERVICE_NAME], capture_output=True, env=system.program_env())
        else:
            subprocess.run(["systemctl", *scope, "disable", SERVICE_NAME], capture_output=True,
                           env=system.program_env())
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", *scope, "daemon-reload"], capture_output=True, env=system.program_env())

    # ---- the Windows firewall (the port of the master)
    def firewall_open(self) -> bool | None:
        """The port of the master is open in the Windows firewall (None: not Windows)."""
        if os.name != "nt":
            return None
        return system.firewall_port() == self.port()

    def set_firewall(self, enabled: bool) -> None:
        system.set_firewall(self.port() if enabled else None)

    # ---- the programs of a node
    def detect_programs(self, replace: bool = False) -> dict[str, str]:
        """Fills tisma_slicer and tisma_fea of node.toml with the programs of Tisma Slicer found on this machine (only
        the empty or wrong ones, unless replace). Returns what was set."""
        node = self.read_node()
        slicer, fea = system.find_tisma_programs()
        found = {}
        for key, path in (("tisma_slicer", slicer), ("tisma_fea", fea)):
            current = str(node.get(key) or "")
            if path and (replace or not current or not Path(current).is_file()):
                found[key] = path
        if found:
            self.write_node(found)
        return found

    # ---- diagnosis
    def checks(self) -> list[Check]:
        """What this machine needs to serve, checked one by one (the window, tisma-cloud check)."""
        out: list[Check] = []
        role = self.role()
        if not role:
            return [Check("Rol", False, "Sin rol: elegí maestro o nodo")]
        st = self.status()
        out.append(Check("Servicio", st.running and st.healthy is not False, st.detail, None if st.running else "start"))
        on = self.autostart_enabled()
        problems = self.autostart_problems() if on else []
        out.append(Check("Inicio con la máquina", True if on and not problems else None if not on else False,
                         ("Tarea programada «Tisma Cloud»" if os.name == "nt" else "Servicio de systemd") if on and not problems
                         else ("La tarea " + ", ".join(problems) + ": crearla de nuevo") if problems
                         else "Desactivado: el servicio no arranca solo después de un reinicio", "autostart"))
        if role == "master":
            fw = self.firewall_open()
            if fw is not None:
                rule = system.firewall_port()
                out.append(Check("Firewall de Windows", fw,
                                 f"Puerto {self.port()} abierto a la red" if fw else
                                 f"La regla abre el puerto {rule}, no el {self.port()}" if rule else
                                 f"El puerto {self.port()} está cerrado: los nodos y los clientes no llegan", "firewall"))
            if st.running and st.healthy:
                try:
                    has = self.has_admin()
                    out.append(Check("Administrador", True if has else None, "Hay un administrador" if has else
                                     "Sin administrador: crealo al abrir el panel"))
                except Exception as e:  # noqa: BLE001
                    out.append(Check("Administrador", None, f"No se pudo leer la base de datos: {e}"))
            if os.name == "nt":
                version = system.webview2_version()
                out.append(Check("Ventana del panel (WebView2)", True if version else None,
                                 f"WebView2 {version}" if version else
                                 "Este Windows no tiene WebView2 (Windows Server 2019 y 2022 no lo traen): instalá "
                                 "«Microsoft Edge WebView2 Runtime» o abrí el panel desde otra PC en "
                                 f"http://<IP de este servidor>:{self.port()}/admin"))
            env = self.read_env()
            url = env.get("TISMA_PUBLIC_URL", "")
            if url:
                out.append(Check("Dirección pública", True if url.startswith("https://") else None,
                                 url if url.startswith("https://") else
                                 f"{url}: sin HTTPS, las contraseñas viajan sin cifrar"))
            else:
                # Made by the master: the dynamic DNS name or its public IPv4.
                found = self.public_address()
                host = env.get("TISMA_DDNS_HOST", "") or found.get("ip", "")
                out.append(Check("Dirección pública", None if host else False,
                                 f"http://{host}:{self.port()} (automática, sin HTTPS: ver README)" if host else
                                 "Automática: el maestro todavía no averiguó su IP pública (¿en marcha, con internet?)"))
            if env.get("TISMA_DDNS_HOST"):
                found = self.public_address()
                error = str(found.get("ddns_error") or "")
                out.append(Check("DNS dinámico", False if error else True if found.get("ddns_ip") else None,
                                 f"{env['TISMA_DDNS_HOST']}: {error.removeprefix('stop: ')}" if error else
                                 f"{env['TISMA_DDNS_HOST']} → {found['ddns_ip']}" if found.get("ddns_ip") else
                                 f"{env['TISMA_DDNS_HOST']}: todavía sin actualizar"))
            if self.master_computes():
                slicer, fea = system.find_tisma_programs()
                out.append(Check("El maestro calcula", bool(slicer and fea),
                                 f"Nodo «{MASTER_NODE}» con {slicer}" if slicer and fea else
                                 "Faltan tisma-slicer y tisma-fea: vienen en la carpeta tisma-slicer del paquete de "
                                 "Tisma Cloud, junto al programa"))
        else:
            node = self.read_node()
            for key, label in (("tisma_slicer", "Programa tisma-slicer"), ("tisma_fea", "Programa tisma-fea")):
                path = str(node.get(key) or "")
                ok = bool(path) and Path(path).is_file()
                out.append(Check(label, ok, path if ok else (f"No existe: {path}" if path else
                                                             "No configurado: «Buscar Tisma Slicer» (vienen en la "
                                                             "carpeta tisma-slicer del paquete)"),
                                 None if ok else "programs"))
            asked = self.node_path.with_name(self.node_path.name + ".join").is_file()
            out.append(Check("Token del nodo", True if node.get("token") else None, "Configurado" if node.get("token")
                             else "Pidió unirse al maestro: aceptalo en la ventana del maestro (Nodos)" if asked
                             else "Sin token: al iniciar, el nodo pide unirse al maestro; aceptalo en la ventana del "
                                  "maestro (Nodos)"))
            master = str(node.get("master_url") or "").rstrip("/")
            try:
                import ssl

                context = None if node.get("verify_tls", True) else ssl._create_unverified_context()  # noqa: S323
                with urllib.request.urlopen(f"{master}/v1/health", timeout=4, context=context) as r:
                    reach = r.status == 200
                detail = f"{master} responde"
            except Exception as e:  # noqa: BLE001
                reach, detail = False, f"{master} no responde ({getattr(e, 'reason', e)})"
            out.append(Check("Maestro", reach, detail))
            work = Path(node.get("work_dir") or "node-work")
            work = work if work.is_absolute() else self.home / work
            try:
                work.mkdir(parents=True, exist_ok=True)
                probe = work / ".tisma-write-test"
                probe.write_text("ok")
                probe.unlink()
                out.append(Check("Carpeta de trabajo", True, str(work)))
            except OSError as e:
                out.append(Check("Carpeta de trabajo", False, f"{work}: {e}"))
        return out

    def fix(self, key: str) -> str:
        """Fixes one item of the diagnosis (Check.fix). Returns what was done."""
        if key == "start":
            return self.start().detail
        if key == "autostart":
            self.set_autostart(True)
            return "Inicio con la máquina activado"
        if key == "firewall":
            self.set_firewall(True)
            return f"Puerto {self.port()} abierto en el firewall de Windows"
        if key == "programs":
            found = self.detect_programs(replace=True)
            if not found:
                raise RuntimeError("No se encontraron tisma-slicer y tisma-fea: copiá la carpeta tisma-slicer del "
                                   "paquete de Tisma Cloud junto al programa, o indicá los programas")
            return "Programas encontrados: " + ", ".join(found.values())
        raise ValueError(f"Nothing to fix for {key}")

    # ---- running in the foreground (the process started by start())
    def run(self, child: bool = False, local_node: bool = False) -> int:
        """Runs the role of this machine until it is stopped, writing the log when not in a terminal. Without child: the
        supervisor, which writes the pid file and runs the role in a child process, started again when it fails."""
        role = self.role()
        if not role:
            print("No role: tisma-cloud role master|node", file=sys.stderr)
            return 1
        os.chdir(self.home)
        if not system.is_terminal(sys.stdout):
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(self.log_path, "a", buffering=1, encoding="utf-8")
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            sys.stdout = sys.stderr = log
        if not child:
            self.pid_path.write_text(str(os.getpid()))
            try:
                return self._supervise()
            finally:
                if self._read_pid() == os.getpid():
                    self.pid_path.unlink(missing_ok=True)
        from .node.__main__ import main as node_main

        if role == "master" and local_node:
            return node_main(["--config", str(self.master_node_path), "--no-join"])
        if role == "master":
            from .cli import load_env, serve_master

            load_env(self.env_path)
            return serve_master("0.0.0.0", self.port())
        return node_main(["--config", str(self.node_path)])

    def children(self) -> dict[str, list[str]]:
        """The processes of the service: the master or the node, and the node of the master when it computes too."""
        out = {"main": run_command(self.home, "--child")}
        if self.role() == "node":
            # The programs that came with the package, when node.toml names none that exists (a moved folder).
            self.detect_programs()
        if self.role() == "master" and self.master_computes():
            try:
                ready = self.prepare_master_node() is not None
            except Exception as e:  # noqa: BLE001 (the master serves without it)
                print(f"[tisma-cloud] el nodo del maestro no se pudo preparar: {e}", flush=True)
                ready = False
            if ready:
                out["node"] = run_command(self.home, "--child", "--local-node")
            else:
                print("[tisma-cloud] el maestro no calcula: falta Tisma Slicer en esta máquina", flush=True)
        return out

    def _supervise(self) -> int:
        """Runs the role in a child process (and the node of the master in another one when it computes too) and starts
        each one again when it stops with an error (after 5 s, doubling up to a minute while it keeps failing). A clean
        exit of the master or the node ends the service."""
        commands = self.children()
        # The children use the unpacked files of the executable of the supervisor (they end with it), not a copy each.
        env = dict(os.environ)
        env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        running: dict[str, subprocess.Popen] = {}
        started: dict[str, float] = {}
        delay = {name: 5. for name in commands}
        restart_at = {name: 0. for name in commands}

        def stop_all() -> None:
            for proc in running.values():
                if proc.poll() is None:
                    proc.terminate()

        def stop(_signum, _frame):  # noqa: ANN001
            stop_all()
            raise SystemExit(0)

        if os.name != "nt":
            signal.signal(signal.SIGTERM, stop)
        while True:
            now = time.monotonic()
            for name, cmd in commands.items():
                proc = running.get(name)
                if proc is None:
                    if now >= restart_at[name]:
                        running[name] = subprocess.Popen(cmd, cwd=str(self.home), stdin=subprocess.DEVNULL, env=env,
                                                         creationflags=0x08000000 if os.name == "nt" else 0)  # no window
                        started[name] = now
                    continue
                code = proc.poll()
                if code is None:
                    continue
                del running[name]
                if code == 0 and name == "main":
                    stop_all()
                    return 0
                if now - started[name] > 300:
                    delay[name] = 5.
                what = "el servicio" if name == "main" else "el nodo del maestro"
                print(f"[tisma-cloud] {time.strftime('%Y-%m-%d %H:%M:%S')}: {what} terminó con el código {code}; "
                      f"arranca de nuevo en {delay[name]:.0f} s", flush=True)
                restart_at[name] = now + delay[name]
                delay[name] = min(delay[name] * 2, 60.)
            time.sleep(0.5)
