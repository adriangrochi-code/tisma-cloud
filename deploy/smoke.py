#!/usr/bin/env python3
"""Smoke test of a running master, as a person on internet sees it: run it from outside the local network (a phone's
connection, another place) after installing or changing the master.

    python deploy/smoke.py --url https://tismaservidores.net [--token tu_...] [--project tests/data/bar_project.3mf]

It checks, one line each (OK / FAIL / WARN):
- the address answers /v1/health over HTTPS with a valid certificate;
- the public plans are listed (Tisma and the app read them);
- the administration (/admin) is not reachable from here (it answers 404 outside the local network);
- with --token (a user's token: tisma-cloud admin user-token): the account answers;
- with --project too: a slicing job is computed by a node and its G-code comes back.
Exit code 0 when nothing failed."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, state: str, what: str, detail: str = "") -> None:
        if state == "FAIL":
            self.failed += 1
        print(f"{state:4}  {what}" + (f": {detail}" if detail else ""), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the public address of the master")
    ap.add_argument("--token", default="", help="a user's token (tu_...), for the account and the job checks")
    ap.add_argument("--project", type=Path, help="a .3mf project to slice (needs --token and a node with Tisma Slicer)")
    ap.add_argument("--timeout", type=float, default=600., help="seconds to wait for the job")
    ap.add_argument("--local", action="store_true", help="run from the local network: /admin may answer")
    a = ap.parse_args(argv)
    url = a.url.rstrip("/")
    r = Report()
    c = httpx.Client(base_url=url, timeout=30, follow_redirects=False)

    if not url.startswith("https://"):
        r.line("WARN", "HTTPS", f"{url} is not https: the passwords would travel unencrypted")
    try:
        health = c.get("/v1/health")
    except httpx.HTTPError as ex:
        r.line("FAIL", "Health", f"{url} does not answer ({ex!r})"[:300])
        return 1
    ok = health.status_code == 200 and health.json().get("service") == "tisma-cloud"
    r.line("OK" if ok else "FAIL", "Health", f"{health.status_code}, version {health.json().get('version', '?')}"
           if ok else f"{health.status_code} {health.text[:200]}")

    plans = c.get("/v1/plans")
    names = [p.get("name") for p in plans.json().get("plans", [])] if plans.status_code == 200 else []
    r.line("OK" if names else "FAIL", "Public plans", ", ".join(map(str, names)) if names else
           f"{plans.status_code}: none public (tisma-cloud admin plan-set NAME ... --public)")

    admin = c.get("/admin")
    if a.local:
        r.line("OK", "Administration", f"{admin.status_code} from the local network")
    else:
        r.line("OK" if admin.status_code == 404 else "FAIL", "Administration hidden from internet",
               f"/admin answered {admin.status_code}" + ("" if admin.status_code == 404 else
                                                         ": check TISMA_CLIENT_IP_HEADER and TISMA_ADMIN_NETWORKS"))

    if not a.token:
        r.line("WARN", "Account and job", "skipped (no --token)")
        return 1 if r.failed else 0
    auth = {"Authorization": f"Bearer {a.token}"}
    me = c.get("/v1/me", headers=auth)
    r.line("OK" if me.status_code == 200 else "FAIL", "Account",
           me.json().get("email", "") if me.status_code == 200 else f"{me.status_code} {me.text[:200]}")
    if me.status_code != 200 or a.project is None:
        if a.project is None:
            r.line("WARN", "Job", "skipped (no --project)")
        return 1 if r.failed else 0

    sent = c.post("/v1/jobs", headers=auth, data={"kind": "slice", "params": "{}"},
                  files={"file": (a.project.name, a.project.read_bytes(), "model/3mf")})
    if sent.status_code != 201:
        r.line("FAIL", "Job sent", f"{sent.status_code} {sent.text[:200]}")
        return 1
    job = sent.json()["id"]
    deadline, view, start = time.monotonic() + a.timeout, {}, time.monotonic()
    while time.monotonic() < deadline:
        view = c.get(f"/v1/jobs/{job}", headers=auth).json()
        if view.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(3)
    if view.get("status") != "done":
        r.line("FAIL", "Job computed", f"{view.get('status', '?')} after {time.monotonic() - start:.0f} s "
                                       f"{view.get('error', '')}"[:300])
        return 1
    result = c.get(f"/v1/jobs/{job}/result", headers=auth)
    ok = result.status_code == 200 and b"G1 " in result.content
    r.line("OK" if ok else "FAIL", "Job computed",
           f"{time.monotonic() - start:.0f} s, {len(result.content) >> 10} KB of G-code, print time "
           f"{view.get('summary', {}).get('print_time', '?')}" if ok else f"result {result.status_code}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
