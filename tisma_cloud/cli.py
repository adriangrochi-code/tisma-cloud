"""tisma-cloud: one program for the master server, a computing node and the administration. It is the executable built
by the CI (PyInstaller), to run the service on a machine without Docker or Python. Without arguments it opens the window
of the service (gui.py); over SSH the same is done with these commands, or with the menu of "tisma-cloud console":

    tisma-cloud role master|node        the role of this machine: the central server or a computing node
    tisma-cloud start | stop | restart  the service, in the background
    tisma-cloud status                  running or stopped, its role and port
    tisma-cloud logs [N]                the last N lines of its log (40)
    tisma-cloud config [KEY [VALUE]]    show the settings, or change one (port, TISMA_* of the master, keys of a node)
    tisma-cloud autostart on|off        start the service with the machine
    tisma-cloud firewall on|off         open the port of the master in the Windows firewall
    tisma-cloud check [--fix]           check what the machine needs to serve, and fix what can be fixed
    tisma-cloud console                 a menu with all of the above (for SSH)
    tisma-cloud panel                   the control panel of the master in a window (servers, queue, jobs, users)
    tisma-cloud admin <command> ...     administration of the master (as tisma-cloud-admin: users, nodes, plans...)
    tisma-cloud run                     the service in the foreground (what start runs)
    tisma-cloud --home DIR ...          use the files of the service in DIR
    tisma-cloud master [--host H] [--port P] [--env .env]   the master in the foreground
    tisma-cloud node [--config node.toml]                   a node in the foreground

The files of the service (settings, log) are in the folder of the program, or in TISMA_HOME."""

from __future__ import annotations

import io
import os
import sys
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path

USAGE = __doc__.split("\n\n", 1)[1]


