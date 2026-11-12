"""The signed list of versions served again byte for byte (app_updates.md of tisma_engine, rule 13)."""

from __future__ import annotations

import dataclasses

import httpx
from fastapi.testclient import TestClient

from conftest import new_app
from tisma_cloud.updates import UpdateMirror

URL = "https://example.test/updates.json"
# Spaces and key order as the publisher wrote them: the signature is over these exact bytes.
LISTING = b'{ "format": "tisma-updates",  "versions": [ {"version": "1.0.1"} ] }\n'
SIGNATURE = bytes(range(64))


def files(listing: bytes = LISTING, signature: bytes = SIGNATURE):
    def fetch(url: str) -> bytes:
        return signature if url.endswith(".sig") else listing
    return fetch


def test_served_byte_for_byte_without_a_session(settings):
    with TestClient(new_app(dataclasses.replace(settings, updates_url=""))) as c:
        assert c.get("/v1/updates").status_code == 503
        services = c.app.state.services
        services.updates = UpdateMirror(URL, settings.storage_dir / "updates", files())
        assert services.updates.refresh()
        r = c.get("/v1/updates")
        assert r.status_code == 200 and r.content == LISTING and r.headers["content-type"] == "application/json"
        sig = c.get("/v1/updates.sig")
        assert sig.status_code == 200 and sig.content == SIGNATURE


def test_a_bad_download_keeps_the_good_pair(tmp_path):
    folder = tmp_path / "updates"
    good = UpdateMirror(URL, folder, files())
    assert good.refresh() and not good.refresh()  # the same pair again: nothing changes
    for fetch in (files(signature=b"short"), files(listing=b"not json"), files(listing=b"[1, 2]")):
        bad = UpdateMirror(URL, folder, fetch)
        assert not bad.refresh() and bad.error and bad.current() == (LISTING, SIGNATURE)

    def down(url: str) -> bytes:
        raise httpx.ConnectError("no network")

    offline = UpdateMirror(URL, folder, down)
    assert not offline.refresh() and "no network" in offline.error
    # After a restart without network, the last good pair is served.
    assert offline.current() == (LISTING, SIGNATURE)
    assert not (folder / "updates.json.part").exists()


def test_new_list_replaces_both(tmp_path):
    mirror = UpdateMirror(URL, tmp_path, files())
    mirror.refresh()
    newer = LISTING.replace(b"1.0.1", b"1.0.2")
    mirror.fetch = files(newer, bytes(64))
    assert mirror.refresh() and mirror.current() == (newer, bytes(64))
    assert (tmp_path / "updates.json").read_bytes() == newer and (tmp_path / "updates.json.sig").read_bytes() == bytes(64)


def test_off_without_an_address(tmp_path):
    mirror = UpdateMirror("", tmp_path, files())
    assert not mirror.needed and not mirror.refresh() and mirror.current() is None


def test_a_list_and_a_signature_of_two_publications_are_not_kept(tmp_path):
    """The list and its signature are two downloads: a new list published between them gave the old list with the new
    signature, and the mirror replaced a good pair with one no program accepts (an hour, until the next check). With
    the project's key (TISMA_LIBRARY_PROJECT_KEY) the pair is checked; a mismatched one is asked again, else the
    previous pair stays."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tisma_cloud import library

    project = Ed25519PrivateKey.generate()
    key = library.public_bytes(project.public_key()).hex()
    old = b'{"format": "tisma-updates", "versions": [{"version": "1.0.1"}]}'
    new = b'{"format": "tisma-updates", "versions": [{"version": "1.0.2"}]}'
    pairs = {"old": (old, project.sign(old)), "new": (new, project.sign(new))}
    mirror = UpdateMirror(URL, tmp_path, files(*pairs["old"]), project_key=key)
    assert mirror.refresh() and mirror.current() == pairs["old"]
    # Published between the two downloads, both times: the old pair stays.
    mirror.fetch = files(old, pairs["new"][1])
    assert not mirror.refresh() and mirror.current() == pairs["old"]
    assert "signature" in mirror.error
    # Published between the two downloads once: asked again, the new pair is kept.
    answers = iter([old, pairs["new"][1], new, pairs["new"][1]])
    mirror.fetch = lambda url: next(answers)
    assert mirror.refresh() and mirror.current() == pairs["new"]
    # A pair that does not match on the disk (cut between its two files) is not served after a restart.
    (tmp_path / "updates.json").write_bytes(old)
    assert UpdateMirror(URL, tmp_path, files(), project_key=key).current() is None
    # Without the project's key the shape is all that is checked, as before.
    assert UpdateMirror(URL, tmp_path, files()).current() == (old, pairs["new"][1])


def test_a_master_certified_while_running_checks_the_list_without_a_restart(tmp_path):
    """The one-click certification sets TISMA_LIBRARY_PROJECT_KEY in .env, which the running master reads only when
    it starts: until a restart its mirror did not check the list's signature. The certification also keeps the
    project's public key next to the certificate, and the mirror reads it there."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tisma_cloud import library

    project = Ed25519PrivateKey.generate()
    storage = tmp_path / "storage"
    storage.mkdir()
    old = b'{"format": "tisma-updates", "versions": [{"version": "1.0.1"}]}'
    new = b'{"format": "tisma-updates", "versions": [{"version": "1.0.2"}]}'
    mirror = UpdateMirror(URL, storage / "updates", files(old, project.sign(old)),
                          project_key=lambda: library.certified_project_key(storage))
    assert mirror.refresh()
    pem = bytearray(project.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    library.certify(library.LibraryKey(storage), pem)
    assert library.certified_project_key(storage) == library.public_bytes(project.public_key()).hex()
    mirror.fetch = files(old, project.sign(new))
    assert not mirror.refresh() and mirror.current() == (old, project.sign(old))


def test_the_three_step_certification_keeps_the_project_key_too(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tisma_cloud import library

    project = Ed25519PrivateKey.generate()
    key = library.LibraryKey(tmp_path)
    public = library.public_bytes(project.public_key())
    key.install_certificate(library.b64(project.sign(library.CERT_PREFIX + key.public)), library.b64(public))
    assert library.certified_project_key(tmp_path) == public.hex()


def test_nothing_published_yet_is_a_quiet_notice(tmp_path, caplog):
    """GitHub answers 404 until the release of the versions exists: an info line «Sin actualizaciones publicadas
    todavía», not a warning at every start (beta drill, 11-12)."""
    import logging

    import httpx

    from tisma_cloud import updates

    def not_found(url: str) -> bytes:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    lst = updates.UpdateMirror("https://example.invalid/updates.json", tmp_path, fetch=not_found)
    with caplog.at_level(logging.INFO, logger="tisma_cloud"):
        assert not lst.refresh()
    assert "Sin actualizaciones publicadas" in lst.error
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_stop_of_a_long_wait_is_not_logged_as_an_error():
    import asyncio
    import logging
    import sys

    from tisma_cloud.cli import ShutdownCancelFilter

    def record(exc: BaseException) -> logging.LogRecord:
        try:
            raise exc
        except BaseException:
            return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
                                     None, sys.exc_info())

    f = ShutdownCancelFilter()
    assert not f.filter(record(asyncio.CancelledError("Task cancelled, timeout graceful shutdown exceeded")))
    assert f.filter(record(asyncio.CancelledError()))
    assert f.filter(record(RuntimeError("a real error")))
    note = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                             "Cancel %s running task(s), timeout graceful shutdown exceeded", (8,), None)
    assert f.filter(note) and note.levelname == "INFO"
