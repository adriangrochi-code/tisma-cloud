"""python -m tisma_cloud.node --config node.toml"""

from __future__ import annotations

import argparse
import os
import logging
from pathlib import Path

from .agent import Agent, cleanup_work_dir
from .config import NodeConfig
from .connection import STATE_FILE, StateFile
from .join import Rejected, ask_to_join, write_token

log = logging.getLogger("tisma_cloud.node")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tisma-cloud-node", description="Tisma Cloud computing node")
    parser.add_argument("--config", default="node.toml")
    # The node of the master has its token made by the master itself: it never asks to join.
    parser.add_argument("--no-join", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Run by the service, its supervisor puts the date (UTC) and the component in front of each line of the log.
    supervised = os.environ.get("TISMA_SUPERVISED") == "1"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s" if supervised else
                        "%(asctime)s %(levelname)s %(message)s")
    # One line per request is too much for a service that polls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config_path = Path(args.config)
    cfg = NodeConfig.load(config_path)
    cleanup_work_dir(cfg.work_dir)
    # How the last request to the master went, next to node.toml (the folder of the service): «status» reads it.
    state_path = config_path.with_name(STATE_FILE)
    state = StateFile(state_path)
    # What a run before this one saw is not said as if it were now.
    state.write(False, f"Conectando con el maestro {cfg.master_url}…")
    while True:
        if not cfg.token:
            if args.no_join:
                log.error("No token in %s", config_path)
                return 1
            try:
                cfg.token = ask_to_join(cfg, config_path, status_file=state)
            except Rejected:
                state.write(False, "El maestro rechazó esta máquina: el nodo se detuvo (iniciarlo de nuevo vuelve a "
                                   "pedir unirse)")
                log.error("The master rejected this machine: the node stops (start it again to ask again)")
                return 0
        agent = Agent(cfg, state_path=state_path)
        agent.run_forever()
        if not agent.unknown_token:
            return 0
        if args.no_join:
            return 1
        # Removed from the master: the token is forgotten and the machine asks to join again.
        write_token(config_path, "")
        cfg.token = ""


if __name__ == "__main__":
    raise SystemExit(main())
