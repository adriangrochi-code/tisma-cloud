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

import httpx
import pytest
from conftest import DATA, make_3mf

from tisma_cloud.node.agent import Agent
from tisma_cloud.node.config import NodeConfig, auto_slots, memory_gb
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


FAKE_ENGINE = """\
#!/usr/bin/env python3
import json, sys, zipfile
args = sys.argv[1:]
if args == ["--help"]:
    print("usage: tisma-engine slice <project> -o <out> --summary <summary.json> [--sandbox] [--threads <n>]")
    sys.exit(0)
get = lambda k: args[args.index(k) + 1]
command, project = args[0], args[1]
out, summary = get("-o"), get("--summary")
assert "--sandbox" in args
names = zipfile.ZipFile(project).namelist()
settings = zipfile.ZipFile(project).read("Metadata/tisma_project.json") if "Metadata/tisma_project.json" in names else b""
model = zipfile.ZipFile(project).read("3D/3dmodel.model")
if b"FAIL" in settings or b"TOO_LARGE" in model:
    json.dump({"error": "Object too large for the bed"}, open(summary, "w"))
    sys.exit(4)
if b"ENGINE_BUG" in model:
    sys.stderr.write("the G-code did not pass its check\\n")
    sys.exit(6)
if command == "analyze" and get("--mode") == "resin-structure":
    json.dump({"error": "this analysis (resin-structure) does not run in the cloud yet", "code": "not_in_cloud",
               "error_es": "este análisis (estructura de resina) todavía no corre en la nube"}, open(summary, "w"))
    sys.exit(3)
for p in (0, 50, 100):
    print(f"progress {p}", flush=True)
if command == "slice":
    open(out, "w").write("G1 X1\\n")
    json.dump({"print_time": "10m 8s", "print_time_s": 608, "filament_mm": 921.48, "filament_g": 2.75,
               "layers": 50, "names": names}, open(summary, "w"))
else:
    open(out, "wb").write(b"ANALYSIS")
    json.dump({"verdict": "holds", "args": args}, open(summary, "w"))
"""


@pytest.fixture
def engine_cfg(tmp_path):
    bins = tmp_path / "bin-engine"
    bins.mkdir()
    p = bins / "tisma-engine"
    p.write_text(FAKE_ENGINE.replace("/usr/bin/env python3", sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return NodeConfig(master_url="http://testserver", token="", tisma_engine=str(p), work_dir=tmp_path / "work",
                      gpu=False, timeout_s=30)


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
    assert view["summary"] == {"print_time": "10m 8s", "filament_g": 2.75, "filament_mm": 921.48,
                               "slicer": "tisma-slicer"}
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content.startswith(b"G1 X1")
    # Nothing left in the work directory.
    assert list(node_cfg.work_dir.iterdir()) == []
    assert agent.run_once() is None


@posix_only
def test_result_is_sent_again_while_the_master_is_busy(client, f, node_cfg, monkeypatch):
    # Under load the master answered a node's result with 429 and the node failed a job already computed.
    from tisma_cloud.node import agent as agent_mod

    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), node_cfg)
    job = f.submit(user)
    real_post = client.post
    answers = iter([429, 503])

    def busy_then_real(url, *args, **kwargs):
        if url.endswith("/result"):
            code = next(answers, None)
            if code is not None:
                return httpx.Response(code, headers={"retry-after": "0"}, request=httpx.Request("POST", url))
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(client, "post", busy_then_real)
    monkeypatch.setattr(agent_mod, "RESULT_RETRY_S", 30.)
    assert agent.run_once() == "done"
    assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["status"] == "done"
    # Busy for longer than RESULT_RETRY_S: then it is reported, not left running.
    job = f.submit(user)
    answers = iter([503] * 1000)
    monkeypatch.setattr(agent_mod, "RESULT_RETRY_S", 0.)
    assert agent.run_once() == "failed"
    assert "could not be sent" in client.get(f"/v1/jobs/{job['id']}", headers=user).json()["error"]


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
    # tisma-fea has no "the project's own quality" (an empty one, for tisma-engine) nor "extreme" (its "ultra").
    for sent, given in (({"mode": "resin-structure"}, "normal"), ({"quality": "extreme"}, "ultra")):
        job = f.submit(user, kind="fea", params=json.dumps(sent))
        assert agent.run_once() == "done"
        args = client.get(f"/v1/jobs/{job['id']}", headers=user).json()["summary"]["args"]
        assert args[args.index("--quality") + 1] == given


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
    # 0: automatic, from the cores and the memory of the machine.
    assert cfg.master_url == "https://cloud.example" and cfg.token == "tn_x"
    assert cfg.slots == auto_slots(os.cpu_count() or 1, memory_gb()) and cfg.slots >= 1 and cfg.fea_slots == 1
    p.write_text('master_url = "http://x"\ntisma_slicer = "s"\ntisma_fea = "f"\nslots = 3\nfea_slots = 0\n')
    cfg = NodeConfig.load(p)
    assert cfg.slots == 3 and cfg.fea_slots == 1
    # The work folder (relative: next to node.toml) is absolute, whatever the current folder: the programs run in it.
    assert cfg.work_dir == (tmp_path / "node-work").resolve()
    p.write_text('master_url = "http://x"\ntisma_slicer = "s"\ntisma_fea = "f"\nwork_dir = "trabajos"\n')
    monkeypatch.chdir(tmp_path.parent)
    assert NodeConfig.load(p).work_dir == (tmp_path / "trabajos").resolve()
    p.write_text('master_url = "http://x"\ntisma_slicer = "s"\ntisma_fea = "f"\ncolour = 1\n')
    with pytest.raises(ValueError, match="colour"):
        NodeConfig.load(p)


