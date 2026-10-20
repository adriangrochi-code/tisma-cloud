# Third-party components of the Tisma Cloud server

Licenses as declared in the package metadata (PyPI `License-Expression` / `License`) of the versions tested on
2026-10-20. All of them allow use in proprietary software. None of them is copied into this directory: they are
installed with pip.

| Component | Version tested | License | Used by |
|---|---|---|---|
| FastAPI | 0.151.0 | MIT | master |
| Starlette | 1.9.0 | BSD-3-Clause | master (through FastAPI) |
| Pydantic / pydantic-core | 2.14.1 / 2.50.1 | MIT | master |
| Uvicorn | 0.54.0 | BSD-3-Clause | master |
| SQLAlchemy | 2.1.5 | MIT | master |
| psycopg / psycopg-binary | 3.3.6 | LGPL-3.0-only | master (PostgreSQL driver) |
| python-multipart | 0.0.32 | Apache-2.0 | master (uploads) |
| httpx / httpcore | 0.28.1 / 1.0.9 | BSD-3-Clause | node, tests |
| PyJWT | 2.15.1 | MIT | master (Sign in with Google: ID token verification) |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | master (RS256 signatures, through PyJWT) |
| cffi / pycparser | 2.1.1 / 3.11 | MIT-0 / BSD-3-Clause | dependencies of cryptography |
| aiosmtpd | 1.4.6 | Apache-2.0 | tests only (an SMTP server to test the e-mails) |
| anyio, h11, annotated-types, typing-inspection | — | MIT | dependencies of the above |
| idna, click | — | BSD-3-Clause | dependencies of the above |
| typing-extensions | 4.16.0 | PSF-2.0 | dependency of the above |
| certifi | 2026.10.19 | MPL-2.0 | dependency of httpx (CA certificates) |
| pytest | 9.1.1 | MIT | tests only |

psycopg is LGPL-3.0: it is used unmodified as a separately installed library (pip), which the LGPL allows in
proprietary software; if it is ever modified, the modified source must be published.

Programs run as separate processes (not linked):

| Program | License | Notes |
|---|---|---|
| Tisma Slicer (`tisma-slicer`, `tisma-fea`) | AGPL-3.0 | The source of the version used by the service, with every change, must stay published (AGPL §13). |
| PostgreSQL 16 | PostgreSQL License | Database (Docker image `postgres:16`). |
| Caddy 2.8 | Apache-2.0 | HTTPS proxy (Docker image `caddy:2.8`). |
| Google Identity Services (`accounts.google.com/gsi/client`) | Google's terms | Loaded by the account page from Google only when `TISMA_GOOGLE_CLIENT_IDS` is set; not distributed with the server. |

This is a technical summary, not legal advice.
