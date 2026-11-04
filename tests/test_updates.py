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
