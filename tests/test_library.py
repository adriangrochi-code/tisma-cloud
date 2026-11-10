"""The library shared between users (library.py; p2p_library.md of tisma_engine): only metadata and hashes are
published; the index and the blocklist are signed by the library key; reports reach the panel's review."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tisma_cloud import library
from tisma_cloud.models import LibraryBlock, utcnow

WEBP = b"RIFF" + (20).to_bytes(4, "little") + b"WEBPVP8 " + bytes(12)


class App:
    """An app of an account: its peer key, bound to the account, and the models it publishes."""

    def __init__(self, client, headers) -> None:
        self.client, self.headers = client, headers
        self.key = Ed25519PrivateKey.generate()
        self.public = library.b64(library.public_bytes(self.key.public_key()))

    def bind(self) -> dict:
        r = self.client.post("/v1/library/peer-key", headers=self.headers, json={"public_key": self.public})
        assert r.status_code == 200, r.text
        return r.json()

    def body(self, files: list[dict] | None = None, **extra) -> dict:
        files = files or [{"path": "pieza.3mf", "role": "project", "size": 2048,
                           "sha256": hashlib.sha256(b"pieza").hexdigest()}]
        signature = library.b64(self.key.sign(library.manifest(files)))
        return {"name": "Soporte de cámara", "description": "Para una GoPro", "tags": ["soporte"], "license": "CC-BY-4.0",
                "author_name": "Lucía", "files": files, "peer_key": self.public, "manifest_signature": signature,
                "thumbnail_webp": base64.b64encode(WEBP).decode(),
                "analysis": {"kind": "fea", "verdict": "holds", "safety_factor": 2.4}, **extra}

    def publish(self, expect: int = 201, **body) -> dict:
        r = self.client.post("/v1/library/models", headers=self.headers, json=self.body(**body))
        assert r.status_code == expect, r.text
        return r.json()


PROJECT = Ed25519PrivateKey.generate()
PROJECT_HEX = library.public_bytes(PROJECT.public_key()).hex()


def certify(client) -> None:
    """The owner's signature of the library key, with a project key of the tests."""
    key = client.app.state.services.library.key
    key.install_certificate(library.b64(PROJECT.sign(library.CERT_PREFIX + key.public)), PROJECT_HEX)


@pytest.fixture(autouse=True)
def certified(client):
    certify(client)


def signed(client, name: str) -> dict:
    """The list, after checking its signature with the library key the master gives and that key's certificate."""
    given = client.get("/v1/library/key").json()
    key = library.unb64(given["public_key"], 32)
    assert library.verify(bytes.fromhex(PROJECT_HEX), library.unb64(given["certificate"], 64), library.CERT_PREFIX + key)
    data = client.get(f"/v1/library/{name}.json").content
    sig = client.get(f"/v1/library/{name}.json.sig").content
    assert len(sig) == 64 and library.verify(key, sig, data)
    return json.loads(data)


