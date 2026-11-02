"""How many users of each plan the computing nodes can serve.

Each plan reserves a share of a node for every job of its users: plan.cpu_cores processor cores and plan.cuda_cores
CUDA cores of the graphics card (0: the plan's jobs do not count on the graphics card). The nodes report their
processor cores (os.cpu_count()) and the CUDA cores the administrator wrote in their node.toml (NVIDIA publishes the
number of each card; there is no portable way to read it from the card). With those numbers the report says:

- at once: users of the plan whose jobs can all run together at the plan's limit (max_concurrent each);
- per month: users of the plan whose monthly hours fit in the nodes running all the month without a stop.

The second is an upper bound (it assumes the queue is never empty); the first is the safe one.
"""

from __future__ import annotations

import json
from typing import Iterable

from .models import Node, Plan

# Hours in an average month (365.25 × 24 / 12).
HOURS_PER_MONTH = 730.5


def node_resources(nodes: Iterable[Node]) -> dict:
    """Processor and CUDA cores of the enabled nodes, from what each one reported."""
    cpu = cuda = count = 0
    for node in nodes:
        if not node.enabled:
            continue
        try:
            info = json.loads(node.info or "{}")
        except ValueError:
            info = {}
        cpu += max(0, int(info.get("cpus") or 0))
        cuda += max(0, int(info.get("cuda_cores") or 0))
        count += 1
    return {"nodes": count, "cpu_cores": cpu, "cuda_cores": cuda}


def plan_capacity(plan: Plan, resources: dict) -> dict:
    """Users of one plan the nodes can serve (see the module). None: the plan asks for nothing that limits it."""
    cpu_job = max(1, plan.cpu_cores or 1)
    cuda_job = max(0, plan.cuda_cores or 0)
    concurrent = max(1, plan.max_concurrent or 1)
    limits_at_once = [resources["cpu_cores"] // (cpu_job * concurrent)]
    if cuda_job:
        limits_at_once.append(resources["cuda_cores"] // (cuda_job * concurrent))
    at_once = min(limits_at_once)
    per_month = None
    if plan.monthly_hours and plan.monthly_hours > 0:
        limits = [resources["cpu_cores"] * HOURS_PER_MONTH / (cpu_job * plan.monthly_hours)]
        if cuda_job:
            limits.append(resources["cuda_cores"] * HOURS_PER_MONTH / (cuda_job * plan.monthly_hours))
        per_month = int(min(limits))
    return {"plan": plan.name, "cpu_cores_per_job": cpu_job, "cuda_cores_per_job": cuda_job,
            "max_concurrent": concurrent, "monthly_hours": plan.monthly_hours,
            "users_at_once": at_once, "users_per_month": per_month}


def report(nodes: Iterable[Node], plans: Iterable[Plan]) -> dict:
    resources = node_resources(nodes)
    return {"resources": resources, "hours_per_month": HOURS_PER_MONTH,
            "plans": [plan_capacity(p, resources) for p in plans]}
