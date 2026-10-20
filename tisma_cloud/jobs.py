"""Kinds of jobs, their parameters and their queue (job class).

The users pay a monthly plan with hours of computing (models.Plan): a job is charged the time it actually ran on a
node, not an estimate.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from typing import IO

# The queues. A node takes the classes of its priority list (Node.priorities), in that order.
JOB_CLASSES = ("slice", "slice_large", "fea", "fea_optimize")
JOB_CLASS_DESCRIPTIONS = {
    "slice": "Slicing of a plate",
    "slice_large": "Slicing of a large plate (many triangles)",
    "fea": "Structural analysis",
    "fea_optimize": "Lightest infill / walls of a resin part (many analyses)",
}

FEA_MODES = ("analyze", "lightest-infill", "resin-structure")
FEA_QUALITIES = ("fast", "normal", "high", "ultra")

class JobError(ValueError):
    """A job that cannot be accepted (bad parameters or project)."""


@dataclass
class JobPlan:
    kind: str
    job_class: str
    params: dict


def _bool(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise JobError(f"'{key}' must be true or false")
    return value


def _index(params: dict, key: str) -> int:
    value = params.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 100_000:
        raise JobError(f"'{key}' must be an index")
    return value


def plan_job(kind: str, params: dict, triangles: int, large_slice_triangles: int) -> JobPlan:
    """Checks the parameters of a job and chooses its class (its queue)."""
    if not isinstance(params, dict):
        raise JobError("The parameters must be a JSON object")
    if kind == "slice":
        if params:
            raise JobError(f"Unknown parameters: {', '.join(sorted(params))}")
        return JobPlan(kind, "slice_large" if triangles > large_slice_triangles else "slice", {})
    if kind == "fea":
        unknown = set(params) - {"mode", "quality", "object", "instance", "solid", "zones", "ribs"}
        if unknown:
            raise JobError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        mode = params.get("mode", "analyze")
        quality = params.get("quality", "normal")
        if mode not in FEA_MODES:
            raise JobError(f"'mode' must be one of {', '.join(FEA_MODES)}")
        if quality not in FEA_QUALITIES:
            raise JobError(f"'quality' must be one of {', '.join(FEA_QUALITIES)}")
        clean = {
            "mode": mode,
            "quality": quality,
            "object": _index(params, "object"),
            "instance": _index(params, "instance"),
            "solid": _bool(params, "solid", False),
            "zones": _bool(params, "zones", True),
            "ribs": _bool(params, "ribs", False),
        }
        return JobPlan(kind, "fea" if mode == "analyze" else "fea_optimize", clean)
    raise JobError("'kind' must be slice or fea")


# Largest uncompressed size of a project read for the estimation (a zip bomb stops there and counts as large).
MAX_SCANNED_BYTES = 4 << 30


def count_triangles(project: IO[bytes], stop_after: int) -> int:
    """Triangles of the meshes of a 3MF project; the count stops once past stop_after (enough to choose the queue).

    The XML is not parsed: the "<triangle " tags are counted in the decompressed stream, so a damaged or hostile file
    costs at most MAX_SCANNED_BYTES of reading. Raises JobError when the file is not a 3MF (zip) project.
    """
    try:
        archive = zipfile.ZipFile(project)
    except zipfile.BadZipFile as ex:
        raise JobError("The project is not a 3MF file") from ex
    names = [i for i in archive.infolist() if i.filename.startswith("3D/") and i.filename.endswith(".model")]
    if not names:
        raise JobError("The 3MF file has no model")
    count = 0
    scanned = 0
    tag = b"<triangle "
    for info in names:
        with archive.open(info) as stream:
            tail = b""
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                scanned += len(chunk)
                data = tail + chunk
                count += data.count(tag)
                # Keep the end of the chunk in case a tag is split between two chunks (never counted twice: the
                # kept part is shorter than the tag).
                tail = data[-(len(tag) - 1):]
                if count > stop_after or scanned > MAX_SCANNED_BYTES:
                    return max(count, stop_after + 1)
    return count
