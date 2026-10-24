"""What the service needs from the operating system of the machine, mostly Windows (Windows Server): the scheduled task
that starts it with the machine, the rule of the Windows firewall for the port of the master, the browser engine of
the panel window (WebView2) and the programs of Tisma Slicer a computing node runs.

Sources:
- Task Scheduler schema: https://learn.microsoft.com/windows/win32/taskschd/task-scheduler-schema
- Firewall cmdlets (NetSecurity module): https://learn.microsoft.com/powershell/module/netsecurity/
- Detecting the WebView2 Runtime: https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution
- Inno Setup uninstall key "<AppId>_is1" with InstallLocation: https://jrsoftware.org/ishelp/ (Uninstall registry key)
- GetConsoleMode fails for a handle that is not a console: https://learn.microsoft.com/windows/console/getconsolemode
- Memory of Windows: GlobalMemoryStatusEx, https://learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-globalmemorystatusex
- Memory of Linux: MemTotal and MemAvailable of /proc/meminfo, https://man7.org/linux/man-pages/man5/proc_meminfo.5.html
- The executable of PyInstaller and the processes it starts (PYINSTALLER_RESET_ENVIRONMENT, LD_LIBRARY_PATH_ORIG):
  https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

FIREWALL_RULE = "Tisma Cloud"
# AppId of the installer of Tisma Slicer (packaging/windows/TismaSlicer.iss).
TISMA_SLICER_APP_ID = "{EF9BF87F-A56B-4EBB-B89B-4C957B7E3899}"
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
# Programs a node runs: the console slicer and the structural analysis.
# They come with the package of Tisma Cloud, in this folder next to the executable (server.yml): the Windows package of
# Tisma Slicer, or its AppImage extracted on Linux (usr/bin).
BUNDLED_FOLDER = "tisma-slicer"
SLICER_NAMES = ("tisma-slicer-console.exe", "tisma-slicer.exe") if os.name == "nt" else ("tisma-slicer",)
FEA_NAMES = ("tisma-fea.exe",) if os.name == "nt" else ("tisma-fea",)

# Windows: no console window for the helper programs.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:  # another drive
        return False


def program_env(**extra: str) -> dict[str, str]:
    """The environment of another program started by this one. The executable of PyInstaller adds the folder of its
    unpacked files to the search of libraries of its process (LD_LIBRARY_PATH on Linux, the original value kept in
    LD_LIBRARY_PATH_ORIG; PATH by some of its hooks), which the programs it starts would inherit and then load its
    libraries instead of their own: those changes are undone ("Launching External Programs from the Frozen
    Application")."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
        else:
            env.pop("LD_LIBRARY_PATH", None)
        unpacked = getattr(sys, "_MEIPASS", None)
        if unpacked and "PATH" in env:
            env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p and not _inside(p, unpacked))
    env.update(extra)
    return env


def own_env(**extra: str) -> dict[str, str]:
    """The environment of a new instance of this program that lives on after the one that starts it (the service
    started from the window, the window of the panel). The executable of PyInstaller unpacks its files in a temporary
    folder that is deleted when it ends, and since PyInstaller 6.9 a process of the same executable uses the folder of
    the one that started it unless PYINSTALLER_RESET_ENVIRONMENT is 1: without it the service fails as soon as the
    window is closed ("Failed to start embedded python interpreter")."""
    env = program_env(**extra)
    if getattr(sys, "frozen", False):
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("env", program_env())
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, **kwargs)


def _powershell(script: str) -> subprocess.CompletedProcess:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script])


# ---- the scheduled task (Windows)
def is_terminal(stream) -> bool:  # noqa: ANN001
    """Whether the stream is a terminal. On Windows isatty() is also true for the NUL device, the output of the service
    started in the background: there only a console counts."""
    try:
        if stream is None or not stream.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))
    except (AttributeError, OSError, ValueError):
        return False