def test_programs_are_told_about_the_graphics_card(tmp_path, node_cfg):
    """With gpu on, the slicer may use the graphics card (TISMA_GPU_SLICING); off, neither the slicer nor the analysis."""
    import re as regex

    from tisma_cloud.node.runners import _run

    show = [sys.executable, "-c", "import os, sys; sys.stderr.write(os.environ.get('TISMA_GPU_SLICING', '-') + "
            "os.environ.get('TISMA_FEA_GPU', '-'))"]
    args = (regex.compile("^$"), lambda _p: None, lambda: False, tmp_path)
    node_cfg.gpu = True
    assert _run(show, node_cfg, *args) == (0, "1-")
    node_cfg.gpu = False
    assert _run(show, node_cfg, *args) == (0, "00")


def test_auto_slots():
    # One job per two cores, and 2 GB of memory for each.
    assert auto_slots(4, 15.5) == 2
    assert auto_slots(16, 32.) == 8
    assert auto_slots(16, 8.) == 4
    assert auto_slots(1, None) == 1 and auto_slots(8, 1.) == 1
    # tisma-engine: one job per core, and 1 GB of memory for each.
    assert auto_slots(4, 15.5, engine=True) == 4
    assert auto_slots(16, 8., engine=True) == 8
    assert auto_slots(1, None, engine=True) == 1 and auto_slots(8, 0.5, engine=True) == 1


def test_one_analysis_at_a_time(client, f, node_cfg):
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "fea,slice"), node_cfg)
    a1 = f.submit(user, kind="fea", params="{}")
    a2 = f.submit(user, kind="fea", params="{}")
    plate = f.submit(user)
    # The first slot takes an analysis and holds its place; the next one may only take the plate.
    job, holds = agent.claim_for_slot()
    assert job["id"] == a1["id"] and holds
    job, holds = agent.claim_for_slot()
    assert job["id"] == plate["id"] and not holds
    assert agent.claim_for_slot() == (None, False)
    # The analysis ended: the next one can be taken.
    agent.release_fea()
    job, holds = agent.claim_for_slot()
    assert job["id"] == a2["id"] and holds


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


