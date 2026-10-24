"""Alerts of the master for its administrators: a computing node that stopped answering, jobs waiting too long in the
queue, little free disk. Checked by the periodic sweep of the master; each problem is e-mailed when it starts, again
every alert_repeat_h while it lasts, and when it is solved. The active ones are shown in the administration panel.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .mail import Mailer
from .models import Alert, Job, Node, User, utcnow

log = logging.getLogger("tisma_cloud.alerts")


@dataclass
class Problem:
    key: str          # stable id of the problem ("node:3", "queue", "disk"): one alert per key
    message: str      # for people (Spanish, like the panel)


def find_problems(session: Session, settings, storage_dir: Path, now: datetime | None = None,  # noqa: ANN001
                  refused: dict[str, int] | None = None) -> list[Problem]:
    now = now or utcnow()
    problems: list[Problem] = []
    # Files refused in the last hour (threats.py): projects, G-code, found by the antivirus, antivirus not answering.
    refused = refused or {}
    if refused.get("scanner"):
        problems.append(Problem("scanner", "El antivirus (ClamAV) no responde: no se aceptan archivos hasta que vuelva."))
    names = {"project": "proyectos", "gcode": "G-code", "virus": "con virus"}
    found = {k: v for k, v in refused.items() if k in names}
    if found:
        detail = ", ".join(f"{names[k]}: {v}" for k, v in found.items())
        problems.append(Problem("threats", f"En la última hora se rechazaron {sum(found.values())} archivo(s) "
                                           f"peligrosos ({detail})."))
    # Nodes seen at least once (a node never connected is not installed yet) and silent for a while.
    silent = now - timedelta(minutes=settings.alert_node_minutes)
    for node in session.execute(select(Node).where(Node.enabled.is_(True), Node.last_seen.is_not(None),
                                                   Node.last_seen < silent)).scalars():
        minutes = int((now - node.last_seen).total_seconds() // 60)
        problems.append(Problem(f"node:{node.id}", f"El nodo «{node.name}» no responde hace {minutes} min."))
    # The oldest job waiting: no node takes its class, or all of them are busy or down.
    oldest = session.execute(select(func.min(Job.created_at)).where(Job.status == "queued")).scalar()
    if oldest is not None and oldest < now - timedelta(minutes=settings.alert_queue_minutes):
        waiting = session.execute(select(func.count()).select_from(Job).where(Job.status == "queued")).scalar_one()
        minutes = int((now - oldest).total_seconds() // 60)
        problems.append(Problem("queue", f"Hay {waiting} trabajo(s) en la cola; el más viejo espera hace {minutes} min."))
    try:
        usage = shutil.disk_usage(storage_dir)
    except OSError:
        usage = None
    if usage is not None:
        free_gb = usage.free / 1e9
        if free_gb < settings.alert_disk_free_gb:
            problems.append(Problem("disk", f"Queda poco disco en el central: {free_gb:.1f} GB libres "
                                            f"de {usage.total / 1e9:.0f} GB."))
    return problems


def recipients(session: Session, settings) -> list[str]:  # noqa: ANN001
    """TISMA_ALERT_EMAILS, or the administrators of the master."""
    listed = [e.strip() for e in settings.alert_emails.split(",") if e.strip()]
    if listed:
        return listed
    return list(session.execute(select(User.email).where(User.is_admin.is_(True), User.enabled.is_(True))).scalars())


def update(session: Session, settings, storage_dir: Path, mailer: Mailer, now: datetime | None = None,  # noqa: ANN001
           refused: dict[str, int] | None = None) -> list[Alert]:
    """Compares the problems now with the open alerts: opens, repeats and closes them, and e-mails each change.
    Returns the open alerts."""
    now = now or utcnow()
    current = {p.key: p for p in find_problems(session, settings, storage_dir, now, refused)}
    open_alerts = {a.key: a for a in session.execute(select(Alert).where(Alert.resolved_at.is_(None))).scalars()}
    to_send: list[tuple[str, str]] = []
    repeat = timedelta(hours=settings.alert_repeat_h)
    for key, problem in current.items():
        alert = open_alerts.get(key)
        if alert is None:
            alert = Alert(key=key, message=problem.message, started_at=now, notified_at=now)
            session.add(alert)
            to_send.append(("Tisma Cloud: " + problem.message, problem.message))
        else:
            alert.message = problem.message
            if alert.notified_at is None or now - alert.notified_at >= repeat:
                alert.notified_at = now
                to_send.append(("Tisma Cloud (sigue): " + problem.message,
                                f"{problem.message}\n\nEmpezó: {alert.started_at:%Y-%m-%d %H:%M} UTC."))
    for key, alert in open_alerts.items():
        if key not in current:
            alert.resolved_at = now
            to_send.append(("Tisma Cloud: resuelto", f"Resuelto: {alert.message}\n\n"
                                                     f"Duró desde {alert.started_at:%Y-%m-%d %H:%M} UTC."))
    session.flush()
    if to_send:
        emails = recipients(session, settings)
        for subject, text in to_send:
            log.warning("%s", subject)
            for email in emails:
                try:
                    mailer.send(email, subject, text + "\n\nPanel: /admin (red local).\n")
                except Exception:  # noqa: BLE001 (an unreachable mail server must not stop the sweep)
                    log.exception("Alert e-mail to %s not sent", email)
    return [a for a in session.execute(select(Alert).where(Alert.resolved_at.is_(None))
                                       .order_by(Alert.started_at)).scalars()]
