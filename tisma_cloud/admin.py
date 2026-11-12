"""Administration from the master machine (works on the database directly):

    python -m tisma_cloud.admin plan-set basic --hours 10 --concurrent 2 [--rpm 120] [--priority 1] [--classes slice,orient]
    python -m tisma_cloud.admin plan-list
    python -m tisma_cloud.admin user-add EMAIL --plan basic [--paid-until 2026-12-31] [--admin] [--password]
    python -m tisma_cloud.admin user-set EMAIL [--plan P] [--paid-until DATE] [--enable | --disable] [--password]
    python -m tisma_cloud.admin user-token EMAIL [--label L]
    python -m tisma_cloud.admin user-list
    python -m tisma_cloud.admin node-add NAME --priorities fea_optimize,fea,slice_large,slice
    python -m tisma_cloud.admin node-set NAME [--priorities ...] [--enable | --disable]
    python -m tisma_cloud.admin node-token NAME
    python -m tisma_cloud.admin node-list
    python -m tisma_cloud.admin node-del NAME
    python -m tisma_cloud.admin node-requests
    python -m tisma_cloud.admin node-accept ID [--name NAME] [--priorities ...]
    python -m tisma_cloud.admin node-reject ID
    python -m tisma_cloud.admin queue
    python -m tisma_cloud.admin reports [--status new,confirmed] [--app pc] [--format md | jsonl] [--out FILE]

Tokens are shown once, when they are created. In production the web site creates the users and records their
payments through /v1/site (see README.md); these commands are for the administrator and for tests.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime

from sqlalchemy import func, select

from . import crypto, pairing, reports
from .db import Database
from .jobs import JOB_CLASS_DESCRIPTIONS, JOB_CLASSES
from .models import ApiToken, BugReport, Job, Node, Plan, User
from .scheduler import account
from .security import hash_password, hash_token, new_token
from .settings import Settings


def parse_priorities(text: str) -> str:
    items = [p.strip() for p in text.split(",") if p.strip()]
    unknown = [p for p in items if p not in JOB_CLASSES]
    if not items or unknown or len(set(items)) != len(items):
        raise SystemExit(f"Priorities: job classes separated by commas, each once, of: {', '.join(JOB_CLASSES)}")
    return ",".join(items)


def parse_classes(text: str) -> str:
    """The job classes a plan includes, in the order of JOB_CLASSES; "all" (or empty): all of them."""
    if text.strip().lower() in ("", "all"):
        return ""
    items = {c.strip() for c in text.split(",") if c.strip()}
    unknown = sorted(items - set(JOB_CLASSES))
    if unknown:
        raise SystemExit(f"Classes: unknown {', '.join(unknown)}; job classes separated by commas, or all, of: "
                         f"{', '.join(JOB_CLASSES)}")
    return ",".join(c for c in JOB_CLASSES if c in items)


def classes_text(plan: Plan) -> str:
    return plan.classes or "all"


def parse_date(text: str) -> datetime:
    """A date or time given here, as the database keeps it (UTC, without zone). A date alone is through that whole day
    and a time without a zone is on this machine's clock: «--paid-until 2026-12-01» in Uruguay lasts to 23:59:59 of
    the 1st there, not to 21:00 of the 30th."""
    from datetime import time as day_time, timezone

    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        raise SystemExit(f"Not a date: {text} (YYYY-MM-DD)") from None
    if "T" not in text and " " not in text.strip():
        value = datetime.combine(value.date(), day_time(23, 59, 59))
    if value.tzinfo is None:
        value = value.astimezone()          # this machine's zone
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def ask_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < 8 or password != getpass.getpass("Again: "):
        raise SystemExit("The passwords differ or are shorter than 8 characters")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tisma-cloud-admin", description="Tisma Cloud administration")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan-set", help="create or change a plan")
    p.add_argument("name")
    p.add_argument("--hours", type=float, required=True, help="hours of computing per month")
    p.add_argument("--concurrent", type=int, required=True, help="jobs waiting or running at the same time")
    p.add_argument("--rpm", type=int, default=60, help="requests to the API per minute")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--remote-printers", type=int, default=0, help="printers of the account usable from afar")
    p.add_argument("--extra-printer-cents", type=int, default=0,
                   help="monthly price in cents of each remote printer more (0: no more)")
    p.add_argument("--classes", help="job classes the plan includes, comma separated, or all (a new plan: all; "
                                     "a plan that exists: kept without it)")
    p.add_argument("--public", action=argparse.BooleanOptionalAction, default=None,
                   help="listed in GET /v1/plans, for users to choose (a new plan: not; a plan that exists: kept)")
    sub.add_parser("plan-list")

    for name in ("user-add", "user-set"):
        p = sub.add_parser(name)
        p.add_argument("email")
        p.add_argument("--plan", required=name == "user-add")
        p.add_argument("--paid-until")
        p.add_argument("--password", action="store_true", help="ask a password for the login from Tisma")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--unlimited", action="store_true",
                       help="internal account: unlimited computing, never charged (the team)")
        g.add_argument("--limited", action="store_true", help="a normal account again, limited by its plan")
        if name == "user-add":
            p.add_argument("--admin", action="store_true")
        else:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--enable", action="store_true")
            g.add_argument("--disable", action="store_true")
    p = sub.add_parser("user-token", help="a new access token for a user (instead of the login)")
    p.add_argument("email")
    p.add_argument("--label", default="admin")
    sub.add_parser("user-list")

    classes = "; ".join(f"{c}: {d}" for c, d in JOB_CLASS_DESCRIPTIONS.items())
    p = sub.add_parser("node-add", help="register a computing node and print its token")
    p.add_argument("name")
    p.add_argument("--priorities", required=True, help=f"job classes in order, comma separated ({classes})")
    p = sub.add_parser("node-set", help="change the priorities of a node or enable/disable it")
    p.add_argument("name")
    p.add_argument("--priorities")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    p = sub.add_parser("node-token", help="replace the token of a node")
    p.add_argument("name")
    sub.add_parser("node-list")
    p = sub.add_parser("node-del", help="remove a node (its running jobs go back to the queue)")
    p.add_argument("name")
    sub.add_parser("node-requests", help="the machines of the local network that ask to be nodes")
    p = sub.add_parser("node-accept", help="make the node a machine asked for (it takes its token by itself)")
    p.add_argument("id")
    p.add_argument("--name", default="", help="name of the node (the name of the machine without it)")
    p.add_argument("--priorities", help=f"job classes in order, comma separated (all without it; {classes})")
    p = sub.add_parser("node-reject", help="refuse a machine that asks to be a node")
    p.add_argument("id")
    sub.add_parser("queue", help="jobs waiting and running by class")
    p = sub.add_parser("metrics", help="the jobs, waits, failures, accounts and nodes of the last days")
    p.add_argument("--days", type=float, default=1., help="how many days back (1 by default)")
    p.add_argument("--json", action="store_true", help="as JSON")
    sub.add_parser("library-key", help="the public key of the library and whether its certificate is installed")
    p = sub.add_parser("library-cert", help="install the owner's signature of the library key (project key, base64)")
    p.add_argument("certificate")
    p = sub.add_parser("library-certify", help="certify the library key with the project key (clave.pem on the USB "
                                              "stick), read into memory only")
    p.add_argument("--key", required=True, help="the project's private key (PEM, PKCS#8)")
    p.add_argument("--replace-project-key", action="store_true",
                   help="the project key changed on purpose: replace TISMA_LIBRARY_PROJECT_KEY with this key's")
    p = sub.add_parser("reports", help="the catalog of the problems reported, for the programmers (and their AI)")
    p.add_argument("--status", default="new,confirmed", help="comma separated: " + ", ".join(reports.STATUSES))
    p.add_argument("--app", choices=reports.APPS)
    p.add_argument("--format", choices=("md", "jsonl"), default="md")
    p.add_argument("--out", help="file to write (the screen without it)")

    args = parser.parse_args(argv)
    settings = Settings()
    if args.command == "library-certify":
        import getpass

        from .manager import Manager
        from . import library

        manager = Manager()
        try:
            try:
                print(manager.certify_library(args.key, replace=args.replace_project_key))
            except library.KeyPasswordNeeded:
                print(manager.certify_library(args.key, getpass.getpass("Contraseña de la clave: "),
                                              args.replace_project_key))
        except (library.LibraryError, RuntimeError) as ex:
            print(ex, file=sys.stderr)
            return 1
        return 0
    if args.command in ("library-key", "library-cert"):
        # The library's signing key (library.py): its public key, for the owner to sign once with the project key on
        # the USB, and that certificate installed.
        from pathlib import Path

        from . import library

        key = library.LibraryKey(Path(settings.storage_dir))
        if args.command == "library-cert":
            try:
                key.install_certificate(args.certificate, settings.library_project_key)
            except library.LibraryError as ex:
                print(ex, file=sys.stderr)
                return 1
        cert = key.certificate()
        print(f"Clave de la biblioteca: {library.b64(key.public)}")
        print("Para firmar con la clave del proyecto: los bytes de «tisma-library-key/1» + salto de línea + esos 32 bytes")
        print(f"Certificado: {'instalado' if cert else 'falta (tisma-cloud admin library-cert <firma en base64>)'}")
        return 0
    crypto.configure(settings)
    db = Database(settings.database_url)
    db.create_all()
    crypto.migrate(db)

    with db.session() as s:
        if args.command == "plan-set":
            plan = s.get(Plan, args.name) or Plan(name=args.name)
            plan.monthly_hours, plan.max_concurrent = args.hours, max(1, args.concurrent)
            plan.requests_per_minute, plan.priority = max(1, args.rpm), args.priority
            plan.remote_printers, plan.extra_printer_cents = max(0, args.remote_printers), max(0, args.extra_printer_cents)
            if args.classes is not None:
                plan.classes = parse_classes(args.classes)
            if args.public is not None:
                plan.public = args.public
            s.add(plan)
            print(f"Plan {plan.name}: {plan.monthly_hours:g} h/month, {plan.max_concurrent} at a time, "
                  f"{plan.requests_per_minute} requests/min, priority {plan.priority}, "
                  f"{plan.remote_printers} remote printers (+{plan.extra_printer_cents} cents each one more), "
                  f"classes {classes_text(plan)}{', public' if plan.public else ''}")
        elif args.command == "plan-list":
            for plan in s.execute(select(Plan).order_by(Plan.monthly_hours)).scalars():
                print(f"{plan.name:15} {plan.monthly_hours:8g} h/month  {plan.max_concurrent:3} at a time  "
                      f"{plan.requests_per_minute:5} req/min  priority {plan.priority}  "
                      f"{plan.remote_printers} remote printers (+{plan.extra_printer_cents} cents)  "
                      f"classes {classes_text(plan)}")
        elif args.command in ("user-add", "user-set"):
            email = args.email.strip().lower()
            if args.plan and s.get(Plan, args.plan) is None:
                print(f"No plan {args.plan} (plan-set creates it)", file=sys.stderr)
                return 1
            user = s.execute(select(User).where(User.email_index == crypto.index(email))).scalar_one_or_none()
            if args.command == "user-add":
                if user is not None:
                    print(f"The user {email} exists", file=sys.stderr)
                    return 1
                user = User(email=email, plan_name=args.plan, is_admin=args.admin,
                            unlimited=settings.is_internal_email(email))
                s.add(user)
            elif user is None:
                print(f"No user {email}", file=sys.stderr)
                return 1
            elif args.plan:
                user.plan_name = args.plan
            if args.paid_until:
                user.paid_until = parse_date(args.paid_until)
            if args.password:
                user.password_hash = hash_password(ask_password())
            if getattr(args, "enable", False) or getattr(args, "disable", False):
                user.enabled = args.enable
                if not args.enable and user.id is not None:
                    from . import scheduler

                    # Its queued jobs are cancelled (their inputs go with the daily cleanup); running ones stopped.
                    scheduler.stop_user_jobs(s, user.id)
            if args.unlimited or args.limited:
                user.unlimited = args.unlimited
            s.flush()
            if args.command == "user-add":
                token = new_token("tu")
                s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="admin"))
                print(f"User {email} created. Access token (shown only now):\n{token}")
            else:
                print(f"{email}: plan {user.plan_name}, paid until {user.paid_until}, "
                      f"{'enabled' if user.enabled else 'disabled'}{', unlimited' if user.unlimited else ''}")
        elif args.command == "user-token":
            user = s.execute(select(User).where(User.email_index == crypto.index(args.email))).scalar_one_or_none()
            if user is None:
                print(f"No user {args.email}", file=sys.stderr)
                return 1
            token = new_token("tu")
            s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label=args.label))
            print(f"Access token of {user.email} (shown only now):\n{token}")
        elif args.command == "user-list":
            for u in sorted(s.execute(select(User)).scalars(), key=lambda u: u.email):  # (encrypted in the database)
                acc = account(s, u, settings.grace_days)
                flags = (" admin" if u.is_admin else "") + (" unlimited" if u.unlimited else "") + \
                    ("" if u.enabled else " disabled")
                state = "paid" if acc.paid else "NOT PAID"
                print(f"{u.email:35} {acc.plan:10} {state:8} until {u.paid_until or '-'}  "
                      f"{acc.hours_used:.2f}/{acc.hours_included:g} h  {acc.active_jobs} active{flags}")
        elif args.command == "node-add":
            if s.execute(select(Node).where(Node.name == args.name)).scalar_one_or_none():
                print(f"The node {args.name} exists", file=sys.stderr)
                return 1
            token = new_token("tn")
            s.add(Node(name=args.name, token_hash=hash_token(token), priorities=parse_priorities(args.priorities)))
            print(f"Node {args.name} registered. Token for its node.toml (shown only now):\n{token}")
        elif args.command in ("node-set", "node-token"):
            node = s.execute(select(Node).where(Node.name == args.name)).scalar_one_or_none()
            if node is None:
                print(f"No node {args.name}", file=sys.stderr)
                return 1
            if args.command == "node-token":
                token = new_token("tn")
                node.token_hash = hash_token(token)
                print(f"New token of {node.name} (shown only now):\n{token}")
            else:
                if args.priorities is not None:
                    node.priorities = parse_priorities(args.priorities)
                if args.enable or args.disable:
                    node.enabled = args.enable
                print(f"{node.name}: {node.priorities} {'enabled' if node.enabled else 'disabled'}")
        elif args.command == "node-list":
            running = dict(s.execute(
                select(Job.node_id, func.count()).where(Job.status == "running").group_by(Job.node_id)
            ).all())
            for n in s.execute(select(Node).order_by(Node.name)).scalars():
                seen = n.last_seen.isoformat(timespec="seconds") if n.last_seen else "never"
                print(f"{n.name:20} {n.priorities:40} running {running.get(n.id, 0)}  last seen {seen}"
                      f"{'' if n.enabled else '  disabled'}")
        elif args.command == "node-del":
            try:
                requeued = pairing.delete_node(s, args.name)
            except pairing.PairingError as e:
                print(e, file=sys.stderr)
                return 1
            print(f"Node {args.name} removed; {requeued} running job(s) back to the queue")
        elif args.command == "node-requests":
            for r in pairing.pending(s):
                info = r["info"]
                print(f"{r['id']}  {r['name']:20} {r['address']:16} {info.get('cpus', '?')} CPU  "
                      f"{info.get('os', '')}  since {r['created_at']}")
        elif args.command in ("node-accept", "node-reject"):
            try:
                if args.command == "node-reject":
                    pairing.reject(s, args.id)
                    print("Rejected")
                else:
                    priorities = parse_priorities(args.priorities).split(",") if args.priorities else None
                    node = pairing.accept(s, args.id, args.name, priorities)
                    print(f"Node {node.name} made: the machine takes its token by itself in a few seconds")
            except pairing.PairingError as e:
                print(e, file=sys.stderr)
                return 1
        elif args.command == "queue":
            rows = s.execute(
                select(Job.job_class, Job.status, func.count()).where(Job.status.in_(("queued", "running")))
                .group_by(Job.job_class, Job.status)
            ).all()
            counts = {(c, st): n for c, st, n in rows}
            for c in JOB_CLASSES:
                print(f"{c:14} queued {counts.get((c, 'queued'), 0):5}  running {counts.get((c, 'running'), 0):5}")
        elif args.command == "metrics":
            from . import metrics

            m = metrics.report(s, args.days, node_minutes=settings.alert_node_minutes)
            if args.json:
                print(json.dumps(m, indent=1, ensure_ascii=False))
            else:
                print(metrics.text(m))
        elif args.command == "reports":
            statuses = [x for x in args.status.split(",") if x]
            unknown = [x for x in statuses if x not in reports.STATUSES]
            if unknown:
                print(f"Unknown status: {', '.join(unknown)}", file=sys.stderr)
                return 2
            query = select(BugReport).where(BugReport.status.in_(statuses))
            if args.app:
                query = query.where(BugReport.app == args.app)
            groups = reports.catalog(s.execute(query).scalars().all())
            text = reports.to_jsonl(groups) if args.format == "jsonl" else reports.to_markdown(groups)
            if args.out:
                with open(args.out, "w", encoding="utf-8", newline="\n") as out:
                    out.write(text)
                print(f"{len(groups)} problems written to {args.out}")
            else:
                sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
