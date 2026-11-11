# Server session: state

Repository `adriangrochi-code/tisma-cloud` (private, owner's decision 2026-11-04), branch `main`; it was `server/` of the
public slicer repository, moved here with its history (`git subtree split`). Spec/QA contact:
`session_01QadJfL7KViJEaJL92p8Swb`.

Tests: venv, `pip install -e '.[master,test]'`, `python -m pytest -q` (265 passed, 3 skipped
at caeb35e).

Dates (owner, relayed by QA 2026-11-04, corrected): closed beta in December 2026 on the owner's real server (tunnel,
Windows service), which must be up and stable for it; 1.0 in the second week of January 2027.

## Work list (in order)

0. Owner's rule (10-11, via QA): every 3 h, part of the block goes to finding bugs and optimising the server (errors,
   security, load, slow queries, memory, Spanish texts); report to QA in a few lines.

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

## Bug hunt (owner, 2026-11-09: «Continúa buscando errores»)

Fixed and told to QA: fd2fa78 (panel class names), 94ed2aa (cuenta.html re-login, plan contents, payment form),
c0f00d4 (/docs local-only), d8be574 (mail check), 7c0046e (master_url without scheme), 90b5af8 (a node's own
failure requeues the job; < 2 GB free pauses a node), caeb35e (a full master disk is 507, told the Scanner session).
CI: red since 11-09 because the slicer has no green build of main/dev since 10-23 and its artifacts expire (14 days;
the last ones, dev run 40347874688, on 11-09 ~16 UTC). 99dc53e keeps a copy in the release `tisma-slicer-cache` of
this repository and uses it when no artifact is left. Since b943920 (11-11) the package carries no Tisma Slicer
and the CI no longer downloads it; the release `tisma-slicer-cache` is left as it is (the owner decides about it). The window's Nodos actions: errors in Spanish, toggle guarded (next commit).

## Coverage of the review (owner, 2026-11-10 21:00: everything reviewed by 2026-11-12 03:10 UTC; summary every 3 h)

