"""Files of the jobs: the project sent by the user and the result made by the node, in storage_dir/jobs/<id>/."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import BinaryIO

# .tea: the result of an analysis of tisma-engine (its cloud.md, rule 19).
ALLOWED_RESULTS = (".gcode", ".bgcode", ".tfea", ".tea")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class TooLarge(Exception):
    pass


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        # Job ids are UUIDs made by the server, never names sent by users.
        return self.root / "jobs" / job_id

    def input_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input.3mf"

    def result_path(self, job_id: str, extension: str) -> Path:
        return self.job_dir(job_id) / f"result{extension}"

    def find_result(self, job_id: str) -> Path | None:
        for ext in ALLOWED_RESULTS:
            path = self.result_path(job_id, ext)
            if path.exists():
                return path
        return None

    @staticmethod
    def save_stream(stream: BinaryIO, path: Path, max_bytes: int) -> int:
        """Copies the stream to the file; raises TooLarge (and removes the file) past max_bytes."""
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = stream.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge()
                    out.write(chunk)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return size

    def remove_input(self, job_id: str) -> None:
        """The project of the user is not kept once the job ended (privacy): only the result, until it is removed."""
        self.input_path(job_id).unlink(missing_ok=True)

    def remove_job(self, job_id: str) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)


def safe_filename(name: str, default: str) -> str:
    """A plain file name (no directories, no odd characters) for the download of a result."""
    name = Path(name.replace("\\", "/")).name
    name = _SAFE_NAME.sub("_", name).strip(" .")
    return name[:100] or default
