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
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | master (RS256 signatures through PyJWT; AES-256-GCM of the personal data in the database) |
| cffi / pycparser | 2.1.1 / 3.11 | MIT-0 / BSD-3-Clause | dependencies of cryptography |
| aiosmtpd | 1.4.6 | Apache-2.0 | tests only (an SMTP server to test the e-mails) |
| anyio, h11, annotated-types, typing-inspection | — | MIT | dependencies of the above |
| idna, click | — | BSD-3-Clause | dependencies of the above |
| typing-extensions | 4.16.0 | PSF-2.0 | dependency of the above |
| certifi | 2026.10.19 | MPL-2.0 | dependency of httpx (CA certificates) |
| pytest | 9.1.1 | MIT | tests only |
| pywebview | 6.2.1 | BSD-3-Clause | the window of the control panel in `tisma-cloud.exe` (Windows only) |
| pythonnet / clr_loader / bottle / proxy_tools | 3.2.1 / 0.3.1 / 0.13.4 / 0.1.0 | MIT | dependencies of pywebview on Windows |
| Microsoft WebView2 SDK (`Microsoft.Web.WebView2.Core.dll`, `.WinForms.dll`, `WebView2Loader.dll`, in pywebview) | — | Microsoft redistributable SDK terms | the Edge engine of Windows; the runtime is part of Windows 10/11 |
| PyInstaller | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception (allows building and distributing non-free programs) | builds the `tisma-cloud` executable (CI); not part of the service |
| pyinstaller-hooks-contrib / altgraph | 2026.9 / 0.17.5 | GPL-2.0-or-later or Apache-2.0 (hooks) / MIT | dependencies of PyInstaller |

psycopg is LGPL-3.0: it is used unmodified as a separately installed library (pip), which the LGPL allows in
proprietary software; if it is ever modified, the modified source must be published. The `tisma-cloud` executable
bundles it (one file): it is built for the machines of the service, not given to anyone. Before giving the executable
to third parties, build it as a folder (PyInstaller "onedir", the psycopg files replaceable) and include the LGPL text.

Programs run as separate processes (not linked):

| Program | License | Notes |
|---|---|---|
| Tisma Slicer (`tisma-slicer`, `tisma-fea`) | AGPL-3.0 | The source of the version used by the service, with every change, must stay published (AGPL §13). The package of `tisma-cloud` carries the unmodified programs of a build of `main` in `tisma-slicer/`, with the AGPL text (`LICENSE-AGPL-3.0.txt`) and the address of the exact source (`SOURCE.txt`, the public repository at that commit): two separate programs side by side ("aggregate", AGPL §5; GNU GPL FAQ "mere aggregation", https://www.gnu.org/licenses/gpl-faq.html#MereAggregation), the server runs them as processes and links nothing of them. |
| PostgreSQL 16 | PostgreSQL License | Database (Docker image `postgres:16`). |
| Caddy 2.8 | Apache-2.0 | HTTPS proxy (Docker image `caddy:2.8`). |
| ClamAV 1.5 (optional) | GPL-2.0 | Antivirus of the uploads (Docker image `clamav/clamav:1.5`, `--profile antivirus`); the master only talks to it over its TCP protocol (clamd INSTREAM), nothing is linked. Its signatures come from Cisco Talos under their own terms. |
| LibreTranslate 1.9.6 (optional) | AGPL-3.0 | Translator of the problem reports (Docker image `libretranslate/libretranslate:v1.9.6`, `--profile translate`), unmodified; the master only calls its HTTP API (`POST /translate`), nothing is linked. Its models come from the Argos Translate index: several are derived from OPUS-MT (CC-BY 4.0, credited in the README of each model) and include Stanza models (Apache-2.0); they are downloaded by the container, not distributed with the server. |
| Google Identity Services (`accounts.google.com/gsi/client`) | Google's terms | Loaded by the account page from Google only when `TISMA_GOOGLE_CLIENT_IDS` is set; not distributed with the server. |

This is a technical summary, not legal advice.
