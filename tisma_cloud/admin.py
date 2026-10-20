"""Administration from the master machine (works on the database directly):

    python -m tisma_cloud.admin plan-set basic --hours 10 --concurrent 2 [--rpm 120] [--priority 1]
    python -m tisma_cloud.admin plan-list
    python -m tisma_cloud.admin user-add EMAIL --plan basic [--paid-until 2026-12-31] [--admin] [--password]
    python -m tisma_cloud.admin user-set EMAIL [--plan P] [--paid-until DATE] [--enable | --disable] [--password]
    python -m tisma_cloud.admin user-token EMAIL [--label L]
    python -m tisma_cloud.admin user-list
    python -m tisma_cloud.admin node-add NAME --priorities fea_optimize,fea,slice_large,slice
    python -m tisma_cloud.admin node-set NAME [--priorities ...] [--enable | --disable]
    python -m tisma_cloud.admin node-token NAME
    python -m tisma_cloud.admin node-list
    python -m tisma_cloud.admin queue

Tokens are shown once, when they are created. In production the web site creates the users and records their
payments through /v1/site (see README.md); these commands are for the administrator and for tests.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime

from sqlalchemy import func, select

from .db import Database
from .jobs import JOB_CLASS_DESCRIPTIONS, JOB_CLASSES
from .models import ApiToken, Job, Node, Plan, User
from .scheduler import account
from .security import hash_password, hash_token, new_token
from .settings import Settings


def parse_priorities(text: str) -> str:
    items = [p.strip() for p in text.split(",") if p.strip()]
    unknown = [p for p in items if p not in JOB_CLASSES]
    if not items or unknown or len(set(items)) != len(items):
        raise SystemExit(f"Priorities: job classes separated by commas, each once, of: {', '.join(JOB_CLASSES)}")
    return ",".join(items)


def parse_date(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"Not a date: {text} (YYYY-MM-DD)") from None


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
    sub.add_parser("plan-list")

    for name in ("user-add", "user-set"):
        p = sub.add_parser(name)
        p.add_argument("email")
        p.add_argument("--plan", required=name == "user-add")
        p.add_argument("--paid-until")
        p.add_argument("--password", action="store_true", help="ask a password for the login from Tisma")
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
    sub.add_parser("queue", help="jobs waiting and running by class")

    args = parser.parse_args(argv)
    settings = Settings()
    db = Database(settings.database_url)
    db.create_all()

    with db.session() as s:
        if args.command == "plan-set":
            plan = s.get(Plan, args.name) or Plan(name=args.name)
            plan.monthly_hours, plan.max_concurrent = args.hours, max(1, args.concurrent)
            plan.requests_per_minute, plan.priority = max(1, args.rpm), args.priority
            s.add(plan)
            print(f"Plan {plan.name}: {plan.monthly_hours:g} h/month, {plan.max_concurrent} at a time, "
                  f"{plan.requests_per_minute} requests/min, priority {plan.priority}")
        elif args.command == "plan-list":
            for plan in s.execute(select(Plan).order_by(Plan.monthly_hours)).scalars():
                print(f"{plan.name:15} {plan.monthly_hours:8g} h/month  {plan.max_concurrent:3} at a time  "
                      f"{plan.requests_per_minute:5} req/min  priority {plan.priority}")
        elif args.command in ("user-add", "user-set"):
            email = args.email.strip().lower()
            if args.plan and s.get(Plan, args.plan) is None:
                print(f"No plan {args.plan} (plan-set creates it)", file=sys.stderr)
                return 1
            user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if args.command == "user-add":
                if user is not None:
                    print(f"The user {email} exists", file=sys.stderr)
                    return 1
                user = User(email=email, plan_name=args.plan, is_admin=args.admin)
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
            s.flush()
            if args.command == "user-add":
                token = new_token("tu")
                s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="admin"))
                print(f"User {email} created. Access token (shown only now):\n{token}")
            else:
                print(f"{email}: plan {user.plan_name}, paid until {user.paid_until}, "
                      f"{'enabled' if user.enabled else 'disabled'}")
        elif args.command == "user-token":
            user = s.execute(select(User).where(User.email == args.email.strip().lower())).scalar_one_or_none()
            if user is None:
                print(f"No user {args.email}", file=sys.stderr)
                return 1
            token = new_token("tu")
            s.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label=args.label))
            print(f"Access token of {user.email} (shown only now):\n{token}")
        elif args.command == "user-list":
            for u in s.execute(select(User).order_by(User.email)).scalars():
                acc = account(s, u, settings.grace_days)
                flags = (" admin" if u.is_admin else "") + ("" if u.enabled else " disabled")
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
        elif args.command == "queue":
            rows = s.execute(
                select(Job.job_class, Job.status, func.count()).where(Job.status.in_(("queued", "running")))
                .group_by(Job.job_class, Job.status)
            ).all()
            counts = {(c, st): n for c, st, n in rows}
            for c in JOB_CLASSES:
                print(f"{c:14} queued {counts.get((c, 'queued'), 0):5}  running {counts.get((c, 'running'), 0):5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