def load_env(path: str | os.PathLike) -> int:
    """Sets the variables of a .env file (KEY=VALUE lines, # comments, optional quotes) which are not set yet."""
    file = Path(path)
    if not file.is_file():
        return 0
    count = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def take_option(args: list[str], name: str, default: str) -> str:
    """Removes "--name value" (or --name=value) from args and returns the value."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            value = args[i + 1]
            del args[i:i + 2]
            return value
        if a.startswith(name + "="):
            del args[i]
            return a.split("=", 1)[1]
    return default


class _NoHealthChecks:
    """Keeps the checks of /v1/health (the window asks every few seconds) out of the access log."""

    def filter(self, record) -> bool:  # noqa: ANN001
        return "/v1/health" not in record.getMessage()


def raise_open_files(wanted: int) -> int:
    """Raises the limit of open files of the process (each connection is one) up to wanted, within what the system
    allows. Returns the limit in force (0: unknown, e.g. on Windows)."""
    try:
        import resource
    except ImportError:
        return 0
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        except (ValueError, OSError):
            pass
    return soft


def serve_master(host: str, port: int) -> int:
    import logging

    import uvicorn

    from .api import create_app
    from .settings import Settings

    logging.getLogger("uvicorn.access").addFilter(_NoHealthChecks())

    # The port, for the public address made when TISMA_PUBLIC_URL is empty (publicip.py).
    os.environ["TISMA_PORT"] = str(port)
    settings = Settings()
    # Room for the connections plus the database, the files being sent and the log.
    files = raise_open_files(settings.max_connections + 1000)
    if 0 < files < settings.max_connections + 100:
        logging.getLogger("tisma_cloud").warning("Only %d open files allowed: fewer than TISMA_MAX_CONNECTIONS=%d",
                                                 files, settings.max_connections)
    # As deploy/Dockerfile.master: behind Caddy, short keep-alive.
    uvicorn.run(create_app(settings), host=host, port=port, proxy_headers=True, forwarded_allow_ips="*",
                limit_concurrency=settings.max_connections, timeout_keep_alive=5, server_header=False, backlog=4096,
                access_log=settings.access_log)
    return 0


def run_master(args: list[str]) -> int:
    host = take_option(args, "--host", "0.0.0.0")
    port = int(take_option(args, "--port", "8000"))
    load_env(take_option(args, "--env", ".env"))
    if args:
        print(f"Unknown arguments: {' '.join(args)}\n\n{USAGE}", file=sys.stderr)
        return 1
    return serve_master(host, port)


def run_admin(manager, args: list[str]) -> int:
    from .admin import main as admin_main

    load_env(take_option(args, "--env", str(manager.env_path)))
    return admin_main(args)


def admin_output(manager, args: list[str]) -> tuple[int, str]:
    """An administration command with its output, for the window and the console."""
    out = io.StringIO()
    # In the folder of the service, as its process: the relative paths of .env (a SQLite file) are the same.
    with chdir(manager.home), redirect_stdout(out), redirect_stderr(out):
        try:
            code = run_admin(manager, list(args))
        except SystemExit as e:  # argparse
            code = int(e.code or 0)
        except Exception as e:  # noqa: BLE001
            print(f"Error: {e}")
            code = 1
    return code, out.getvalue()


def print_status(manager) -> None:
    st = manager.status()
    role = st.role and {"master": "maestro", "node": "nodo"}[st.role] or "sin rol"
    print(f"Tisma Cloud ({role}): {st.detail}")
    print(f"Carpeta: {manager.home}")


def show_config(manager) -> None:
    from .manager import MASTER_FIELDS, NODE_FIELDS

    role = manager.role()
    if role == "master":
        env = manager.read_env()
        print(f"port = {manager.port()}")
        for key, label, secret in MASTER_FIELDS:
            value = env.get(key, "")
            print(f"{key} = {'********' if secret and value else value}    # {label}")
    elif role == "node":
        node = manager.read_node()
        for key, label, _kind, secret in NODE_FIELDS:
            value = node.get(key, "")
            print(f"{key} = {'********' if secret and value else value}    # {label}")
    else:
        print("Sin rol: tisma-cloud role master|node")


def console_nodes(manager, ask) -> None:
    """Console: what each node of the master does (its job classes in order) and enabling it."""
    from .jobs import JOB_CLASSES
    from .manager import JOB_CLASS_NAMES

    if manager.role() != "master":
        print("Los nodos se manejan en el maestro.")
        return
    nodes = manager.nodes()
    if not nodes:
        print("No hay nodos: agregalos con Administración > node-add NOMBRE --priorities slice,fea")
        return
    for i, n in enumerate(nodes, 1):
        state = ("en línea" if n["online"] else "sin señal") + ("" if n["enabled"] else ", deshabilitado")
        print(f" {i}) {n['name']:16} {state:24} hace: {', '.join(JOB_CLASS_NAMES[p] for p in n['priorities'])}")
    pick = ask("Nodo a cambiar (Enter: ninguno): ")
    if not pick.isdigit() or not 1 <= int(pick) <= len(nodes):
        return
    node = nodes[int(pick) - 1]
    for i, c in enumerate(JOB_CLASSES, 1):
        print(f" {i}) {JOB_CLASS_NAMES[c]} ({c})")
    order = ask("Qué hace, en orden de prioridad (números separados por comas, por ejemplo 3,1), "
                "d: deshabilitar, h: habilitar: ").lower()
    if order == "d":
        manager.set_node(node["name"], enabled=False)
    elif order == "h":
        manager.set_node(node["name"], enabled=True)
    elif order:
        picks = [int(x) for x in order.replace(" ", "").split(",") if x.isdigit()]
        manager.set_node(node["name"], priorities=[JOB_CLASSES[p - 1] for p in picks if 1 <= p <= len(JOB_CLASSES)])
    print("Guardado: vale desde su próximo trabajo.")


def console(manager) -> int:
    """A text menu for SSH (or any terminal) with the actions of the window."""
    from .manager import MASTER_FIELDS, NODE_FIELDS

    def ask(prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            return "0"

    while True:
        print()
        print_status(manager)
        print(" 1) Estado          2) Iniciar        3) Detener        4) Reiniciar")
        print(" 5) Elegir rol      6) Configuración  7) Registro       8) Administración")
        print(" 9) Inicio automático con la máquina  10) Nodos: qué hace cada uno")
        print("11) Diagnóstico     12) Firewall de Windows                          0) Salir")
        choice = ask("> ")
        try:
            if choice in ("0", "q", "salir"):
                return 0
            if choice == "1":
                continue
            if choice == "2":
                print(manager.start().detail)
            elif choice == "3":
                print(manager.stop().detail)
            elif choice == "4":
                print(manager.restart().detail)
            elif choice == "5":
                role = ask("Rol: 1) maestro (servidor central)  2) nodo de cómputo (esclavo): ")
                if role in ("1", "2"):
                    manager.set_role("master" if role == "1" else "node")
            elif choice == "6":
                show_config(manager)
                fields = ([("port", "Puerto")] + [(k, l) for k, l, _s in MASTER_FIELDS]
                          if manager.role() == "master" else [(f[0], f[1]) for f in NODE_FIELDS])
                for i, (key, label) in enumerate(fields, 1):
                    print(f" {i:2}) {label} ({key})")
                pick = ask("Número a cambiar (Enter: ninguno): ")
                if pick.isdigit() and 1 <= int(pick) <= len(fields):
                    key = fields[int(pick) - 1][0]
                    manager.set_setting(key, ask(f"{key} = "))
                    print("Guardado. Reiniciá el servicio para usarlo.")
            elif choice == "7":
                print("\n".join(manager.logs(40)) or "(sin registro)")
            elif choice == "8":
                if manager.role() != "master":
                    print("La administración es del maestro.")
                    continue
                print("Comandos: user-list, user-add EMAIL --plan P [--admin] [--unlimited] [--password],")
                print("          user-set EMAIL [--unlimited|--limited] [--paid-until AAAA-MM-DD], node-list,")
                print("          node-add NOMBRE --priorities slice,fea, node-set, plan-list, plan-set, queue")
                line = ask("admin> ")
                if line:
                    code, output = admin_output(manager, line.split())
                    print(output.rstrip() or f"(código {code})")
            elif choice == "10":
                console_nodes(manager, ask)
            elif choice == "11":
                if print_checks(manager) and ask("¿Arreglar lo que se pueda? (s/n): ").lower().startswith("s"):
                    print_checks(manager, fix=True)
            elif choice == "12":
                on = manager.firewall_open()
                if on is None:
                    print("Solo en Windows (en Linux ver deploy/firewall.nft).")
                elif ask(f"Puerto {manager.port()} {'abierto' if on else 'cerrado'} en el firewall. ¿Cambiar? (s/n): "
                         ).lower().startswith("s"):
                    manager.set_firewall(not on)
                    print("Abierto." if not on else "Cerrado.")
            elif choice == "9":
                on = manager.autostart_enabled()
                if ask(f"Inicio automático {'activado' if on else 'desactivado'}. ¿Cambiar? (s/n): ").lower().startswith("s"):
                    manager.set_autostart(not on)
                    print("Activado." if not on else "Desactivado.")
        except Exception as e:  # noqa: BLE001
            print(f"Error: {e}")


CHECK_MARKS = {True: "✔", False: "✖", None: "!"}


def print_checks(manager, fix: bool = False) -> int:
    """The diagnosis of the machine; with fix, what can be fixed is fixed first. 1 when something is wrong."""
    checks = manager.checks()
    if fix:
        for c in checks:
            if c.ok is False and c.fix:
                try:
                    print(f"Arreglando «{c.name}»: {manager.fix(c.fix)}")
                except Exception as e:  # noqa: BLE001
                    print(f"No se pudo arreglar «{c.name}»: {e}")
        checks = manager.checks()
    for c in checks:
        print(f" {CHECK_MARKS[c.ok]} {c.name}: {c.detail}" + (" (tisma-cloud check --fix)" if c.ok is False and c.fix
                                                            and not fix else ""))
    return 1 if any(c.ok is False for c in checks) else 0


PANEL_WITHOUT_WEBVIEW2 = (
    "Este Windows no tiene WebView2, el motor de la ventana del panel (Windows Server 2019 y 2022 no lo traen).\n"
    "- Instalá «Microsoft Edge WebView2 Runtime» (gratis, de Microsoft): "
    "https://developer.microsoft.com/microsoft-edge/webview2/\n"
    "- O abrí el panel desde el navegador de otra PC: http://<IP de este servidor>:{port}/admin "
    "(con el puerto abierto en el firewall: tisma-cloud firewall on)")


def open_panel(manager) -> int:
    """The administration panel of the master (/admin) in a window of its own, signed in: on Windows with the Edge
    browser engine of the system (pywebview), elsewhere, or without it, in the browser."""
    import webbrowser

    if manager.role() != "master":
        print("El panel es del maestro.", file=sys.stderr)
        return 1
    if not manager.health():
        print("El maestro no está en marcha: tisma-cloud start", file=sys.stderr)
        return 1
    if os.name == "nt":
        from .system import webview2_version

        # Windows Server 2019 and 2022 have neither WebView2 nor a modern browser (Internet Explorer cannot show
        # the panel).
        if webview2_version() is None:
            print(PANEL_WITHOUT_WEBVIEW2.format(port=manager.port()), file=sys.stderr)
            return 2
    url = manager.panel_url()
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        return 0
    try:
        webview.create_window("Tisma Cloud · Panel", url, width=1280, height=900, min_size=(900, 600))
        webview.start()
    except Exception as e:  # noqa: BLE001
        print(f"La ventana del panel no se pudo abrir ({e}).\n" + PANEL_WITHOUT_WEBVIEW2.format(port=manager.port()),
              file=sys.stderr)
        return 2
    return 0


def open_gui(manager) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as e:  # no tkinter
        print(f"The window is not available ({e}); use the commands or tisma-cloud console.\n\n{USAGE}",
              file=sys.stderr)
        return 1
    return gui_main(manager)


def main(argv: list[str] | None = None) -> int:
    from .manager import Manager

    args = list(sys.argv[1:] if argv is None else argv)
    # The folder of the service, before the command (the scheduled task and the supervisor pass it).
    if len(args) >= 2 and args[0] == "--home":
        os.environ["TISMA_HOME"] = args[1]
        args = args[2:]
    manager = Manager()
    command, rest = (args[0], args[1:]) if args else ("gui", [])
    # The commands of the service work in its folder (relative paths of .env, node.toml).
    if command not in ("master", "node", "-h", "--help", "help"):
        os.chdir(manager.home)
    if command == "gui":
        return open_gui(manager)
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        if command == "role":
            if not rest:
                print(manager.role() or "(sin rol)")
                return 0
            manager.set_role(rest[0])
            print_status(manager)
            return 0
        if command == "start":
            print(manager.start().detail)
            return 0 if manager.status().running else 1
        if command == "stop":
            print(manager.stop().detail)
            return 0
        if command == "restart":
            print(manager.restart().detail)
            return 0 if manager.status().running else 1
        if command == "status":
            print_status(manager)
            return 0 if manager.status().running else 3
        if command == "logs":
            print("\n".join(manager.logs(int(rest[0]) if rest else 40)))
            return 0
        if command == "config":
            if len(rest) >= 2:
                manager.set_setting(rest[0], " ".join(rest[1:]))
            show_config(manager)
            return 0
        if command == "autostart":
            if rest:
                manager.set_autostart(rest[0] in ("on", "1", "si", "sí", "yes"))
            print("on" if manager.autostart_enabled() else "off")
            return 0
        if command == "firewall":
            if rest:
                manager.set_firewall(rest[0] in ("on", "1", "si", "sí", "yes"))
            on = manager.firewall_open()
            print("(solo en Windows)" if on is None else "on" if on else "off")
            return 0
        if command == "check":
            return print_checks(manager, fix="--fix" in rest)
        if command == "console":
            return console(manager)
        if command == "panel":
            return open_panel(manager)
        if command == "run":
            return manager.run(child="--child" in rest, local_node="--local-node" in rest)
        if command == "master":
            return run_master(rest)
        if command == "node":
            from .node.__main__ import main as node_main

            return node_main(rest)
        if command == "admin":
            return run_admin(manager, rest)
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Unknown command {command}\n\n{USAGE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
