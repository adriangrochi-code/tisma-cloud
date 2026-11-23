#!/usr/bin/env python3
"""A master and its nodes come back by themselves (the owner, 23-11-2026): with the program of the package.

    python deploy/reconnect_drill.py [--exe tisma-cloud] [--folder reconnect-drill] [--port 18997]

A master (that does not compute) and two nodes in folders of their own, accepted; a fake tisma-engine that takes
about 30 s per job, so that there is one running when things are cut:

1. the master is restarted while a node runs a job: both nodes are seen again in less than 90 s (and the master
   says they reconnected), and the job ends well;
2. the node running a job is killed at once (no «stop»: its tisma-cloud.pid stays) and started again: it comes back
   by itself, and the job goes back to the queue and ends well.

Exit code 0 when both happen. Linux and macOS (the fake engine is a Python script)."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ENGINE = """#!{python}
import json, sys, time, zipfile
args = sys.argv[1:]
if args == ["--help"]:
    print("usage: tisma-engine slice <project> -o <out> --summary <summary.json> [--sandbox] [--threads <n>] "
          "[--translation-dir <folder>]")
    sys.exit(0)
get = lambda k: args[args.index(k) + 1]
zipfile.ZipFile(args[1]).namelist()
for p in (0, 50, 100):
    print(f"progress {{p}}", flush=True)
    time.sleep(10)
open(get("-o"), "w").write("G1 X1\\n")
json.dump({{"print_time": "10m", "print_time_s": 600, "layers": 50}}, open(get("--summary"), "w"))
"""

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "tests" / "data" / "box_project.3mf"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", default="tisma-cloud", help="the program (of the package, or the installed one)")
    ap.add_argument("--folder", type=Path, default=Path("reconnect-drill"))
    ap.add_argument("--port", type=int, default=18997)
    a = ap.parse_args()
    root = a.folder.resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TISMA_NO_AUTOSTART="1")
    url = f"http://127.0.0.1:{a.port}"

    def tc(home: str, *args: str, check: bool = True) -> str:
        r = subprocess.run([a.exe, "--home", str(root / home), *args], env=env, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SystemExit(f"tisma-cloud --home {home} {' '.join(args)}: {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def nodes() -> dict[str, dict]:
        out = {}
        for line in tc("master", "admin", "node-list").splitlines():
            parts = line.split()
            seen = line.split("last seen ", 1)[1].split()[0]
            back = line.split("reconnected ", 1)[1].split()[0] if "reconnected " in line else ""
            out[parts[0]] = {"seen": seen, "reconnected": back}
        return out

    def stamp(text: str) -> float:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S")) - time.timezone if text not in ("", "never") else 0.

    def logs() -> None:
        for home in ("master", "n1", "n2"):
            print(f"---- {home}\n{tc(home, 'logs', '40', check=False)}")

    engine = root / "tisma-engine"
    engine.write_text(ENGINE.format(python=sys.executable))
    engine.chmod(0o755)
    tc("master", "role", "master")
    tc("master", "config", "port", str(a.port))
    tc("master", "config", "master_computes", "off")
    tc("master", "admin", "plan-set", "drill", "--hours", "10", "--concurrent", "4", "--classes", "all")
    tc("master", "admin", "user-add", "drill@tisma.test", "--plan", "drill")
    tc("master", "admin", "user-set", "drill@tisma.test", "--paid-until", "2099-01-01")
    token = next(w for w in tc("master", "admin", "user-token", "drill@tisma.test").split() if w.startswith("tu_"))
    tc("master", "start")
    for home in ("n1", "n2"):
        tc(home, "role", "node")
        for key, value in (("master_url", url), ("tisma_engine", str(engine)), ("name", home), ("slots", "1")):
            tc(home, "config", key, value)
        tc(home, "start")
    deadline = time.monotonic() + 60
    accepted = 0
    while accepted < 2 and time.monotonic() < deadline:
        time.sleep(2)
        for line in tc("master", "admin", "node-requests").splitlines():
            tc("master", "admin", "node-accept", line.split()[0])
            accepted += 1
    def joined() -> bool:
        listed = nodes()
        return len(listed) == 2 and all(v["seen"] != "never" for v in listed.values())

    while time.monotonic() < deadline and not joined():
        time.sleep(2)
    if not joined():
        logs()
        raise SystemExit("the two nodes did not join")
    h = {"Authorization": f"Bearer {token}"}

    def submit() -> str:
        r = httpx.post(f"{url}/v1/jobs", headers=h, files={"file": ("box.3mf", PROJECT.read_bytes())},
                       data={"kind": "slice"}, timeout=30)
        r.raise_for_status()
        job = r.json()["id"]
        end = time.monotonic() + 60
        while httpx.get(f"{url}/v1/jobs/{job}", headers=h, timeout=10).json()["status"] != "running":
            if time.monotonic() > end:
                raise SystemExit("the job was not taken")
            time.sleep(0.5)
        return job

    def wait_job(job: str, limit: float) -> str:
        end = time.monotonic() + limit
        status = "?"
        while time.monotonic() < end:
            try:
                status = httpx.get(f"{url}/v1/jobs/{job}", headers=h, timeout=10).json()["status"]
            except httpx.HTTPError:
                status = "?"
            if status in ("done", "failed", "cancelled"):
                break
            time.sleep(2)
        return status

    ok = True
    # 1. The master restarted with a job running.
    job = submit()
    t0 = time.time()
    tc("master", "restart")
    back: dict[str, float] = {}
    end = time.monotonic() + 120
    while len(back) < 2 and time.monotonic() < end:
        for name, v in nodes().items():
            if name not in back and stamp(v["seen"]) > t0 and stamp(v["reconnected"]) >= t0 - 1:
                back[name] = stamp(v["seen"]) - t0
        time.sleep(2)
    status = wait_job(job, 180)
    print(f"1. master restarted: nodes back after {json.dumps({k: round(v) for k, v in back.items()})} s; job {status}")
    if len(back) < 2 or max(back.values()) >= 90 or status != "done":
        ok = False

    # 2. The node running a job cut at once, and started again.
    job = submit()
    runner = next(n for n in ("n1", "n2") if (root / n / "node-work" / job).is_dir())
    pid = int((root / runner / "tisma-cloud.pid").read_text().split()[0])
    t0 = time.time()
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    tc(runner, "start")
    # The first time the master hears it again, while the job goes back to the queue and ends.
    seen, status = 0., "?"
    end = time.monotonic() + 240
    while time.monotonic() < end:
        v = nodes()[runner]
        if not seen and stamp(v["seen"]) > t0:
            seen = stamp(v["seen"]) - t0
        try:
            status = httpx.get(f"{url}/v1/jobs/{job}", headers=h, timeout=10).json()["status"]
        except httpx.HTTPError:
            status = "?"
        if seen and status in ("done", "failed", "cancelled"):
            break
        time.sleep(2)
    running = "En marcha" in tc(runner, "status", check=False)
    print(f"2. {runner} cut during its job and started again: running {running}, seen after {seen:.0f} s; job {status} "
          f"after {time.time() - t0:.0f} s")
    if not running or not seen or status != "done":
        ok = False
    if not ok:
        logs()
    for home in ("n1", "n2", "master"):
        tc(home, "stop", check=False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