| Area | Routes / parts | State |
|---|---|---|
| Library: index, blocklist, signing, key and certificate | /v1/library/* (15), library.py | revisado 11-10: signed lists vs spec, cache invalidation (disabled authors), gzip, certificate restore cases, revoked keys, control/bidi chars, races of revisions, fuzz; engine test vectors |
| Library: peers, relay tickets, announcements | /v1/library/peers*, relay-ticket, peers.py | revisado 11-10: own-address only (reflection), blocked IPs, rate limit, memory only. Relay node and seeder: not built (wait for the engine's libp2p spike) |
| Library review in the panel | /v1/admin/library/* (8), admin.html | revisado 11-10: browser test (reports, remove, blocks incl. model and IP, relay cap) |
| Account files and scans to «Mi biblioteca» | /v1/files (5), /v1/me/scans | revisado 11-10: race of «Imprimir» (unique), quota, 410/409/404, deletion with account, per-device taken |
| 3D scans upload | /v1/scans (5), scans.py | revisado 11-06/11-10: pieces, 507 when full, zip limits, e2e in CI; 11-11 pieces after a power cut or a lost file: counted from the disk, fsync before counting (this commit) |
| Jobs and nodes | /v1/jobs (5), /v1/node (8), scheduler, agent | revisado 11-09/11-10: result sent twice (lock), node_error requeue, cancel vs node failure, full disk (507, node pause), DB restart, parallel claims; 11-11 heartbeat of a node with more than 64 jobs (this commit) |
| Auth and accounts | /v1/auth (8), /v1/me (13) | revisado 11-06/11-09/11-10: login limits, codes per day, password by code closes all sessions; FOUND pre-account hijacking through Google sign-in, fixed cadd7ca; 11-11 delete-account and export: half-sent scans, admin hour notes and reports left (a423603); export missing App Store/Microsoft, library, files, scans, hours (this commit) |
| Billing and plans | /v1/billing (6), /v1/plans, /v1/site (3), /v1/admin/plans, payments | revisado 11-10: provider notices (amount/currency check, idempotent), FOUND one payment notified at once paying 3-4 months, fixed 7b7cbe2; two payments at once both count; Google Play revisado 11-10: the app and Google's notification of one purchase at once gave 500 (unique token), fixed; access is Google's expiry (not added up), refunds, upgrades covered by tests |
| Remote printers | /v1/bridges (5), /v1/remote (6) | revisado 11-10: token cache on disable; FOUND a print command handed to two waits of one bridge (printed twice), fixed; result sent twice answers 404 |
| Problem reports | /v1/reports, /v1/admin/reports (6), report-groups (2) | revisado 11-11: reward race (3af49d4, PostgreSQL), redaction of accented e-mails and Windows folders with spaces (c2e3149). Reports kept before c2e3149 are not redacted again |
| Admin panel: users, payments, capacity, queue, jobs, alerts, nodes | /v1/admin/* | revisado 11-09 (names, payments form, browser console clean); 11-11 users/payments: «Cuenta interna…» for a new address (f37ff86), jobs of a disabled account kept running (69c1b29). Open question to the owner: a report reward while a store subscription lasts adds nothing |
| Updates mirror | /v1/updates, .sig | revisado 11-06 (503 until the engine's release exists) |
| Guard, limits, headers | guard.py, ratelimit.py | revisado 11-09/11-10: nosniff, 411, docs local-only, daily limiter memory |
| Mail and alerts | mail.py, alerts.py | revisado 11-10: Spanish texts, panel link in alerts |
| Backup / restore | backup.py, restore_drill.py | revisado 11-06/11-10: drill in CI, Spanish errors, library.key/cert restore cases |
| Upgrade | db.py, upgrade_drill.py | revisado 11-10 (SQLite and PostgreSQL locally, CI) |
| Windows service and window | manager.py, gui.py, system.py | revisado 11-09/11-10: Nodos tab, Spanish errors, diagnosis checks; CI on Windows Server 2022 |
| Pages | /cuenta, /admin, /docs, /openapi.json | revisado 11-09 (browser, docs local-only) |

## Done for QA

- `admin plan-set --classes a,b|all --public/--no-public` (QA item 2: the engine's plan lock reads `classes` from
  `GET /v1/plans`; CLI plans were not public, so they were not listed).

## Open from QA (2026-11-04)

- Content-Length on uploads (411): closed 11-09, the text «Content-Length is required» is the one cloud.md (spec/next, line 67) gives.
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
- 11-06 QA item 1 (engine by default): node routes by project kind (runners.project_kind): new Tisma project -> engine only, Tisma Slicer project -> Tisma Slicer only (each program ignores the other's settings: the engine reads only the parts of a Slic3r_PE project, engine formats.md rule 18 and src/io/threemf.cpp), bare model -> engine with Tisma Slicer fallback on codes 1,2,3,6 or a crash. Before this, a node without the engine sliced a new-Tisma project with Tisma Slicer defaults silently. The package now carries the engine nightly in tisma-engine/ (public repo, gh release download nightly); real test test_real_engine_slices_the_new_tisma_projects (box bare/project, machine.macros start/end, bambu.plate) in the package CI. Pending: items 2 (profile pack cache) and 3 (update test).
- 11-06 QA item 3: /v1/updates is the signed list of the Tisma apps' versions, not a server updater; built deploy/upgrade_drill.py instead: the 10-24 version (a7a9805, first with service commands; 3 new columns and 3 new tables since) with accounts, jobs, a node and node.toml, then the current one on the same folder. SQLite and PostgreSQL pass locally (answers 1.3 / 1.6 s after start); CI tests job runs both. README «Actualizar Tisma Cloud», BETA.md step 9. QA item 2 (profile pack cache) not built: nodes never read the pack (engine cloud.md rule 18; machine.macros copied into the project), told QA.
- 11-06 06:45: real engine nightly in CI (Windows and Linux): a new-Tisma project with settings sliced by the engine; a bare model got code 2 (the engine slices only Tisma projects) and Tisma Slicer sliced it through the fallback; machine.macros and the bambu group are unknown to this nightly (they reached engine dev today 01:42 and 02:10 UTC, after it): reported by the test, checked in full once a nightly has them. Upgrade drill green in CI (SQLite and PostgreSQL).
- 11-09 bug hunt (owner: «continúa buscando errores»): browser pass over /admin and /cuenta and a fuzz of the 81 API routes (no 5xx, no traceback). Fixed: fd2fa78 panel names of scan/aero/orient; 94ed2aa login again after leaving a new account (form stayed on «Crear cuenta»), plans say what they include, subtitle, panel payment input warns, payment states in Spanish; this commit: /docs and /openapi.json were public from internet (listing admin and node routes), now local-network only like /admin. Noted, not a bug: /v1/updates 503 until the engine publishes its updates release.
- 11-09: the engine nightly is 70f9335 (has machine.macros and bambu.plate). CI run 42628984969 (dcd7ee2), Windows and Linux: the real engine sliced the projects with machine.macros (start/end G-code in the result) and bambu.plate, nothing skipped; the test is now strict (a setting the nightly does not know fails it). 507 for a full master disk (caeb35e; the Scanner app handles it). Artifact quota full (9.6 GB of old tisma-cloud packages): retention 7 days (dcd7ee2); deleting the old ones was refused in this session, asked the owner.
- 11-09 22:40 (QA: no commits for hours): Windows install flow reviewed in code (window: role, service, autostart task, firewall, first admin, panel, Nodos). Fixed: owner-facing English errors -> Spanish (5c4a778 role/service/port/admin/schtasks/firewall; 973ded6 backup causes in the alert and console). Not run on a real Windows desktop: CI covers the packaged service, task and firewall on Windows Server 2022.
- 11-10 (owner via QA, «lo antes posible así subimos modelos»): p2p library server side (library.py, p2p_library.md of the engine spec/next 2d9d539). Signed index and blocklist (Ed25519, library.key on the master, certified once by the owner with the project key: admin library-key / library-cert), publish metadata only, revisions, author removal, reports, panel review routes (remove -> files blocked; account/peer/address blocks, addresses 24 h and deleted when they end). Format proposal sent to QA. Not built yet: panel UI for the review, the optional seeder node (needs the engine's peer library). Tests: 274 SQLite; library+api on PostgreSQL.
- 11-10: owner's decisions applied (75762be): no G-code, roles project/profile/analysis/photo, checked, 503 without certificate (TISMA_LIBRARY_UNCERTIFIED to try), revoked keys, addresses hashed with a daily salt; deploy/certify_library_key.py (openssl, same PEM as the engine's sign_release.py, which this session cannot change). Account files for scans (7 bis): POST/GET/PATCH/DELETE /v1/files, publish with from_file; contract sent to the Scanner session. Open: relay node with bandwidth caps (8 ter/quater) and the seeder: they wait for the engine's choice of peer library and protocol.
- 11-10: engine chose libp2p (Noise, circuit relay v2, request-response /tisma/library/1.0.0, 1 MiB pieces). Master side built (peers.py): PeerId from the peer key, relay tickets signed by the library key, announcements in memory only (30 min), peer lookup with direct addresses only between two «Directo» apps. swarm = {protocol, piece_size, piece_roots} (proposed to the engine instead of full piece lists). Not built: the relay node itself (go-libp2p) and the seeder; waiting for the engine's test peer. PeerId derivation checked only for its 12D3KooW prefix, not against a real libp2p yet.
- 11-10 bug hunt (owner via engine session: wrong answers, auth/signature edges, races, slow routes, leaks; one commit per fix with a test): cade484 disabled author kept in the cached index; e1f3017 «Imprimir» race made 7 copies of a scan (unique constraint); 332d9d7 concurrent revisions gave 500 (retry); 1eaa28e index of 5000 models 4.5 MB now gzip (built 0.24 s, cached 7 ms); 9c15393 removed/deleted thumbnails still served. Fuzz of the library/files routes with odd values: no 5xx. 290 tests.
- 11-10: the API's "detail" texts stay in English; the Scanner app translates the ones a person sees by status + start of the text (CloudClient.HttpError.spanish, scanner/next). Any change to a person-facing detail of auth, /v1/scans, /v1/files or /v1/library publish: send the Scanner session the exact text.
- 11-10 21:25 (owner, spec/next cada191, cloud.md 5b): plans sold through the official stores. App Store added (apple.py, Apple's app-store-server-library MIT, Apple Root CA - G3 in tisma_cloud/data, fingerprint 63:34:3A:BF…:91:79 taken from the file downloaded from apple.com; Apple does not print it on its page, not cross-checked with a second source). Routes /v1/billing/apple/{account,purchase,notify}; access = expiresDate unless revoked, older transactions do not undo newer; the account's access is the longest of all its stores (stores.py). Web payment off unless TISMA_WEB_PAYMENTS=on. Microsoft Store: later.
- 11-10 21:50 (owner, spec/next 77e6a3f): Windows sells the plan through the Microsoft Store (in-app subscription add-on). To do after the pending review items: verify Store purchases with the Microsoft Store collections/purchase API (the app's Azure AD identity) and renewals/cancellations; same «longest access wins» (stores.py). Linux: account plan only.
- 11-10 23:30 (owner via QA, 22:55): one-click library certification. Diagnóstico «Certificar con mi pendrive» (also «Arreglar» on «Biblioteca entre usuarios») and `tisma-cloud admin library-certify --key PATH [--replace-project-key]`: Manager.certify_library -> library.certify. The PEM is read into a bytearray (library.read_secret, 64 KB cap) and zeroed after (wipe); no temp file, no log, no network; the file on the USB is left as it is. Sets TISMA_LIBRARY_PROJECT_KEY when unset; a key whose public half differs is refused (ProjectKeyMismatch) unless the owner confirms the replacement (GUI yes/no, CLI flag). A new library key is certified again the same way. The 3-step path still works and gives the same certificate (Ed25519 is deterministic: tested against deploy/certify_library_key.py). Limits said plainly: OpenSSL inside cryptography keeps its own copy of the key until the object is freed (not zeroable from Python), and a typed password is a Python str. GUI checked under Xvfb (file picker mocked): certified, check turns «Bien». Not checked in the GUI: the password and replace dialogs (covered by the manager tests). Tests: tests/test_library_certify.py.
- 11-10 23:10 (owner via QA, urgent): «Cuenta interna…» of the window ran admin user-set (existing accounts only) and failed silently for a new address: f37ff86 (Manager.make_internal). «Abrir el panel web»: not reproduced on Linux; the child's errors were lost in a closing console, now shown and the panel falls back to the browser: 7a8e0dd. Exact Windows message still unknown.
- 11-11 01:30 (owner via QA: «distribuí todo y seguí así»): Microsoft Store subscriptions (microsoft.py), from Microsoft's docs (view-and-grant-products-from-a-service, get-subscriptions-for-a-user, renew-a-windows-store-id-key, read 11-11): Entra ID client credentials, GET /v1/billing/microsoft/ticket (purchase-key service ticket + publisherUserId), POST /v1/billing/microsoft/purchase (key kept encrypted, aud/clientId/userId checked, recurrences/query with pages), daily refresh with key renewal 7 days before its end, Active/InDunning until expirationTimeWithGrace. Microsoft sends no notifications. Tested against responses shaped as documented, not against the real Store. Open for the owner: subscription add-ons need a Partner Center account Microsoft enabled for them; the renewal page asks a token for «https://onestore.microsoft.com/.default» (v2 wording) and we use the v1 resource https://onestore.microsoft.com: to confirm with the real Store. Next: engine nodes black-box (rules 18-21), bridge (12-17) and library blocking, against the spec.
- 11-11 02:00: parts 2 and 3 of the owner's list checked against cloud.md (spec/next 3f1decf): engine nodes (rules 18-21) already follow the contract (fake-engine tests of every command and argument; real nightly slicing in the package CI). New: test_real_engine_keeps_the_contract_of_its_other_commands (orient, aero, analyze of the real nightly: result + summary keys, or a stable code of rule 19a) in its own non-blocking CI step (964c4d0); not run here (the nightly download needs GitHub API access this session was refused). Bridge rules 12-17 (id, 32 printers, fields, actions, camera only while watched, 10 bridges, plan limit) and library blocks are covered by tests/test_remote.py and tests/test_library.py.
- 11-11 17:16 (owner via QA): «Deja los bug por ahora y sigamos con la hoja de ruta»: the hourly bug hunt ends. 11-11 fixes after the last log line: 4d87fd4 (5c purchases), ac6c226 (plan of the longest access), da5b210 (purchase_account_token), 8d0c56a (scan pieces after a power cut), a423603/b9fd077 (delete account, export), d21f9e9 (>64 jobs heartbeat), 82c4123/08125d7/875c60d (sweep steps apart), 27a9db1 (--paid-until local day), 1b6ee6e (hidden characters by Unicode class), 9b02f4c (printer id twice), 513dc0f/82df8cf (updates list signature), 1288158/9b6cd5f (IPv6 by /64), e81edb5 (library reports retention), d95b5a6 (Microsoft key only while entitled), 4ac870c (store ids race), 7b6857b (quadratic e-mail redaction: CPU for minutes from an anonymous report), 9022e06 (report size limit). Open questions to the owner: old reports cleanup, report reward from the store end, usage month in UTC, a data.key rotation command. Next: the roadmap part QA names (asked which document: «P2 redes y límites» is not in PLAN.md, whose P2 is the engine's devices).
- 11-11 17:30: QA named my roadmap part: PLAN.md «Tisma Cloud en la 1.0» (1 engine nodes, 2 bridge, 3 model library + Thingiverse, 4 purchases 5c). 1, 2 and 4 done and tested (real engine and Tisma Slicer in the package CI). 3 blocked and asked: Thingiverse waits for the engine's real-token confirmation (thingiverse.md rule 20, nothing in tisma_engine yet); model_library.md (server stores models and G-code, free slicing 10 %) conflicts with p2p_library.md (owner 11-10: the master keeps only the index); asked whether to do rule 16 (G-code store by hash, gzip then xz, recipe, cap) for the cloud's own results now.
- 11-11 20:45 (QA: «no hace falta esperar al motor»): tisma-relay in relay/ (Go 1.26, go-libp2p 0.51.0): relay (8 ter/quater/sexies: ticket protocol, ACL, per-account hourly cap from the bandwidth counter, signed blocklist) and `seed` mode (3, 3 octies, 8 septies: have/pieces/get, piece_roots and piece checks, safe-file checks, ClamAV as a program, POST /v1/library/models/<id>/check, direct announce). Tested: Go tests with real libp2p peers (P2P2, P2P8-P2P11), a ticket made by peers.ticket (Python) read by Go, a Go announcement verified by the master's code, the relay against a running certified master. Not tested against the apps (the engine has no libp2p yet), nor with a real ClamAV (a fake clamscan). CI job «relay». relay/relay (35 MB binary) was committed by mistake in df10259 and removed in 15ed45b: it stays in the history unless the owner wants a rewrite. Thingiverse: spec dropped by the owner (spec/next 1fa01424).
- 11-11 21:05 (owner via QA: Tisma Slicer left, the engine is the only slicer): b943920 `refactor:` nodes compute only with tisma-engine: no routing by project kind, no fallback, needs_engine for a node without it (scan-only nodes stay); tisma_slicer/tisma_fea of an old node.toml read and unused; auto slots one per core and 1 GB; manager/window/system look only in tisma-engine/ and tisma-scan/ («Buscar programas»); package without tisma-slicer/, smoke test with tests/data/box_project.3mf (a new-Tisma project), bare-model slice of the nightly reported in the non-blocking step (it gave code 2 on 11-06: if it still does, bare models fail now); upgrade drill sets tisma_engine after the update (local SQLite run green). 357 tests SQLite, node/manager/pairing/scans/system on PostgreSQL, window checked under Xvfb. Not touched: the remote-printer bridge (remote.py) still says «Tisma Slicer left open». 2f0bc2e: relay job installs ClamAV + freshclam (EICAR .ndb stand-in with a warning if the download fails) and runs the EICAR/3MF/seeder tests with the real clamscan (locally only with the stand-in signature: this sandbox gets 403 from database.clamav.net).