@posix_only
def test_engine_slices(client, f, engine_cfg):
    """With tisma_engine set, the node slices with the new slicer and its summary (cloud.md of the engine)."""
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
    # A project of another slicer is accepted: the engine opens it itself.
    job = f.submit(user, data=make_3mf(extra={"Metadata/project_settings.config": "{}"}))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "done", view
    summary = view["summary"]
    assert summary["print_time"] == "10m 8s" and summary["filament_g"] == 2.75 and summary["layers"] == 50
    assert "Metadata/project_settings.config" in summary["names"]
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content.startswith(b"G1 X1")
    assert list(engine_cfg.work_dir.iterdir()) == []


@posix_only
def test_engine_failure_uses_its_summary(client, f, engine_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
    job = f.submit(user, data=make_3mf(extra={"Metadata/tisma_project.json": '{"format": "tisma-project", "FAIL": 1}'}))
    assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and view["error"] == "Object too large for the bed"
    assert f.me(user)["hours_used"] == 0


@posix_only
def test_engine_runs_analyses(client, f, engine_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "fea_optimize,fea"), engine_cfg)
    job = f.submit(user, kind="fea", params=json.dumps({"mode": "lightest-infill", "quality": "fast", "object": 1,
                                                        "zones": False, "ribs": True}))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    args = view["summary"]["args"]
    assert args[0] == "analyze" and args[args.index("--mode") + 1] == "lightest-infill"
    assert args[args.index("--object") + 1] == "1"
    assert "--no-zones" in args and "--ribs" in args and "--no-gpu" in args and "--solid" not in args
    assert view["result_filename"].endswith(".tea")
    assert client.get(f"/v1/jobs/{job['id']}/result", headers=user).content == b"ANALYSIS"



@posix_only
def test_engine_analysis_quality_and_errors(client, f, engine_cfg):
    user = f.user()
    agent = agent_for(client, f.node("n", "fea_optimize,fea"), engine_cfg)
    # Without a quality, none is passed: the engine uses the project's own, as the interface (FM25).
    job = f.submit(user, kind="fea")
    assert agent.run_once() == "done"
    args = client.get(f"/v1/jobs/{job['id']}", headers=user).json()["summary"]["args"]
    assert "--quality" not in args
    # A refusal of the engine keeps its code and its Spanish text for the program.
    job = f.submit(user, kind="fea", params=json.dumps({"mode": "resin-structure"}))
    assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["error"] == "this analysis (resin-structure) does not run in the cloud yet"
    assert view["summary"] == {"code": "not_in_cloud",
                               "error_es": "este análisis (estructura de resina) todavía no corre en la nube"}

def test_engine_node_config(tmp_path):
    p = tmp_path / "node.toml"
    p.write_text('master_url = "http://x"\ntisma_engine = "/opt/tisma/tisma-engine"\n')
    cfg = NodeConfig.load(p)
    assert cfg.tisma_engine == "/opt/tisma/tisma-engine" and cfg.tisma_slicer == "" and cfg.tisma_fea == ""
    # Automatic slots: one job per core with the engine.
    assert cfg.slots == auto_slots(os.cpu_count() or 1, memory_gb(), engine=True)
    p.write_text('master_url = "http://x"\ntisma_slicer = "s"\n')
    with pytest.raises(ValueError, match="tisma_fea"):
        NodeConfig.load(p)


