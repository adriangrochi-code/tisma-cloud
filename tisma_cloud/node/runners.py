"""Runs a job with the Tisma programs (tisma-slicer, tisma-fea), as separate programs."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import NodeConfig

# Largest total uncompressed size of a project accepted by a node.
MAX_PROJECT_BYTES = 4 << 30
CONFIG_FILE = "Metadata/Slic3r_PE.config"
MODEL_CONFIG_FILE = "Metadata/Slic3r_PE_model.config"
# Projects of other slicers (Bambu Studio, OrcaSlicer): their settings are converted by Tisma when loaded, so they are
# not accepted (the client always sends Tisma projects).
FOREIGN_CONFIGS = ("Metadata/project_settings.config",)


class JobFailed(Exception):
    pass


class JobCancelled(Exception):
    pass


@dataclass
class Outcome:
    result: Path
    summary: dict = field(default_factory=dict)


Progress = Callable[[int], None]
Cancelled = Callable[[], bool]


def sanitize_project(src: Path, dst: Path, for_slicing: bool) -> None:
    """Copies the project without what could run commands on the node.

    The print settings may name post-processing scripts (post_process), which the slicer runs after exporting the
    G-code: they are removed. Projects of other slicers are refused, and so are archives that decompress to more than
    MAX_PROJECT_BYTES. Resin projects are not sliced in the cloud yet.
    """
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise JobFailed("The project is not a 3MF file") from None
    with archive:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > MAX_PROJECT_BYTES:
            raise JobFailed("The project is too large once decompressed")
        names = {i.filename for i in infos}
        if any(n in names for n in FOREIGN_CONFIGS):
            raise JobFailed("Only Tisma projects are accepted: save the project with Tisma Slicer first")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for info in infos:
                if info.is_dir():
                    continue
                data = archive.read(info)
                if info.filename == CONFIG_FILE:
                    text = data.decode("utf-8", errors="replace")
                    if for_slicing and re.search(r"^; printer_technology = SLA\s*$", text, re.M):
                        raise JobFailed("Slicing resin projects in the cloud is not available yet")
                    text = re.sub(r"^; post_process = .*$", "; post_process = ", text, flags=re.M)
                    data = text.encode("utf-8")
                elif info.filename == MODEL_CONFIG_FILE:
                    # Settings of the objects: post_process is not one of them, removed in case a project has it.
                    text = data.decode("utf-8", errors="replace")
                    text = re.sub(r'<metadata[^>]*key="post_process"[^>]*/>', "", text)
                    data = text.encode("utf-8")
                out.writestr(info.filename, data, compress_type=zipfile.ZIP_DEFLATED)


def _run(cmd: list[str], cfg: NodeConfig, progress_re: re.Pattern, progress: Progress, cancelled: Cancelled,
         cwd: Path) -> tuple[int, str]:
    """Runs the program, reports its progress lines and stops it on cancel or timeout. Returns (code, stderr tail)."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ)
    if not cfg.gpu:
        env["TISMA_FEA_GPU"] = "0"
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            env=env, text=True, errors="replace", **kwargs)
    err_tail: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = progress_re.match(line)
            if m:
                progress(int(m.group(1)))

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_tail.append(line.rstrip())
            del err_tail[:-30]

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()
    start = time.monotonic()
    reason = ""
    while proc.poll() is None:
        if cancelled():
            reason = "cancelled"
        elif time.monotonic() - start > cfg.timeout_s:
            reason = "timeout"
        if reason:
            _kill(proc)
            break
        time.sleep(0.2)
    proc.wait()
    for t in readers:
        t.join(timeout=5)
    if reason == "cancelled":
        raise JobCancelled()
    if reason == "timeout":
        raise JobFailed(f"The job took longer than {cfg.timeout_s} s")
    return proc.returncode, "\n".join(err_tail)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _gcode_summary(path: Path) -> dict:
    """Print time and filament from the comments at the end of a text G-code."""
    summary: dict = {}
    with open(path, "rb") as f:
        if f.read(4) == b"GCDE":
            return summary
        f.seek(max(0, path.stat().st_size - 256 * 1024))
        tail = f.read().decode("utf-8", errors="replace")
    patterns = {
        "print_time": r"^; estimated printing time \(normal mode\) = (.+)$",
        "filament_g": r"^; (?:total )?filament used \[g\] = ([\d.]+)",
        "filament_mm": r"^; (?:total )?filament used \[mm\] = ([\d.]+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, tail, re.M)
        if m:
            summary[key] = m.group(1).strip() if key == "print_time" else float(m.group(1))
    return summary


def run_slice(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
              cancelled: Cancelled) -> Outcome:
    clean = work / "project.3mf"
    sanitize_project(project, clean, for_slicing=True)
    out = work / "plate.gcode"
    # A fresh data directory: no profile or setting of another job leaks into this one; the project carries its own.
    datadir = Path(tempfile.mkdtemp(prefix="datadir-", dir=work))
    try:
        cmd = [cfg.tisma_slicer, "--datadir", str(datadir), "--loglevel", "1", "--export-gcode", "--output", str(out),
               str(clean)]
        code, err = _run(cmd, cfg, re.compile(r"^\s*(\d+)% =>"), progress, cancelled, work)
    finally:
        shutil.rmtree(datadir, ignore_errors=True)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise JobFailed(err.strip() or f"Slicing failed (code {code})")
    with open(out, "rb") as f:
        binary = f.read(4) == b"GCDE"
    if binary:
        out = out.rename(out.with_suffix(".bgcode"))
    return Outcome(out, _gcode_summary(out))


def run_fea(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
            cancelled: Cancelled) -> Outcome:
    clean = work / "project.3mf"
    sanitize_project(project, clean, for_slicing=False)
    out = work / "result.tfea"
    summary_path = work / "summary.json"
    cmd = [cfg.tisma_fea, "--input", str(clean), "--output", str(out), "--summary", str(summary_path),
           "--mode", params.get("mode", "analyze"), "--quality", params.get("quality", "normal"),
           "--object", str(int(params.get("object", 0))), "--instance", str(int(params.get("instance", 0)))]
    if params.get("solid"):
        cmd.append("--solid")
    if params.get("zones") is False:
        cmd.append("--no-zones")
    if params.get("ribs"):
        cmd.append("--ribs")
    if not cfg.gpu:
        cmd.append("--no-gpu")
    code, err = _run(cmd, cfg, re.compile(r"^progress (\d+)"), progress, cancelled, work)
    summary: dict = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            summary = {}
    if code != 0 or not out.exists():
        raise JobFailed(summary.get("error") or err.strip() or f"The analysis failed (code {code})")
    return Outcome(out, summary)


RUNNERS = {"slice": run_slice, "fea": run_fea}
