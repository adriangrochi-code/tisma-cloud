# Server session: state

Branch `server/next` (from `ccr-7e2013d9-qwmclz`). Scope: `server/` only. Spec/QA contact: `session_01QadJfL7KViJEaJL92p8Swb`.

Tests: venv, `pip install -e 'server[master]' pytest aiosmtpd`, `python -m pytest -q server/tests` (249 passed, 2 skipped
at the last commit; `aiosmtpd` is needed by the SMTP test and is not in the extras).

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
   other request body is capped (TISMA_MAX_BODY_KB, 1 MB). Next: queue under load, Windows installer review.

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
