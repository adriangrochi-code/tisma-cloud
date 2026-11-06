# Server session: state

Repository `adriangrochi-code/tisma-cloud` (private, owner's decision 2026-11-04), branch `main`; it was `server/` of the
public slicer repository, moved here with its history (`git subtree split`). Spec/QA contact:
`session_01QadJfL7KViJEaJL92p8Swb`.

Tests: venv, `pip install -e '.[master,test]'`, `python -m pytest -q` (249 passed, 2 skipped
at the last commit).

Dates (owner, relayed by QA 2026-11-04, corrected): closed beta in December 2026 on the owner's real server (tunnel,
Windows service), which must be up and stable for it; 1.0 in the second week of January 2027.

## Work list (in order)

1. Set up: done.
2. Server against the engine's `docs/spec/features/cloud.md` (read-only clone in
   `/home/user/adriangrochi-code/tisma_engine`, owner's permission 2026-11-04; dev bc900da + spec/next): rules 1 to 21
   and 10b/10c/19a hold; `tests/test_contract.py` checks them rule by rule. Differences, decided:
   - aero `extreme` (48 cells): the server accepts it (8cd9006, owner's request, engine dev 47f6f14); cloud.md rule 6
     and aerodynamics.md still list up to `high`: the spec is behind, told QA.
   - `GET /v1/jobs?limit` above 200 is capped at 200, not refused: matches «hasta 200».
   - Rule 22 (Thingiverse proxy, thingiverse.md rule 21): not built. It is after 1.0 and its routes wait for the
     engine's confirmation with a real token (thingiverse.md rule 20). Decision: after the beta work, roadmap first.
3. Cloudflare tunnel guide and Windows manager: d891fc8 (service ImagePath, cert.pem, ingress checks, log file,
   `config KEY --clear`; client IP header only from `TISMA_TRUSTED_PROXIES`). Not tested on Windows nor with a real
   cloudflared (downloading it was refused). Still to review: the Windows installer flow.
4. Robustness: a node that restarts gets its lost jobs requeued from its heartbeat (orphans, 60 s grace). Daily backups
   made by the master itself for the tisma-cloud program (backup.py: SQLite online copy, checked; `tisma-cloud backup`,
   `restore`; alert; Diagnóstico). Uploads security: any
   other request body is capped (TISMA_MAX_BODY_KB, 1 MB). Queue under load: deploy/loadtest.py, 100 users / 500 jobs / 8 nodes
   on SQLite: 500 done, 0 failed (after fixing node results refused by the users' upload places). Next: Windows
   installer review.

## Done for QA

- `admin plan-set --classes a,b|all --public/--no-public` (QA item 2: the engine's plan lock reads `classes` from
  `GET /v1/plans`; CLI plans were not public, so they were not listed).

## Open from QA (2026-11-04)

- Content-Length on uploads (411): keep, make the error text clear (check against cloud.md once readable).
- After a container restart the master logs «El nodo qa1 no responde hace 25 min»: the alert closes on the node's next
  request (any request updates last_seen) and its jobs are requeued after node_timeout_s; a node that comes back at
  once had its jobs stuck as running: fixed (heartbeat orphans).
- aero estimates for high/extreme not measured.
- Owner decision relayed by QA: this session may read all of tisma_engine (never write it, never send it this repo's
  PrusaSlicer-derived details) and, here, only server/, qa/, docs and CLAUDE.md. Adding the repo still needs the
  owner's own approval in this session.
- 9711f1b: passes for QA on a clean run; its earlier failure was two node programs with one token. Now the master
  spots them (twins.py: a random instance id in the heartbeat), requeues nothing from them and opens an alert; a new
  program's list is trusted after 35 s.
- app_updates.md rule 13 (spec/next ec3ee8a): GET /v1/updates and /v1/updates.sig, mirrored hourly byte for byte
  from the engine's `updates` release (updates.py). That release does not exist yet (404): the server answers 503.
- Tisma Scanner in the cloud (owner 2026-11-04: inside Tisma Cloud, a plan of its own, this session also changes
  openscanner): job kind/class `scan`, uploads in pieces at /v1/scans (scans.py, ScanUpload), node runner run_scan()
  with `tisma_scan`. The openscanner side (tools/tisma_scan.py, the app's client) is on its own branch there.
- 2026-11-04: merged by the owner. Slicer PR #24 (dev): server/ removed from the public repository (still in alpha
  and some feature branches until they take dev; still in its history). openscanner PR #1: the app and tisma_scan on
  its main branch (ccr-0a51a497-evjxni); new openscanner work goes on a fresh branch from it.
- Open: a scan that completes with pieza.stl (needs a COLMAP with CUDA or OpenMVS; building OpenMVS here was blocked by
  the session's permissions) and bundling COLMAP/OpenMVS with the scan node. Waiting for the owner on doing it in CI.
- CI of this repository: the tests job passed at 3d8e74d; the two executable jobs were still queued at 16:20.
- Scan proven end to end without CUDA (openscanner CI run 41880333430: synthetic 40x30x20 mm box -> STL 41.17 x 31.08 x
  20.14 mm). openscanner PR #2 (scan/cpu-pipeline) waits for the owner. This repo's .github/workflows/scan-e2e.yml
  runs master -> node -> result with it, on openscanner's scan/cpu-pipeline: switch OPENSCANNER_REF to
  ccr-0a51a497-evjxni once PR #2 is merged. TextureMesh crashes (-11) in the CPU build: no textura.zip there.
- openscanner AGPL-3.0: waits for the owner's own confirmation in this session (relayed by QA only).

## Ownership (2026-11-04, owner via QA)

The Tisma Scanner session (session_01QadJfNpbVkgeJVY8DZiSRZ, openscanner branch scanner/next) owns the app and the
pipeline. This session owns the cloud side: /v1/scans, the scan nodes and packaging tisma_scan for nodes (the
TismaScan-windows artifact of openscanner's server-windows.yml: tisma_scan.exe with the official COLMAP 3.9.1 and
OpenMVS 2.4.0, found by tisma-cloud in tisma-scan/ next to the program, ec5be1f). The tisma_scan contract was sent to
that session (command line, progress lines, files, summary and its codes, --check). Pending: the CI of openscanner
cb85aef (the package and a synthetic scan on Windows) and whether scanner/next has 64d0c37 and cb85aef.
- 18:54: the Windows run of cb85aef got to the scan; COLMAP.bat splits the commas of --ImageReader.camera_params
  (cmd.exe). The scanner session fixes it on scanner/next (bin/colmap.exe with lib/ on PATH and QT_PLUGIN_PATH, also
  for a TISMA_COLMAP set to the .bat) and now owns server-windows.yml. After its green run: README «Nodo de escaneo
  en Windows», TISMA_COLMAP=C:\COLMAP\bin\colmap.exe instead of COLMAP.bat.
- 19:02: scan-e2e.yml (be8ffee) green, run 41886937961: app-style upload -> master -> node (tisma_scan.py, CPU COLMAP
  + OpenMVS) -> result zip (pieza.stl, informe, ply, log); 40.99 x 31.36 x 20.10 mm for 40 x 30 x 20; 25 min on CPU.
  QA told. Untested: Windows, a real phone scan, an NVIDIA card. (The httpx2 test note is solved: node/errors.py, 11-06.)
- 20:50: TismaScan-windows green on scanner/next (run 41903680499, d720958): bin\colmap.exe without the .bat,
  --check, synthetic box 42.43 x 32.49 x 20.62 mm, closed mesh, 16 min without NVIDIA. README step 3 now points at
  bin\colmap.exe. The scanner session also made the printed mat give the scale without --mat (contract unchanged).
  Left: openscanner PR #2 / scanner/next merge (owner), then OPENSCANNER_REF in scan-e2e.yml.
- 21:20: licence check of TismaScan-windows (QA). Green on scanner/next 830da96 (run 41910637103): BSD/AGPL texts
  checked, OpenMVS 2.4.0 source zip shipped. Open: the official COLMAP/OpenMVS zips bring no notice files (0 found),
  so the bundled DLLs (Qt LGPL, Boost, CGAL, OpenCV, CUDA runtime...) lack their licences; asked the scanner session
  to list the DLLs and add those texts. Until then the package is fine on the owner's own servers but not for third
  parties.
- 22:05: 1d07f56 (scanner/next) lists the 95 bundled DLLs (~40 libraries) with their licence texts and fails on an unknown DLL. GPL-2.0+ (SPQR) and LGPL (Qt 5, GMP, LibRaw, CHOLMOD) inside: documented (f40e68d) that the package is for the owner's own nodes; a source companion artifact is the precondition for third parties (owner decides). Run 41919077404 scan step was still running at 22:02.
- 23:50 owner's decision (via QA): the server and its node packages, TismaScan-windows included, run only on the owner's own machines and are never given to third parties; no source companion artifact. Written as the rule in THIRD_PARTY_LICENSES.md and README.
- 11-06 (owner's rule: no idle time; take the next item, deploy tests and docs while the real server waits):
  scan-e2e.yml follows openscanner scanner/next and runs weekly (946eef3); deploy/BETA.md, the beta checklist for the
  i5 (package, master, tunnel, plans, nodes, scan node, smoke test, secrets, before inviting); deploy/smoke.py, the
  check of a deployed master from internet (tested locally against a master and a fake node, both outcomes of the
  /admin check).
- Load with scans: deploy/loadtest.py --scans/--scan-mb/--prefix. 50 users x 40 MB scans in pieces + 200 slicing jobs, 8 nodes, SQLite, 4 cores: 250 done, 0 failed, 2 GB in 30 s (README «Medido con carga»); a scan run also in the CI load test on PostgreSQL.
- 11-06: bug found by the CI load test on PostgreSQL (runs of 21edd2a and f4c3498): two jobs of one user ending at once both inserted the month's usage row; one result got a 500 (UniqueViolation usage_user_id_month_key) and its job was left running. charge_time is now one INSERT ... ON CONFLICT DO UPDATE; test_jobs_of_one_user_ending_at_once_all_count fails 3/3 on PostgreSQL with the old code and passes with the new. Full suite: PostgreSQL 252 passed, SQLite 253 passed (local PostgreSQL 16).
- 11-06: CI green on 2835c57. Local PostgreSQL 16 load: 100 users / 500 jobs / 8 nodes 500 done, 30 users with 24 MB scans 90 done, no errors. node/errors.py: the node catches the errors of httpx and httpx2 (Starlette's test client prefers httpx2); the full suite now passes with httpx2 installed too (253 passed).
- 11-06: scan-e2e against openscanner scanner/next green (run 42159768933, 89347a1): 40.98 x 31.48 x 20.10 mm, 17.6 min on CPU. Run 42153388641 hung over an hour in apt (mirror), not code: the apt step is now bounded (2 x 10 min, step 25 min). Docs reviewed against the code: paths, commands and TISMA_* variables all exist; the stale docs/CLOUD_ARCHITECTURE.md reference removed (f6b6425). Owner-independent items left: none urgent; Thingiverse proxy (rule 22) waits for after the beta and the engine's confirmation.
- 11-06 QA list (no idle time): (1) CI step: packaged master computing with its own node + smoke.py with a real job, Windows and Linux (3aa8035); (3) deploy/restore_drill.py: backup, restore on an empty machine with data.key, refusal without it; 300 accounts: backup 0.35 s, restore 0.08 s, answers 1.3 s after start; in CI on Windows and Linux. Next: (2) own security review, (4) metrics and alerts in BETA.md, (5) guide for beta testers.
- 11-06 security review (QA item 2): found and fixed (a) password guessing from many addresses: login was limited only per minute (10/min per account = ~14,000 guesses a day); now at most 20 wrong passwords an hour per account (right ones never count; reset by code still works); (b) reset codes: at most 10 codes a day per address (50 guesses of 6 digits a day); (c) X-Content-Type-Options: nosniff on every answer. Checked, fine: codes (6 digits, 15 min, 5 attempts, hashed), node routes only for the node running the job, result downloads as attachments with fixed types, admin hidden outside LAN, sessions closed after 90 days unused, request bodies capped, scan zip paths.
- 11-06 QA items 4 and 5: BETA.md «Qué mirar durante la beta» (the four e-mail alerts with their settings and what to do, the panel, failed jobs, reports); deploy/BETA_TESTERS.md, a short guide for beta testers (account and code, Tisma and the app, how to report a problem, their data), checked against cuenta.html and the README data table. Not written by me: the exact menu names inside Tisma and the app beyond those in the README.
- 11-06 05:25: CI green for all five QA items. Windows Server 2022 (run 42165509821): the packaged master with its own node sliced bar_project.3mf through smoke.py (98 KB G-code, 10m 8s print, 3 s); restore drill: restore 2.95 s, answering 7.8 s after start. Told QA.
