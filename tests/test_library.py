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


def scan_done(f, user_headers, name: str = "engranaje.zip") -> str:
    """A finished scan job of the account, with its result zip as a node leaves it."""
    import io
    import zipfile

    from tisma_cloud.models import Job

    me = f.me(user_headers)
    with f.db.session() as s:
        from tisma_cloud.models import User
        from sqlalchemy import select

        uid = s.execute(select(User.id).where(User.email_index == __import__("tisma_cloud.crypto", fromlist=["index"])
                                              .index(me["email"]))).scalar_one()
        job = Job(user_id=uid, kind="scan", job_class="scan", params="{}", input_name=name, status="done",
                  created_at=utcnow(), order_at=utcnow(), finished_at=utcnow())
        s.add(job)
        s.flush()
        job_id = job.id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pieza.stl", b"solid pieza\nendsolid pieza\n")
        z.writestr("pieza_informe.json", json.dumps({"dimensions_mm_xyz": [40.9, 31.4, 20.1]}))
        z.writestr("pieza.ply", b"ply\n")
        z.writestr("log.txt", "ok")
    path = f.client.app.state.services.storage.result_path(job_id, ".zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return job_id


def test_a_scanned_part_is_kept_in_the_account_and_published_from_it(client, f):
    user = f.user("sofia@x.com")
    job_id = scan_done(f, user)
    # «Imprimir»: kept, private, with the part and its measures only (no cloud, no log).
    r = client.post("/v1/files", headers=user, json={"scan_job": job_id})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["name"] == "engranaje" and item["kind"] == "scan" and item["published_model"] is None
    assert [x["path"] for x in item["files"]] == ["pieza.stl", "pieza_informe.json"]
    stl = next(x for x in item["files"] if x["path"] == "pieza.stl")
    assert stl["sha256"] == hashlib.sha256(b"solid pieza\nendsolid pieza\n").hexdigest()
    # Again: the same item (the app may retry).
    again = client.post("/v1/files", headers=user, json={"scan_job": job_id})
    assert again.status_code == 200 and again.json()["id"] == item["id"]
    assert [x["id"] for x in client.get("/v1/files", headers=user).json()["files"]] == [item["id"]]
    got = client.get(f"/v1/files/{item['id']}/pieza.stl", headers=user)
    assert got.status_code == 200 and got.content.startswith(b"solid pieza") and "engranaje.stl" in got.headers["content-disposition"]
    # Private: another account sees nothing.
    other = f.user("otro@x.com")
    assert client.get(f"/v1/files/{item['id']}/pieza.stl", headers=other).status_code == 404
    assert client.post("/v1/files", headers=other, json={"scan_job": job_id}).status_code == 404
    assert client.get("/v1/files", headers=other).json()["files"] == []
    assert client.patch(f"/v1/files/{item['id']}", headers=user, json={"name": "Engranaje 40"}).json()["name"] == "Engranaje 40"
    # «Publicar»: the same files, signed by the app.
    app = App(client, user)
    app.bind()
    files = [{"path": "pieza.stl", "role": "project", "size": stl["size"], "sha256": stl["sha256"]}]
    wrong = [{"path": "pieza.stl", "role": "project", "size": stl["size"], "sha256": "d" * 64}]
    assert client.post("/v1/library/models", headers=user, json={**app.body(wrong), "from_file": item["id"]}).status_code == 422
    r = client.post("/v1/library/models", headers=user, json={**app.body(files), "from_file": item["id"]})
    assert r.status_code == 201, r.text
    assert client.get("/v1/files", headers=user).json()["files"][0]["published_model"] == r.json()["id"]
    assert [m["id"] for m in signed(client, "index")["models"]] == [r.json()["id"]]
    assert client.delete(f"/v1/files/{item['id']}", headers=user).status_code == 200
    assert client.get("/v1/files", headers=user).json()["files"] == []


def test_what_cannot_be_kept(client, f, settings):
    from tisma_cloud.models import Job

    user = f.user("sofia@x.com")
    job_id = scan_done(f, user)
    with f.db.session() as s:
        s.get(Job, job_id).status = "running"
    assert client.post("/v1/files", headers=user, json={"scan_job": job_id}).status_code == 409
    with f.db.session() as s:
        s.get(Job, job_id).status = "done"
    client.app.state.services.storage.remove_job(job_id)
    assert client.post("/v1/files", headers=user, json={"scan_job": job_id}).status_code == 410
    assert client.post("/v1/files", headers=user, json={"scan_job": "nada"}).status_code == 404
    # A slicing job is not a scan.
    sliced = f.submit(user)
    assert client.post("/v1/files", headers=user, json={"scan_job": sliced["id"]}).status_code == 404


def test_the_files_quota_and_deleting_the_account(client, f):
    user = f.user("sofia@x.com")
    services = client.app.state.services
    services.settings.account_files_gb = 0
    assert client.post("/v1/files", headers=user, json={"scan_job": scan_done(f, user)}).status_code == 413
    services.settings.account_files_gb = 5
    item = client.post("/v1/files", headers=user, json={"scan_job": scan_done(f, user)}).json()
    app = App(client, user)
    app.bind()
    app.publish()
    folder = services.settings.storage_dir / "files" / item["id"]
    assert folder.is_dir() and len(signed(client, "index")["models"]) == 1
    with f.db.session() as s:
        from sqlalchemy import select

        from tisma_cloud.models import AccountFile
        uid = s.execute(select(AccountFile.user_id)).scalar_one()
    services.delete_account(uid)
    assert not folder.exists() and signed(client, "index")["models"] == []


def test_the_tisma_apps_take_the_scans_into_mi_biblioteca_once_per_device(client, f):
    user = f.user("sofia@x.com")
    item = client.post("/v1/files", headers=user, json={"scan_job": scan_done(f, user)}).json()
    phone = client.get("/v1/me/scans", headers=user, params={"device": "Pixel 8"}).json()["scans"]
    assert [(x["id"], x["name"], x["file"]) for x in phone] == [(item["id"], "engranaje", "engranaje.stl")]
    mesh = client.get(f"/v1/me/scans/{item['id']}/mesh", headers=user)
    assert mesh.status_code == 200 and hashlib.sha256(mesh.content).hexdigest() == phone[0]["sha256"]
    assert client.post(f"/v1/me/scans/{item['id']}/taken", headers=user, json={"device": "Pixel 8"}).json() == {"ok": True}
    assert client.post(f"/v1/me/scans/{item['id']}/taken", headers=user, json={"device": "Pixel 8"}).status_code == 200
    # Taken by the phone, still there for the computer.
    assert client.get("/v1/me/scans", headers=user, params={"device": "Pixel 8"}).json()["scans"] == []
    assert len(client.get("/v1/me/scans", headers=user, params={"device": "PC del taller"}).json()["scans"]) == 1
    other = f.user("otro@x.com")
    assert client.get("/v1/me/scans", headers=other).json()["scans"] == []
    assert client.post(f"/v1/me/scans/{item['id']}/taken", headers=other, json={"device": "x"}).status_code == 404
    assert client.delete(f"/v1/files/{item['id']}", headers=user).status_code == 200


def test_the_relay_cap_is_set_in_the_panel(client, f):
    admin = f.user("root@x.com", admin=True)
    assert client.get("/v1/admin/library/settings", headers=admin).json() == {"relay_mb_per_hour_per_account": 500}
    r = client.put("/v1/admin/library/settings", headers=admin, json={"relay_mb_per_hour_per_account": 2000})
    assert r.json() == {"relay_mb_per_hour_per_account": 2000}
    for bad in ({"relay_mb_per_hour_per_account": -1}, {"relay_mb_per_hour_per_account": "x"}, {"other": 1}):
        assert client.put("/v1/admin/library/settings", headers=admin, json=bad).status_code == 422
    assert client.get("/v1/admin/library/settings", headers=f.user()).status_code == 403


def announce(client, app: App, models: list[str], direct: bool, addrs: list[str], when: int | None = None) -> int:
    import time

    from tisma_cloud import peers

    body = {"peer_key": app.public, "models": models, "addrs": addrs, "direct": direct,
            "time": int(time.time()) if when is None else when}
    body["signature"] = library.b64(app.key.sign(peers.announcement_bytes(body)))
    return client.post("/v1/library/peers/announce", headers=app.headers, json=body).status_code


def test_relay_tickets_and_who_shares_a_model(settings):
    import dataclasses
    import time

    from fastapi.testclient import TestClient

    from conftest import Factory, new_app
    from tisma_cloud import peers

    relay = "/dns4/relay.tismaservidores.net/udp/4001/quic-v1/p2p/12D3KooWRelay"
    with TestClient(new_app(dataclasses.replace(settings, library_relays=relay)), client=("192.168.1.20", 50000)) as c:
        certify(c)
        f = Factory(c)
        lucia, ana, diego = (App(c, f.user(e)) for e in ("lucia@x.com", "ana@x.com", "diego@x.com"))
        for a in (lucia, ana, diego):
            a.bind()
        model = lucia.publish()["id"]
        # The relay's ticket: signed by the library key, with the account, the PeerId, its end and the cap.
        r = c.post("/v1/library/relay-ticket", headers=lucia.headers, json={"peer_key": lucia.public}).json()
        payload_b64, sig_b64 = r["ticket"].split(".")
        pad = lambda t: base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))  # noqa: E731
        key = library.unb64(c.get("/v1/library/key").json()["public_key"], 32)
        assert library.verify(key, pad(sig_b64), pad(payload_b64))
        payload = json.loads(pad(payload_b64))
        assert payload["peer_id"] == peers.peer_id(library.unb64(lucia.public, 32)) and payload["peer_id"].startswith("12D3KooW")
        assert payload["cap_mb_per_hour"] == 500 and payload["expires"] > time.time() and r["relays"] == [relay]
        # Someone else's key: no ticket.
        assert c.post("/v1/library/relay-ticket", headers=ana.headers, json={"peer_key": lucia.public}).status_code == 409
        # Lucía shares directly, Diego through the relay; the master knows their addresses, nobody else unless both
        # chose «Directo».
        assert announce(c, lucia, [model], True, ["/ip4/203.0.113.7/udp/4001/quic-v1"]) == 200
        assert announce(c, diego, [model], False, ["/ip4/198.51.100.2/tcp/4001"]) == 200
        assert announce(c, ana, [], True, ["/ip4/192.0.2.9/tcp/4001"]) == 200
        seen_by_direct = c.get("/v1/library/peers", headers=ana.headers, params={"model": model, "peer_key": ana.public})
        found = {p["peer_id"]: p["addrs"] for p in seen_by_direct.json()["peers"]}
        lucia_id, diego_id = (peers.peer_id(library.unb64(a.public, 32)) for a in (lucia, diego))
        assert found[lucia_id] == ["/ip4/203.0.113.7/udp/4001/quic-v1"]
        assert found[diego_id] == [f"{relay}/p2p-circuit/p2p/{diego_id}"]
        # An app through the relay sees nobody's address.
        seen = c.get("/v1/library/peers", headers=diego.headers, params={"model": model, "peer_key": diego.public}).json()
        assert seen["peers"] == [{"peer_id": lucia_id, "addrs": [f"{relay}/p2p-circuit/p2p/{lucia_id}"]}]
        assert "203.0.113.7" not in json.dumps(seen) and "198.51.100.2" not in str(c.get("/v1/library/peers",
               headers=ana.headers, params={"model": model}).json())
        # A bad signature, an old time, a blocked peer.
        assert announce(c, lucia, [model], True, [], when=int(time.time()) - 3600) == 422
        body = {"peer_key": lucia.public, "models": [model], "addrs": [], "direct": True, "time": int(time.time()),
                "signature": library.b64(ana.key.sign(b"x"))}
        assert c.post("/v1/library/peers/announce", headers=lucia.headers, json=body).status_code == 422
        admin = f.user("root@x.com", admin=True)
        c.post("/v1/admin/library/blocks", headers=admin, json={"kind": "peer", "value": diego.public})
        assert announce(c, diego, [model], False, []) == 403
        ids = [p["peer_id"] for p in c.get("/v1/library/peers", headers=ana.headers, params={"model": model}).json()["peers"]]
        assert diego_id not in ids
        # The address an app announces from, blocked for a day: no announcing, no tickets.
        c.post("/v1/admin/library/blocks", headers=admin, json={"kind": "address", "value": "192.168.1.20"})
        assert announce(c, ana, [], True, []) == 403
        assert c.post("/v1/library/relay-ticket", headers=ana.headers, json={"peer_key": ana.public}).status_code == 403


def test_the_swarm_of_a_model_is_checked(client, f):
    app = App(client, f.user())
    app.bind()
    sha = hashlib.sha256(b"pieza").hexdigest()
    good = {"protocol": "libp2p/tisma-library/1", "piece_size": 1048576, "piece_roots": {sha: "e" * 64}}
    assert app.publish(swarm=good)["swarm"] == good
    for bad in ({**good, "piece_size": 4096}, {**good, "protocol": "bittorrent"}, {**good, "piece_roots": {}},
                {**good, "pieces": {sha: ["e" * 64]}}):
        app.publish(expect=422, swarm=bad)