def test_publish_only_metadata_and_a_signed_index(client, f):
    user = f.user("lucia@x.com")
    app = App(client, user)
    index = signed(client, "index")
    assert index["format"] == "tisma-library-index" and index["version"] == 1 and index["models"] == []
    # Publishing needs the app's key bound to the account first.
    app.publish(expect=409)
    account = app.bind()["account"]
    assert account.startswith("ua_") and "lucia" not in account
    entry = app.publish()
    later = signed(client, "index")
    assert later["sequence"] > index["sequence"]
    assert [m["id"] for m in later["models"]] == [entry["id"]]
    m = later["models"][0]
    assert m["author"] == {"account": account, "name": "Lucía"} and m["license"] == "CC-BY-4.0"
    assert m["files"][0]["sha256"] == hashlib.sha256(b"pieza").hexdigest() and m["size"] == 2048
    # The author's files carry its signature: any app checks them with the peer key of the index.
    assert library.verify(library.unb64(m["peer_key"], 32), library.unb64(m["manifest_signature"], 64),
                          library.manifest(m["files"]))
    # The thumbnail is served by the master, recognised by the hash in the index.
    thumb = client.get(f"/v1/library/thumbs/{m['thumbnail']['sha256']}.webp")
    assert thumb.status_code == 200 and thumb.content == WEBP
    # The same list again: 304 with its ETag.
    r = client.get("/v1/library/index.json")
    assert client.get("/v1/library/index.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_what_cannot_be_published(client, f):
    user = f.user()
    app = App(client, user)
    app.bind()
    other = App(client, f.user("otro@x.com"))
    # Someone else's key cannot be bound, nor sign for this account.
    assert client.post("/v1/library/peer-key", headers=f.user("tercero@x.com"),
                       json={"public_key": app.public}).status_code == 409
    files = [{"path": "a.3mf", "role": "project", "size": 10, "sha256": "a" * 64}]
    body = app.body(files)
    body["manifest_signature"] = library.b64(other.key.sign(library.manifest(files)))
    assert client.post("/v1/library/models", headers=user, json=body).status_code == 422
    for bad in ({"license": "WTFPL"}, {"files": [{"path": "a.gcode", "role": "gcode", "size": 1, "sha256": "a" * 64}]},
                {"files": [{"path": "a.3mf", "role": "project", "size": 1, "sha256": "a" * 64},
                           {"path": "plate.bgcode", "role": "profile", "size": 1, "sha256": "b" * 64}]},
                {"files": [{"path": "a.3mf", "role": "project", "size": 1, "sha256": "a" * 64},
                           {"path": "setup.exe", "role": "photo", "size": 1, "sha256": "b" * 64}]},
                {"files": [{"path": "a.3mf", "role": "other", "size": 1, "sha256": "a" * 64}]},
                {"files": [{"path": "../a.3mf", "role": "project", "size": 1, "sha256": "a" * 64}]},
                {"files": [{"path": "a.3mf", "role": "project", "size": 1, "sha256": "A" * 64}]},
                {"thumbnail_webp": base64.b64encode(b"\x89PNG....").decode()}):
        b = app.body(**{k: v for k, v in bad.items() if k == "files"})
        b.update({k: v for k, v in bad.items() if k != "files"})
        assert client.post("/v1/library/models", headers=user, json=b).status_code == 422, bad
    assert client.get("/v1/library/models", headers=user).status_code in (404, 405)


def test_reports_review_and_the_signed_blocklist(client, f):
    admin = f.user("root@x.com", admin=True)
    author = f.user("lucia@x.com")
    app = App(client, author)
    app.bind()
    entry = app.publish()
    sha = entry["files"][0]["sha256"]
    before = signed(client, "blocklist")
    assert before["format"] == "tisma-library-blocklist" and before["files"] == []
    assert before["expires"] > before["generated"]
    # «Denunciar»: the model and the file, from another account.
    reporter = f.user("ana@x.com")
    assert client.post("/v1/library/reports", headers=reporter,
                       json={"sha256": sha, "reason": "No es de quien lo publicó"}).status_code == 201
    assert client.post("/v1/library/reports", headers=reporter, json={"sha256": "b" * 64, "reason": "x"}).status_code == 404
    assert client.post("/v1/library/reports", headers=reporter, json={"model": entry["id"]}).status_code == 422
    reports = client.get("/v1/admin/library/reports", headers=admin).json()["models"]
    assert reports[0]["model"]["id"] == entry["id"] and reports[0]["reports"][0]["reason"].startswith("No es")
    # Not for anyone else.
    assert client.get("/v1/admin/library/reports", headers=reporter).status_code == 403
    # Removed in the review: out of the index, its file in the blocklist, and it cannot come back.
    r = client.post(f"/v1/admin/library/models/{entry['id']}/remove", headers=admin, json={"reason": "copia"})
    assert r.json() == {"ok": True, "blocked_files": 1}
    assert signed(client, "index")["models"] == []
    after = signed(client, "blocklist")
    assert after["files"] == [{"sha256": sha, "reason": "copia"}] and after["sequence"] > before["sequence"]
    app.publish(expect=422)
    assert client.get("/v1/admin/library/reports", headers=admin).json()["models"] == []


def test_suspended_accounts_and_blocked_peers_leave_the_index(client, f):
    admin = f.user("root@x.com", admin=True)
    author = f.user("lucia@x.com")
    app = App(client, author)
    account = app.bind()["account"]
    app.publish()
    assert len(signed(client, "index")["models"]) == 1
    block = client.post("/v1/admin/library/blocks", headers=admin, json={"kind": "account", "value": account}).json()
    assert signed(client, "index")["models"] == [] and signed(client, "blocklist")["accounts"] == [account]
    app.publish(expect=403)
    assert client.delete(f"/v1/admin/library/blocks/{block['id']}", headers=admin).status_code == 200
    assert len(signed(client, "index")["models"]) == 1
    client.post("/v1/admin/library/blocks", headers=admin, json={"kind": "peer", "value": app.public})
    assert signed(client, "index")["models"] == [] and signed(client, "blocklist")["peers"] == [app.public]


def test_an_address_is_blocked_for_a_day_and_then_forgotten(client, f):
    admin = f.user("root@x.com", admin=True)
    r = client.post("/v1/admin/library/blocks", headers=admin, json={"kind": "address", "value": "203.0.113.5"})
    assert r.status_code == 201
    until = r.json()["until"]
    blocklist = signed(client, "blocklist")
    # Only as a hash with the salt of the day, never the address itself (p2p_library.md 8 quinquies).
    salt = bytes.fromhex(blocklist["address_salt"])
    assert blocklist["addresses"] == [{"sha256": hashlib.sha256(salt + b"203.0.113.5").hexdigest(), "until": until}]
    assert b"203.0.113.5" not in client.get("/v1/library/blocklist.json").content
    # Another day, another salt.
    key = client.app.state.services.library.key
    assert key.address_salt("2026-11-10") != key.address_salt("2026-11-11")
    assert client.post("/v1/admin/library/blocks", headers=admin,
                       json={"kind": "address", "value": "no es una ip"}).status_code == 422
    # When it ends, the row is deleted (a personal datum) and the list moves on.
    with f.db.session() as s:
        s.query(LibraryBlock).update({LibraryBlock.until: utcnow() - timedelta(minutes=1)})
    assert signed(client, "blocklist")["addresses"] == []
    with f.db.session() as s:
        assert s.query(LibraryBlock).count() == 0


def test_revisions_by_their_author_and_removal_by_the_author(client, f):
    author = f.user("lucia@x.com")
    app = App(client, author)
    app.bind()
    entry = app.publish()
    files = [{"path": "pieza.3mf", "role": "project", "size": 4096, "sha256": "c" * 64}]
    r = client.post(f"/v1/library/models/{entry['id']}/revisions", headers=author, json=app.body(files))
    assert r.status_code == 201 and r.json()["revision"] == 2
    models = signed(client, "index")["models"]
    assert [(m["id"], m["revision"], m["size"]) for m in models] == [(entry["id"], 2, 4096)]
    # Another account cannot add a revision to it.
    other = App(client, f.user("otro@x.com"))
    other.bind()
    assert client.post(f"/v1/library/models/{entry['id']}/revisions", headers=other.headers,
                       json=other.body(files)).status_code == 404
    assert client.delete(f"/v1/library/models/{entry['id']}", headers=author).status_code == 200
    assert signed(client, "index")["models"] == []
    assert [m["removed"] for m in client.get("/v1/library/mine", headers=author).json()["models"]] == [True, True]


def test_the_certificate_of_the_library_key(tmp_path):
    project = Ed25519PrivateKey.generate()
    project_b64 = library.b64(library.public_bytes(project.public_key()))
    key = library.LibraryKey(tmp_path)
    assert key.certificate() is None
    # The same key after a restart.
    assert library.LibraryKey(tmp_path).public == key.public
    with pytest.raises(library.LibraryError, match="TISMA_LIBRARY_PROJECT_KEY"):
        key.install_certificate("x", "")
    # The project key as tools/sign_release.py prints it (64 hex digits) or in base64.
    assert library.project_key_bytes(library.public_bytes(project.public_key()).hex()) == library.unb64(project_b64, 32)
    wrong = library.b64(project.sign(b"otra cosa"))
    with pytest.raises(library.LibraryError, match="no es una firma"):
        key.install_certificate(wrong, project_b64)
    good = library.b64(project.sign(library.CERT_PREFIX + key.public))
    key.install_certificate(good, project_b64)
    assert library.verify(library.unb64(project_b64, 32), key.certificate(), library.CERT_PREFIX + key.public)


def test_the_admin_commands_of_the_library_key(tmp_path, monkeypatch, capsys):
    from tisma_cloud import admin

    project = Ed25519PrivateKey.generate()
    monkeypatch.setenv("TISMA_DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv("TISMA_STORAGE_DIR", str(tmp_path / "admin-storage"))
    monkeypatch.setenv("TISMA_LIBRARY_PROJECT_KEY", library.b64(library.public_bytes(project.public_key())))
    assert admin.main(["library-key"]) == 0
    out = capsys.readouterr().out
    public = library.unb64(out.split("Clave de la biblioteca: ")[1].split()[0], 32)
    assert "falta" in out
    assert admin.main(["library-cert", library.b64(project.sign(b"otra"))]) == 1
    assert admin.main(["library-cert", library.b64(project.sign(library.CERT_PREFIX + public))]) == 0
    assert "instalado" in capsys.readouterr().out


def test_without_a_certificate_the_library_is_not_served(settings, tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from conftest import new_app

    with TestClient(new_app(dataclasses.replace(settings, storage_dir=tmp_path / "other"))) as fresh:
        assert fresh.get("/v1/library/key").json()["certificate"] is None
        for name in ("index.json", "index.json.sig", "blocklist.json", "blocklist.json.sig"):
            assert fresh.get(f"/v1/library/{name}").status_code == 503
    with TestClient(new_app(dataclasses.replace(settings, storage_dir=tmp_path / "other",
                                                library_uncertified=True))) as trying:
        assert trying.get("/v1/library/index.json").status_code == 200


def test_revoked_keys_are_in_the_blocklist(settings, tmp_path):
    import dataclasses

    from fastapi.testclient import TestClient

    from conftest import new_app

    old = library.b64(bytes(32))
    with TestClient(new_app(dataclasses.replace(settings, library_revoked_keys=old))) as c:
        certify(c)
        assert signed(c, "blocklist")["revoked_keys"] == [old]


def test_the_owner_certifies_the_key_with_the_usb_key_and_openssl(tmp_path, client):
    """deploy/certify_library_key.py with a PEM key as tools/sign_release.py of the engine reads it."""
    import shutil
    import subprocess
    import sys

    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    pem = tmp_path / "clave.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(pem)], check=True, capture_output=True)
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "deploy"))
    import certify_library_key as tool

    project = tool.public_key_raw("openssl", str(pem))
    key = library.LibraryKey(tmp_path / "master")
    with pytest.raises(tool.Failure, match="pendrive"):
        tool.certify(str(pem), library.b64(key.public), public_hex="00" * 32)
    cert = tool.certify(str(pem), library.b64(key.public), public_hex=project.hex())
    key.install_certificate(cert, project.hex())
    assert key.certificate() is not None
