"""A node without a token asks the master to join (pairing.py on the master) and waits until the administrator
accepts it, then writes the token it gets in its node.toml. The request (its number and secret) is kept next to
node.toml, so that a restart waits for the same request instead of asking again."""

from __future__ import annotations

import json
import logging
import platform
import re
import time
from pathlib import Path
from typing import Callable

import httpx

from .agent import machine_info
from .config import NodeConfig

log = logging.getLogger("tisma_cloud.node")


class Rejected(Exception):
    """The administrator of the master rejected this machine."""


def request_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + ".join")


def write_token(config_path: Path, token: str) -> None:
    """Sets the token in node.toml, keeping the rest of the file as it is (an empty token removes it)."""
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    line = f'token = "{token}"'
    if re.search(r"^\s*token\s*=.*$", text, re.M):
        text = re.sub(r"^\s*token\s*=.*$", lambda _m: line, text, count=1, flags=re.M)
    else:
        text = text + ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    config_path.write_text(text, encoding="utf-8")


def ask_to_join(cfg: NodeConfig, config_path: Path, client: httpx.Client | None = None,
                sleep: Callable[[float], None] = time.sleep) -> str:
    """Asks the master to make this machine a node and waits for the answer. Returns the token (also written in
    node.toml); raises Rejected when the administrator rejects it."""
    client = client or httpx.Client(base_url=cfg.master_url, verify=cfg.verify_tls, timeout=30.)
    state_path = request_path(config_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("master") != cfg.master_url:
            state = None
    except (OSError, ValueError):
        state = None
    name = cfg.name or platform.node() or "nodo"
    backoff, poll_s = 5., 5.
    while True:
        try:
            if state is None:
                r = client.post("/v1/node/join", json={"name": name, "info": machine_info(cfg)})
                r.raise_for_status()
                answer = r.json()
                state = {"master": cfg.master_url, "id": answer["id"], "secret": answer["secret"]}
                poll_s = float(answer.get("poll_s", 5))
                state_path.write_text(json.dumps(state), encoding="utf-8")
                log.info("Asked %s to make this machine the node %s: accept it in the window of the master (Nodes)",
                         cfg.master_url, name)
            r = client.get(f"/v1/node/join/{state['id']}", headers={"X-Join-Secret": state["secret"]})
            if r.status_code == 404:
                # Forgotten by the master (a day without an answer, another database): asked again.
                state = None
                state_path.unlink(missing_ok=True)
                continue
            r.raise_for_status()
            answer = r.json()
            backoff = 5.
            if answer["status"] == "accepted":
                write_token(config_path, answer["token"])
                state_path.unlink(missing_ok=True)
                log.info("The master accepted this machine as the node %s", answer.get("name", name))
                return answer["token"]
            if answer["status"] == "rejected":
                state_path.unlink(missing_ok=True)
                raise Rejected()
            sleep(poll_s)
        except httpx.HTTPError as ex:
            # Not reachable, or not from this network (404 on the join of a master on internet), or too many requests.
            log.warning("Asking the master to join failed (%s), again in %.0f s", ex, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, 60.)
