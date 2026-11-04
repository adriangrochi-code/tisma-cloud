#!/usr/bin/env python3
"""A 3D scan through all of Tisma Cloud: sent in pieces to a running master as the app does, run by a node with
tisma_scan, and its result zip checked (pieza.stl, the measures of the summary).

    python deploy/scan_e2e.py --url http://127.0.0.1:8000 --token tu_... --scan scan.zip [--expect 40,30,20]

The master must have the account (a plan with the class "scan") and a node with "scan" in its priorities running.
--expect checks the measures of the part (largest first) within 15 %."""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--scan", type=Path, required=True)
    ap.add_argument("--expect", default="", help="the measures of the part in mm, e.g. 40,30,20")
    ap.add_argument("--timeout", type=float, default=7200.)
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url, headers={"Authorization": f"Bearer {a.token}"}, timeout=120)
    data = a.scan.read_bytes()
    r = c.post("/v1/scans", json={"size": len(data), "name": a.scan.name})
    r.raise_for_status()
    up = r.json()
    offset = 0
    while offset < len(data):
        r = c.put(f"/v1/scans/{up['id']}/data", params={"offset": offset},
                  content=data[offset:offset + up["chunk_size"]])
        if r.status_code not in (200, 409):
            r.raise_for_status()
        offset = r.json()["received"]
    r = c.post(f"/v1/scans/{up['id']}/complete")
    r.raise_for_status()
    job = r.json()
    print(f"scan {len(data) >> 10} KB in pieces of {up['chunk_size'] >> 10} KB -> job {job['id']}", flush=True)
    deadline = time.monotonic() + a.timeout
    last = None
    while time.monotonic() < deadline:
        view = c.get(f"/v1/jobs/{job['id']}").json()
        state = (view["status"], view["progress"])
        if state != last:
            print(f"  {view['status']} {view['progress']} %", flush=True)
            last = state
        if view["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(10)
    print(json.dumps(view, indent=1))
    if view["status"] != "done":
        return 1
    result = c.get(f"/v1/jobs/{job['id']}/result")
    result.raise_for_status()
    names = zipfile.ZipFile(io.BytesIO(result.content)).namelist()
    print("result:", names)
    if "pieza.stl" not in names:
        return 1
    if a.expect:
        real = sorted((float(v) for v in a.expect.split(",")), reverse=True)
        got = sorted(view["summary"]["dimensions_mm_xyz"], reverse=True)
        print("measures", got, "expected", real)
        if any(abs(g / r - 1) > 0.15 for g, r in zip(got, real)):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
