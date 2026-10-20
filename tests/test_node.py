from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
from conftest import DATA, make_3mf

from tisma_cloud.node.agent import Agent
from tisma_cloud.node.config import NodeConfig
from tisma_cloud.node.runners import JobFailed, sanitize_project

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake programs are shell scripts")


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_sanitize_removes_post_processing(tmp_path):
    config = "; layer_height = 0.2\n; post_process = \"touch /tmp/pwned\"\n; printer_technology = FFF\n"
    model_config = ('<?xml version="1.0"?><config><object id="1"><metadata type="object" key="post_process" '
                    'value="rm -rf /"/><metadata type="object" key="name" value="bar"/></object></config>')
    src = write(tmp_path, "in.3mf", make_3mf(config=config, extra={"Metadata/Slic3r_PE_model.config": model_config}))
    dst = tmp_path / "out.3mf"
    sanitize_project(src, dst, for_slicing=True)
    with zipfile.ZipFile(dst) as z:
        cfg = z.read("Metadata/Slic3r_PE.config").decode()
        model = z.read("Metadata/Slic3r_PE_model.config").decode()
        assert "touch" not in cfg and "; post_process = \n" in cfg and "; layer_height = 0.2" in cfg
        assert "rm -rf" not in model and 'key="name"' in model
        assert z.read("3D/3dmodel.model") == zipfile.ZipFile(src).read("3D/3dmodel.model")


def test_sanitize_refuses(tmp_path):
    with pytest.raises(JobFailed, match="not a 3MF"):
        sanitize_project(write(tmp_path, "a", b"nope"), tmp_path / "o", True)
    bambu = write(tmp_path, "b.3mf", make_3mf(extra={"Metadata/project_settings.config": "{}"}))
    with pytest.raises(JobFailed, match="Only Tisma projects"):
        sanitize_project(bambu, tmp_path / "o", False)
    resin = write(tmp_path, "r.3mf", make_3mf(config="; printer_technology = SLA\n"))
    with pytest.raises(JobFailed, match="resin"):
        sanitize_project(resin, tmp_path / "o", True)
    # The analysis of a resin part is fine.
    sanitize_project(resin, tmp_path / "o", False)


FAKE_SLICER = """\
#!/usr/bin/env python3
import sys, time, zipfile
args = sys.argv[1:]
out = args[args.index("--output") + 1]
names = zipfile.ZipFile(args[-1]).namelist()
if "Metadata/Slic3r_PE.config" in names and b"FAIL" in zipfile.ZipFile(args[-1]).read("Metadata/Slic3r_PE.config"):
    print("Object too large for the bed", file=sys.stderr)
    sys.exit(1)
for p in (10, 50, 90):
    print(f"{p:3d}% => step", flush=True)
    time.sleep(float(__import__("os").environ.get("FAKE_DELAY", "0")))
with open(out, "w") as f:
    f.write("G1 X1\\n; filament used [mm] = 921.48\\n; total filament used [g] = 2.75\\n"
            "; estimated printing time (normal mode) = 10m 8s\\n")
"""

FAKE_FEA = """\
#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
get = lambda k: args[args.index(k) + 1]
print("progress 0", flush=True)
print("progress 100", flush=True)
open(get("--output"), "wb").write(b"TFEA" + json.dumps(args).encode())
json.dump({"ok": "true", "mode": get("--mode"), "args": args}, open(get("--summary"), "w"))
"""


