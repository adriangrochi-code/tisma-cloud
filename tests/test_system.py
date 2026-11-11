"""What the service needs from the operating system (system.py), the supervisor and the diagnosis (manager.py)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from tisma_cloud import cli, manager as manager_module, system
from tisma_cloud.manager import Manager

TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_task_definition_keeps_the_service_running():
    xml = system.task_xml(r"C:\Tisma Cloud\tisma-cloud.exe", r'--home "C:\Tisma & Co" run', r"C:\Tisma & Co", "Tisma <Cloud>")
    root = ET.fromstring(xml.split("\n", 1)[1])  # without the declaration of UTF-16 (the file is written as UTF-16)
    settings = root.find("t:Settings", TASK_NS)
    # No limit (schtasks /SC ONSTART stops it after 3 days), normal priority, started again when it fails.
    assert settings.find("t:ExecutionTimeLimit", TASK_NS).text == "PT0S"
    assert settings.find("t:Priority", TASK_NS).text == "5"
    assert settings.find("t:RestartOnFailure/t:Count", TASK_NS).text == "999"
    assert settings.find("t:DisallowStartIfOnBatteries", TASK_NS).text == "false"
    assert root.find("t:Principals/t:Principal/t:UserId", TASK_NS).text == "S-1-5-18"  # SYSTEM
    assert root.find("t:Triggers/t:BootTrigger", TASK_NS) is not None
    exec_ = root.find("t:Actions/t:Exec", TASK_NS)
    assert exec_.find("t:Command", TASK_NS).text == r"C:\Tisma Cloud\tisma-cloud.exe"
    assert exec_.find("t:Arguments", TASK_NS).text == r'--home "C:\Tisma & Co" run'
    assert exec_.find("t:WorkingDirectory", TASK_NS).text == r"C:\Tisma & Co"
    assert system.task_problems(xml) == []
    # A task made by schtasks /SC ONSTART (the older versions) has the defaults.
    old = xml.replace("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT72H</ExecutionTimeLimit>")
    old = old[:old.index("<RestartOnFailure>")] + old[old.index("</RestartOnFailure>") + len("</RestartOnFailure>"):]
    assert len(system.task_problems(old)) == 2


def test_run_command_with_the_folder(tmp_path):
    cmd = manager_module.run_command(tmp_path, "--child")
    assert cmd[-4:] == ["--home", str(tmp_path), "run", "--child"]
    assert manager_module.run_command()[-1] == "run"


def test_home_option_of_the_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TISMA_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "service"
    assert cli.main(["--home", str(home), "role", "node"]) == 0
    assert (home / "tisma-cloud.toml").read_text(encoding="utf-8").count('role = "node"') == 1


def test_relative_home(tmp_path, monkeypatch):
    """TISMA_HOME=service-home: the commands change to the folder and still find its files."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TISMA_HOME", "service-home")
    assert cli.main(["role", "master"]) == 0
    # Each command is a process of its own, started in the folder of the user.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["config", "port", "18999"]) == 0
    assert Manager(tmp_path / "service-home").port() == 18999
    assert (tmp_path / "service-home" / "tisma-cloud.toml").is_file()


