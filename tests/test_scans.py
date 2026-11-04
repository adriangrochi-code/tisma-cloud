"""3D scans of Tisma Scanner in the cloud: sent in pieces, a plan of their own, run by a node with tisma_scan."""

from __future__ import annotations

import dataclasses
import io
import json
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import Factory, new_app
from tisma_cloud.models import Plan, ScanUpload, utcnow


def make_scan(photos: int = 3, extra: dict[str, bytes] | None = None, session: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if session:
            z.writestr("scan_demo/session.json", json.dumps({"device": {"model": "demo"}}))
        z.writestr("scan_demo/frames.csv", "frame,file\n")
        for i in range(photos):
            z.writestr(f"scan_demo/images/0/{i}.jpg", b"\xff\xd8\xff" + bytes(2000) + b"\xff\xd9")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def small_chunks(settings):
    # Pieces of 1 MB in the tests.
    return dataclasses.replace(settings, scan_chunk_mb=1)


@pytest.fixture
def c(small_chunks):
    with TestClient(new_app(small_chunks)) as client:
        yield client


def scan_plan(f: Factory) -> str:
    with f.db.session() as s:
        s.add(Plan(name="escaneo", monthly_hours=10, max_concurrent=2, public=True, classes="scan"))
    return "escaneo"


def send(c: TestClient, user: dict, data: bytes, name: str = "pieza.zip") -> dict:
    r = c.post("/v1/scans", headers=user, json={"size": len(data), "name": name})
    assert r.status_code == 201, r.text
    up = r.json()
    for offset in range(0, len(data), up["chunk_size"]):
        piece = data[offset:offset + up["chunk_size"]]
        r = c.put(f"/v1/scans/{up['id']}/data", params={"offset": offset}, headers=user, content=piece)
        assert r.status_code == 200 and r.json()["received"] == offset + len(piece), r.text
    return up


def test_scan_in_pieces_becomes_a_job_run_by_a_scan_node(c):
    f = Factory(c)
    user = f.user("ana@x.com", plan=scan_plan(f))
    data = make_scan(extra={"scan_demo/big.bin": bytes(range(256)) * 9000})  # more than two pieces of 1 MB
    assert len(data) > 2 << 20
    up = send(c, user, data)
    assert c.get(f"/v1/scans/{up['id']}", headers=user).json()["received"] == len(data)
    job = c.post(f"/v1/scans/{up['id']}/complete", headers=user).json()
    assert job["kind"] == "scan" and job["job_class"] == "scan" and job["status"] == "queued"
    # Asked again (the answer was lost): the same job.
    assert c.post(f"/v1/scans/{up['id']}/complete", headers=user).json()["id"] == job["id"]
    # Only a node that does scans takes it, and gets the very same zip.
    slicer = f.node("slicer", "slice,fea")
    assert f.claim(slicer) is None
    scanner = f.node("gpu", "scan")
    claimed = f.claim(scanner)
    assert claimed["id"] == job["id"] and claimed["kind"] == "scan"
    assert c.get(f"/v1/node/jobs/{job['id']}/input", headers=scanner).content == data
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as z:
        z.writestr("pieza.stl", "solid pieza\nendsolid pieza\n")
        z.writestr("pieza_informe.json", "{}")
    r = c.post(f"/v1/node/jobs/{job['id']}/result", headers=scanner,
               files={"file": ("scan.zip", result.getvalue(), "application/zip")},
               data={"summary": json.dumps({"size_mm": [40.2, 30.2, 20.3], "volume_mm3": 24300})})
    assert r.status_code == 200, r.text
    view = c.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "done" and view["result_filename"] == "pieza.zip"
    assert view["summary"]["size_mm"] == [40.2, 30.2, 20.3]
    got = c.get(f"/v1/jobs/{job['id']}/result", headers=user)
    assert got.status_code == 200 and got.content == result.getvalue()


def test_resume_after_a_cut(c):
    f = Factory(c)
    user = f.user("ana@x.com", plan=scan_plan(f))
    data = make_scan(extra={"scan_demo/big.bin": bytes(1 << 20) + b"x" * 1000})
    up = c.post("/v1/scans", headers=user, json={"size": len(data), "name": "a.zip"}).json()
    piece = up["chunk_size"]
    assert c.put(f"/v1/scans/{up['id']}/data", params={"offset": 0}, headers=user, content=data[:piece]).status_code \
        == 200
    # The phone lost the answer and sends the first piece again: 409 says where to go on.
    r = c.put(f"/v1/scans/{up['id']}/data", params={"offset": 0}, headers=user, content=data[:piece])
    assert r.status_code == 409 and r.json()["received"] == piece
    # Not complete yet.
    assert c.post(f"/v1/scans/{up['id']}/complete", headers=user).status_code == 409
    r = c.put(f"/v1/scans/{up['id']}/data", params={"offset": piece}, headers=user, content=data[piece:])
    assert r.status_code == 200
    assert c.post(f"/v1/scans/{up['id']}/complete", headers=user).status_code == 201


def test_plan_without_scans_and_limits(c, small_chunks):
    f = Factory(c)
    scan_plan(f)
    with f.db.session() as s:
        s.add(Plan(name="laminar", monthly_hours=10, max_concurrent=2, public=True, classes="slice,slice_large"))
    slicer_user = f.user("lam@x.com", plan="laminar")
    r = c.post("/v1/scans", headers=slicer_user, json={"size": 1000, "name": "a.zip"})
    assert r.status_code == 402 and "escaneo" in r.json()["detail"]
    user = f.user("ana@x.com", plan="escaneo")
    # Too large, a piece too large, a piece without its length, and another account's scan.
    assert c.post("/v1/scans", headers=user, json={"size": (small_chunks.max_scan_gb << 30) + 1}).status_code == 413
    up = c.post("/v1/scans", headers=user, json={"size": 3 << 20, "name": "a.zip"}).json()
    assert c.put(f"/v1/scans/{up['id']}/data", params={"offset": 0}, headers=user,
                 content=bytes((1 << 20) * 2 + 10)).status_code == 413

    def parts():
        yield b"x" * 100

    assert c.put(f"/v1/scans/{up['id']}/data", params={"offset": 0}, headers=user, content=parts()).status_code == 411
    other = f.user("otro@x.com", plan="escaneo")
    assert c.get(f"/v1/scans/{up['id']}", headers=other).status_code == 404
    assert c.put(f"/v1/scans/{up['id']}/data", params={"offset": 0}, headers=other, content=b"x").status_code == 404
    # Not through /v1/jobs.
    assert f.submit(user, kind="scan", expect=422)["detail"].startswith("A 3D scan")
    # The public plans say which one includes scans.
    plans = {p["name"]: p for p in c.get("/v1/plans").json()["plans"]}
    assert plans["escaneo"]["classes"] == ["scan"] and "scan" not in plans["laminar"]["classes"]


@pytest.mark.parametrize("data, error", [
    (b"not a zip", "not a zip"),
    (make_scan(session=False), "session.json"),
    (make_scan(photos=0), "no photos"),
    (make_scan(extra={"../evil.txt": b"x"}), "not allowed"),
    (make_scan(extra={"scan_demo/gps.csv": b"t,provider,lat,lon\n1,gps,-34.9,-56.2\n"}), "absolute position"),
])
def test_bad_scans_are_refused(c, data, error):
    f = Factory(c)
    user = f.user("ana@x.com", plan=scan_plan(f))
    up = send(c, user, data)
    r = c.post(f"/v1/scans/{up['id']}/complete", headers=user)
    assert r.status_code == 422 and error in r.json()["detail"]
    # Its pieces are gone.
    assert c.get(f"/v1/scans/{up['id']}", headers=user).status_code == 404


def test_given_up_scans_are_removed(c, small_chunks):
    f = Factory(c)
    user = f.user("ana@x.com", plan=scan_plan(f))
    up = c.post("/v1/scans", headers=user, json={"size": 5000, "name": "a.zip"}).json()
    services = c.app.state.services
    part = Path(small_chunks.storage_dir) / "scans" / f"{up['id']}.zip.part"
    assert part.exists()
    with f.db.session() as s:
        s.get(ScanUpload, up["id"]).updated_at = utcnow() - timedelta(hours=25)
    services.sweep()
    assert not part.exists() and c.get(f"/v1/scans/{up['id']}", headers=user).status_code == 404
    # Deleted by the app.
    up = c.post("/v1/scans", headers=user, json={"size": 5000, "name": "b.zip"}).json()
    assert c.delete(f"/v1/scans/{up['id']}", headers=user).status_code == 200
    assert c.get(f"/v1/scans/{up['id']}", headers=user).status_code == 404


FAKE_SCAN = r'''
import json, sys, zipfile
from pathlib import Path
args = sys.argv[1:]
scan, out, summary = Path(args[0]), Path(args[args.index("-o") + 1]), Path(args[args.index("--summary") + 1])
names = zipfile.ZipFile(scan).namelist()
if not any(n.endswith("session.json") for n in names):
    summary.write_text(json.dumps({"error": "Not a scan", "code": "bad_scan", "error_es": "No es un escaneo"}))
    sys.exit(2)
for p in (10, 60, 100):
    print(f"progress {p}", flush=True)
(out / "pieza.stl").write_text("solid pieza\nendsolid pieza\n")
(out / "pieza_informe.json").write_text(json.dumps({"size_mm": [40, 30, 20]}))
(out / "log.txt").write_text("ok\n")
summary.write_text(json.dumps({"size_mm": [40, 30, 20], "photos": len(names), "args": args[1:]}))
'''


def test_runner_runs_tisma_scan_and_zips_its_files(tmp_path):
    from tisma_cloud.node.config import NodeConfig
    from tisma_cloud.node.runners import JobFailed, run_scan

    script = tmp_path / "tisma_scan.py"
    script.write_text(FAKE_SCAN)
    cfg = NodeConfig(master_url="http://m", token="t", tisma_scan=str(script), gpu=False)
    work = tmp_path / "w"
    work.mkdir()
    (work / "input.3mf").write_bytes(make_scan())
    seen: list[int] = []
    outcome = run_scan(cfg, {"threads": 2}, work / "input.3mf", work, seen.append, lambda: False)
    assert outcome.result.name == "scan.zip" and seen[-1] == 100
    assert sorted(zipfile.ZipFile(outcome.result).namelist()) == ["log.txt", "pieza.stl", "pieza_informe.json"]
    assert outcome.summary["size_mm"] == [40, 30, 20]
    assert "--threads" in outcome.summary["args"] and "--no-gpu" in outcome.summary["args"]
    # A failure carries its code and its text in Spanish to the job.
    work2 = tmp_path / "w2"
    work2.mkdir()
    (work2 / "input.3mf").write_bytes(make_scan(session=False))
    with pytest.raises(JobFailed) as ex:
        run_scan(cfg, {}, work2 / "input.3mf", work2, seen.append, lambda: False)
    assert str(ex.value) == "Not a scan" and ex.value.details == {"code": "bad_scan", "error_es": "No es un escaneo"}
    # A crash before the summary (a missing module): a coded error, not the traceback.
    crash = tmp_path / "crash.py"
    crash.write_text("import no_such_module_for_scans\n")
    work3 = tmp_path / "w3"
    work3.mkdir()
    (work3 / "input.3mf").write_bytes(make_scan())
    with pytest.raises(JobFailed) as ex:
        run_scan(dataclasses.replace(cfg, tisma_scan=str(crash)), {}, work3 / "input.3mf", work3, seen.append,
                 lambda: False)
    assert "Traceback" not in str(ex.value) and ex.value.details["code"] == "scan_error"
    assert ex.value.details["error_es"] == "El escaneo falló en el servidor de cálculo"
    # Without tisma_scan the node refuses.
    with pytest.raises(JobFailed):
        run_scan(dataclasses.replace(cfg, tisma_scan=""), {}, work2 / "input.3mf", work2, seen.append, lambda: False)


def test_scan_only_node_config(tmp_path):
    from tisma_cloud.node.config import NodeConfig

    toml = tmp_path / "node.toml"
    toml.write_text(f'master_url = "http://m:8000"\ntoken = "tn_x"\ntisma_scan = "{sys.executable}"\n')
    cfg = NodeConfig.load(toml)
    assert cfg.tisma_scan == sys.executable and cfg.scan_timeout_s == 6 * 3600


def test_check_of_a_scan_node(tmp_path, monkeypatch):
    from tisma_cloud.manager import Manager

    script = tmp_path / "tisma_scan.py"
    script.write_text(READY_CHECK.format(ready="False"))
    m = Manager(tmp_path)
    m.set_role("node")
    m.write_node({"tisma_scan": str(script), "tisma_slicer": "", "tisma_fea": "", "master_url": "http://127.0.0.1:9"})
    checks = {c.name: c for c in m.checks()}
    assert checks["Escaneos 3D"].ok is False and "COLMAP is missing" in checks["Escaneos 3D"].detail
    assert checks["Escaneos 3D"].fix == "scan_modules"
    assert checks["Escaneos 3D: aviso"].ok is None
    # A node only for scans: no complaint about tisma-slicer.
    assert "Programa tisma-slicer" not in checks
    script.write_text(READY_CHECK.format(ready="True"))
    assert {c.name: c for c in m.checks()}["Escaneos 3D"].ok is True
    m.write_node({"tisma_scan": str(tmp_path / "missing.exe")})
    assert {c.name: c for c in m.checks()}["Escaneos 3D"].ok is False


READY_CHECK = """import json, sys
ready = {ready}
if sys.argv[1:] == ["--check"]:
    print(json.dumps({{"ready": ready, "problems": [] if ready else ["COLMAP is missing"],
                      "warnings": ["no NVIDIA card found"]}}))
    sys.exit(0 if ready else 3)
"""


def test_node_takes_scans_only_when_ready(tmp_path):
    from tisma_cloud.node.agent import Agent
    from tisma_cloud.node.config import NodeConfig

    script = tmp_path / "tisma_scan.py"
    script.write_text(READY_CHECK.format(ready="False"))
    claims: list[dict] = []

    class Answer:
        status_code = 204

    class Client:
        headers: dict = {}

        def post(self, url, json=None, params=None, timeout=None):  # noqa: A002
            claims.append(json)
            return Answer()

    cfg = NodeConfig(master_url="http://m", token="t", tisma_scan=str(script), work_dir=tmp_path / "w")
    agent = Agent(cfg, client=Client())
    assert not agent.check_scans(force=True)
    assert agent.info["scan"]["problems"] == ["COLMAP is missing"]
    agent.claim_for_slot(wait=0)
    assert "scan" in claims[-1]["exclude"]
    script.write_text(READY_CHECK.format(ready="True"))
    assert agent.check_scans(force=True)
    agent.claim_for_slot(wait=0)
    assert "scan" not in claims[-1]["exclude"]
