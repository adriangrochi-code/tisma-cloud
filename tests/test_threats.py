"""Uploads checked before they are kept (threats.py): projects, G-code for the printers, ClamAV."""

from __future__ import annotations

import io
import socket
import struct
import threading
import zipfile
from pathlib import Path

import pytest
from conftest import make_3mf

from tisma_cloud.models import Job, RemoteCommand
from tisma_cloud.threats import Clamd, ScanUnavailable, Threat, check_gcode, check_project

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def project_with(tmp_path: Path, name: str, data: bytes, compress=zipfile.ZIP_DEFLATED) -> Path:
    path = tmp_path / "p.3mf"
    buf = io.BytesIO(make_3mf())
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr(zipfile.ZipInfo(name), data, compress_type=compress)
    path.write_bytes(buf.getvalue())
    return path


def test_a_project_is_only_a_project(tmp_path):
    good = tmp_path / "ok.3mf"
    good.write_bytes(make_3mf(config="; layer_height = 0.2\n"))
    check_project(good)
    bad = {
        "../../etc/cron.d/x": b"* * * * * root sh",                       # a path out of the folder
        "Metadata/tool.exe": b"anything",                                   # a program by its name
        "Metadata/thumbnail.png": b"MZ\x90\x00 a Windows program",          # a program by its content
        "Metadata/run": b"#!/bin/sh\nrm -rf /",                             # a script
        "3D/extra.model": b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaa">]><model/>',  # XML entities
    }
    for name, data in bad.items():
        with pytest.raises(Threat):
            check_project(project_with(tmp_path, name, data))
    with pytest.raises(Threat):
        check_project(project_with(tmp_path, "3D/zeros.bin", b"\0" * (60 << 20)))   # a zip bomb
    not_zip = tmp_path / "x.3mf"
    not_zip.write_bytes(b"MZ not a zip")
    with pytest.raises(Threat):
        check_project(not_zip)


def test_gcode_commands_that_change_the_printer(tmp_path):
    g = tmp_path / "a.gcode"
    g.write_bytes(b"; M997 in a comment does nothing\nG28\nM500 ; vendors save the mesh\nN7 M502\nM9970\n")
    found = check_gcode(g)
    assert [(x.command, x.line) for x in found] == [("M500", 3), ("M502", 4)] and not any(x.blocked for x in found)
    for command in (b"M997", b"m30 /sys/config.g", b"SAVE_CONFIG", b"RUN_SHELL_COMMAND CMD=wipe", b"M28 x.g"):
        g.write_bytes(b"G28\n" * 10 + command + b"\nG1 X1\n")
        with pytest.raises(Threat, match="line 11"):
            check_gcode(g)


def test_a_dangerous_project_is_refused_and_not_queued(client, f):
    user = f.user()
    buf = io.BytesIO(make_3mf())
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("Metadata/payload.dll", b"MZ...")
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", buf.getvalue())})
    assert r.status_code == 422 and "program" in r.json()["detail"]
    services = client.app.state.services
    with f.db.session() as s:
        assert s.query(Job).count() == 0
    assert not list((services.storage.root / "jobs").glob("*/input.3mf"))
    # The administrators are told.
    alerts = services.check_alerts()
    assert any(a.key == "threats" and "proyectos: 1" in a.message for a in alerts)


def test_a_dangerous_gcode_does_not_reach_the_printer(client, f):
    ana = f.user("ana@x.com")
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "PC", "printers": [{"id": "p", "name": "MK3S",
                                                                                  "actions": ["print"]}]})
    client.get("/v1/bridges/pc-1/commands?wait=0", headers=ana)
    url = "/v1/remote/bridges/pc-1/printers/p/print"
    r = client.post(url, headers=ana, files={"file": ("x.gcode", b"G28\nM997\n")})
    assert r.status_code == 422 and "M997" in r.json()["detail"] and "firmware" in r.json()["detail"]
    with f.db.session() as s:
        assert s.query(RemoteCommand).count() == 0
    # Commands some vendors use are only reported.
    r = client.post(url, headers=ana, files={"file": ("y.gcode", b"G28\nM500\nG1 X1\n")})
    assert r.status_code == 201 and r.json()["warnings"] == [{"command": "M500", "line": 2,
                                                              "what": "saves the settings of the printer"}]


