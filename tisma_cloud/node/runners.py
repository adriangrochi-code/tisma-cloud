"""Runs a job with the Tisma programs (tisma-engine, or tisma-slicer and tisma-fea), as separate programs."""

from __future__ import annotations

import dataclasses
import json
import logging
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

from ..system import program_env
from .config import NodeConfig

# Largest total uncompressed size of a project accepted by a node.
MAX_PROJECT_BYTES = 4 << 30
CONFIG_FILE = "Metadata/Slic3r_PE.config"
MODEL_CONFIG_FILE = "Metadata/Slic3r_PE_model.config"
# Projects of other slicers (Bambu Studio, OrcaSlicer): their settings are converted by Tisma when loaded, so they are
# not accepted (the client always sends Tisma projects).
FOREIGN_CONFIGS = ("Metadata/project_settings.config",)


class JobFailed(Exception):
    """A job that could not be done. details: what the engine's summary adds to its error (its stable "code" and
    "error_es", the text in Spanish), kept with the job so the program shows it in the user's language."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class JobCancelled(Exception):
    pass


@dataclass
class Outcome:
    result: Path
    summary: dict = field(default_factory=dict)


Progress = Callable[[int], None]
Cancelled = Callable[[], bool]


def sanitize_project(src: Path, dst: Path, for_slicing: bool, engine: bool = False) -> None:
    """Copies the project without what could run commands on the node.

    The print settings may name post-processing scripts (post_process), which the slicer runs after exporting the
    G-code: they are removed. Projects of other slicers are refused, and so are archives that decompress to more than
    MAX_PROJECT_BYTES. Resin projects are not sliced in the cloud yet.

    For tisma-engine (engine=True) only the size is checked and the files are copied as they are: the engine runs with
    --sandbox, which runs no post-processing script nor plugin, and it opens the projects of other slicers itself.
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
        if not engine and any(n in names for n in FOREIGN_CONFIGS):
            raise JobFailed("Only Tisma projects are accepted: save the project with Tisma Slicer first")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for info in infos:
                if info.is_dir():
                    continue
                data = archive.read(info)
                if engine:
                    pass
                elif info.filename == CONFIG_FILE:
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
    env = program_env()
    if cfg.gpu:
        # The slicer casts the visibility rays of the seam on the graphics card (OpenCL: the NVIDIA cards and the Intel
        # HD Graphics 4000; on the CPU when there is none). TISMA_GPU_DEVICE, when set, picks the card by its name.
        env.setdefault("TISMA_GPU_SLICING", "1")
    else:
        env["TISMA_FEA_GPU"] = "0"
        env["TISMA_GPU_SLICING"] = "0"
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


ENGINE_PROGRESS = re.compile(r"^progress (\d+)")


def _engine_summary(path: Path) -> dict:
    """The JSON summary the engine writes (cloud.md, rule 18); empty when it is missing or not an object."""
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return summary if isinstance(summary, dict) else {}


_ENGINE_HELP: dict[str, str] = {}


def _engine_has(engine: str, option: str) -> bool:
    """Whether this tisma-engine lists the option in its --help (read once per program; any failure: no)."""
    if engine not in _ENGINE_HELP:
        try:
            done = subprocess.run([engine, "--help"], capture_output=True, text=True, timeout=30)
            _ENGINE_HELP[engine] = done.stdout + done.stderr
        except (OSError, subprocess.SubprocessError):
            _ENGINE_HELP[engine] = ""
    return option in _ENGINE_HELP[engine]


def _run_engine(cfg: NodeConfig, command: str, project: Path, out: Path, extra: list[str], work: Path,
                progress: Progress, cancelled: Cancelled, params: dict | None = None) -> dict:
    """Runs tisma-engine with the contract of its cloud.md (rules 18 to 21) and returns its summary."""
    summary_path = work / "summary.json"
    cmd = [cfg.tisma_engine, command, str(project), "-o", str(out), "--summary", str(summary_path), "--sandbox"]
    cmd += extra
    threads = int(params.get("threads") or 0) if params else 0
    # Only to an engine that has the option (cloud.md, rule 19d): an older one would refuse the whole command.
    if threads > 0 and _engine_has(cfg.tisma_engine, "--threads"):
        cmd += ["--threads", str(threads)]
    if not cfg.gpu:
        cmd.append("--no-gpu")
    code, err = _run(cmd, cfg, ENGINE_PROGRESS, progress, cancelled, work)
    summary = _engine_summary(summary_path)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        what = {"slice": "Slicing", "orient": "The orientation"}.get(command, "The analysis")
        details = {k: str(summary[k])[:1000] for k in ("code", "error_es") if summary.get(k)}
        raise JobFailed(str(summary.get("error") or "") or err.strip() or f"{what} failed (code {code})", details)
    for k in ("error", "code", "error_es"):
        summary.pop(k, None)
    return summary


def run_slice(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
              cancelled: Cancelled) -> Outcome:
    if cfg.tisma_engine:
        clean = work / "project.3mf"
        sanitize_project(project, clean, for_slicing=True, engine=True)
        out = work / "plate.gcode"
        summary = _run_engine(cfg, "slice", clean, out, [], work, progress, cancelled, params)
        with open(out, "rb") as f:
            binary = f.read(4) == b"GCDE"
        if binary:
            out = out.rename(out.with_suffix(".bgcode"))
        return Outcome(out, summary)
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
    if cfg.tisma_engine:
        clean = work / "project.3mf"
        sanitize_project(project, clean, for_slicing=False, engine=True)
        out = work / "result.tea"
        extra = ["--mode", params.get("mode", "analyze"),
                 "--object", str(int(params.get("object", 0))), "--instance", str(int(params.get("instance", 0)))]
        if params.get("quality"):
            extra += ["--quality", params["quality"]]
        if params.get("solid"):
            extra.append("--solid")
        if params.get("zones") is False:
            extra.append("--no-zones")
        if params.get("ribs"):
            extra.append("--ribs")
        return Outcome(out, _run_engine(cfg, "analyze", clean, out, extra, work, progress, cancelled, params))
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


