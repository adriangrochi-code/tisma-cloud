"""The signed list of versions of Tisma, served as a second address (app_updates.md of tisma_engine, rule 13).

The list is updates.json plus updates.json.sig (64 bytes, Ed25519 over the exact bytes of the JSON), files of the fixed
`updates` release of the engine on GitHub. The master downloads both every hour and serves them byte for byte at
GET /v1/updates and GET /v1/updates.sig, without a session. It never parses and writes the JSON again: the signature is
over its exact bytes. It keeps only a pair that looks whole (a JSON object, a signature of 64 bytes), so a cut download
never replaces a good one; and, when it knows the project's public key (TISMA_LIBRARY_PROJECT_KEY, set when the library
is certified), only a pair whose signature is good: the two files are two downloads, and a list published between them
would pair the old list with the new signature (asked again once; otherwise the previous pair stays)."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger("tisma_cloud")

SIGNATURE_BYTES = 64
MAX_LIST_BYTES = 1 << 20
CHECK_S = 3600.

Fetch = Callable[[str], bytes]


def fetch_url(url: str) -> bytes:
    """GET with redirects (GitHub sends release files from another host), at most MAX_LIST_BYTES."""
    with httpx.Client(follow_redirects=True, timeout=30., headers={"User-Agent": f"TismaCloud/{__version__}"}) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_LIST_BYTES:
                    raise ValueError(f"{url} is larger than {MAX_LIST_BYTES} bytes")
            return bytes(data)


class UpdateMirror:
    def __init__(self, url: str, folder: Path, fetch: Fetch = fetch_url,
                 project_key: str | Callable[[], str] = "") -> None:
        self.url = url.strip()
        # A text, or a function read again at each check (the key may be set while the master runs).
        self._project_key = project_key
        self.folder = Path(folder)
        self.fetch = fetch
        self._lock = threading.Lock()
        self._pair: tuple[bytes, bytes] | None = None
        self.error = ""
        self._load()

    @property
    def needed(self) -> bool:
        return bool(self.url)

    def _paths(self) -> tuple[Path, Path]:
        return self.folder / "updates.json", self.folder / "updates.json.sig"

    def _load(self) -> None:
        listing, signature = self._paths()
        try:
            pair = listing.read_bytes(), signature.read_bytes()
        except OSError:
            return
        if self._whole(*pair):
            self._pair = pair

    @property
    def project_key(self) -> bytes | None:
        text = self._project_key() if callable(self._project_key) else self._project_key
        return self._key(text or "")

    @staticmethod
    def _key(text: str) -> bytes | None:
        from .library import LibraryError, project_key_bytes

        try:
            return project_key_bytes(text) if text.strip() else None
        except LibraryError:
            log.warning("TISMA_LIBRARY_PROJECT_KEY is not a key: the list of versions is kept without checking it")
            return None

    def _whole(self, listing: bytes, signature: bytes) -> bool:
        if len(signature) != SIGNATURE_BYTES or not listing or len(listing) > MAX_LIST_BYTES:
            return False
        try:
            if not isinstance(json.loads(listing), dict):
                return False
        except ValueError:
            return False
        return self._signed(listing, signature)

    def _signed(self, listing: bytes, signature: bytes) -> bool:
        key = self.project_key
        if key is None:
            return True
        from .library import verify

        return verify(key, signature, listing)

    def current(self) -> tuple[bytes, bytes] | None:
        """The list and its signature, always from the same download."""
        with self._lock:
            return self._pair

    def _failed(self, error: str) -> None:
        # Once per kind of failure, not every hour (before the release exists, GitHub answers 404 for days).
        if error != self.error:
            log.warning("%s", error)
        self.error = error

    def refresh(self) -> bool:
        """Downloads the list and its signature; keeps them when they look whole. Returns whether they changed."""
        if not self.url:
            return False
        for attempt in range(2):
            try:
                listing = self.fetch(self.url)
                signature = self.fetch(self.url + ".sig")
            except (httpx.HTTPError, OSError, ValueError) as ex:
                self._failed(f"The list of versions could not be downloaded: {ex}")
                return False
            if self._whole(listing, signature):
                break
            if attempt == 0 and self.project_key is not None:
                continue            # published between the two downloads: once more
            what = "is not signed by the project's key (or its signature is of another list)" \
                if self.project_key is not None else "is not whole"
            self._failed(f"The list of versions or its signature {what} ({len(listing)} and {len(signature)} bytes): "
                         f"the previous one is kept")
            return False
        self.error = ""
        with self._lock:
            if self._pair == (listing, signature):
                return False
            self.folder.mkdir(parents=True, exist_ok=True)
            for path, data in zip(self._paths(), (listing, signature)):
                tmp = path.with_name(path.name + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, path)
            self._pair = (listing, signature)
        log.info("List of versions updated (%d bytes)", len(listing))
        return True
