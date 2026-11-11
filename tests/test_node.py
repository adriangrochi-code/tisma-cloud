from __future__ import annotations

import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from conftest import make_3mf

from tisma_cloud.node.agent import Agent
from tisma_cloud.node.config import NodeConfig, auto_slots, memory_gb
from tisma_cloud.node.runners import JobFailed, sanitize_project

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake programs are shell scripts")


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_sanitize_copies_the_project(tmp_path):
    # tisma-engine runs with --sandbox (no post-processing script nor plugin) and reads other slicers' projects itself.
    src = write(tmp_path, "in.3mf", make_3mf(config="; post_process = \"touch /tmp/x\"\n",
                                             extra={"Metadata/project_settings.config": "{}"}))
    dst = tmp_path / "out.3mf"
    sanitize_project(src, dst)
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        assert {n: a.read(n) for n in a.namelist()} == {n: b.read(n) for n in b.namelist()}


def test_sanitize_refuses(tmp_path, monkeypatch):
    from tisma_cloud.node import runners

    with pytest.raises(JobFailed, match="not a 3MF"):
        sanitize_project(write(tmp_path, "a", b"nope"), tmp_path / "o")
    monkeypatch.setattr(runners, "MAX_PROJECT_BYTES", 100)
    with pytest.raises(JobFailed, match="too large"):
        sanitize_project(write(tmp_path, "b.3mf", make_3mf()), tmp_path / "o")


