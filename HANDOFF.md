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
  QA told. Untested: Windows, a real phone scan, an NVIDIA card. Test note: where httpx2 is installed, starlette's
  TestClient uses it and test_a_removed_node_forgets_its_token_and_asks_again fails (the agent catches httpx's
  errors); CI has no httpx2 and passes.
- 20:50: TismaScan-windows green on scanner/next (run 41903680499, d720958): bin\colmap.exe without the .bat,
  --check, synthetic box 42.43 x 32.49 x 20.62 mm, closed mesh, 16 min without NVIDIA. README step 3 now points at
  bin\colmap.exe. The scanner session also made the printed mat give the scale without --mat (contract unchanged).
  Left: openscanner PR #2 / scanner/next merge (owner), then OPENSCANNER_REF in scan-e2e.yml.