@posix_only
def test_engine_runs_aero_and_orient(client, f, engine_cfg):
    """The aerodynamic analysis (CFD) and the automatic orientation run with the engine (cloud.md, 19b and 19c)."""
    user = f.user()
    agent = agent_for(client, f.node("n", "aero,orient"), engine_cfg)
    job = f.submit(user, kind="aero", params=json.dumps({"quality": "fast", "speed": 12.5, "direction": [0, 1, 0],
                                                         "object": 2}))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    args = view["summary"]["args"]
    assert args[0] == "aero" and args[args.index("--quality") + 1] == "fast"
    assert args[args.index("--speed") + 1] == "12.5" and args[args.index("--direction") + 1] == "0,1,0"
    assert args[args.index("--object") + 1] == "2" and "--no-gpu" in args
    # The processor cores of the user's plan (1 by default): the engine runs with that many threads.
    assert args[args.index("--threads") + 1] == "1"
    assert view["result_filename"].endswith(".taero")
    job = f.submit(user, kind="orient", params=json.dumps({"instance": 1}))
    assert agent.run_once() == "done"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["summary"]["args"][0] == "orient" and view["result_filename"].endswith(".json")


@posix_only
def test_aero_needs_the_engine(client, f, node_cfg):
    """A node with only the programs of Tisma Slicer cannot run an aerodynamic analysis: the job fails and is free."""
    user = f.user()
    agent = agent_for(client, f.node("n", "aero"), node_cfg)
    job = f.submit(user, kind="aero")
    assert agent.run_once() == "failed"
    assert "tisma_engine" in client.get(f"/v1/jobs/{job['id']}", headers=user).json()["error"]
    assert f.me(user)["hours_used"] == 0


@posix_only
def test_engine_without_threads_option(client, f, engine_cfg):
    """An engine whose --help does not list --threads gets no --threads (it would refuse the whole command)."""
    from tisma_cloud.node import runners

    text = engine_cfg.tisma_engine and open(engine_cfg.tisma_engine).read()
    with open(engine_cfg.tisma_engine, "w") as out:
        out.write(text.replace(" [--threads <n>]", ""))
    runners._ENGINE_HELP.pop(engine_cfg.tisma_engine, None)
    user = f.user()
    agent = agent_for(client, f.node("n", "aero"), engine_cfg)
    job = f.submit(user, kind="aero")
    assert agent.run_once() == "done"
    args = client.get(f"/v1/jobs/{job['id']}", headers=user).json()["summary"]["args"]
    assert "--threads" not in args
    runners._ENGINE_HELP.pop(engine_cfg.tisma_engine, None)


def test_errors_do_not_show_the_node_folders(tmp_path):
    from tisma_cloud.node.agent import _without_paths
    work = tmp_path / "work" / "job1"
    text = f"{work}{os.sep}project.3mf: project: settings: infill.density: must be a number (in {work})"
    assert _without_paths(text, work) == "project.3mf: project: settings: infill.density: must be a number (in )"
    assert str(tmp_path) not in _without_paths(text, work)


ENGINE_PROJECT = {"Metadata/tisma_project.json": '{"format": "tisma-project", "version": 1, "settings": {}}'}


@pytest.fixture
def both_cfg(engine_cfg, node_cfg):
    """A node with tisma-engine and Tisma Slicer, as the package brings them."""
    node_cfg.tisma_engine = engine_cfg.tisma_engine
    return node_cfg


def _sliced_by(client, f, cfg, data: bytes, name: str = "n") -> dict:
    user = f.user(f"{name}@x.com")
    agent = agent_for(client, f.node(name, "slice"), cfg)
    job = f.submit(user, data=data)
    agent.run_once()
    return client.get(f"/v1/jobs/{job['id']}", headers=user).json()


