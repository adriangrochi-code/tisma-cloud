#!/usr/bin/env python3
"""Restore drill: a master with data, its backup, and the same data back on a new, empty machine, timed. It follows
the steps of README «Copias de seguridad» with the tisma-cloud commands, as the owner would after losing the disk:

    python deploy/restore_drill.py [--users 300] [--exe tisma-cloud] [--folder drill]

1. Machine A: a master (SQLite) with accounts, plans and finished jobs; «tisma-cloud backup».
2. Machine B, empty: role master, the backup and data.key copied over (data.key never goes in the backups),
   «tisma-cloud restore», start.
3. B must answer A's tokens with the right e-mail (the personal data is encrypted with data.key), list all the
   accounts, keep their jobs and used hours. Without data.key, B must refuse to start and say why.

Prints the time of each step; exit code 0 when all the checks pass."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


def tc(exe: str, home: Path, *args: str, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TISMA_")}
    env["TISMA_HOME"] = str(home)
    run = subprocess.run([exe, *args], capture_output=True, text=True, env=env, cwd=home, timeout=600)
    if check and run.returncode != 0:
        raise RuntimeError(f"tisma-cloud {' '.join(args)}: {run.returncode}\n{run.stdout}\n{run.stderr}")
    return run.stdout + run.stderr


def healthy(url: str, seconds: float = 40.) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            if httpx.get(f"{url}/v1/health", timeout=2).status_code == 200:
                return time.monotonic() - start
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer in {seconds:.0f} s")


def finish_jobs(url: str, node_token: str, n: int) -> None:
    """A simulated node takes n jobs and gives their results, so the accounts have used hours."""
    c = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {node_token}"}, timeout=30)
    done = 0
    while done < n:
        r = c.post("/v1/node/claim", params={"wait": 2}, json={})
        if r.status_code != 200:
            continue
        job = r.json()["job"]
        c.get(f"/v1/node/jobs/{job['id']}/input")
        time.sleep(0.05)
        c.post(f"/v1/node/jobs/{job['id']}/result", files={"file": ("p.gcode", b"G1 X1\n", "text/plain")},
               data={"summary": json.dumps({"print_time": "1m"})}).raise_for_status()
        done += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=300)
    ap.add_argument("--exe", default="tisma-cloud", help="the tisma-cloud program (the .exe of the package, or the "
                                                         "command of pip)")
    ap.add_argument("--folder", type=Path, default=Path("restore-drill"), help="where machines A and B are made")
    ap.add_argument("--port", type=int, default=18996)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from loadtest import project

    folder = a.folder.resolve()
    shutil.rmtree(folder, ignore_errors=True)
    home_a, home_b = folder / "A", folder / "B"
    home_a.mkdir(parents=True)
    home_b.mkdir()
    url = f"http://127.0.0.1:{a.port}"
    times: dict[str, float] = {}
    failed: list[str] = []

    # ---- machine A
    tc(a.exe, home_a, "role", "master")
    tc(a.exe, home_a, "config", "port", str(a.port))
    tc(a.exe, home_a, "config", "master_computes", "off")
    tc(a.exe, home_a, "admin", "plan-set", "basico", "--hours", "10", "--concurrent", "5", "--public")
    start = time.monotonic()
    for i in range(a.users):
        tc(a.exe, home_a, "admin", "user-add", f"persona{i}@tisma.test", "--plan", "basico")
    times["accounts_made_s"] = time.monotonic() - start
    tokens = [re.search(r"tu_[A-Za-z0-9_-]+", tc(a.exe, home_a, "admin", "user-token", f"persona{i}@tisma.test"))
              .group(0) for i in (0, a.users // 2, a.users - 1)]
    for i in (0, a.users // 2, a.users - 1):
        tc(a.exe, home_a, "admin", "user-set", f"persona{i}@tisma.test", "--paid-until", "2099-01-01")
    node = re.search(r"tn_[A-Za-z0-9_-]+", tc(a.exe, home_a, "admin", "node-add", "n1", "--priorities", "slice"))
    tc(a.exe, home_a, "start")
    healthy(url)
    for token in tokens:
        for _ in range(2):
            httpx.post(f"{url}/v1/jobs", headers={"Authorization": f"Bearer {token}"}, data={"kind": "slice"},
                       files={"file": ("p.3mf", project(), "model/3mf")}, timeout=30).raise_for_status()
    finish_jobs(url, node.group(0), 2 * len(tokens))
    auth0 = {"Authorization": f"Bearer {tokens[0]}"}
    used_a = httpx.get(f"{url}/v1/me", headers=auth0).json()
    jobs_a = sorted(j["id"] for j in httpx.get(f"{url}/v1/jobs", headers=auth0).json()["jobs"])
    tc(a.exe, home_a, "stop")
    start = time.monotonic()
    made = tc(a.exe, home_a, "backup").strip().splitlines()[-1]
    times["backup_s"] = time.monotonic() - start
    backup_file = Path(made.split(" (")[0])
    backup_file = backup_file if backup_file.is_absolute() else home_a / backup_file

    # ---- machine B: empty, then what the owner keeps off the machine (the backup and data.key)
    tc(a.exe, home_b, "role", "master")
    tc(a.exe, home_b, "config", "port", str(a.port))
    tc(a.exe, home_b, "config", "master_computes", "off")
    copy = folder / backup_file.name
    shutil.copy(backup_file, copy)
    (home_b / "storage").mkdir(exist_ok=True)
    start = time.monotonic()
    shutil.copy(home_a / "storage" / "data.key", home_b / "storage" / "data.key")
    tc(a.exe, home_b, "restore", str(copy))
    times["restore_s"] = time.monotonic() - start
    start = time.monotonic()
    tc(a.exe, home_b, "start")
    healthy(url)
    times["start_to_answer_s"] = time.monotonic() - start

    for token, i in zip(tokens, (0, a.users // 2, a.users - 1)):
        me = httpx.get(f"{url}/v1/me", headers={"Authorization": f"Bearer {token}"})
        if me.status_code != 200 or me.json().get("email") != f"persona{i}@tisma.test":
            failed.append(f"token of persona{i}: {me.status_code} {me.text[:200]}")
    used_b = httpx.get(f"{url}/v1/me", headers=auth0).json()
    if used_a.get("hours_used") != used_b.get("hours_used") or used_a.get("plan") != used_b.get("plan"):
        failed.append(f"account: {used_a} before, {used_b} after")
    jobs_b = sorted(j["id"] for j in httpx.get(f"{url}/v1/jobs", headers=auth0).json()["jobs"])
    if not jobs_a or jobs_a != jobs_b:
        failed.append(f"jobs of persona0: {len(jobs_a)} before, {len(jobs_b)} after")
    listed = len(re.findall(r"persona\d+@tisma\.test", tc(a.exe, home_b, "admin", "user-list")))
    if listed != a.users:
        failed.append(f"user-list shows {listed} of {a.users} accounts")
    tc(a.exe, home_b, "stop")

    # ---- without data.key the master must refuse to start, and say why (never serve unreadable data)
    (home_b / "storage" / "data.key").unlink()
    tc(a.exe, home_b, "start", check=False)
    try:
        healthy(url, 15)
        failed.append("the master started without the data.key of its database")
    except RuntimeError:
        log = (home_b / "logs" / "tisma-cloud.log").read_text(encoding="utf-8", errors="replace")
        if "not the one this database was encrypted with" not in log:
            failed.append("without data.key the master did not start, but its log does not say why")
    finally:
        tc(a.exe, home_b, "stop", check=False)

    size = backup_file.stat().st_size
    print(json.dumps({"users": a.users, "backup": backup_file.name, "backup_kb": size >> 10,
                      **{k: round(v, 2) for k, v in times.items()}, "failed": failed}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