@pytest.fixture
def node_cfg(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in (("tisma-slicer", FAKE_SLICER), ("tisma-fea", FAKE_FEA)):
        p = bins / name
        p.write_text(body.replace("/usr/bin/env python3", sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return NodeConfig(master_url="http://testserver", token="", tisma_slicer=str(bins / "tisma-slicer"),
                      tisma_fea=str(bins / "tisma-fea"), work_dir=tmp_path / "work", gpu=False, timeout_s=30)


def agent_for(client, node_headers, cfg) -> Agent:
    cfg.token = node_headers["Authorization"].split(" ", 1)[1]
    # The TestClient is an httpx client: the agent talks to the app without a network.
    return Agent(cfg, client=client)


@posix_only
def test_agent_slices(client, f, node_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), node_cfg)
    job = f.submit(user, data=make_3mf(config="; post_process = evil\n"))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "done", view
    assert view["summary"] == {"print_time": "10m 8s", "filament_g": 2.75, "filament_mm": 921.48}
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content.startswith(b"G1 X1")
    # Nothing left in the work directory.
    assert list(node_cfg.work_dir.iterdir()) == []
    assert agent.run_once() is None


@posix_only
def test_agent_reports_failures(client, f, node_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), node_cfg)
    job = f.submit(user, data=make_3mf(config="; FAIL\n"))
    assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and "too large for the bed" in view["error"]
    assert f.me(user)["hours_used"] == 0
    job = f.submit(user, data=make_3mf(extra={"Metadata/project_settings.config": "{}"}))
    assert agent.run_once() == "failed"
    assert "Only Tisma projects" in client.get(f"/v1/jobs/{job['id']}", headers=user).json()["error"]


@posix_only
def test_agent_runs_analyses(client, f, node_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "fea_optimize,fea"), node_cfg)
    job = f.submit(user, kind="fea", params=json.dumps({"mode": "lightest-infill", "quality": "fast", "object": 1,
                                                        "zones": False, "ribs": True}))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["result_filename"] == "part.tfea"
    args = view["summary"]["args"]
    assert args[args.index("--mode") + 1] == "lightest-infill"
    assert args[args.index("--quality") + 1] == "fast"
    assert args[args.index("--object") + 1] == "1"
    assert "--no-zones" in args and "--ribs" in args and "--no-gpu" in args and "--solid" not in args
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content.startswith(b"TFEA")


@posix_only
def test_agent_stops_cancelled_jobs(client, f, node_cfg, monkeypatch):
    import threading
    import time

    monkeypatch.setenv("FAKE_DELAY", "1")
    monkeypatch.setattr("tisma_cloud.node.agent.PROGRESS_EVERY_S", 0.)
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), node_cfg)
    job = f.submit(user)
    result = {}
    t = threading.Thread(target=lambda: result.update(state=agent.run_once()))
    t.start()
    time.sleep(0.5)
    client.post(f"/v1/jobs/{job['id']}/cancel", headers=user)
    t.join(20)
    assert result["state"] == "cancelled"
    assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["status"] == "cancelled"


def test_node_config(tmp_path, monkeypatch):
    p = tmp_path / "node.toml"
    p.write_text('master_url = "https://cloud.example/"\ntisma_slicer = "s"\ntisma_fea = "f"\nslots = 0\n')
    monkeypatch.setenv("TISMA_NODE_TOKEN", "tn_x")
    cfg = NodeConfig.load(p)
    assert cfg.master_url == "https://cloud.example" and cfg.token == "tn_x" and cfg.slots == 1
    p.write_text('master_url = "x"\ntisma_slicer = "s"\ntisma_fea = "f"\ncolour = 1\n')
    with pytest.raises(ValueError, match="colour"):
        NodeConfig.load(p)


# ---- with the real programs: TISMA_SLICER_BIN and TISMA_FEA_BIN point to tisma-slicer and tisma-fea.
SLICER = os.environ.get("TISMA_SLICER_BIN")
FEA = os.environ.get("TISMA_FEA_BIN")


@pytest.mark.skipif(not (SLICER and FEA), reason="set TISMA_SLICER_BIN and TISMA_FEA_BIN")
def test_real_programs(client, f, node_cfg, tmp_path):
    node_cfg.tisma_slicer, node_cfg.tisma_fea = SLICER, FEA
    node_cfg.gpu = False
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "fea_optimize,fea,slice_large,slice"), node_cfg)
    project = (DATA / "bar_project.3mf").read_bytes()

    # A post-processing script in the project must not run on the node.
    marker = tmp_path / "pwned"
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(project)) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "Metadata/Slic3r_PE.config":
                data = data.replace(b"; post_process = \n", f'; post_process = "touch {marker}"\n'.encode())
                assert b"touch" in data
            out.writestr(info.filename, data)
    job = f.submit(user, data=buf.getvalue())
    assert agent.run_once() == "done", client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    gcode = client.get(f"/v1/jobs/{job['id']}/result", headers=user).content
    assert b"G1 " in gcode and view["summary"]["print_time"]
    assert not marker.exists()

    job = f.submit(user, kind="fea", params=json.dumps({"quality": "fast"}), data=project)
    assert agent.run_once() == "done", client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["summary"]["ok"] == "true" and float(view["summary"]["result"]["safety_factor"]) > 0
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content.startswith(b"TFEA")

    job = f.submit(user, kind="fea", params=json.dumps({"mode": "resin-structure"}), data=project)
    assert agent.run_once() == "failed"
    assert "resin printer" in client.get(f"/v1/jobs/{job['id']}", headers=user).json()["error"]
    shutil.rmtree(node_cfg.work_dir, ignore_errors=True)
