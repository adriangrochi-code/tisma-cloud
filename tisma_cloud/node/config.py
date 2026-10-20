"""Settings of a computing node (node.toml, see deploy/node.example.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class NodeConfig:
    master_url: str
    token: str
    tisma_slicer: str
    tisma_fea: str
    work_dir: Path = Path("./node-work")
    # Jobs run at the same time (one per machine is best for slicing, which already uses every core).
    slots: int = 1
    # Longest run of a job [s]; it is stopped and failed after that.
    timeout_s: int = 3600
    # The structural analysis may use the graphics card (Vulkan).
    gpu: bool = True
    # Verify the HTTPS certificate of the master (disable only on a local network with a self-signed certificate).
    verify_tls: bool = True

    @staticmethod
    def load(path: str | Path) -> "NodeConfig":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # The token may come from the environment instead of the file.
        data.setdefault("token", os.environ.get("TISMA_NODE_TOKEN", ""))
        known = set(NodeConfig.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown settings in {path}: {', '.join(sorted(unknown))}")
        for key in ("master_url", "token", "tisma_slicer", "tisma_fea"):
            if not data.get(key):
                raise ValueError(f"'{key}' is missing in {path}")
        cfg = NodeConfig(**data)
        cfg.work_dir = Path(cfg.work_dir)
        cfg.slots = max(1, int(cfg.slots))
        cfg.master_url = cfg.master_url.rstrip("/")
        return cfg