def _engine_only(cfg: NodeConfig, what: str) -> None:
    """The aerodynamic analysis and the orientation exist only in tisma-engine (cloud.md, rules 19b and 19c)."""
    if not cfg.tisma_engine:
        raise JobFailed(f"This node cannot run {what}: it needs tisma_engine in its configuration")


def run_aero(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
             cancelled: Cancelled) -> Outcome:
    _engine_only(cfg, "aerodynamic analyses")
    clean = work / "project.3mf"
    sanitize_project(project, clean, for_slicing=False, engine=True)
    out = work / "result.taero"
    direction = params.get("direction", "x")
    if isinstance(direction, list):
        direction = ",".join(f"{float(v):g}" for v in direction)
    extra = ["--object", str(int(params.get("object", 0))), "--instance", str(int(params.get("instance", 0))),
             "--quality", str(params.get("quality", "normal")), "--speed", f"{float(params.get('speed', 10.)):g}",
             "--direction", str(direction)]
    return Outcome(out, _run_engine(cfg, "aero", clean, out, extra, work, progress, cancelled, params))


def run_orient(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
               cancelled: Cancelled) -> Outcome:
    _engine_only(cfg, "automatic orientations")
    clean = work / "project.3mf"
    sanitize_project(project, clean, for_slicing=False, engine=True)
    out = work / "orientation.json"
    extra = ["--object", str(int(params.get("object", 0))), "--instance", str(int(params.get("instance", 0)))]
    return Outcome(out, _run_engine(cfg, "orient", clean, out, extra, work, progress, cancelled, params))


# The files of a 3D scan the node sends back, in one zip (the result of the job).
SCAN_RESULTS = ("pieza.stl", "pieza_informe.json", "pieza.ply", "textura.zip", "log.txt")


def scan_command(cfg: NodeConfig) -> list[str]:
    """The command of tisma_scan: the program, or a .py script with the Python of the machine (not the one of this
    node, which may be the tisma-cloud program itself)."""
    if not cfg.tisma_scan:
        return []
    if cfg.tisma_scan.lower().endswith(".py"):
        python = shutil.which("python3") or shutil.which("python") or shutil.which("py")
        if not python:
            raise JobFailed("tisma_scan is a Python script, and this machine has no Python")
        return [python, cfg.tisma_scan]
    return [cfg.tisma_scan]


def run_scan(cfg: NodeConfig, params: dict, project: Path, work: Path, progress: Progress,
             cancelled: Cancelled) -> Outcome:
    """A 3D scan of Tisma Scanner (scans.py of the master) rebuilt with tisma_scan. Its contract:

        tisma_scan <scan.zip> -o <folder> --summary <summary.json> [--threads <n>] [--no-gpu]

    - progress on the standard output, one line per change: "progress <0 to 100>";
    - in <folder>: pieza.stl (the part in mm), pieza_informe.json (measures, volume, deviation, scale source) and, when
      made, pieza.ply (the cloud), textura.zip (the textured mesh) and log.txt;
    - <summary.json>: what the app shows (from the report) or, when it fails, "error" (English), "code" and
      "error_es" (as the engine, cloud.md rule 19a); exit code other than 0.

    The node sends back one zip with those files (the result of the job)."""
    command = scan_command(cfg)
    if not command:
        raise JobFailed("This node cannot run 3D scans: it needs tisma_scan in its configuration")
    scan = work / "scan.zip"
    project.rename(scan)
    out = work / "scan-out"
    out.mkdir()
    summary_path = work / "summary.json"
    cmd = [*command, str(scan), "-o", str(out), "--summary", str(summary_path)]
    threads = int(params.get("threads") or 0)
    if threads > 0:
        cmd += ["--threads", str(threads)]
    if not cfg.gpu:
        cmd.append("--no-gpu")
    long_cfg = dataclasses.replace(cfg, timeout_s=max(cfg.timeout_s, cfg.scan_timeout_s))
    code, err = _run(cmd, long_cfg, ENGINE_PROGRESS, progress, cancelled, work)
    summary = _engine_summary(summary_path)
    if code != 0 or not (out / "pieza.stl").is_file():
        details = {k: str(summary[k])[:1000] for k in ("code", "error_es") if summary.get(k)}
        if summary.get("error") and details.get("code"):
            raise JobFailed(str(summary["error"]), details)
        # It ended without saying why (it crashed before its summary: a missing module, a broken install): what it
        # printed is for the node's log, not for the person.
        logging.getLogger("tisma_cloud").warning("tisma_scan ended with code %s without a summary:\n%s", code,
                                                 err.strip())
        raise JobFailed("The 3D scan failed on the computing server",
                        {"code": "scan_error", "error_es": "El escaneo falló en el servidor de cálculo"})
    for k in ("error", "code", "error_es"):
        summary.pop(k, None)
    result = work / "scan.zip.result"
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as z:
        for name in SCAN_RESULTS:
            path = out / name
            if path.is_file():
                # The texture and the cloud are compressed already or large: stored as they are.
                z.write(path, name, compress_type=zipfile.ZIP_STORED if name.endswith((".zip", ".ply"))
                        else zipfile.ZIP_DEFLATED)
    final = work / "scan.zip"
    scan.unlink(missing_ok=True)
    result.rename(final)
    return Outcome(final, summary)


RUNNERS = {"slice": run_slice, "fea": run_fea, "aero": run_aero, "orient": run_orient, "scan": run_scan}
