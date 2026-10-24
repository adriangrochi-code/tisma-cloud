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
from pathlib import Path

import httpx

from .. import __version__
from ..jobs import FEA_CLASSES
from .config import NodeConfig, memory_gb
from .runners import RUNNERS, JobCancelled, JobFailed

log = logging.getLogger("tisma_cloud.node")

HEARTBEAT_S = 15
PROGRESS_EVERY_S = 2.


def machine_info(cfg: NodeConfig) -> dict:
    info = {"host": platform.node(), "os": platform.platform(), "cpus": os.cpu_count(), "slots": cfg.slots,
            "fea_slots": cfg.fea_slots, "gpu": cfg.gpu, "version": __version__}
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

    def heartbeat(self) -> None:
        with self._lock:
            running = list(self._running)
        r = self.client.post("/v1/node/heartbeat", json={"running": running, "info": self.info}, timeout=30.)
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
            outcome = runner(self.cfg, job.get("params", {}), project, work, progress, cancel.is_set)
            with open(outcome.result, "rb") as f:
                r = self.client.post(f"/v1/node/jobs/{job_id}/result",
                                     files={"file": (outcome.result.name, f, "application/octet-stream")},
                                     data={"summary": json.dumps(outcome.summary)}, timeout=600.)
            r.raise_for_status()
            log.info("Job %s done", job_id)
            return "done"
        except JobCancelled:
            self._report_failure(job_id, "Cancelled", cancelled=True)
            log.info("Job %s cancelled", job_id)
            return "cancelled"
        except JobFailed as ex:
            self._report_failure(job_id, str(ex))
            log.info("Job %s failed: %s", job_id, ex)
            return "failed"
        except Exception as ex:  # noqa: BLE001 (a node error must not stop the node)
            log.exception("Job %s failed on the node", job_id)
            self._report_failure(job_id, f"Error of the computing server: {ex}")
            return "failed"
        finally:
            with self._lock:
                self._running.pop(job_id, None)
            shutil.rmtree(work, ignore_errors=True)

    def _report_failure(self, job_id: str, error: str, cancelled: bool = False) -> None:
        try:
            self.client.post(f"/v1/node/jobs/{job_id}/fail", json={"error": error[:4000], "cancelled": cancelled},
                             timeout=30.)
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
            job = self.claim(wait, exclude=() if may_analyse else FEA_CLASSES)
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
