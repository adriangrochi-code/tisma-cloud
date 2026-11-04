"""The loop of a computing node: claim a job from the master, run it, send the result.

Each slot claims jobs on its own (long polling: the master answers when there is a job or after a while), so a node
with two slots runs two jobs at once. A heartbeat tells the master the node is alive and which jobs it runs, and
brings back the jobs to stop (cancelled by the user, or given to another node after this one was lost).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import threading
import time
import uuid
from pathlib import Path

import httpx

from .. import __version__
from ..jobs import FEA_CLASSES
from .config import NodeConfig, memory_gb
from .runners import RUNNERS, JobCancelled, JobFailed, scan_readiness

log = logging.getLogger("tisma_cloud.node")

HEARTBEAT_S = 15
# A result is sent again while the master answers these (busy, restarting) or is out of reach, for this long [s].
RETRY_STATUS = (429, 502, 503, 504)
RESULT_RETRY_S = 900.
# How often a node asks tisma_scan --check again (a COLMAP installed meanwhile, a card removed) [s].
SCAN_CHECK_S = 600.
PROGRESS_EVERY_S = 2.


def machine_info(cfg: NodeConfig) -> dict:
    info = {"host": platform.node(), "os": platform.platform(), "cpus": os.cpu_count(), "slots": cfg.slots,
            "fea_slots": cfg.fea_slots, "gpu": cfg.gpu, "cuda_cores": max(0, int(cfg.cuda_cores)),
            "version": __version__}
    memory = memory_gb()
    if memory is not None:
        info["memory_gb"] = round(memory, 1)
    return info


class Agent:
    def __init__(self, cfg: NodeConfig, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.client = client or httpx.Client(base_url=cfg.master_url, verify=cfg.verify_tls, timeout=60.)
        self.client.headers["Authorization"] = f"Bearer {cfg.token}"
        self.info = machine_info(cfg)
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}
        # This run of the node program: the master tells two programs with one token apart (twins.py).
        self.instance = uuid.uuid4().hex
        # 3D scans: taken only while tisma_scan --check says this machine has everything (runners.scan_readiness).
        self.scan_ready = False
        self._scan_checked = 0.
        # Taken by a slot while it may claim, or runs, a structural analysis.
        self._fea = threading.BoundedSemaphore(max(1, cfg.fea_slots))
        self._stop = threading.Event()
        # The master does not know the token (the node was removed): the agent stops and the node asks to join again.
        self.unknown_token = False
        cfg.work_dir.mkdir(parents=True, exist_ok=True)

    # ---- master
    def claim(self, wait: int | None = None, exclude: tuple[str, ...] = ()) -> dict | None:
        params = {"wait": wait} if wait is not None else None
        r = self.client.post("/v1/node/claim", json={"info": self.info, "exclude": list(exclude)}, params=params,
                             timeout=120.)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()["job"]

    def check_scans(self, force: bool = False) -> bool:
        """Asks tisma_scan --check every SCAN_CHECK_S (and at start); the result goes with the machine's data, so the
        administration sees why a node takes no scans."""
        now = time.monotonic()
        if not force and now - self._scan_checked < SCAN_CHECK_S:
            return self.scan_ready
        self._scan_checked = now
        state = scan_readiness(self.cfg)
        if state["ready"] != self.scan_ready or force:
            if state["ready"]:
                log.info("3D scans: ready")
            elif self.cfg.tisma_scan:
                log.warning("3D scans: not taken until this is fixed: %s", "; ".join(state["problems"]))
        self.scan_ready = state["ready"]
        self.info["scan"] = state
        return self.scan_ready

    def heartbeat(self) -> None:
        self.check_scans()
        with self._lock:
            running = list(self._running)
        r = self.client.post("/v1/node/heartbeat", json={"running": running, "info": self.info,
                                                                "instance": self.instance}, timeout=30.)
        r.raise_for_status()
        for job_id in r.json().get("cancel", []):
            with self._lock:
                if job_id in self._running:
                    self._running[job_id].set()

    # ---- one job
    def process(self, job: dict) -> str:
        """Runs a claimed job; returns its final state (done, failed, cancelled)."""
        job_id = job["id"]
        cancel = threading.Event()
        with self._lock:
            self._running[job_id] = cancel
        work = self.cfg.work_dir / job_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        last = {"t": 0., "p": -1}

        def progress(p: int) -> None:
            now = time.monotonic()
            if p == last["p"] or (now - last["t"] < PROGRESS_EVERY_S and p < 100):
                return
            last.update(t=now, p=p)
            try:
                r = self.client.post(f"/v1/node/jobs/{job_id}/progress", json={"progress": p}, timeout=30.)
                if r.status_code == 404 or (r.status_code == 200 and r.json().get("cancel")):
                    cancel.set()
            except httpx.HTTPError:
                pass

        try:
            project = work / "input.3mf"
            with self.client.stream("GET", f"/v1/node/jobs/{job_id}/input", timeout=300.) as r:
                r.raise_for_status()
                with open(project, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            runner = RUNNERS.get(job["kind"])
            if runner is None:
                raise JobFailed(f"This node does not run {job['kind']} jobs")
            params = dict(job.get("params", {}))
            # The processor cores of the job's plan (capacity.py): the engine runs with that many threads.
            params["threads"] = int(job.get("cpu_cores") or 0)
            outcome = runner(self.cfg, params, project, work, progress, cancel.is_set)
            self._send_result(job_id, outcome)
            log.info("Job %s done", job_id)
            return "done"
        except JobCancelled:
            self._report_failure(job_id, "Cancelled", cancelled=True)
            log.info("Job %s cancelled", job_id)
            return "cancelled"
        except JobFailed as ex:
            self._report_failure(job_id, _without_paths(str(ex), work), details=ex.details)
            log.info("Job %s failed: %s", job_id, ex)
            return "failed"
        except Exception as ex:  # noqa: BLE001 (a node error must not stop the node)
            log.exception("Job %s failed on the node", job_id)
            self._report_failure(job_id, _without_paths(f"Error of the computing server: {ex}", work))
            return "failed"
        finally:
            with self._lock:
                self._running.pop(job_id, None)
            shutil.rmtree(work, ignore_errors=True)

    def _send_result(self, job_id: str, outcome) -> None:  # noqa: ANN001
        """Sends the result, trying again while the master is busy (429, 502-504: a restart, an update) or out of
        reach, as long as RESULT_RETRY_S: a computed job is not failed for a moment of the master."""
        deadline = time.monotonic() + RESULT_RETRY_S
        wait = 2.
        while True:
            try:
                with open(outcome.result, "rb") as f:
                    r = self.client.post(f"/v1/node/jobs/{job_id}/result",
                                         files={"file": (outcome.result.name, f, "application/octet-stream")},
                                         data={"summary": json.dumps(outcome.summary)}, timeout=600.)
                if r.status_code not in RETRY_STATUS:
                    r.raise_for_status()
                    return
                retry_after = r.headers.get("retry-after", "")
                pause = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else wait
                reason = f"the master answered {r.status_code}"
            except httpx.TransportError as ex:
                pause, reason = wait, f"the master is out of reach ({ex})"
            if time.monotonic() + pause > deadline or self._stop.is_set():
                raise JobFailed(f"The result could not be sent: {reason}")
            log.warning("Job %s: %s; sending the result again in %.0f s", job_id, reason, pause)
            self._stop.wait(pause)
            wait = min(wait * 2, 60.)

    def _report_failure(self, job_id: str, error: str, cancelled: bool = False, details: dict | None = None) -> None:
        body: dict = {"error": error[:4000], "cancelled": cancelled}
        if details:
            body["details"] = details
        try:
            self.client.post(f"/v1/node/jobs/{job_id}/fail", json=body, timeout=30.)
        except httpx.HTTPError:
            log.warning("Could not report the end of job %s", job_id)

    def run_once(self, wait: int = 0) -> str | None:
        job = self.claim(wait)
        return self.process(job) if job else None

    def claim_for_slot(self, wait: int | None = None) -> tuple[dict | None, bool]:
        """Claims a job for a slot: an analysis only while fewer than fea_slots run. Returns the job and whether the
        slot holds an analysis place (release it with release_fea() when the job ends)."""
        may_analyse = self._fea.acquire(blocking=False)
        try:
            # A scan only on a machine that has all it needs; an analysis only with a free analysis place.
            exclude = set(() if may_analyse else FEA_CLASSES) | (set() if self.scan_ready else {"scan"})
            job = self.claim(wait, exclude=tuple(sorted(exclude)))
        except BaseException:
            if may_analyse:
                self._fea.release()
            raise
        holds = may_analyse and job is not None and job.get("job_class") in FEA_CLASSES
        if may_analyse and not holds:
            self._fea.release()
        return job, holds

    def release_fea(self) -> None:
        self._fea.release()

    # ---- the service
    def _slot(self, index: int) -> None:
        backoff = 1.
        while not self._stop.is_set():
            try:
                job, holds = self.claim_for_slot()
                backoff = 1.
                try:
                    if job:
                        self.process(job)
                finally:
                    if holds:
                        self.release_fea()
            except httpx.HTTPStatusError as ex:
                if ex.response.status_code == 401:
                    self._token_refused()
                    return
                log.warning("Slot %d: the master answered %s, retrying in %.0f s", index, ex.response.status_code,
                            backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.)
            except httpx.HTTPError as ex:
                log.warning("Slot %d: master not reachable (%s), retrying in %.0f s", index, ex, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.)

    def _token_refused(self) -> None:
        if not self.unknown_token:
            log.warning("The master does not know the token of this node (was it removed?)")
        self.unknown_token = True
        self._stop.set()

    def _heartbeats(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            try:
                self.heartbeat()
            except httpx.HTTPStatusError as ex:
                if ex.response.status_code == 401:
                    self._token_refused()
                    return
                log.warning("Heartbeat failed: %s", ex)
            except httpx.HTTPError as ex:
                log.warning("Heartbeat failed: %s", ex)

    def run_forever(self) -> None:
        log.info("Node with %d slot(s) (%d for analyses) working for %s", self.cfg.slots, self.cfg.fea_slots,
                 self.cfg.master_url)
        self.check_scans(force=True)
        threads = [threading.Thread(target=self._heartbeats, daemon=True)]
        threads += [threading.Thread(target=self._slot, args=(i,), daemon=True) for i in range(self.cfg.slots)]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads[1:]):
                time.sleep(1)
        except KeyboardInterrupt:
            self._stop.set()
            with self._lock:
                for ev in self._running.values():
                    ev.set()


def cleanup_work_dir(work_dir: Path) -> None:
    """Removes what a previous run of the node left (jobs interrupted by a restart)."""
    if work_dir.exists():
        for p in work_dir.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _without_paths(text: str, work: Path) -> str:
    """An error for the user without the node's folders: the programs name the files of the job by their full path
    (/work/<job>/project.3mf), which says nothing to the user and shows how the node is laid out."""
    for folder in {str(work), str(work.resolve())}:
        text = text.replace(folder + os.sep, "").replace(folder, "")
    return text