def test_supervisor_starts_the_service_again(tmp_path, monkeypatch, capsys):
    """The role crashes twice, then ends cleanly: the supervisor starts it three times and ends."""
    m = Manager(tmp_path)
    m.set_role("node")
    counter = tmp_path / "runs"
    script = tmp_path / "role.py"
    script.write_text("import pathlib, sys\n"
                      f"p = pathlib.Path({str(counter)!r})\n"
                      "n = int(p.read_text()) + 1 if p.exists() else 1\n"
                      "p.write_text(str(n))\n"
                      "sys.exit(0 if n >= 3 else 7)\n", encoding="utf-8")
    monkeypatch.setattr(manager_module, "run_command", lambda home=None, *a: [sys.executable, str(script)])
    # The clock of the supervisor goes forward by what it sleeps (and the role runs for real meanwhile).
    clock, starts = [0.], []
    real_sleep, real_popen = time.sleep, subprocess.Popen

    def sleep(s: float) -> None:
        clock[0] += s
        real_sleep(0.005)

    def popen(*args, **kwargs):  # noqa: ANN002, ANN003
        starts.append(clock[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(manager_module.time, "sleep", sleep)
    monkeypatch.setattr(manager_module.subprocess, "Popen", popen)
    assert m._supervise() == 0
    assert counter.read_text() == "3"
    # Started again 5 s after the first error and 10 s after the second one.
    assert len(starts) == 3 and 5. <= starts[1] - starts[0] < 60. and 10. <= starts[2] - starts[1] < 60.
    out = capsys.readouterr().out
    assert "arranca de nuevo en 5 s" in out and "arranca de nuevo en 10 s" in out


def test_processes_of_the_executable(tmp_path, monkeypatch):
    """The executable of PyInstaller: the programs it starts do not get the libraries of its unpacked files, and a new
    instance of itself (the service, the panel) unpacks its own files instead of using those of the window."""
    unpacked = str(tmp_path / "_MEI123")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", unpacked, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", unpacked + os.pathsep + "/opt/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/lib")
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.join(unpacked, "bin"), "/usr/bin"]))
    env = system.program_env(TISMA_FEA_GPU="0")
    assert env["LD_LIBRARY_PATH"] == "/opt/lib" and "LD_LIBRARY_PATH_ORIG" not in env
    assert env["PATH"] == "/usr/bin" and env["TISMA_FEA_GPU"] == "0"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in env
    assert system.own_env()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in system.program_env()
    # Started from the window, the service does not depend on the files of the window (deleted when it is closed).
    m = Manager(tmp_path / "home")
    m.set_role("node")
    seen = {}
    monkeypatch.setattr(m, "autostart_enabled", lambda: False)
    monkeypatch.setattr(manager_module.subprocess, "Popen", lambda cmd, env=None, **kwargs: seen.update(env=env))
    m.start(wait_s=0)
    assert seen["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert seen["env"]["TISMA_HOME"] == str(tmp_path / "home")
    # Run from Python, nothing changes.
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lib")
    assert system.own_env()["LD_LIBRARY_PATH"] == "/opt/lib" and "PYINSTALLER_RESET_ENVIRONMENT" not in system.own_env()


def test_programs_come_with_the_package(tmp_path, monkeypatch):
    """tisma-engine/ and tisma-scan/ next to the executable: the programs of a node made with the package."""
    exe = tmp_path / "Tisma Cloud" / "tisma-cloud.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    m = Manager(tmp_path / "home")
    m.set_role("node")
    assert system.find_engine_program() is None and not m.read_node().get("tisma_engine")
    engine = exe.parent.resolve() / system.ENGINE_FOLDER / system.ENGINE_NAMES[0]
    engine.parent.mkdir()
    engine.write_text("")
    assert system.find_engine_program() == str(engine)
    assert m.detect_programs() == {"tisma_engine": str(engine)}
    # Found again after its folder moved (the service looks when it starts).
    m.write_node({"tisma_engine": str(tmp_path / "old" / "tisma-engine")})
    m.children()
    assert m.read_node()["tisma_engine"] == str(engine)
    # A path typed by the user which exists is kept.
    other = tmp_path / "other-engine"
    other.write_text("")
    m.set_setting("tisma_engine", str(other))
    assert m.detect_programs() == {}
    assert m.read_node()["tisma_engine"] == str(other)
    # Without the folder of the scans the node makes none; with it (TismaScan-windows unpacked next to the program), it
    # finds its tisma_scan.
    assert system.find_scan_program() is None and not m.read_node().get("tisma_scan")
    scan = exe.parent.resolve() / system.SCAN_FOLDER / system.SCAN_NAMES[0]
    scan.parent.mkdir()
    scan.write_text("")
    assert system.find_scan_program() == str(scan)
    assert m.detect_programs() == {"tisma_scan": str(scan)}
    assert m.read_node()["tisma_scan"] == str(scan)
    # The translation tables of the profile pack (tisma-profiles/translation, deploy/fetch_translation.py).
    assert system.find_translation_dir() is None
    tables = exe.parent.resolve() / system.PROFILES_FOLDER / "translation"
    tables.mkdir(parents=True)
    assert system.find_translation_dir() is None  # a folder without tables
    (tables / "prusaslicer.json").write_text("{}")
    assert m.detect_programs() == {"translation_dir": str(tables)}
    assert m.read_node()["translation_dir"] == str(tables)
    assert {c.name: c for c in m.checks()}["Tablas de traducción"].ok is True


def test_diagnosis_of_a_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system, "find_engine_program", lambda: None)
    m = Manager(tmp_path)
    m.set_role("node")
    m.set_setting("master_url", "http://127.0.0.1:9")  # nothing listens there
    checks = {c.name: c for c in m.checks()}
    assert checks["Programa tisma-engine"].ok is False and checks["Programa tisma-engine"].fix == "programs"
    assert checks["Maestro"].ok is False
    # Without a token the node asks the master to join: a warning, not a fault.
    assert checks["Token del nodo"].ok is None and "pide unirse" in checks["Token del nodo"].detail
    assert checks["Carpeta de trabajo"].ok is True
    with pytest.raises(RuntimeError):
        m.fix("programs")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--home", str(tmp_path), "check"]) == 1
    out = capsys.readouterr().out
    assert "✖ Maestro" in out and "tisma-cloud check --fix" in out


@pytest.mark.skipif(sys.platform == "win32", reason="the firewall rule is only made on Windows")
def test_firewall_is_windows_only(tmp_path):
    m = Manager(tmp_path)
    m.set_role("master")
    assert m.firewall_open() is None
    assert all(c.name != "Firewall de Windows" for c in m.checks())
    with pytest.raises(RuntimeError):
        m.set_firewall(True)


def test_the_null_device_is_no_terminal():
    # The output of the service started in the background (Windows takes NUL for a terminal in isatty()).
    import os

    with open(os.devnull, "w") as null:
        assert not system.is_terminal(null)
    assert not system.is_terminal(None)