FAKE_ENGINE = """\
#!/usr/bin/env python3
import json, os, sys, time, zipfile
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
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
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


def agent_for(client, node_headers, cfg) -> Agent:
    cfg.token = node_headers["Authorization"].split(" ", 1)[1]
    # The TestClient is an httpx client: the agent talks to the app without a network.
    return Agent(cfg, client=client)


@posix_only
def test_result_is_sent_again_while_the_master_is_busy(client, f, engine_cfg, monkeypatch):
    # Under load the master answered a node's result with 429 and the node failed a job already computed.
    from tisma_cloud.node import agent as agent_mod

    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
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
def test_a_node_error_sends_the_job_back_to_the_queue(client, f, engine_cfg, monkeypatch, settings):
    # A full disk or an error of the node program is the node's trouble, not the person's: another node takes the job.
    from tisma_cloud.node import runners

    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
    job = f.submit(user)

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setitem(runners.RUNNERS, "slice", no_space)
    assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "queued" and not view.get("error"), view
    # Its project is kept, and a node in order finishes it.
    monkeypatch.setitem(runners.RUNNERS, "slice", runners.run_slice)
    assert agent.run_once() == "done"
    assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["status"] == "done"
    # Not forever: after the master's limit of attempts the job fails with the node's error.
    job = f.submit(user)
    monkeypatch.setitem(runners.RUNNERS, "slice", no_space)
    for _ in range(settings.max_attempts):
        assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert view["status"] == "failed" and "No space left" in view["error"], view
    assert agent.run_once() is None


def test_a_node_with_a_full_disk_takes_no_jobs(client, f, engine_cfg, monkeypatch):
    from collections import namedtuple

    from tisma_cloud.node import agent as agent_mod

    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
    engine_cfg.work_dir.mkdir(parents=True, exist_ok=True)
    usage = namedtuple("usage", "total used free")
    free = {"bytes": 1 << 30}
    monkeypatch.setattr(agent_mod.shutil, "disk_usage", lambda path: usage(1 << 40, 0, free["bytes"]))
    assert agent.disk_full()
    f.submit(user)
    claims = []
    monkeypatch.setattr(agent, "claim_for_slot", lambda *a, **k: claims.append(1) or (None, False))
    monkeypatch.setattr(agent_mod, "DISK_WAIT_S", 0.01)
    import threading

    slot = threading.Thread(target=agent._slot, args=(0,), daemon=True)
    slot.start()
    slot.join(0.2)
    assert claims == []
    free["bytes"] = 50 << 30
    assert not agent.disk_full()
    slot.join(0.2)
    agent._stop.set()
    slot.join(5)
    assert claims, "the node takes jobs again once there is room"


@posix_only
def test_agent_stops_cancelled_jobs(client, f, engine_cfg, monkeypatch):
    import threading
    import time

    monkeypatch.setenv("FAKE_DELAY", "1")
    monkeypatch.setattr("tisma_cloud.node.agent.PROGRESS_EVERY_S", 0.)
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
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
    p.write_text('master_url = "https://cloud.example/"\ntisma_engine = "e"\nslots = 0\n')
    monkeypatch.setenv("TISMA_NODE_TOKEN", "tn_x")
    cfg = NodeConfig.load(p)
    # 0: automatic, from the cores and the memory of the machine.
    assert cfg.master_url == "https://cloud.example" and cfg.token == "tn_x"
    assert cfg.slots == auto_slots(os.cpu_count() or 1, memory_gb()) and cfg.slots >= 1 and cfg.fea_slots == 1
    p.write_text('master_url = "http://x"\ntisma_engine = "e"\nslots = 3\nfea_slots = 0\n')
    cfg = NodeConfig.load(p)
    assert cfg.slots == 3 and cfg.fea_slots == 1
    # The work folder (relative: next to node.toml) is absolute, whatever the current folder: the programs run in it.
    assert cfg.work_dir == (tmp_path / "node-work").resolve()
    p.write_text('master_url = "http://x"\ntisma_engine = "e"\nwork_dir = "trabajos"\n')
    monkeypatch.chdir(tmp_path.parent)
    assert NodeConfig.load(p).work_dir == (tmp_path / "trabajos").resolve()
    p.write_text('master_url = "http://x"\ntisma_engine = "e"\ncolour = 1\n')
    with pytest.raises(ValueError, match="colour"):
        NodeConfig.load(p)


def test_auto_slots():
    # One job per core, and 1 GB of memory for each (tisma-engine).
    assert auto_slots(4, 15.5) == 4
    assert auto_slots(16, 8.) == 8
    assert auto_slots(1, None) == 1 and auto_slots(8, 0.5) == 1


def test_one_analysis_at_a_time(client, f, engine_cfg):
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "fea,slice"), engine_cfg)
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
    assert agent.run_once() is None


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
    assert cfg.tisma_engine == "/opt/tisma/tisma-engine"
    assert cfg.slots == auto_slots(os.cpu_count() or 1, memory_gb())
    # The settings of Tisma Slicer of an older node.toml are read and left unused.
    p.write_text('master_url = "http://x"\ntisma_engine = "e"\ntisma_slicer = "s"\ntisma_fea = "f"\n')
    cfg = NodeConfig.load(p)
    assert cfg.tisma_engine == "e" and not hasattr(cfg, "tisma_slicer")
    # A node needs the engine, unless it only takes scans.
    p.write_text('master_url = "http://x"\ntisma_slicer = "s"\ntisma_fea = "f"\n')
    with pytest.raises(ValueError, match="tisma_engine"):
        NodeConfig.load(p)
    p.write_text('master_url = "http://x"\ntisma_scan = "/opt/scan/tisma_scan"\n')
    assert NodeConfig.load(p).tisma_engine == ""


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
def test_jobs_need_the_engine(client, f, engine_cfg):
    """A node without tisma-engine (a scan node) fails any other job with a clear code, and the job is free."""
    engine_cfg.tisma_engine = ""
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "slice,fea,aero"), engine_cfg)
    for kind in ("slice", "fea", "aero"):
        job = f.submit(user, kind=kind) if kind != "slice" else f.submit(user)
        assert agent.run_once() == "failed"
        view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
        assert "tisma_engine" in view["error"] and view["summary"]["code"] == "needs_engine"
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
def test_every_project_goes_to_the_engine(client, f, engine_cfg):
    """The new Tisma's projects, Tisma Slicer's and bare models: all of them are sliced by tisma-engine, with no other
    program to try when it fails."""
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "slice"), engine_cfg)
    for data in (make_3mf(extra=ENGINE_PROJECT), make_3mf(config="; layer_height = 0.2\n"), make_3mf()):
        job = f.submit(user, data=data)
        assert agent.run_once() == "done"
        assert client.get(f"/v1/jobs/{job['id']}", headers=user).json()["summary"]["slicer"] == "tisma-engine"
    job = f.submit(user, data=_model_with("ENGINE_BUG"))
    assert agent.run_once() == "failed"
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    assert "did not pass its check" in view["error"] and "fallback_from" not in (view.get("summary") or {})


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


@pytest.mark.skipif(not ENGINE, reason="set TISMA_ENGINE_BIN")
def test_real_engine_slices_the_new_tisma_projects(client, f, tmp_path):
    cfg = NodeConfig(master_url="http://testserver", token="", tisma_engine=ENGINE, work_dir=tmp_path / "work", gpu=False,
                     timeout_s=600)
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "slice_large,slice"), cfg)

    problems: list[str] = []

    def sliced(data: bytes, what: str) -> tuple[dict, bytes]:
        job = f.submit(user, data=data)
        agent.run_once()
        view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
        if view["status"] != "done":
            # machine.macros and bambu.plate are in the nightly since 2026-11-09 (70f9335): a setting it does not know
            # any more is a failure too.
            problems.append(f"{what}: {view['status']}: {view.get('error')} {view.get('summary')}")
            return view, b""
        return view, client.get(f"/v1/jobs/{job['id']}/result", headers=user).content

    # A project of the new Tisma with its settings: the engine.
    view, gcode = sliced(box_project({"layers": {"height": 0.25}}), "settings")
    if gcode and not (view["summary"]["slicer"] == "tisma-engine" and b"G1 " in gcode):
        problems.append(f"settings: {view['summary']}")
    # The machine's own G-code with macros (CONFIG.md, machine.macros): the project's start G-code is in the result.
    view, gcode = sliced(box_project({"machine": {"macros": {
        "dialect": "prusaslicer", "templates": {"start": "; TISMA CLOUD TEST START\nG28\n",
                                                "end": "; TISMA CLOUD TEST END\n"}}}}), "machine.macros")
    if gcode and not (b"TISMA CLOUD TEST START" in gcode and b"TISMA CLOUD TEST END" in gcode):
        problems.append("machine.macros: the start or end G-code is not in the result")
    # The plate of a Bambu Lab printer (CONFIG.md, bambu.plate): a project that carries it slices.
    sliced(box_project({"bambu": {"plate": "Textured PEI Plate"}}), "bambu.plate")
    assert not problems, "\n".join(problems)


@pytest.mark.skipif(not ENGINE, reason="set TISMA_ENGINE_BIN")
def test_real_engine_slices_a_bare_model(client, f, tmp_path):
    """A 3MF with no settings (a model from a library, or saved by another program). The engine is the only slicer of
    the nodes since 2026-11-11; on 11-06 it gave code 2 for any 3MF not saved by Tisma, which Tisma Slicer sliced
    then. Reported by the CI without blocking the package, as the contract of the other commands."""
    cfg = NodeConfig(master_url="http://testserver", token="", tisma_engine=ENGINE, work_dir=tmp_path / "work", gpu=False,
                     timeout_s=600)
    user = f.user()
    agent = agent_for(client, f.node("n", "slice"), cfg)
    job = f.submit(user, data=box_project())
    agent.run_once()
    view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
    print(f"bare model: {view['status']} {view.get('error')} {view.get('summary')}")
    assert view["status"] == "done", view
    assert b"G1 " in client.get(f"/v1/jobs/{job['id']}/result", headers=user).content


def test_a_master_address_without_its_scheme_is_refused_at_start(tmp_path):
    """«192.168.1.10:8000» can never be reached (no http://): the node says so instead of retrying for ever."""
    for url, ok in (("192.168.1.10:8000", False), ("http://", False), ("http://192.168.1.10:8000", True),
                    ("https://tismaservidores.net", True)):
        path = tmp_path / "node.toml"
        path.write_text(f'master_url = "{url}"\ntoken = "tn_x"\ntisma_engine = "e"\n')
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


# The stable codes of a failed calculation (cloud.md, rule 19a).
ENGINE_CODES = {"bad_mode", "bad_quality", "not_in_cloud", "needs_conditions", "no_valid_result", "usage", "error"}


@pytest.mark.skipif(not ENGINE, reason="set TISMA_ENGINE_BIN")
def test_real_engine_keeps_the_contract_of_its_other_commands(client, f, tmp_path):
    """orient, aero and analyze of the real engine, as a black box (cloud.md, rules 19 to 19c): each one either ends
    with its result and the summary its rule lists, or fails with a stable code and a text for the person."""
    cfg = NodeConfig(master_url="http://testserver", token="", tisma_engine=ENGINE, work_dir=tmp_path / "work", gpu=False, timeout_s=600)
    user = f.user(plan=f.plan("big", concurrent=10))
    agent = agent_for(client, f.node("n", "fea_optimize,fea,aero,orient"), cfg)
    project = box_project({"layers": {"height": 0.2}})
    expected = {"orient": ({"cost", "cost_before", "candidates"}, ".json"),
                "aero": ({"drag_n", "cd", "reynolds"}, ".taero"),
                "fea": (set(), ".tea")}
    problems: list[str] = []
    for kind, params in (("orient", {}), ("aero", {"quality": "fast", "speed": 10, "direction": "x"}),
                         ("fea", {"mode": "analyze", "quality": "fast"})):
        job = f.submit(user, kind=kind, data=project, params=json.dumps(params))
        agent.run_once()
        view = client.get(f"/v1/jobs/{job['id']}", headers=user).json()
        keys, suffix = expected[kind]
        summary = view.get("summary") or {}
        print(f"{kind}: {view['status']} {view.get('error')} {summary}")
        if view["status"] == "done":
            if not view["result_filename"].endswith(suffix) or not keys <= set(summary):
                problems.append(f"{kind}: done without its result or summary: {view['result_filename']} {summary}")
        elif view["status"] == "failed":
            if summary.get("code") not in ENGINE_CODES or not view.get("error"):
                problems.append(f"{kind}: failed without a stable code: {view.get('error')} {summary}")
        else:
            problems.append(f"{kind}: {view['status']}")
    assert not problems, problems
