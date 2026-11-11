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
| app-store-server-library (Apple) | 3.1.2 | MIT | master (App Store purchases and notifications v2: checks what Apple signed; added 2026-11-10) |
| pyOpenSSL | 26.4.0 | Apache-2.0 | dependency of app-store-server-library (certificate chain) |
| asn1 / cattrs / attrs | 3.2.0 / 26.2.1 / 26.1.0 | MIT | dependencies of app-store-server-library |
| requests | 2.34.2 | Apache-2.0 | dependency of app-store-server-library (Apple's OCSP) |
| Apple Root CA - G3 (`tisma_cloud/data/AppleRootCA-G3.cer`) | — | a public certificate of Apple (apple.com/certificateauthority) | the root that App Store data is checked against |
| anyio, h11, annotated-types, typing-inspection | — | MIT | dependencies of the above |
| idna, click | — | BSD-3-Clause | dependencies of the above |
| typing-extensions | 4.16.0 | PSF-2.0 | dependency of the above |
| certifi | 2026.10.19 | MPL-2.0 | dependency of httpx (CA certificates) |
| pytest | 9.1.1 | MIT | tests only |
| pywebview | 6.2.1 | BSD-3-Clause | the window of the control panel in `tisma-cloud.exe` (Windows only) |
| pythonnet / clr_loader / bottle / proxy_tools | 3.2.1 / 0.3.1 / 0.13.4 / 0.1.0 | MIT | dependencies of pywebview on Windows |
| Microsoft WebView2 SDK (`Microsoft.Web.WebView2.Core.dll`, `.WinForms.dll`, `WebView2Loader.dll`, in pywebview) | — | Microsoft redistributable SDK terms | the Edge engine of Windows; the runtime is part of Windows 10/11 |
| go-libp2p | 0.51.0 | MIT OR Apache-2.0 | `relay/` (tisma-relay: circuit relay v2, Noise, the ticket protocol; added 2026-11-11) |
| go-yamux | 5.1.0 | MPL-2.0 | dependency of go-libp2p (stream multiplexing); used unchanged; tisma-relay runs only on the owner's machines |
| other Go modules of `relay/go.sum` (go-multiaddr, go-libp2p-asn-util, quic-go, webtransport-go, pion, protobuf, golang.org/x/*, flynn/noise, goupnp, ...) | as in `relay/go.sum` | MIT, Apache-2.0, BSD-2/3-Clause, ISC (checked one by one on 2026-11-11: none GPL) | dependencies of go-libp2p |
| Go | 1.26.0 | BSD-3-Clause | builds `relay/` |
| PyInstaller | 6.22.3 | GPL-2.0-or-later with the PyInstaller bootloader exception (allows building and distributing non-free programs) | builds the `tisma-cloud` executable (CI); not part of the service |
| pyinstaller-hooks-contrib / altgraph | 2026.9 / 0.17.5 | GPL-2.0-or-later or Apache-2.0 (hooks) / MIT | dependencies of PyInstaller |

psycopg is LGPL-3.0: it is used unmodified as a separately installed library (pip), which the LGPL allows in
proprietary software; if it is ever modified, the modified source must be published. The `tisma-cloud` executable
bundles it (one file): it is built for the machines of the service, not given to anyone. Before giving the executable
to third parties, build it as a folder (PyInstaller "onedir", the psycopg files replaceable) and include the LGPL text.

Programs run as separate processes (not linked):

| Program | License | Notes |
|---|---|---|
| tisma-engine (`tisma-engine`) | PolyForm Noncommercial 1.0.0, commercial use reserved to its owner | The only program the nodes slice and analyse with since 2026-11-11, when the package stopped carrying Tisma Slicer (public repository `adriangrochi-code/tisma_engine`). Its owner is the owner of Tisma Cloud, who uses it in this service. The package carries its unmodified nightly package in `tisma-engine/`, with its `LICENSE`, its `THIRD_PARTY.md` and, in `SOURCE.txt`, the nightly's name and notes (its commit). The node runs it as a process (`tisma-engine slice|analyze|...`, its `cloud.md` rules 18 to 21). The package runs only on the owner's machines. |
| PostgreSQL 16 | PostgreSQL License | Database (Docker image `postgres:16`). |
| Caddy 2.8 | Apache-2.0 | HTTPS proxy (Docker image `caddy:2.8`). |
| ClamAV 1.5 (optional) | GPL-2.0 | Antivirus of the uploads (Docker image `clamav/clamav:1.5`, `--profile antivirus`); the master only talks to it over its TCP protocol (clamd INSTREAM), nothing is linked. Its signatures come from Cisco Talos under their own terms. |
| LibreTranslate 1.9.6 (optional) | AGPL-3.0 | Translator of the problem reports (Docker image `libretranslate/libretranslate:v1.9.6`, `--profile translate`), unmodified; the master only calls its HTTP API (`POST /translate`), nothing is linked. Its models come from the Argos Translate index: several are derived from OPUS-MT (CC-BY 4.0, credited in the README of each model) and include Stanza models (Apache-2.0); they are downloaded by the container, not distributed with the server. |
| Google Identity Services (`accounts.google.com/gsi/client`) | Google's terms | Loaded by the account page from Google only when `TISMA_GOOGLE_CLIENT_IDS` is set; not distributed with the server. |

This is a technical summary, not legal advice.

## Programs a scan node runs (not part of Tisma Cloud, installed next to it)

Tisma Cloud runs tisma_scan as a separate program (subprocess, node/runners.py), and tisma_scan runs COLMAP and
OpenMVS the same way; nothing links them. Tisma Cloud contains no code of them. On Windows they come in the folder
`tisma-scan` (openscanner's TismaScan-windows package), with their licenses in `tisma-scan/licenses` (README,
«Nodo de escaneo en Windows»):

- **COLMAP** 3.9.1 (https://github.com/colmap/colmap): BSD-3-Clause; its official Windows release, unmodified.
- **OpenMVS** 2.4.0 (https://github.com/cdcseacave/openMVS): AGPL-3.0; its official Windows release, unmodified, used
  as a separate program. Its corresponding source is that release's tag, https://github.com/cdcseacave/openMVS/tree/v2.4.0
  (named in `tisma-scan/LEEME.txt`).
- The libraries inside those two releases (about 40, listed with their licence in `tisma-scan/licenses/BUNDLED_DLLS.txt`
  and their texts in `tisma-scan/licenses/third-party/`). Some are GPL-2.0+ (SuiteSparse SPQR) or LGPL (Qt 5, GMP,
  LibRaw, CHOLMOD): giving the package to a third party means also offering their source code. **The package runs only
  on the owner's own machines and is never given to third parties** (owner's decision, 2026-11-04), so no source
  offer is made. Changing that rule would first need a companion artifact with the exact sources of those libraries
  and a written offer in `LEEME.txt`.
- **tisma_scan** (the owner's `openscanner` repository), with numpy (BSD-3-Clause), OpenCV (Apache-2.0) and Open3D
  (MIT) inside.