def _model_with(marker: str) -> bytes:
    """A bare model (no settings) whose model file carries a marker for the fake engine."""
    src = zipfile.ZipFile(io.BytesIO(make_3mf()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "3D/3dmodel.model":
                body = body.replace(b"<build>", f"<!-- {marker} --><build>".encode())
            out.writestr(info.filename, body)
    return buf.getvalue()


@posix_only
def test_each_project_goes_to_the_program_that_reads_its_settings(client, f, both_cfg):
    # The new Tisma's project: the engine (Tisma Slicer would ignore its settings).
    view = _sliced_by(client, f, both_cfg, make_3mf(extra=ENGINE_PROJECT), "a")
    assert view["status"] == "done" and view["summary"]["slicer"] == "tisma-engine"
    # Tisma Slicer's project: Tisma Slicer (the engine would take only its parts, not its settings).
    view = _sliced_by(client, f, both_cfg, make_3mf(config="; layer_height = 0.2\n"), "b")
    assert view["status"] == "done" and view["summary"]["slicer"] == "tisma-slicer"
    # A bare model: the engine, which replaces Tisma Slicer.
    view = _sliced_by(client, f, both_cfg, make_3mf(), "c")
    assert view["status"] == "done" and view["summary"]["slicer"] == "tisma-engine"
    assert "fallback_from" not in view["summary"]


@posix_only
def test_a_bare_model_falls_back_to_tisma_slicer_when_the_engine_fails(client, f, both_cfg):
    # The engine's own G-code check failed (code 6): Tisma Slicer slices the model, and the summary says so.
    view = _sliced_by(client, f, both_cfg, _model_with("ENGINE_BUG"), "a")
    assert view["status"] == "done" and view["summary"]["slicer"] == "tisma-slicer"
    assert view["summary"]["fallback_from"] == "tisma-engine (code 6)"
    # The model cannot be printed (code 4): no second try, the person sees the engine's reason.
    view = _sliced_by(client, f, both_cfg, _model_with("TOO_LARGE"), "b")
    assert view["status"] == "failed" and view["error"] == "Object too large for the bed"


@posix_only
def test_a_project_needs_the_program_that_reads_it(client, f, engine_cfg, node_cfg):
    # The new Tisma's project on a node without the engine: refused with a clear code, never sliced with defaults.
    view = _sliced_by(client, f, node_cfg, make_3mf(extra=ENGINE_PROJECT), "a")
    assert view["status"] == "failed" and view["summary"]["code"] == "needs_engine"
    assert "tisma-engine" in view["summary"]["error_es"]
    # Tisma Slicer's project on a node with only the engine: the same.
    view = _sliced_by(client, f, engine_cfg, make_3mf(config="; layer_height = 0.2\n"), "b")
    assert view["status"] == "failed" and view["summary"]["code"] == "needs_slicer"


# ---- with the real tisma-engine: TISMA_ENGINE_BIN (the engine's nightly package, as the CI puts it in the package).
ENGINE = os.environ.get("TISMA_ENGINE_BIN")


def box_project(settings: dict | None = None) -> bytes:
    """A 20 x 20 x 10 mm box placed at (128, 128) on the bed: a bare model, or with settings a project of the new Tisma
    (tisma_engine docs/PROJECT.md, «El paquete 3MF»: the mesh in its own coordinates, its place in the build item,
    Metadata/tisma_project.json with the settings in the format of docs/CONFIG.md)."""
    v = [(x, y, z) for z in (0, 10) for y in (-10, 10) for x in (-10, 10)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4), (2, 6, 3), (3, 6, 7), (0, 4, 2),
             (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    vertices = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in v)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    model = ('<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><object id="1" type="model">'
             f'<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh></object></resources>'
             '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 128 128 0"/></build></model>')
    files = {"3D/3dmodel.model": model,
             "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org'
                                    '/package/2006/content-types"><Default Extension="model" ContentType="application/'
                                    'vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="rels" '
                                    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>',
             "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.'
                            'org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
                            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'}
    if settings is not None:
        files["Metadata/tisma_project.json"] = json.dumps(
            {"format": "tisma-project", "version": 1, "settings": settings,
             "models": [{"name": "box.stl", "mesh": 0, "offset": [0.0, 0.0, 0.0]}]})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


@pytest.mark.skipif(not ENGINE, reason="set TISMA_ENGINE_BIN (and TISMA_SLICER_BIN for the Tisma Slicer project)")
def test_real_engine_slices_the_new_tisma_projects(client, f, tmp_path):
    cfg = NodeConfig(master_url="http://testserver", token="", tisma_engine=ENGINE, tisma_slicer=SLICER or "",
                     tisma_fea=FEA or "", work_dir=tmp_path / "work", gpu=False, timeout_s=600)
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "slice_large,slice"), cfg)

    problems: list[str] = []

    def sliced(data: bytes, what: str) -> tuple[dict, bytes]:
        job = f.submit(user, data=data)
        agent.run_once()
        view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
        if view["status"] != "done":
            error = str(view.get("error") or "")
            if "unknown setting" in error or "unknown group" in error:
                # A setting of the engine's docs that this nightly does not know yet (machine.macros and bambu reached
                # its dev on 2026-11-06, after that day's nightly): checked once the nightly has it.
                print(f"not in this tisma-engine nightly yet, {what}: {error}")
            else:
                problems.append(f"{what}: {view['status']}: {error} {view.get('summary')}")
            return view, b""
        return view, client.get(f"/v1/jobs/{job['id']}/result", headers=user).content

    # A project of the new Tisma with its settings: the engine.
    view, gcode = sliced(box_project({"layers": {"height": 0.25}}), "settings")
    if gcode and not (view["summary"]["slicer"] == "tisma-engine" and b"G1 " in gcode):
        problems.append(f"settings: {view['summary']}")
    # A bare model: this engine slices only projects saved by Tisma (code 2 for any other 3MF, measured 11-06), so
    # Tisma Slicer slices it and the summary says the engine was tried first.
    if SLICER:
        view, gcode = sliced(box_project(), "bare model")
        if gcode and view["summary"].get("fallback_from") != "tisma-engine (code 2)":
            problems.append(f"bare model: {view['summary']}")
    # The machine's own G-code with macros (CONFIG.md, machine.macros): the project's start G-code is in the result.
    view, gcode = sliced(box_project({"machine": {"macros": {
        "dialect": "prusaslicer", "templates": {"start": "; TISMA CLOUD TEST START\nG28\n",
                                                "end": "; TISMA CLOUD TEST END\n"}}}}), "machine.macros")
    if gcode and not (b"TISMA CLOUD TEST START" in gcode and b"TISMA CLOUD TEST END" in gcode):
        problems.append("machine.macros: the start or end G-code is not in the result")
    # The plate of a Bambu Lab printer (CONFIG.md, bambu.plate): a project that carries it slices.
    sliced(box_project({"bambu": {"plate": "Textured PEI Plate"}}), "bambu.plate")
    # A project of Tisma Slicer goes to Tisma Slicer, which reads its settings.
    if SLICER:
        view, gcode = sliced((DATA / "bar_project.3mf").read_bytes(), "Tisma Slicer project")
        if gcode and view["summary"]["slicer"] != "tisma-slicer":
            problems.append(f"Tisma Slicer project: {view['summary']}")
    assert not problems, "\n".join(problems)



def test_a_master_address_without_its_scheme_is_refused_at_start(tmp_path):
    """«192.168.1.10:8000» can never be reached (no http://): the node says so instead of retrying for ever."""
    for url, ok in (("192.168.1.10:8000", False), ("http://", False), ("http://192.168.1.10:8000", True),
                    ("https://tismaservidores.net", True)):
        path = tmp_path / "node.toml"
        path.write_text(f'master_url = "{url}"\ntoken = "tn_x"\ntisma_slicer = "s"\ntisma_fea = "f"\n')
        if ok:
            assert NodeConfig.load(path).master_url == url.rstrip("/")
        else:
            with pytest.raises(ValueError, match="http:// or https://"):
                NodeConfig.load(path)
    # The window and «tisma-cloud config master_url» refuse it before the service tries it.
    from tisma_cloud.manager import Manager

    m = Manager(tmp_path / "home")
    m.set_role("node")
    with pytest.raises(ValueError, match="http:// o https://"):
        m.set_setting("master_url", "192.168.1.10:8000")
    m.set_setting("master_url", " http://192.168.1.10:8000 ")
    assert m.read_node()["master_url"] == "http://192.168.1.10:8000"
