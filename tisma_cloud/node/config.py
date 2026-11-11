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


def auto_slots(cpus: int, memory: float | None) -> int:
    """Jobs at a time for a machine: one per core and 1 GB for each. A slicing job of tisma-engine keeps about one core
    busy, so on 4 cores four at a time sliced 1737 Benchys per hour against 1113 with two (README, Capacity); its
    largest test plate took 0.3 GB."""
    slots = max(1, cpus)
    if memory is not None:
        slots = min(slots, max(1, int(memory)))
    return slots


# Settings of the nodes made before Tisma Slicer was left (the owner, 2026-11-11): still read, not used.
OBSOLETE = ("tisma_slicer", "tisma_fea")


@dataclass
class NodeConfig:
    master_url: str
    # Empty: the node asks the master to join (join.py) and writes the token it gets here.
    token: str
    # tisma-engine: it runs the slicing and the analyses (its command line contract is docs/spec/features/cloud.md of
    # the engine, rules 18 to 21). The only slicer since Tisma Slicer was left (the owner, 2026-11-11).
    tisma_engine: str = ""
    # The translation tables of the profile pack (translation/ of a release of Tisma-profiles-, which the package brings
    # in tisma-profiles/): with them the engine reads the settings of other slicers' projects (--translation-dir).
    # Empty or missing: those projects slice with the job's settings and the engine says so.
    translation_dir: str = ""
    # The 3D scans of Tisma Scanner (jobs of kind "scan"): the program that rebuilds a part from its photos (tisma_scan
    # of the openscanner repository: COLMAP, OpenMVS and a graphics card): the program, or its tools/tisma_scan.py,
    # which runs with the Python of the machine. Its contract is in node/runners.py, run_scan().
    tisma_scan: str = ""
    # Longest run of a 3D scan [s]: a few hundred photos take tens of minutes on a graphics card.
    scan_timeout_s: int = 6 * 3600
    # The name it asks to join with (the name of the machine when empty).
    name: str = ""
    work_dir: Path = Path("./node-work")
    # Jobs run at the same time; 0: automatic (auto_slots()): a slicing job of tisma-engine keeps about one core busy,
    # so one per core (README, Capacity).
    slots: int = 0
    # Structural analyses run at the same time (they take much memory and the graphics card).
    fea_slots: int = 1
    # Longest run of a job [s]; it is stopped and failed after that.
    timeout_s: int = 3600
    # The graphics card may be used: the structural analysis (Vulkan) and the visibility of the seam when slicing (OpenCL).
    gpu: bool = True
    # CUDA cores of the node's NVIDIA card, as NVIDIA publishes them for the model (0: none or not counted). Only for
    # the capacity report of the master (capacity.py): there is no portable way to read it from the card.
    cuda_cores: int = 0
    # Verify the HTTPS certificate of the master (disable only on a local network with a self-signed certificate).
    verify_tls: bool = True

    @staticmethod
    def load(path: str | Path) -> "NodeConfig":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # The token may come from the environment instead of the file.
        data.setdefault("token", os.environ.get("TISMA_NODE_TOKEN", ""))
        for old in OBSOLETE:
            data.pop(old, None)
        known = set(NodeConfig.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown settings in {path}: {', '.join(sorted(unknown))}")
        if data.get("tisma_scan") and not data.get("tisma_engine"):
            # A node only for 3D scans.
            required: tuple[str, ...] = ("master_url", "tisma_scan")
        else:
            required = ("master_url", "tisma_engine")
        for key in required:
            if not data.get(key):
                raise ValueError(f"'{key}' is missing in {path}")
        url = str(data["master_url"]).strip()
        if not url.lower().startswith(("http://", "https://")) or len(url.split("://", 1)[1].strip("/")) == 0:
            # Without the scheme no request can ever reach the master: say so instead of retrying for ever.
            raise ValueError(f"master_url in {path} must start with http:// or https:// "
                             f"(for example http://192.168.1.10:8000), not {url!r}")
        cfg = NodeConfig(**data)
        # A relative folder is the one next to node.toml: the programs run inside the folder of each job, so the paths
        # given to them must not depend on the current folder.
        cfg.work_dir = (Path(path).parent / cfg.work_dir).resolve()
        cfg.slots = int(cfg.slots) if int(cfg.slots) > 0 else auto_slots(os.cpu_count() or 1, memory_gb())
        cfg.fea_slots = max(1, int(cfg.fea_slots))
        cfg.master_url = cfg.master_url.rstrip("/")
        return cfg
