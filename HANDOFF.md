# Server session: state

Branch `server/next` (from `ccr-7e2013d9-qwmclz`). Scope: `server/` only. Spec/QA contact: `session_01QadJfL7KViJEaJL92p8Swb`.

Tests: venv, `pip install -e 'server[master]' pytest aiosmtpd`, `python -m pytest -q server/tests` (213 passed, 2 skipped
at d891fc8; `aiosmtpd` is needed by the SMTP test and is not in the extras).

## Work list (in order)

1. Set up: done.
2. Server against the engine's `docs/spec/features/cloud.md`: **blocked**, this session has no access to
   `adriangrochi-code/tisma_engine` (adding the repo was refused). Waiting on the owner.
3. Cloudflare tunnel guide and Windows manager: d891fc8 (service ImagePath, cert.pem, ingress checks, log file,
   `config KEY --clear`; client IP header only from `TISMA_TRUSTED_PROXIES`). Not tested on Windows nor with a real
   cloudflared (downloading it was refused). Still to review: the Windows installer flow.
4. Robustness (node reconnection, job retries, queue under load, backups/restore, uploads security): not started.
