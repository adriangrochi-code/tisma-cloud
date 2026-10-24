"""The machines of the local network that ask to be computing nodes, and the removal of a node.

A node without a token asks the master to join (POST /v1/node/join, only from the local network) with the name of its
machine, and gets the number and the secret of its request. The administrator sees the request in the window of the
server (or the panel, or tisma-cloud admin node-requests) and accepts it, which makes the node and its token, or rejects
it. The machine asks for the state of its request every few seconds with its secret, takes the token once (the request
is deleted then) and writes it in its node.toml. Requests not answered are forgotten after a day.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .jobs import JOB_CLASSES
from .models import Job, Node, NodeRequest, utcnow
from .security import hash_token, new_token

# How long a request waits for the administrator, or an answer waits for the machine.
VALIDITY = timedelta(days=1)
# Requests waiting at the same time: more are refused (a machine of the network asking in a loop).
MAX_PENDING = 50
# What the machine reports about itself, at most [characters of JSON].
MAX_INFO = 4000


class PairingError(ValueError):
    pass


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_name(name: str) -> str:
    """A name of node from the name of a machine: letters, digits, '.', '_' and '-', at most 60 characters."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")[:60]
    return name or "nodo"


def _free_name(s: Session, name: str) -> str:
    """The name, or the name with -2, -3... when a node already has it."""
    taken = set(s.execute(select(Node.name).where(Node.name.like(f"{name}%"))).scalars())
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def request_join(s: Session, name: str, address: str, info: dict) -> tuple[str, str]:
    """A machine asks to be a node. Returns the number of its request and its secret. The same machine asking again
    (same name and address, a restart that lost its request) gets the request it had, with a new secret."""
    name = clean_name(name)
    text = json.dumps(info if isinstance(info, dict) else {})[:MAX_INFO]
    secret = secrets.token_urlsafe(24)
    req = s.execute(select(NodeRequest).where(NodeRequest.status == "pending", NodeRequest.name == name,
                                              NodeRequest.address == address)).scalars().first()
    if req is None:
        pending = s.execute(select(func.count()).select_from(NodeRequest)
                            .where(NodeRequest.status == "pending")).scalar_one()
        if pending >= MAX_PENDING:
            raise PairingError("Too many machines are asking to join: try again later")
        req = NodeRequest(name=name, address=address)
        s.add(req)
    req.info, req.secret_hash, req.created_at = text, _hash(secret), utcnow()
    s.flush()
    return req.id, secret


def join_state(s: Session, request_id: str, secret: str) -> dict | None:
    """What the administrator answered: {"status": "pending"}, {"status": "accepted", "name", "token"} (once: the
    request is deleted) or {"status": "rejected"} (also once). None: no such request, or a wrong secret."""
    req = s.get(NodeRequest, request_id)
    if req is None or not hmac.compare_digest(req.secret_hash, _hash(secret)):
        return None
    if req.status == "pending":
        return {"status": "pending"}
    answer = {"status": req.status}
    if req.status == "accepted":
        answer.update(name=req.node_name, token=req.token)
    s.delete(req)
    return answer


def request_view(req: NodeRequest) -> dict:
    return {"id": req.id, "name": req.name, "address": req.address, "info": req.info_dict, "status": req.status,
            "created_at": req.created_at.isoformat(timespec="seconds")}


def pending(s: Session) -> list[dict]:
    return [request_view(r) for r in s.execute(select(NodeRequest).where(NodeRequest.status == "pending")
                                                .order_by(NodeRequest.created_at)).scalars()]


def _pending_request(s: Session, request_id: str) -> NodeRequest:
    req = s.get(NodeRequest, request_id)
    if req is None or req.status != "pending":
        raise PairingError("No such request waiting for an answer")
    return req


def accept(s: Session, request_id: str, name: str = "", priorities: list[str] | None = None) -> Node:
    """Makes the node of a request: with the name asked (another one when it is taken) and all the job classes unless
    priorities. The machine takes its token the next time it asks."""
    req = _pending_request(s, request_id)
    priorities = list(priorities or JOB_CLASSES)
    if not priorities or any(p not in JOB_CLASSES for p in priorities) or len(set(priorities)) != len(priorities):
        raise PairingError(f"A node does at least one of {', '.join(JOB_CLASSES)}, each once")
    token = new_token("tn")
    node = Node(name=_free_name(s, clean_name(name or req.name)), token_hash=hash_token(token),
                priorities=",".join(priorities), info=req.info)
    s.add(node)
    req.status, req.node_name, req.token = "accepted", node.name, token
    s.flush()
    return node


def reject(s: Session, request_id: str) -> None:
    req = _pending_request(s, request_id)
    req.status = "rejected"


def delete_node(s: Session, name: str, now: datetime | None = None) -> int:
    """Removes a node. Its running jobs go back to the queue (the cancelled ones end), the old jobs keep no reference
    to it. Returns how many jobs were requeued. The machine, if it is still running, is no longer accepted and asks
    to join again."""
    node = s.execute(select(Node).where(Node.name == name)).scalar_one_or_none()
    if node is None:
        raise PairingError(f"No node {name}")
    now = now or utcnow()
    requeued = 0
    for job in s.execute(select(Job).where(Job.node_id == node.id, Job.status == "running")).scalars():
        if job.cancel_requested:
            job.status, job.finished_at = "cancelled", now
        else:
            job.status, job.progress, job.started_at = "queued", 0, None
            requeued += 1
    s.execute(update(Job).where(Job.node_id == node.id).values(node_id=None))
    s.delete(node)
    s.flush()
    return requeued


def drop_old(s: Session, now: datetime | None = None) -> int:
    """Forgets the requests (and the answers not taken) older than VALIDITY."""
    limit = (now or utcnow()) - VALIDITY
    return s.execute(delete(NodeRequest).where(NodeRequest.created_at < limit)).rowcount or 0
