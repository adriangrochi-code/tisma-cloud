#!/usr/bin/env python3
"""Load test of a real master: many users sending jobs and asking for them every 2 s (as Tisma and the app do), and
simulated nodes that take the jobs and give a result at once. It measures what a person feels (the answers of the API)
and whether anything fails; the computing itself is not measured (the nodes do not slice).

    python deploy/loadtest.py --url http://127.0.0.1:8000 --users 50 --jobs 5 --nodes 4 [--scans 1 --scan-mb 40]

With --scans, each user also sends that many 3D scans in pieces, as Tisma Scanner does (PUT /v1/scans/{id}/data), at
the same time as the others: the heaviest uploads the master takes.

Needs a master already running and, on it, the accounts and nodes this script makes with the administration commands
(--setup, with TISMA_DATABASE_URL and TISMA_STORAGE_DIR of that master exported)."""

from __future__ import annotations

import argparse
import io
import json
import re
import statistics
import sys
import threading
import time
import zipfile
from collections import Counter

import httpx


def project() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        tri = "".join(f'<triangle v1="0" v2="1" v3="2"/>' for _ in range(12))
        z.writestr("3D/3dmodel.model", f'<?xml version="1.0"?><model><resources><object id="1"><mesh><vertices>'
                                       f'<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
                                       f'<vertex x="0" y="1" z="0"/></vertices><triangles>{tri}</triangles></mesh>'
                                       f'</object></resources></model>')
        z.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def scan_zip(megabytes: int, seed: int) -> bytes:
    """A scan as the master checks it (session.json and photos), of about that size: random bytes, so it does not
    compress, stored as JPEG files of 4 MB."""
    import random

    rng = random.Random(seed)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("scan_carga/session.json", json.dumps({"device": {"model": "load test"}}))
        for i in range(max(1, megabytes // 4)):
            z.writestr(f"scan_carga/images/0/{i}.jpg", rng.randbytes(4 << 20))
    return buf.getvalue()


def admin(*args: str) -> str:
    """An administration command in this process (the same as tisma-cloud admin, without starting Python each time)."""
    import contextlib

    from tisma_cloud import admin as commands

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if commands.main(list(args)) not in (0, None):
            raise RuntimeError(f"admin {' '.join(args)} failed")
    return out.getvalue()


def setup(users: int, nodes: int, prefix: str = "carga") -> tuple[list[str], list[str]]:
    admin("plan-set", "carga", "--hours", "1000", "--concurrent", "50", "--rpm", "10000", "--classes", "all")
    user_tokens, node_tokens = [], []
    for i in range(users):
        email = f"{prefix}{i}@tisma.test"
        admin("user-add", email, "--plan", "carga")
        admin("user-set", email, "--unlimited")
        user_tokens.append(re.search(r"tu_[A-Za-z0-9_-]+", admin("user-token", email)).group(0))
    for i in range(nodes):
        out = admin("node-add", f"{prefix}{i}", "--priorities", "slice,slice_large,scan")
        node_tokens.append(re.search(r"tn_[A-Za-z0-9_-]+", out).group(0))
    return user_tokens, node_tokens


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.times: dict[str, list[float]] = {}
        self.codes: Counter = Counter()
        self.errors: list[str] = []

    def add(self, what: str, seconds: float, code: int | str) -> None:
        with self.lock:
            self.times.setdefault(what, []).append(seconds)
            self.codes[(what, code)] += 1

    def report(self) -> dict:
        out = {}
        for what, values in sorted(self.times.items()):
            values = sorted(values)
            out[what] = {"n": len(values), "p50_ms": round(1000 * statistics.median(values)),
                         "p95_ms": round(1000 * values[int(0.95 * (len(values) - 1))]),
                         "max_ms": round(1000 * values[-1])}
        out["codes"] = {f"{w} {c}": n for (w, c), n in sorted(self.codes.items(), key=str)}
        out["errors"] = self.errors[:20]
        return out


def timed(stats: Stats, what: str, fn):
    start = time.monotonic()
    try:
        r = fn()
    except httpx.HTTPError as ex:
        stats.add(what, time.monotonic() - start, type(ex).__name__)
        with stats.lock:
            stats.errors.append(f"{what}: {ex!r}"[:300])
        return None
    stats.add(what, time.monotonic() - start, r.status_code)
    if r.status_code >= 500:
        with stats.lock:
            stats.errors.append(f"{what}: {r.status_code} {r.text[:200]}")
    return r


def send_scan(c: httpx.Client, data: bytes, stats: Stats) -> str | None:
    """A scan in pieces, as the app sends it; on a 409 it goes on from where the master says. Its job id, or None."""
    r = timed(stats, "scan_start", lambda: c.post("/v1/scans", json={"size": len(data), "name": "carga.zip"}))
    if r is None or r.status_code != 201:
        with stats.lock:
            stats.errors.append(f"scan refused: {getattr(r, 'status_code', '?')} {getattr(r, 'text', '')[:200]}")
        return None
    up = r.json()
    offset, tries = 0, 0
    while offset < len(data) and tries < 200:
        tries += 1
        piece = data[offset:offset + up["chunk_size"]]
        r = timed(stats, "scan_piece", lambda: c.put(f"/v1/scans/{up['id']}/data", params={"offset": offset},
                                                     content=piece))
        if r is None:
            continue
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", "5")))
            continue
        if r.status_code not in (200, 409):
            with stats.lock:
                stats.errors.append(f"scan piece: {r.status_code} {r.text[:200]}")
            return None
        offset = r.json()["received"]
    r = timed(stats, "scan_complete", lambda: c.post(f"/v1/scans/{up['id']}/complete"))
    if r is None or r.status_code != 201:
        with stats.lock:
            stats.errors.append(f"scan complete: {getattr(r, 'status_code', '?')} {getattr(r, 'text', '')[:200]}")
        return None
    return r.json()["id"]


def user(url: str, token: str, jobs: int, stats: Stats, done: Counter, lock: threading.Lock, scans: bytes | None = None,
         scan_count: int = 0) -> None:
    c = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
    data = project()
    mine = []
    for _ in range(scan_count if scans else 0):
        job_id = send_scan(c, scans, stats)
        if job_id:
            mine.append(job_id)
    for _ in range(jobs):
        for _attempt in range(30):
            r = timed(stats, "submit", lambda: c.post("/v1/jobs", data={"kind": "slice", "params": "{}"},
                                                       files={"file": ("p.3mf", data, "model/3mf")}))
            # As a client does: a busy answer is tried again after Retry-After.
            if r is not None and r.status_code == 429:
                time.sleep(float(r.headers.get("retry-after", "5")))
                continue
            break
        if r is not None and r.status_code == 201:
            mine.append(r.json()["id"])
        elif r is not None:
            with stats.lock:
                stats.errors.append(f"submit refused: {r.status_code} {r.text[:200]}")
    pending = set(mine)
    deadline = time.monotonic() + 600
    while pending and time.monotonic() < deadline:
        time.sleep(2)
        for job_id in list(pending):
            r = timed(stats, "status", lambda: c.get(f"/v1/jobs/{job_id}"))
            if r is not None and r.status_code == 200 and r.json()["status"] in ("done", "failed", "cancelled"):
                pending.discard(job_id)
                with lock:
                    done[r.json()["status"]] += 1
    with lock:
        done["unfinished"] += len(pending)


def scan_result() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pieza.stl", "solid pieza\nendsolid pieza\n")
    return buf.getvalue()


def node(url: str, token: str, stop: threading.Event, stats: Stats) -> None:
    c = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    while not stop.is_set():
        r = timed(stats, "claim", lambda: c.post("/v1/node/claim", params={"wait": 2}, json={}))
        if r is None or r.status_code != 200:
            continue
        job = r.json()["job"]
        timed(stats, "input", lambda: c.get(f"/v1/node/jobs/{job['id']}/input"))
        result = ("scan.zip", scan_result(), "application/zip") if job.get("kind") == "scan" else \
            ("plate.gcode", b"; done\nG1 X1\n", "text/plain")
        r = timed(stats, "result", lambda: c.post(f"/v1/node/jobs/{job['id']}/result", files={"file": result},
                                                  data={"summary": json.dumps({"print_time": "1m"})}))
        if r is None or r.status_code != 200:
            # As a real node: a result not taken is reported, so the job does not stay running.
            with stats.lock:
                stats.errors.append(f"result refused: {getattr(r, 'status_code', '?')} {getattr(r, 'text', '')[:200]}")
            timed(stats, "fail", lambda: c.post(f"/v1/node/jobs/{job['id']}/fail", json={"error": "result refused"}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--jobs", type=int, default=5, help="jobs per user")
    ap.add_argument("--nodes", type=int, default=4)
    ap.add_argument("--scans", type=int, default=0, help="3D scans per user, sent in pieces")
    ap.add_argument("--scan-mb", type=int, default=40, help="size of each scan in MB")
    ap.add_argument("--prefix", default="carga", help="names of its accounts and nodes (another one to run it again "
                                                      "on the same master)")
    a = ap.parse_args()
    users, nodes = setup(a.users, a.nodes, a.prefix)
    stats, done, lock, stop = Stats(), Counter(), threading.Lock(), threading.Event()
    node_threads = [threading.Thread(target=node, args=(a.url, t, stop, stats), daemon=True) for t in nodes]
    for t in node_threads:
        t.start()
    start = time.monotonic()
    scan = scan_zip(a.scan_mb, 1) if a.scans else None
    user_threads = [threading.Thread(target=user, args=(a.url, t, a.jobs, stats, done, lock, scan, a.scans))
                    for t in users]
    for t in user_threads:
        t.start()
    for t in user_threads:
        t.join()
    stop.set()
    report = {"users": a.users, "jobs": a.users * a.jobs, "scans": a.users * a.scans, "scan_mb": a.scan_mb,
              "nodes": a.nodes, "seconds": round(time.monotonic() - start),
              "finished": dict(done), **stats.report()}
    print(json.dumps(report, indent=1))
    bad = done["failed"] + done["unfinished"] + sum(n for (w, c), n in stats.codes.items()
                                                    if not isinstance(c, int) or c >= 500)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
