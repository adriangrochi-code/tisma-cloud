"""Settings of a computing node (node.toml, see deploy/node.example.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def memory_gb() -> float | None:
    """Memory of the machine [GB], or None where it is not known (it is read on Linux)."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None


def auto_slots(cpus: int, memory: float | None, engine: bool = False) -> int:
    """Jobs at a time for a machine: one per two cores (measured on 4 cores: two at a time gave the shortest average
    wait of a mixed queue, README), and 2 GB of memory for each (a plate of 225 000 triangles took 0.7 GB).

    With tisma-engine, one per core and 1 GB for each: a slicing job of the engine keeps about one core busy, so on 4
    cores four at a time sliced 1737 Benchys per hour against 1113 with two (README, Capacity); its largest test plate
    took 0.3 GB."""
    per_job = 1 if engine else 2
    slots = max(1, cpus if engine else cpus // 2)
    if memory is not None:
        slots = min(slots, max(1, int(memory // per_job)))
    return slots


@dataclass
class NodeConfig:
    master_url: str
    # Empty: the node asks the master to join (join.py) and writes the token it gets here.
    token: str
    # The programs of Tisma Slicer (tisma-slicer, tisma-fea), used when tisma_engine is empty.
    tisma_slicer: str = ""
    tisma_fea: str = ""
    # The new slicer, tisma-engine: when set, it runs both the slicing and the analyses (its command line contract is
    # docs/spec/features/cloud.md of the engine, rules 18 to 21) and tisma_slicer and tisma_fea are not needed.
    tisma_engine: str = ""
    # The name it asks to join with (the name of the machine when empty).
    name: str = ""
    work_dir: Path = Path("./node-work")
    # Jobs run at the same time; 0: automatic (auto_slots()). A slicing job of tisma-slicer keeps only about 3 of 4
    # cores busy, so two at a time finish a mixed queue sooner; one of tisma-engine keeps about one, so one per core
    # (README, Capacity).
    slots: int = 0
    # Structural analyses run at the same time (they take much memory and the graphics card).
    fea_slots: int = 1
    # Longest run of a job [s]; it is stopped and failed after that.
    timeout_s: int = 3600
    # The graphics card may be used: the structural analysis (Vulkan) and the visibility of the seam when slicing (OpenCL).
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
        if data.get("tisma_engine"):
            required: tuple[str, ...] = ("master_url", "tisma_engine")
        else:
            required = ("master_url", "tisma_slicer", "tisma_fea")
        for key in required:
            if not data.get(key):
                raise ValueError(f"'{key}' is missing in {path}")
        cfg = NodeConfig(**data)
        # A relative folder is the one next to node.toml: the programs run inside the folder of each job, so the paths
        # given to them must not depend on the current folder.
        cfg.work_dir = (Path(path).parent / cfg.work_dir).resolve()
        cfg.slots = int(cfg.slots) if int(cfg.slots) > 0 else auto_slots(os.cpu_count() or 1, memory_gb(),
                                                                         engine=bool(cfg.tisma_engine))
        cfg.fea_slots = max(1, int(cfg.fea_slots))
        cfg.master_url = cfg.master_url.rstrip("/")
        return cfg
