#!/usr/bin/env python3
"""Upgrade drill: an older Tisma Cloud with data, then the current one on the same database and settings, as the owner
updates the master and the nodes (stop, put the new program in place, start).

    python deploy/upgrade_drill.py --old-ref f375604 [--database-url postgresql+psycopg://...] [--folder drill]

1. The old version (that commit of this repository, installed in its own virtual environment): a master with a plan,
   accounts and finished jobs, a registered node, and a node.toml written by its «role node».
2. The current version (the tisma-cloud of this environment) started on the same folder: the database is brought up to
   date at start (db.py adds the new columns).
3. Checks: every account is there, the old tokens still answer with the right e-mail, the jobs and used hours are
   kept, the old node's token still claims, a new job runs from start to end, and the old node.toml still loads.

Prints the time of each step; exit code 0 when every check passes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent


def run(exe: str, home: Path, *args: str, env_extra: dict | None = None, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TISMA_")}
    env.update({"TISMA_HOME": str(home), "TISMA_BACKUP_DIR": "off", "TISMA_UPDATES_URL": ""}, **(env_extra or {}))
    done = subprocess.run([exe, *args], capture_output=True, text=True, env=env, cwd=home, timeout=600)
    if check and done.returncode != 0:
        raise RuntimeError(f"{exe} {' '.join(args)}: {done.returncode}\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


def healthy(url: str, seconds: float = 60.) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            if httpx.get(f"{url}/v1/health", timeout=2).status_code == 200:
                return time.monotonic() - start
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer in {seconds:.0f} s")


def old_program(ref: str, folder: Path) -> str:
    """tisma-cloud of that commit, in its own virtual environment."""
    source = folder / "old-source"
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(source), ref], check=True,
                   capture_output=True)
    env_dir = folder / "old-venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    py = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(py), "-m", "pip", "install", "-q", f"{source}[master]"], check=True)
    return str(env_dir / ("Scripts/tisma-cloud.exe" if os.name == "nt" else "bin/tisma-cloud"))


def project() -> bytes:
    sys.path.insert(0, str(REPO / "deploy"))
    from loadtest import project as make
    return make()


def finish_jobs(url: str, node_token: str, n: int) -> None:
    c = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {node_token}"}, timeout=30)
    done, tries = 0, 0
    while done < n and tries < 200:
        tries += 1
        r = c.post("/v1/node/claim", params={"wait": 2}, json={})
        if r.status_code != 200:
            continue
        job = r.json()["job"]
        c.get(f"/v1/node/jobs/{job['id']}/input")
        time.sleep(0.05)
        c.post(f"/v1/node/jobs/{job['id']}/result", files={"file": ("p.gcode", b"G1 X1\n", "text/plain")},
               data={"summary": json.dumps({"print_time": "1m"})}).raise_for_status()
        done += 1
    if done < n:
        raise RuntimeError(f"only {done} of {n} jobs were finished")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old-ref", required=True, help="the commit of the old version")
    ap.add_argument("--new", default="tisma-cloud", help="the current tisma-cloud program")
    ap.add_argument("--database-url", default="", help="PostgreSQL instead of the SQLite of the folder (emptied first)")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--folder", type=Path, default=Path("upgrade-drill"))
    ap.add_argument("--port", type=int, default=18997)
    a = ap.parse_args(argv)
    folder = a.folder.resolve()
    if folder.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(folder / "old-source")],
                       capture_output=True)
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    home, node_home = folder / "master", folder / "node"
    home.mkdir()
    node_home.mkdir()
    url = f"http://127.0.0.1:{a.port}"
    times: dict[str, float] = {}
    failed: list[str] = []
    db_env = {"TISMA_DATABASE_URL": a.database_url} if a.database_url else {}
    if a.database_url:
        from sqlalchemy import create_engine, text

        engine = create_engine(a.database_url)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        engine.dispose()

    start = time.monotonic()
    old = old_program(a.old_ref, folder)
    times["old_installed_s"] = time.monotonic() - start

    # ---- the old version, with data
    run(old, home, "role", "master")
    run(old, home, "config", "port", str(a.port))
    run(old, home, "config", "master_computes", "off")
    run(old, home, "admin", "plan-set", "basico", "--hours", "10", "--concurrent", "5", env_extra=db_env)
    for i in range(a.users):
        run(old, home, "admin", "user-add", f"persona{i}@tisma.test", "--plan", "basico", env_extra=db_env)
    picked = (0, a.users // 2, a.users - 1)
    tokens = {}
    for i in picked:
        run(old, home, "admin", "user-set", f"persona{i}@tisma.test", "--paid-until", "2099-01-01", env_extra=db_env)
        tokens[i] = re.search(r"tu_[A-Za-z0-9_-]+", run(old, home, "admin", "user-token", f"persona{i}@tisma.test",
                                                       env_extra=db_env)).group(0)
    node_token = re.search(r"tn_[A-Za-z0-9_-]+", run(old, home, "admin", "node-add", "n1", "--priorities", "slice",
                                                     env_extra=db_env)).group(0)
    run(old, node_home, "role", "node")
    run(old, node_home, "config", "master_url", url)
    run(old, node_home, "config", "token", node_token)
    # Its programs: any file that exists (this checks the settings file, not the programs).
    for key in ("tisma_slicer", "tisma_fea"):
        run(old, node_home, "config", key, sys.executable)
    run(old, home, "start", env_extra=db_env)
    healthy(url)
    for i in picked:
        for _ in range(2):
            httpx.post(f"{url}/v1/jobs", headers={"Authorization": f"Bearer {tokens[i]}"}, data={"kind": "slice"},
                       files={"file": ("p.3mf", project(), "model/3mf")}, timeout=30).raise_for_status()
    finish_jobs(url, node_token, 2 * len(picked))
    before = {}
    for i in picked:
        auth = {"Authorization": f"Bearer {tokens[i]}"}
        me = httpx.get(f"{url}/v1/me", headers=auth).json()
        jobs = sorted(j["id"] for j in httpx.get(f"{url}/v1/jobs", headers=auth).json()["jobs"])
        before[i] = (me.get("plan"), me.get("hours_used"), jobs)
    run(old, home, "stop")

    # ---- the current version on the same folder
    start = time.monotonic()
    run(a.new, home, "start", env_extra=db_env)
    healthy(url)
    times["new_start_to_answer_s"] = time.monotonic() - start
    for i in picked:
        auth = {"Authorization": f"Bearer {tokens[i]}"}
        me = httpx.get(f"{url}/v1/me", headers=auth)
        if me.status_code != 200 or me.json().get("email") != f"persona{i}@tisma.test":
            failed.append(f"token of persona{i}: {me.status_code} {me.text[:200]}")
            continue
        jobs = sorted(j["id"] for j in httpx.get(f"{url}/v1/jobs", headers=auth).json()["jobs"])
        after = (me.json().get("plan"), me.json().get("hours_used"), jobs)
        if after != before[i]:
            failed.append(f"persona{i}: {before[i]} before, {after} after")
    listed = len(re.findall(r"persona\d+@tisma\.test", run(a.new, home, "admin", "user-list", env_extra=db_env)))
    if listed != a.users:
        failed.append(f"user-list shows {listed} of {a.users} accounts")
    claim = httpx.post(f"{url}/v1/node/claim", headers={"Authorization": f"Bearer {node_token}"},
                       params={"wait": 0}, json={})
    if claim.status_code not in (200, 204):
        failed.append(f"the old node's token: {claim.status_code} {claim.text[:200]}")
    auth = {"Authorization": f"Bearer {tokens[0]}"}
    r = httpx.post(f"{url}/v1/jobs", headers=auth, data={"kind": "slice"},
                   files={"file": ("p.3mf", project(), "model/3mf")}, timeout=30)
    if r.status_code != 201:
        failed.append(f"a new job after the update: {r.status_code} {r.text[:200]}")
    else:
        finish_jobs(url, node_token, 1)
        if httpx.get(f"{url}/v1/jobs/{r.json()['id']}", headers=auth).json().get("status") != "done":
            failed.append("the new job did not end done")
    run(a.new, home, "stop")
    # The node: the old node.toml read by the current program.
    shown = run(a.new, node_home, "config", check=False)
    if node_token not in shown and "token" not in shown:
        failed.append(f"the old node.toml was not read: {shown[:300]}")
    try:
        sys.path.insert(0, str(REPO))
        from tisma_cloud.node.config import NodeConfig

        NodeConfig.load(node_home / "node.toml")
    except Exception as ex:  # noqa: BLE001
        failed.append(f"the old node.toml does not load: {ex}")

    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(folder / "old-source")],
                   capture_output=True)
    print(json.dumps({"old_ref": a.old_ref, "database": "PostgreSQL" if a.database_url else "SQLite",
                      "users": a.users, **{k: round(v, 2) for k, v in times.items()}, "failed": failed}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