class FakeClamd:
    """A clamd that answers INSTREAM as documented: the EICAR test string is a virus."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.address = "127.0.0.1:%d" % self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                command = b""
                while not command.endswith(b"\0"):
                    command += conn.recv(1)
                assert command == b"zINSTREAM\0"
                data = b""
                while True:
                    size = struct.unpack(">I", conn.recv(4, socket.MSG_WAITALL))[0]
                    if size == 0:
                        break
                    data += conn.recv(size, socket.MSG_WAITALL)
                conn.sendall(b"stream: Eicar-Signature FOUND\0" if EICAR in data else b"stream: OK\0")

    def close(self) -> None:
        # Shut down first: a socket closed while another thread waits in accept() keeps listening.
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()


def test_clamav_checks_every_upload(client, f, tmp_path):
    clamd = FakeClamd()
    services = client.app.state.services
    services.clamd = Clamd(clamd.address)
    path = tmp_path / "x"
    path.write_bytes(b"hello")
    assert services.clamd.scan(path) is None
    path.write_bytes(b"x" * 3_000_000 + EICAR)
    assert services.clamd.scan(path) == "Eicar-Signature"
    user = f.user()
    # (Stored, not compressed: this fake does not open zips; ClamAV does.)
    infected = project_with(tmp_path, "Metadata/notes.txt", EICAR, compress=zipfile.ZIP_STORED).read_bytes()
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", infected)})
    assert r.status_code == 422 and "Eicar-Signature" in r.json()["detail"]
    # Without an answer of the antivirus, nothing is accepted unchecked.
    clamd.close()
    with pytest.raises(ScanUnavailable):
        services.clamd.scan(path)
    r = client.post("/v1/jobs", headers=user, data={"kind": "slice"}, files={"file": ("a.3mf", make_3mf())})
    assert r.status_code == 503
    assert any(a.key == "scanner" for a in services.check_alerts())


def test_a_refused_result_fails_the_job(client, f):
    clamd = FakeClamd()
    services = client.app.state.services
    services.clamd = Clamd(clamd.address)
    user, node = f.user(), f.node()
    job = f.submit(user)
    assert f.claim(node)["id"] == job["id"]
    r = client.post(f"/v1/node/jobs/{job['id']}/result", headers=node, files={"file": ("r.gcode", EICAR)})
    assert r.status_code == 422
    with f.db.session() as s:
        j = s.get(Job, job["id"])
        assert j.status == "failed" and "refused" in j.error
    assert services.storage.find_result(job["id"]) is None
    clamd.close()


def test_a_too_large_result_fails_the_job(client, f):
    client.app.state.services.settings.max_result_mb = 0
    user, node = f.user(), f.node()
    job = f.submit(user)
    f.claim(node)
    r = client.post(f"/v1/node/jobs/{job['id']}/result", headers=node, files={"file": ("r.gcode", b"G1\n" * 1000)})
    assert r.status_code == 413
    with f.db.session() as s:
        # Before, the failure was undone with the request and the job stayed running.
        assert s.get(Job, job["id"]).status == "failed"


def test_a_large_gcode_is_read_in_parts_by_several_processes(client, f, monkeypatch):
    from tisma_cloud import threats

    monkeypatch.setattr(threats, "PARALLEL_BYTES", 1 << 20)
    import tisma_cloud.api as api

    monkeypatch.setattr(api, "PARALLEL_BYTES", 1 << 20)
    ana = f.user("ana@x.com")
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "PC", "printers": [{"id": "p", "name": "MK3S",
                                                                                  "actions": ["print"]}]})
    client.get("/v1/bridges/pc-1/commands?wait=0", headers=ana)
    body = b"G1 X1 Y1 E0.01\n" * 300_000          # 4.5 MB: 4 parts
    url = "/v1/remote/bridges/pc-1/printers/p/print"
    r = client.post(url, headers=ana, files={"file": ("a.gcode", body + b"M500\n")})
    assert r.status_code == 201 and r.json()["warnings"][0]["line"] == 300_001
    r = client.post(url, headers=ana, files={"file": ("b.gcode", body + b"M997\n")})
    assert r.status_code == 422 and "line 300001" in r.json()["detail"]
    assert client.app.state.services._scan_pool is not None
