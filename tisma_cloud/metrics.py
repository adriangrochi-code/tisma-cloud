"""What the owner looks at during the beta: the jobs of the last days, how long they waited and ran, why they failed,
who used the service and how the nodes are. Read by GET /v1/admin/metrics, the panel and «tisma-cloud admin metrics».

Only counts and times: no project, file name or e-mail. The failures are grouped by their error text, which is what
the person saw (the node strips its folders from it, agent._without_paths)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .jobs import JOB_CLASSES
from .models import Job, Node, User, utcnow

FINAL = ("done", "failed", "cancelled")
TOP_ERRORS = 5


def _percentile(values: list[float], fraction: float) -> float | None:
    """The value under which that fraction of them falls (nearest rank), or None without values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return round(ordered[rank - 1], 1)


def report(session: Session, days: float = 1., node_minutes: int = 5, now: datetime | None = None) -> dict:
    """The jobs that ended in the last «days» (by class and outcome, waits, runs, top errors), the accounts that sent
    one, the accounts made, and the nodes seen in the last «node_minutes»."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    rows = session.execute(
        select(Job.job_class, Job.status, Job.created_at, Job.started_at, Job.finished_at, Job.seconds, Job.error,
               Job.user_id)
        .where(Job.status.in_(FINAL), Job.finished_at >= since)
    ).all()
    jobs = {c: {s: 0 for s in FINAL} for c in JOB_CLASSES}
    waits, runs, errors, users = [], [], {}, set()
    seconds = 0.
    for job_class, status, created, started, finished, used, error, user_id in rows:
        jobs.setdefault(job_class, {s: 0 for s in FINAL})[status] += 1
        users.add(user_id)
        if started is not None:
            waits.append((started - created).total_seconds())
            if status == "done" and finished is not None:
                runs.append((finished - started).total_seconds())
        if status == "failed":
            text = (error or "").strip().splitlines()[0][:200] if (error or "").strip() else "(sin texto)"
            errors[text] = errors.get(text, 0) + 1
        seconds += used or 0.
    done = sum(c["done"] for c in jobs.values())
    failed = sum(c["failed"] for c in jobs.values())
    active = session.execute(select(Job.status, func.count()).where(Job.status.in_(("queued", "running")))
                             .group_by(Job.status)).all()
    nodes = session.execute(select(Node.enabled, Node.last_seen)).all()
    seen = now - timedelta(minutes=node_minutes)
    return {
        "since": since.isoformat() + "Z", "days": days,
        "jobs": {c: v for c, v in jobs.items() if any(v.values())},
        "done": done, "failed": failed,
        "failure_rate": round(failed / (done + failed), 3) if done + failed else None,
        "wait_s": {"median": _percentile(waits, .5), "p90": _percentile(waits, .9)},
        "run_s": {"median": _percentile(runs, .5), "p90": _percentile(runs, .9)},
        "hours_used": round(seconds / 3600., 2),
        "top_errors": [{"error": e, "count": n} for e, n in
                       sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_ERRORS]],
        "now": {"queued": 0, "running": 0, **{status: n for status, n in active}},
        "accounts": {"with_jobs": len(users),
                     "new": session.execute(select(func.count()).select_from(User)
                                            .where(User.created_at >= since)).scalar_one(),
                     "total": session.execute(select(func.count()).select_from(User)).scalar_one()},
        "nodes": {"enabled": sum(1 for enabled, _ in nodes if enabled),
                  "online": sum(1 for enabled, last in nodes if enabled and last is not None and last >= seen)},
    }


def text(m: dict) -> str:
    """The report for a terminal, in Spanish like the panel."""

    def secs(v: float | None) -> str:
        return "—" if v is None else f"{v:g} s"

    rate = "—" if m["failure_rate"] is None else f"{m['failure_rate'] * 100:.1f} %"
    lines = [f"Desde {m['since']} ({m['days']:g} día(s))",
             f"Trabajos terminados: {m['done']} bien, {m['failed']} fallidos ({rate}); ahora {m['now']['queued']} "
             f"en cola y {m['now']['running']} corriendo",
             f"Espera: mediana {secs(m['wait_s']['median'])}, 90 % {secs(m['wait_s']['p90'])}; "
             f"cálculo: mediana {secs(m['run_s']['median'])}, 90 % {secs(m['run_s']['p90'])}",
             f"Horas usadas: {m['hours_used']:g}",
             f"Cuentas: {m['accounts']['with_jobs']} mandaron trabajos, {m['accounts']['new']} nuevas, "
             f"{m['accounts']['total']} en total",
             f"Nodos: {m['nodes']['online']} de {m['nodes']['enabled']} conectados"]
    for job_class, counts in m["jobs"].items():
        lines.append(f"  {job_class:14} {counts['done']:5} bien {counts['failed']:5} fallidos "
                     f"{counts['cancelled']:5} cancelados")
    if m["top_errors"]:
        lines.append("Errores más comunes:")
        lines += [f"  {e['count']:4} × {e['error']}" for e in m["top_errors"]]
    return "\n".join(lines)


# The public state of the service (GET /v1/status, the page /estado): what the app and the program show as a notice.
STATUS_TEXT = {
    "ok": ("Funcionando", "Working"),
    "slow": ("Con demoras", "Delayed"),
    "down": ("Caído", "Down"),
}


def status(session: Session, node_minutes: int = 5, slow_minutes: int = 10, now: datetime | None = None) -> dict:
    """«down» when no enabled node answered in the last «node_minutes»; «slow» when the oldest job waiting has waited
    more than «slow_minutes», or some kinds of computing have no node connected that takes them; «ok» otherwise.
    No data of any user: the counts of nodes and the longest wait only."""
    now = now or utcnow()
    seen = now - timedelta(minutes=node_minutes)
    online = [n for n in session.execute(select(Node).where(Node.enabled.is_(True))).scalars()
              if n.last_seen is not None and n.last_seen >= seen]
    served = {c for n in online for c in n.priority_list}
    unavailable = [c for c in JOB_CLASSES if c not in served]
    oldest = session.execute(select(func.min(Job.created_at)).where(Job.status == "queued")).scalar_one()
    wait_s = max(0, int((now - oldest).total_seconds())) if oldest is not None else 0
    if not online:
        state = "down"
        es, en = ("No hay servidores de cálculo conectados: los trabajos esperan en la cola hasta que vuelvan.",
                  "No computing servers are connected: jobs wait in the queue until they are back.")
    elif wait_s > slow_minutes * 60 or unavailable:
        state = "slow"
        parts = ([("Hay más espera de lo normal.", "Waiting times are longer than usual.")]
                 if wait_s > slow_minutes * 60 else [])
        if unavailable:
            parts.append(("Algunos cálculos no están disponibles ahora.", "Some kinds of computing are not available now."))
        es, en = " ".join(p[0] for p in parts), " ".join(p[1] for p in parts)
    else:
        state, es, en = "ok", "Todo funciona con normalidad.", "Everything is working normally."
    label_es, label_en = STATUS_TEXT[state]
    return {"status": state, "label_es": label_es, "label_en": label_en, "message_es": es, "message_en": en,
            "unavailable": unavailable, "longest_wait_s": wait_s, "nodes_online": len(online),
            "checked_at": now.isoformat() + "Z"}
