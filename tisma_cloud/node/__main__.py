"""python -m tisma_cloud.node --config node.toml"""

from __future__ import annotations

import argparse
import logging

from .agent import Agent, cleanup_work_dir
from .config import NodeConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tisma-cloud-node", description="Tisma Cloud computing node")
    parser.add_argument("--config", default="node.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # One line per request is too much for a service that polls.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = NodeConfig.load(args.config)
    cleanup_work_dir(cfg.work_dir)
    Agent(cfg).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