def task_xml(command: str, arguments: str, working_dir: str, description: str) -> str:
    """A task that runs at the start of the machine as SYSTEM, whoever signs in or not, and keeps running.

    schtasks /Create /SC ONSTART leaves the defaults of the Task Scheduler: the task is stopped after 3 days
    (ExecutionTimeLimit PT72H) and runs below normal priority (7). Here: no time limit, normal priority (5), started
    late if the machine was off at boot time, restarted every minute if it fails, never twice at the same time."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def create_task(name: str, cmd: list[str], working_dir: Path, description: str) -> None:
    """Creates (or replaces) the scheduled task. Needs an administrator."""
    xml = task_xml(cmd[0], subprocess.list2cmdline(cmd[1:]), str(working_dir), description)
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        # schtasks reads the definition as UTF-16 with its byte order mark.
        Path(path).write_text(xml, encoding="utf-16")
        r = _run(["schtasks", "/Create", "/F", "/TN", name, "/XML", path])
    finally:
        os.unlink(path)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "schtasks failed (run as administrator)")


def delete_task(name: str) -> None:
    r = _run(["schtasks", "/Delete", "/F", "/TN", name])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "schtasks failed (run as administrator)")


def task_definition(name: str) -> str | None:
    """The XML of the task, or None when it does not exist."""
    r = _run(["schtasks", "/Query", "/XML", "/TN", name])
    return r.stdout if r.returncode == 0 else None


def task_problems(xml: str) -> list[str]:
    """What is wrong in a task made by an older version (schtasks /SC ONSTART): its defaults stop the service."""
    problems = []
    if "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" not in xml:
        problems.append("se detiene a los 3 días")
    if "<RestartOnFailure>" not in xml:
        problems.append("no se reinicia si falla")
    return problems


def run_task(name: str) -> None:
    r = _run(["schtasks", "/Run", "/TN", name])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "schtasks /Run failed")


def end_task(name: str) -> None:
    _run(["schtasks", "/End", "/TN", name])


# ---- the Windows firewall
def firewall_port() -> int | None:
    """The TCP port opened by the rule of Tisma Cloud in the Windows firewall, or None (no rule, or not Windows)."""
    if os.name != "nt":
        return None
    r = _powershell(f"(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue | "
                    "Get-NetFirewallPortFilter).LocalPort")
    value = r.stdout.strip().splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else ""
    return int(value) if value.isdigit() else None


def set_firewall(port: int | None) -> None:
    """Opens the TCP port of the master to the network in the Windows firewall (one inbound rule), or closes it
    (None). Needs an administrator."""
    if os.name != "nt":
        raise RuntimeError("The firewall rule is made by Tisma Cloud only on Windows (on Linux see deploy/firewall.nft)")
    script = f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue"
    if port is not None:
        script += (f"; New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Description 'Tisma Cloud master' "
                   f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {int(port)} -Profile Any | Out-Null")
    r = _powershell(script)
    if r.returncode != 0 or (port is not None and firewall_port() != int(port)):
        raise RuntimeError((r.stderr or r.stdout).strip() or "The firewall rule could not be changed "
                                                             "(run as administrator)")


# ---- the panel window
def webview2_version() -> str | None:
    """The version of the WebView2 Runtime (the engine of the panel window on Windows), or None when it is not
    installed (Windows Server 2019 and 2022 do not include it). Not Windows: None."""
    if os.name != "nt":
        return None
    import winreg

    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}")]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return str(version)
    return None


# ---- the programs of Tisma Slicer (computing nodes)
def tisma_slicer_folders() -> list[Path]:
    """Folders where the programs of Tisma Slicer may be, most likely first: the ones that come with this program
    (tisma-slicer/ next to it), next to it, where the installer of Tisma Slicer put it (for all users or for one), the
    default folders."""
    folders: list[Path] = []
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        folders += [here / BUNDLED_FOLDER, here / BUNDLED_FOLDER / "usr" / "bin", here]
    if os.name == "nt":
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{TISMA_SLICER_APP_ID}_is1",
                                        0, winreg.KEY_READ | view) as key:
                        location, _ = winreg.QueryValueEx(key, "InstallLocation")
                        folders.append(Path(location))
                except OSError:
                    pass
        for env in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                folders.append(Path(base) / ("Programs" if env == "LOCALAPPDATA" else "") / "Tisma Slicer")
    else:
        folders += [Path("/opt/tisma-slicer"), Path("/usr/local/bin"), Path("/usr/bin")]
    seen, out = set(), []
    for f in folders:
        key = str(f).lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def find_program(names: tuple[str, ...]) -> str | None:
    for folder in tisma_slicer_folders():
        for name in names:
            path = folder / name
            if path.is_file():
                if os.name != "nt" and not os.access(path, os.X_OK):
                    # Unpacked from a ZIP (the package made by the CI keeps no permissions).
                    try:
                        path.chmod(path.stat().st_mode | 0o111)
                    except OSError:
                        pass
                return str(path)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_tisma_programs() -> tuple[str | None, str | None]:
    """(tisma-slicer, tisma-fea) of this machine, or None for the ones not found."""
    return find_program(SLICER_NAMES), find_program(FEA_NAMES)


def resources(folder: Path) -> dict:
    """The machine: processors, load (average of the last minute, not on Windows), memory and the disk of [folder]
    (total and free, in GB). What cannot be read is left out."""
    out: dict = {"cpus": os.cpu_count() or 1}
    if hasattr(os, "getloadavg"):
        try:
            out["load"] = round(os.getloadavg()[0], 2)
        except OSError:
            pass
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            out["memory_gb"] = round(status.ullTotalPhys / 2 ** 30, 1)
            out["memory_free_gb"] = round(status.ullAvailPhys / 2 ** 30, 1)
    else:
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                info = {line.split(":")[0]: int(line.split()[1]) for line in f if line.split()[1:2]}
            out["memory_gb"] = round(info["MemTotal"] / 2 ** 20, 1)
            out["memory_free_gb"] = round(info.get("MemAvailable", info.get("MemFree", 0)) / 2 ** 20, 1)
        except (OSError, KeyError, ValueError):
            pass
    try:
        disk = shutil.disk_usage(folder)
        out["disk_gb"] = round(disk.total / 2 ** 30, 1)
        out["disk_free_gb"] = round(disk.free / 2 ** 30, 1)
    except OSError:
        pass
    return out
