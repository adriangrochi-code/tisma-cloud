"""python -m tisma_cloud.node --config node.toml"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .agent import Agent, cleanup_work_dir
from .config import NodeConfig
from .join import Rejected, ask_to_join, write_token

log = logging.getLogger("tisma_cloud.node")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tisma-cloud-node", description="Tisma Cloud computing node")
    parser.add_argument("--config", default="node.toml")
    # The node of the master has its token made by the master itself: it never asks to join.
    parser.add_argument("--no-join", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # One line per request is too much for a service that polls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config_path = Path(args.config)
    cfg = NodeConfig.load(config_path)
    cleanup_work_dir(cfg.work_dir)
    while True:
        if not cfg.token:
            if args.no_join:
                log.error("No token in %s", config_path)
                return 1
            try:
                cfg.token = ask_to_join(cfg, config_path)
            except Rejected:
                log.error("The master rejected this machine: the node stops (start it again to ask again)")
                return 0
        agent = Agent(cfg)
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
