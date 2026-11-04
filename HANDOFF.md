# Server session: state

Branch `server/next` (from `ccr-7e2013d9-qwmclz`). Scope: `server/` only. Spec/QA contact: `session_01QadJfL7KViJEaJL92p8Swb`.

Tests: venv, `pip install -e 'server[master]' pytest aiosmtpd`, `python -m pytest -q server/tests` (215 passed, 2 skipped
at the last commit; `aiosmtpd` is needed by the SMTP test and is not in the extras).

Dates (owner, relayed by QA 2026-11-04): the roadmap closes in the second week of January 2027, when a closed beta
starts on the owner's real server (tunnel, Windows service): it must be running and stable by then.

## Work list (in order)

1. Set up: done.
2. Server against the engine's `docs/spec/features/cloud.md`: **blocked**, this session has no access to
   `adriangrochi-code/tisma_engine` (adding the repo was refused). Waiting on the owner.
3. Cloudflare tunnel guide and Windows manager: d891fc8 (service ImagePath, cert.pem, ingress checks, log file,
   `config KEY --clear`; client IP header only from `TISMA_TRUSTED_PROXIES`). Not tested on Windows nor with a real
   cloudflared (downloading it was refused). Still to review: the Windows installer flow.
4. Robustness: a node that restarts gets its lost jobs requeued from its heartbeat (orphans, 60 s grace). Next: queue
   under load, backups/restore, uploads security.

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
- 9711f1b: QA saw a requeued aero never taken again, and a stale job kept as running on qa1. Not reproduced here
  (scratchpad e2e2/run.sh: requeued at ~66 s, taken again, attempts 2). Suspected: two agents with the same token.
  Asked QA for processes, job rows and node logs.
