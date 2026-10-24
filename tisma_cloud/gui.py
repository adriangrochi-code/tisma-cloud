"""The window of Tisma Cloud on the screen of a server: what the master and its nodes are doing (state, nodes, jobs,
users), the role of the machine (master or computing node), its settings, starting and stopping the service, the
diagnosis and the log. tkinter (part of Python), so that the executable needs nothing else.

The data is read apart from the window (the database, the health of the master, the log) and shown when it arrives,
so the window never waits for them."""

from __future__ import annotations

import os
import sys
import threading
from datetime import date
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
import tkinter as tk
import tkinter.font as tkfont

from .manager import JOB_CLASS_NAMES, MASTER_FIELDS, NODE_FIELDS, ROLE_NAMES, Manager
from .node.config import auto_slots, memory_gb

# The violet of Tisma, ink and surfaces. The status colours are only for states and always come with a word.
VIOLET = "#7A24C9"
VIOLET_HOVER = "#8E34DF"
VIOLET_TRACK = "#ECE3F8"
INK = "#1D1B22"
MUTED = "#6B6B76"
SURFACE = "#F6F5FA"
CARD = "#FFFFFF"
BORDER = "#E4E1EC"
SIDEBAR = "#1E1530"
SIDEBAR_TEXT = "#CFC6E0"
SIDEBAR_ACTIVE = "#33245A"
CONSOLE = "#16121F"
GOOD, WARNING, CRITICAL, OFF = "#0CA30C", "#FAB219", "#D03B3B", "#9A9AA6"
STATUS_COLORS = {"running": GOOD, "starting": WARNING, "stopped": OFF}
JOB_STATES = {"queued": ("En cola", OFF), "running": ("En curso", VIOLET), "done": ("Terminado", GOOD),
              "failed": ("Falló", CRITICAL), "cancelled": ("Cancelado", OFF)}
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

# The pages, in the order of the side bar; the first three only on the master.
PAGES = (("state", "Estado"), ("nodes", "Nodos"), ("jobs", "Trabajos"), ("users", "Usuarios"),
         ("settings", "Configuración"), ("checks", "Diagnóstico"), ("log", "Registro y consola"))
MASTER_PAGES = ("nodes", "jobs", "users")


def _hide_own_console() -> None:
    """Windows: the window was opened with a double click; the console the executable opened is not needed."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    processes = (ctypes.c_uint * 4)()
    if kernel32.GetConsoleProcessList(processes, 4) == 1:  # only this process uses the console: it is its own
        kernel32.FreeConsole()


def _sharp_on_windows() -> None:
    """Windows: drawn at the resolution of the screen, not enlarged and blurred (SetProcessDpiAwareness)."""
    if os.name != "nt":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def number(value: float, decimals: int = 0) -> str:
    """As written in Spanish: 1.284 and 12,5."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def short_time(iso: str) -> str:
    """2026-10-23T12:31:05 as 23/10 12:31."""
    return f"{iso[8:10]}/{iso[5:7]} {iso[11:16]}" if len(iso) >= 16 else iso


def money(payments: list[dict]) -> str:
    if not payments:
        return "0"
    return " + ".join(f"{p['currency']} {number(p['amount_cents'] / 100, 2)}" for p in payments)


class Tile:
    """A number with its label and a line of context, on a card."""

    def __init__(self, app: App, parent: tk.Widget, label: str) -> None:
        self.frame = app.card(parent)
        inner = tk.Frame(self.frame, bg=CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=12)
        tk.Label(inner, text=label, bg=CARD, fg=MUTED, font=app.f_small, anchor="w").pack(fill="x")
        self.value = tk.Label(inner, text="–", bg=CARD, fg=INK, font=app.f_value, anchor="w")
        self.value.pack(fill="x", pady=(2, 0))
        self.sub = tk.Label(inner, text="", bg=CARD, fg=MUTED, font=app.f_small, anchor="w", justify="left")
        self.sub.pack(fill="x")
        # The line of context wraps in the width of the card.
        inner.bind("<Configure>", lambda e: self.sub.config(wraplength=max(80, e.width)))

    def set(self, value: str, sub: str = "") -> None:
        self.value.config(text=value)
        self.sub.config(text=sub)


class Meter:
    """How full something is: the bar is violet, then amber from 80 % and red from 90 %, on a track of the same violet."""

    def __init__(self, app: App, parent: tk.Widget, label: str) -> None:
        self.frame = tk.Frame(parent, bg=CARD)
        top = tk.Frame(self.frame, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=label, bg=CARD, fg=INK, font=app.f_body).pack(side="left")
        self.text = tk.Label(top, text="", bg=CARD, fg=MUTED, font=app.f_small)
        self.text.pack(side="right")
        self.bar = tk.Canvas(self.frame, height=8, bg=CARD, highlightthickness=0)
        self.bar.pack(fill="x", pady=(4, 0))
        self.fraction = 0.
        self.bar.bind("<Configure>", lambda _e: self.draw())

    def set(self, used: float, total: float, text: str) -> None:
        self.fraction = max(0., min(1., used / total)) if total > 0 else 0.
        self.text.config(text=text)
        self.draw()

    def draw(self) -> None:
        c = self.bar
        c.delete("all")
        w = max(c.winfo_width(), 10)
        c.create_rectangle(0, 0, w, 8, fill=VIOLET_TRACK, width=0)
        color = CRITICAL if self.fraction >= .9 else WARNING if self.fraction >= .8 else VIOLET
        if self.fraction > 0:
            c.create_rectangle(0, 0, max(2, w * self.fraction), 8, fill=color, width=0)


class DaysChart:
    """The jobs done on each of the last 7 days: one bar per day, today labelled; the day under the pointer is told
    in the line below (with the failed ones)."""

    def __init__(self, app: App, parent: tk.Widget) -> None:
        self.app = app
        self.canvas = tk.Canvas(parent, height=150, bg=CARD, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.caption = tk.Label(parent, text="", bg=CARD, fg=MUTED, font=app.f_small, anchor="w")
        self.caption.pack(fill="x", pady=(6, 0))
        self.days: list[dict] = []
        self.slots: list[tuple[float, float]] = []
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<Leave>", lambda _e: self.tell(None))

    def set(self, days: list[dict]) -> None:
        self.days = days
        self.draw()
        self.tell(None)

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self.slots = []
        if not self.days:
            return
        w, h = max(c.winfo_width(), 100), 150
        top, base = 18, h - 22
        top_value = max(1, max(d["done"] for d in self.days))
        slot = w / len(self.days)
        bar = min(28., slot * .5)
        c.create_line(0, base, w, base, fill=BORDER)
        for i, d in enumerate(self.days):
            x0 = i * slot + (slot - bar) / 2
            x1 = x0 + bar
            self.slots.append((i * slot, (i + 1) * slot))
            height = (base - top) * d["done"] / top_value
            if height > 0:
                y0 = base - height
                r = min(4., bar / 2, height)
                # Rounded at the top only, standing on the base line.
                c.create_rectangle(x0, y0 + r, x1, base, fill=VIOLET, width=0)
                c.create_rectangle(x0 + r, y0, x1 - r, y0 + r, fill=VIOLET, width=0)
                c.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=VIOLET, width=0)
                c.create_oval(x1 - 2 * r, y0, x1, y0 + 2 * r, fill=VIOLET, width=0)
            day = date.fromisoformat(d["day"])
            c.create_text((x0 + x1) / 2, base + 11, text=WEEKDAYS[day.weekday()], fill=MUTED, font=self.app.f_small)
            if i == len(self.days) - 1:
                c.create_text((x0 + x1) / 2, base - height - 9, text=number(d["done"]), fill=INK, font=self.app.f_small)

    def hover(self, event: tk.Event) -> None:
        for i, (a, b) in enumerate(self.slots):
            if a <= event.x < b:
                return self.tell(i)
        self.tell(None)

    def tell(self, i: int | None) -> None:
        if i is None:
            done = sum(d["done"] for d in self.days)
            failed = sum(d["failed"] for d in self.days)
            self.caption.config(text=f"{number(done)} terminados y {number(failed)} fallidos en 7 días" if self.days else "")
            return
        d = self.days[i]
        day = date.fromisoformat(d["day"])
        self.caption.config(text=f"{WEEKDAYS[day.weekday()]} {day.day}/{day.month}: {number(d['done'])} terminados, "
                                 f"{number(d['failed'])} fallidos")


class App:
    def __init__(self, root: tk.Tk, manager: Manager) -> None:
        self.root = root
        self.m = manager
        self.fields: dict[str, tk.Variable] = {}
        self.data: dict = {}
        self.loading = False
        self.page = ""
        root.title("Tisma Cloud")
        root.geometry("1180x760")
        root.minsize(980, 640)
        root.configure(bg=SURFACE)

        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_body = tkfont.Font(family=family, size=10)
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_bold = tkfont.Font(family=family, size=10, weight="bold")
        self.f_section = tkfont.Font(family=family, size=12, weight="bold")
        self.f_title = tkfont.Font(family=family, size=18, weight="bold")
        self.f_value = tkfont.Font(family=family, size=20, weight="bold")
        self.f_brand = tkfont.Font(family=family, size=15, weight="bold")
        self.f_mono = tkfont.Font(family=tkfont.nametofont("TkFixedFont").actual("family"), size=9)
        self.style()

        self.build_sidebar()
        main = tk.Frame(root, bg=SURFACE)
        main.pack(side="left", fill="both", expand=True)
        self.build_header(main)
        self.body = tk.Frame(main, bg=SURFACE)
        self.body.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        self.pages: dict[str, tk.Frame] = {}
        for key, _label in PAGES:
            self.pages[key] = tk.Frame(self.body, bg=SURFACE)
        self.build_state()
        self.build_nodes()
        self.build_jobs()
        self.build_users()
        self.build_settings()
        self.build_checks()
        self.build_log()
        self.show("state")
        threading.Thread(target=self.load_autostart, daemon=True).start()
        self.refresh()

    # ---- look
    def style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=self.f_body, background=SURFACE)
        style.configure("TFrame", background=SURFACE)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=SURFACE, foreground=INK)
        style.configure("Card.TLabel", background=CARD, foreground=INK)
        style.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED)
        style.configure("TButton", padding=(14, 6), background=CARD, foreground=INK, bordercolor=BORDER,
                        lightcolor=CARD, darkcolor=CARD, focuscolor=CARD)
        style.map("TButton", background=[("active", "#F1ECF8"), ("disabled", SURFACE)],
                  foreground=[("disabled", OFF)])
        style.configure("Accent.TButton", background=VIOLET, foreground="white", bordercolor=VIOLET, lightcolor=VIOLET,
                        darkcolor=VIOLET, focuscolor=VIOLET)
        style.map("Accent.TButton", background=[("active", VIOLET_HOVER), ("disabled", "#C9C9D2")],
                  bordercolor=[("disabled", "#C9C9D2")], lightcolor=[("disabled", "#C9C9D2")],
                  darkcolor=[("disabled", "#C9C9D2")])
        style.configure("Card.TCheckbutton", background=CARD, foreground=INK)
        style.map("Card.TCheckbutton", background=[("active", CARD)])
        style.configure("Card.TRadiobutton", background=CARD, foreground=INK)
        style.map("Card.TRadiobutton", background=[("active", CARD)])
        style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=5)
        style.map("TEntry", bordercolor=[("focus", VIOLET)], lightcolor=[("focus", VIOLET)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK, rowheight=30, borderwidth=0,
                        bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        style.map("Treeview", background=[("selected", "#EFE6FB")], foreground=[("selected", INK)])
        style.configure("Treeview.Heading", background=CARD, foreground=MUTED, font=self.f_small, relief="flat",
                        borderwidth=0, padding=(6, 6), bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        style.map("Treeview.Heading", background=[("active", CARD)])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)

    def section(self, parent: tk.Widget, title: str, note: str = "") -> tk.Frame:
        """A card with a title: returns where its content goes."""
        box = self.card(parent)
        head = tk.Frame(box, bg=CARD)
        head.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(head, text=title, bg=CARD, fg=INK, font=self.f_section).pack(side="left")
        if note:
            tk.Label(head, text=note, bg=CARD, fg=MUTED, font=self.f_small).pack(side="right")
        inner = tk.Frame(box, bg=CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        box.inner = inner  # type: ignore[attr-defined]
        return box

    def table(self, parent: tk.Widget, columns: tuple[tuple[str, str, int], ...], height: int = 10,
              first: tuple[str, int] | None = None) -> ttk.Treeview:
        """A list with columns (key, title, width): the first one is the tree column when given."""
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="both", expand=True)
        view = ttk.Treeview(frame, columns=[c[0] for c in columns], height=height, selectmode="browse",
                            show="tree headings" if first else "headings")
        if first:
            view.heading("#0", text=first[0], anchor="w")
            view.column("#0", width=first[1], stretch=False)
        for key, title, width in columns:
            view.heading(key, text=title, anchor="w")
            view.column(key, width=width, stretch=width >= 200, anchor="w")
        bar = ttk.Scrollbar(frame, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=bar.set)
        view.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        view.tag_configure("muted", foreground=MUTED)
        view.tag_configure("bad", foreground=CRITICAL)
        view.tag_configure("good", foreground=GOOD)
        view.tag_configure("warn", foreground="#9A6B00")
        return view

    # ---- side bar and header
    def build_sidebar(self) -> None:
        side = tk.Frame(self.root, bg=SIDEBAR, width=212)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="Tisma Cloud", bg=SIDEBAR, fg="white", font=self.f_brand, anchor="w").pack(
            fill="x", padx=20, pady=(22, 0))
        self.role_label = tk.Label(side, text="", bg=SIDEBAR, fg=SIDEBAR_TEXT, font=self.f_small, anchor="w")
        self.role_label.pack(fill="x", padx=20, pady=(2, 18))
        self.nav: dict[str, tk.Label] = {}
        for key, label in PAGES:
            item = tk.Label(side, text=label, bg=SIDEBAR, fg=SIDEBAR_TEXT, font=self.f_body, anchor="w", padx=20,
                            pady=9, cursor="hand2")
            item.pack(fill="x", padx=8, pady=1)
            item.bind("<Button-1>", lambda _e, k=key: self.show(k))
            item.bind("<Enter>", lambda _e, k=key: self.nav_color(k, hover=True))
            item.bind("<Leave>", lambda _e, k=key: self.nav_color(k))
            self.nav[key] = item
        home = str(self.m.home)
        tk.Label(side, text=home if len(home) <= 34 else "…" + home[-33:], bg=SIDEBAR, fg="#8D84A0",
                 font=self.f_small, anchor="w", wraplength=180, justify="left").pack(side="bottom", fill="x",
                                                                                      padx=20, pady=(6, 18))
        self.panel_button = tk.Label(side, text="Abrir el panel web", bg=VIOLET, fg="white", font=self.f_bold,
                                     pady=8, cursor="hand2")
        self.panel_button.pack(side="bottom", fill="x", padx=16)
        self.panel_button.bind("<Button-1>", lambda _e: self.open_panel())
        self.panel_button.bind("<Enter>", lambda _e: self.panel_button.config(bg=VIOLET_HOVER))
        self.panel_button.bind("<Leave>", lambda _e: self.panel_button.config(bg=VIOLET))

    def nav_color(self, key: str, hover: bool = False) -> None:
        item = self.nav[key]
        if str(item.cget("state")) == "disabled":
            return
        active = key == self.page
        item.config(bg=SIDEBAR_ACTIVE if active or hover else SIDEBAR, fg="white" if active else SIDEBAR_TEXT)

    def build_header(self, main: tk.Frame) -> None:
        head = tk.Frame(main, bg=SURFACE)
        head.pack(fill="x", padx=24, pady=(18, 14))
        self.title = tk.Label(head, text="", bg=SURFACE, fg=INK, font=self.f_title)
        self.title.pack(side="left")
        self.buttons: dict[str, ttk.Button] = {}
        for key, label in (("restart", "Reiniciar"), ("stop", "Detener"), ("start", "Iniciar")):
            b = ttk.Button(head, text=label, command=lambda k=key: self.service(k),
                           style="Accent.TButton" if key == "start" else "TButton")
            b.pack(side="right", padx=(6, 0))
            self.buttons[key] = b
        pill = tk.Frame(head, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        pill.pack(side="right", padx=(0, 10))
        self.dot = tk.Canvas(pill, width=12, height=12, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(12, 6), pady=8)
        self.status_text = tk.Label(pill, text="", bg=CARD, fg=INK, font=self.f_body)
        self.status_text.pack(side="left", padx=(0, 12))

    def show(self, key: str) -> None:
        if str(self.nav[key].cget("state")) == "disabled":
            return
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.page = key
        self.title.config(text=dict(PAGES)[key])
        for k in self.nav:
            self.nav_color(k)

    # ---- state
    def build_state(self) -> None:
        page = self.pages["state"]
        # No role yet: the choice.
        self.welcome = self.card(page)
        tk.Label(self.welcome, text="¿Qué es esta máquina?", bg=CARD, fg=INK, font=self.f_section).pack(
            anchor="w", padx=20, pady=(18, 4))
        tk.Label(self.welcome, text="El maestro es el servidor central: recibe a los usuarios y reparte los trabajos. "
                 "Un nodo de cómputo lamina y calcula para el maestro.", bg=CARD, fg=MUTED, font=self.f_body,
                 wraplength=760, justify="left").pack(anchor="w", padx=20)
        choices = tk.Frame(self.welcome, bg=CARD)
        choices.pack(anchor="w", padx=20, pady=(14, 20))
        ttk.Button(choices, text="Es el maestro", style="Accent.TButton",
                   command=lambda: self.choose_role("master")).pack(side="left")
        ttk.Button(choices, text="Es un nodo de cómputo", command=lambda: self.choose_role("node")).pack(side="left",
                                                                                                         padx=8)

        # Master and node: the alerts, the numbers, then the details.
        self.alerts = tk.Frame(page, bg="#FCEDED", highlightthickness=1, highlightbackground="#F2C4C4")
        self.alerts_text = tk.Label(self.alerts, text="", bg="#FCEDED", fg=INK, font=self.f_body, justify="left",
                                    anchor="w", wraplength=880)
        self.alerts_text.pack(fill="x", padx=16, pady=10)
        self.tiles_frame = tk.Frame(page, bg=SURFACE)
        for c in range(4):
            self.tiles_frame.columnconfigure(c, weight=1, uniform="tiles")
        self.master_tiles = {key: Tile(self, self.tiles_frame, label) for key, label in (
            ("nodes", "Nodos en línea"), ("running", "Trabajos en curso"), ("today", "Terminados hoy"),
            ("hours", "Horas de cómputo este mes"), ("users", "Usuarios"), ("money", "Cobrado este mes"))}
        self.node_tiles = {key: Tile(self, self.tiles_frame, label) for key, label in (
            ("master", "Maestro"), ("programs", "Programas de Tisma Slicer"), ("slots", "Trabajos a la vez"),
            ("gpu", "Placa de video"))}
        machine = self.section(self.tiles_frame, "Esta máquina")
        self.machine_box = machine
        self.cpu_text = tk.Label(machine.inner, text="", bg=CARD, fg=MUTED, font=self.f_small, anchor="w")
        self.cpu_text.pack(fill="x", pady=(0, 6))
        self.memory = Meter(self, machine.inner, "Memoria")
        self.memory.frame.pack(fill="x", pady=(0, 8))
        self.disk = Meter(self, machine.inner, "Disco")
        self.disk.frame.pack(fill="x")

        self.details = tk.Frame(page, bg=SURFACE)
        self.details.columnconfigure(0, weight=3, uniform="details")
        self.details.columnconfigure(1, weight=2, uniform="details")
        recent = self.section(self.details, "Últimos trabajos")
        recent.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.recent_view = self.table(recent.inner, (("state", "Estado", 110), ("what", "Qué", 200),
                                                    ("user", "Usuario", 150), ("node", "Nodo", 60)), height=8)
        chart = self.section(self.details, "Terminados por día")
        chart.grid(row=0, column=1, sticky="nsew")
        self.chart = DaysChart(self, chart.inner)
        # The node: its diagnosis.
        self.node_checks = self.section(page, "Lo que necesita este nodo")
        self.node_checks_rows = tk.Frame(self.node_checks.inner, bg=CARD)
        self.node_checks_rows.pack(fill="x")

    def layout_state(self, role: str, alerts: list[dict]) -> None:
        """The parts of the state page for the role of the machine (again only when they change)."""
        if role == "master" and alerts:
            self.alerts_text.config(text="Alertas abiertas: " + " · ".join(a["message"] for a in alerts[:4]))
        key = (role, bool(alerts))
        if key == getattr(self, "state_layout", None):
            return
        self.state_layout = key
        for w in (self.welcome, self.alerts, self.tiles_frame, self.details, self.node_checks):
            w.pack_forget()
        for tile in (*self.master_tiles.values(), *self.node_tiles.values()):
            tile.frame.grid_forget()
        self.machine_box.grid_forget()
        if not role:
            self.welcome.pack(fill="x")
            return
        if role == "master" and alerts:
            self.alerts.pack(fill="x", pady=(0, 12))
        self.tiles_frame.pack(fill="x")
        tiles = list(self.master_tiles.values()) if role == "master" else list(self.node_tiles.values())
        for i, tile in enumerate(tiles):
            tile.frame.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 12, 0), pady=(0, 12))
        # The machine takes the rest of the last row (two columns on the master, a whole row on a node).
        row, column = divmod(len(tiles), 4)
        self.machine_box.grid(row=row, column=column, columnspan=4 - column, sticky="nsew",
                              padx=(0 if column == 0 else 12, 0), pady=(0, 12))
        if role == "master":
            self.details.pack(fill="both", expand=True)
        else:
            self.node_checks.pack(fill="x")

    def show_state(self) -> None:
        d = self.data
        role = d.get("role", "")
        board = d.get("board") or {}
        self.layout_state(role, board.get("alerts", []))
        res = d.get("resources") or {}
        cpu = f"{res.get('cpus', '?')} procesadores"
        if "load" in res:
            cpu += f" · carga {number(res['load'], 2)}"
        self.cpu_text.config(text=cpu)
        if "memory_gb" in res:
            used = res["memory_gb"] - res.get("memory_free_gb", 0)
            self.memory.set(used, res["memory_gb"], f"{number(used, 1)} de {number(res['memory_gb'], 1)} GB")
        if "disk_gb" in res:
            used = res["disk_gb"] - res["disk_free_gb"]
            self.disk.set(used, res["disk_gb"], f"{number(res['disk_free_gb'], 1)} GB libres de "
                                                f"{number(res['disk_gb'], 1)} GB")
        if role == "master":
            self.show_master_state(board)
        elif role == "node":
            self.show_node_state()

    def show_master_state(self, b: dict) -> None:
        t = self.master_tiles
        if "error" in b:
            t["nodes"].set("–", f"No se pudo leer la base de datos: {b['error']}")
            return
        nodes = b.get("nodes", [])
        online = sum(1 for n in nodes if n["online"] and n["enabled"])
        working = sum(1 for n in nodes if n["running"])
        silent = [n["name"] for n in nodes if n["enabled"] and not n["online"]]
        asking = len(b.get("requests", []))
        t["nodes"].set(f"{online} de {len(nodes)}", f"{asking} piden unirse: aceptalos en Nodos" if asking else
                       "Agregá un nodo en Nodos" if not nodes else
                       f"{working} trabajando" + (f" · sin señal: {', '.join(silent)}" if silent else ""))
        t["running"].set(number(b.get("running", 0)), f"{number(b.get('queued', 0))} en cola")
        t["today"].set(number(b.get("done_today", 0)), f"{number(b.get('failed_today', 0))} fallaron")
        t["hours"].set(f"{number(b.get('hours_month', 0), 1)} h", "cobradas a los planes")
        t["users"].set(number(b.get("users", 0)), f"{number(b.get('new_users', 0))} nuevos esta semana · "
                                                   f"{number(b.get('paying_users', 0))} con plan pago")
        payments = b.get("payments_month", [])
        t["money"].set(money(payments), f"{number(sum(p['count'] for p in payments))} pagos")
        self.fill(self.recent_view, [
            (j["id"], (JOB_STATES.get(j["status"], (j["status"], OFF))[0] +
                       (f" {j['progress']} %" if j["status"] == "running" else ""),
                       j["input"] or JOB_CLASS_NAMES.get(j["job_class"], j["kind"]), j["user"], j["node"] or "–"),
             "bad" if j["status"] == "failed" else "muted" if j["status"] in ("queued", "cancelled") else "")
            for j in b.get("jobs", [])[:8]])
        self.chart.set(b.get("days", []))

    @staticmethod
    def node_state(n: dict) -> tuple[str, str]:
        if not n["enabled"]:
            return "deshabilitado", OFF
        if not n["online"]:
            return "sin señal", CRITICAL
        if n["running"]:
            return f"trabajando ({n['running']})", VIOLET
        return "en línea, libre", GOOD

    def show_node_state(self) -> None:
        node = self.data.get("node") or {}
        checks = {c.name: c for c in self.checks}
        master = checks.get("Maestro")
        self.node_tiles["master"].set("Responde" if master and master.ok else "No responde" if master else "…",
                                      str(node.get("master_url", "")))
        programs = [checks.get(k) for k in ("Programa tisma-slicer", "Programa tisma-fea")]
        found = sum(1 for c in programs if c and c.ok)
        self.node_tiles["programs"].set(f"{found} de 2", "tisma-slicer y tisma-fea" if found == 2 else
                                        "Configuración > Buscar Tisma Slicer")
        slots = int(node.get("slots", 0))
        self.node_tiles["slots"].set(number(slots) if slots > 0 else f"Auto ({number(auto_slots(os.cpu_count() or 1, memory_gb()))})",
                                     f"hasta {number(int(node.get('timeout_s', 0)))} s cada uno")
        self.node_tiles["gpu"].set("Sí" if node.get("gpu") else "No", "para el análisis estructural")
        for child in self.node_checks_rows.winfo_children():
            child.destroy()
        for c in self.checks:
            self.check_row(self.node_checks_rows, c)

    def check_row(self, parent: tk.Widget, c) -> None:  # noqa: ANN001
        word, color = {True: ("Bien", GOOD), False: ("Falta", CRITICAL), None: ("Aviso", WARNING)}[c.ok]
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=3)
        dot = tk.Canvas(row, width=10, height=10, bg=CARD, highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=color, outline="")
        dot.pack(side="left", padx=(0, 8))
        tk.Label(row, text=f"{c.name} · {word}", bg=CARD, fg=INK, font=self.f_bold, width=30, anchor="w").pack(
            side="left")
        tk.Label(row, text=c.detail, bg=CARD, fg=MUTED, font=self.f_small, anchor="w", justify="left",
                 wraplength=600).pack(side="left", fill="x", expand=True)

    def choose_role(self, role: str) -> None:
        self.role.set(role)
        self.on_role()

    # ---- nodes: the machines that ask to join and what each node does
    def build_nodes(self) -> None:
        asking = self.section(self.pages["nodes"], "Piden unirse",
                              "Las máquinas de esta red con Tisma Cloud como nodo y sin token")
        asking.pack(fill="x", pady=(0, 12))
        self.requests_view = self.table(asking.inner, (("address", "Dirección", 130), ("machine", "Equipo", 300),
                                                       ("since", "Desde", 140)), height=3, first=("Máquina", 160))
        bar = tk.Frame(asking.inner, bg=CARD)
        bar.pack(fill="x", pady=(10, 0))
        self.requests_note = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=self.f_small, anchor="w")
        self.requests_note.pack(side="left")
        ttk.Button(bar, text="Aceptar", style="Accent.TButton", command=self.accept_request).pack(side="right")
        ttk.Button(bar, text="Rechazar", command=self.reject_request).pack(side="right", padx=6)
        self.request_list: list[dict] = []
        box = self.section(self.pages["nodes"], "Qué hace cada nodo",
                           "Doble clic en un nodo para elegir qué hace y en qué orden")
        box.pack(fill="both", expand=True)
        self.nodes_view = self.table(box.inner, (("state", "Estado", 150), ("does", "Qué hace, en orden de prioridad",
                                                 340), ("machine", "Máquina", 220)), first=("Nodo", 140))
        self.nodes_view.bind("<Double-1>", lambda _e: self.edit_node())
        bar = tk.Frame(box.inner, bg=CARD)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Qué hace…", command=self.edit_node).pack(side="left")
        ttk.Button(bar, text="Habilitar / deshabilitar", command=self.toggle_node).pack(side="left", padx=6)
        ttk.Button(bar, text="Eliminar", command=self.delete_node).pack(side="left")
        ttk.Button(bar, text="Agregar nodo a mano…", command=self.add_node).pack(side="right")
        self.node_list: list[dict] = []

    @staticmethod
    def machine_text(info: dict) -> str:
        return " · ".join(x for x in (info.get("host", ""), f"{info['cpus']} CPU" if info.get("cpus") else "",
                                      f"{number(info['memory_gb'], 1)} GB" if info.get("memory_gb") else "") if x)

    def show_nodes(self) -> None:
        board = self.data.get("board") or {}
        self.node_list = board.get("nodes", [])
        rows = []
        for n in self.node_list:
            state, _color = self.node_state(n)
            rows.append((n["name"], (state, " → ".join(JOB_CLASS_NAMES.get(p, p) for p in n["priorities"]),
                                     self.machine_text(n.get("info") or {}) or "–"),
                         "muted" if state == "deshabilitado" else ""))
        self.fill(self.nodes_view, rows, tree=True)
        self.request_list = board.get("requests", [])
        self.fill(self.requests_view, [
            (r["id"], (r["address"], self.machine_text(r.get("info") or {}) or "–", r["created_at"][:16].replace("T", " ")),
             "") for r in self.request_list], tree=True, texts={r["id"]: r["name"] for r in self.request_list})
        self.requests_note.config(text="Ninguna máquina pide unirse. En el nodo: rol «nodo», la dirección de este "
                                       "maestro y token vacío." if not self.request_list else
                                  "Aceptá las máquinas que reconozcas: van a recibir los trabajos de los usuarios.")

    def selected_request(self) -> dict | None:
        sel = self.requests_view.selection()
        req = next((r for r in self.request_list if sel and r["id"] == sel[0]), None)
        if req is None and len(self.request_list) == 1:
            req = self.request_list[0]
        if req is None:
            messagebox.showinfo("Tisma Cloud", "Elegí una máquina de la lista.", parent=self.root)
        return req

    def accept_request(self) -> None:
        req = self.selected_request()
        if req is None:
            return
        name = simpledialog.askstring("Aceptar nodo", f"Nombre del nodo para {req['name']} ({req['address']}):",
                                      initialvalue=req["name"], parent=self.root)
        if not name:
            return
        try:
            made = self.m.accept_join(req["id"], name)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", str(e), parent=self.root)
            return
        messagebox.showinfo("Tisma Cloud", f"{made} es un nodo: en unos segundos toma su token y empieza a trabajar. "
                            "Con «Qué hace…» elegís qué trabajos toma.", parent=self.root)
        self.refresh(reschedule=False)

    def reject_request(self) -> None:
        req = self.selected_request()
        if req is None or not messagebox.askyesno(
                "Tisma Cloud", f"¿Rechazar a {req['name']} ({req['address']})? Su servicio de nodo se detiene.",
                parent=self.root):
            return
        try:
            self.m.reject_join(req["id"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", str(e), parent=self.root)
        self.refresh(reschedule=False)

    def delete_node(self) -> None:
        node = self.selected_node()
        if node is None or not messagebox.askyesno(
                "Tisma Cloud", f"¿Eliminar el nodo {node['name']}? Sus trabajos en curso vuelven a la cola. Si la "
                "máquina sigue encendida pide unirse otra vez (rechazala para que no vuelva).", parent=self.root):
            return
        try:
            requeued = self.m.delete_node(node["name"])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", str(e), parent=self.root)
            return
        if requeued:
            messagebox.showinfo("Tisma Cloud", f"{requeued} trabajo(s) volvieron a la cola.", parent=self.root)
        self.refresh(reschedule=False)

    def add_node(self) -> None:
        """A node made by hand (a machine outside this network, or without asking): its token is shown once, to copy
        into its node.toml."""
        name = simpledialog.askstring("Agregar nodo", "Nombre del nodo (por ejemplo i7-1):", parent=self.root)
        if not name:
            return
        try:
            token = self.m.add_node(name)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", str(e), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Token de {name}")
        dialog.transient(self.root)
        dialog.configure(bg=SURFACE)
        ttk.Label(dialog, text="Copiá el token en la configuración del nodo (Token del nodo). Se muestra solo ahora.",
                  padding=(12, 10)).pack(anchor="w")
        # The text is put in the field itself (a StringVar of this function would be deleted when it returns).
        entry = ttk.Entry(dialog, width=60)
        entry.insert(0, token)
        entry.configure(state="readonly")
        entry.pack(fill="x", padx=12)

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(token)

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cerrar", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Copiar", style="Accent.TButton", command=copy).pack(side="right", padx=6)
        self.refresh(reschedule=False)

    def selected_node(self) -> dict | None:
        sel = self.nodes_view.selection()
        node = next((n for n in self.node_list if sel and n["name"] == sel[0]), None)
        if node is None:
            messagebox.showinfo("Tisma Cloud", "Elegí un nodo de la lista.", parent=self.root)
        return node

    def toggle_node(self) -> None:
        node = self.selected_node()
        if node:
            self.m.set_node(node["name"], enabled=not node["enabled"])
            self.refresh(reschedule=False)

    def edit_node(self) -> None:
        """What a node does: the job classes it takes, in order (the first ones first)."""
        node = self.selected_node()
        if node is None:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Qué hace {node['name']}")
        dialog.transient(self.root)
        dialog.configure(bg=SURFACE)
        ttk.Label(dialog, text="Marcá lo que hace este nodo y ordenalo: toma primero los trabajos de arriba.",
                  padding=(12, 10)).pack(anchor="w")
        order = node["priorities"] + [c for c in JOB_CLASS_NAMES if c not in node["priorities"]]
        checked = {c: tk.BooleanVar(value=c in node["priorities"]) for c in JOB_CLASS_NAMES}
        rows = ttk.Frame(dialog, padding=(12, 0))
        rows.pack(fill="x")

        def draw() -> None:
            for child in rows.winfo_children():
                child.destroy()
            for i, c in enumerate(order):
                ttk.Checkbutton(rows, text=JOB_CLASS_NAMES[c], variable=checked[c]).grid(row=i, column=0, sticky="w",
                                                                                         pady=2)
                ttk.Button(rows, text="↑", width=3, command=lambda i=i: move(i, -1)).grid(row=i, column=1, padx=2)
                ttk.Button(rows, text="↓", width=3, command=lambda i=i: move(i, 1)).grid(row=i, column=2)

        def move(i: int, step: int) -> None:
            j = i + step
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                draw()

        def save() -> None:
            chosen = [c for c in order if checked[c].get()]
            try:
                self.m.set_node(node["name"], priorities=chosen)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Tisma Cloud", str(e), parent=dialog)
                return
            dialog.destroy()
            self.refresh(reschedule=False)

        draw()
        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Guardar", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(buttons, text="Cancelar", command=dialog.destroy).pack(side="right", padx=6)

    # ---- jobs
    def build_jobs(self) -> None:
        box = self.section(self.pages["jobs"], "Trabajos", "Los que esperan, los que corren y los últimos")
        box.pack(fill="both", expand=True)
        self.queue_text = tk.Label(box.inner, text="", bg=CARD, fg=MUTED, font=self.f_small, anchor="w")
        self.queue_text.pack(fill="x", pady=(0, 8))
        self.jobs_view = self.table(box.inner, (("state", "Estado", 110), ("what", "Archivo", 200),
                                                ("kind", "Tipo", 140), ("user", "Usuario", 170), ("node", "Nodo", 60),
                                                ("time", "Tiempo", 60), ("created", "Pedido", 100)))
        self.job_error = tk.Label(box.inner, text="", bg=CARD, fg=CRITICAL, font=self.f_small, anchor="w",
                                  justify="left", wraplength=880)
        self.job_error.pack(fill="x", pady=(8, 0))
        self.jobs_view.bind("<<TreeviewSelect>>", lambda _e: self.show_job_error())
        self.jobs: dict[str, dict] = {}

    def show_jobs(self) -> None:
        b = self.data.get("board") or {}
        self.jobs = {j["id"]: j for j in b.get("jobs", [])}
        self.queue_text.config(text=f"{number(b.get('running', 0))} en curso · {number(b.get('queued', 0))} en cola · "
                                    f"hoy {number(b.get('done_today', 0))} terminados y "
                                    f"{number(b.get('failed_today', 0))} fallidos")
        self.fill(self.jobs_view, [
            (j["id"], (JOB_STATES.get(j["status"], (j["status"], OFF))[0] +
                       (f" {j['progress']} %" if j["status"] == "running" else ""),
                       j["input"] or "–", JOB_CLASS_NAMES.get(j["job_class"], j["kind"]), j["user"], j["node"] or "–",
                       f"{number(j['seconds'])} s" if j["seconds"] else "–", short_time(j["created_at"])),
             "bad" if j["status"] == "failed" else "muted" if j["status"] in ("queued", "cancelled") else "")
            for j in b.get("jobs", [])])

    def show_job_error(self) -> None:
        sel = self.jobs_view.selection()
        job = self.jobs.get(sel[0]) if sel else None
        self.job_error.config(text=f"Error: {job['error']}" if job and job["error"] else "")

    # ---- users
    def build_users(self) -> None:
        box = self.section(self.pages["users"], "Cuentas", "Las más nuevas primero")
        box.pack(fill="both", expand=True)
        self.users_view = self.table(box.inner, (("plan", "Plan", 80), ("paid", "Pagado hasta", 100),
                                                 ("hours", "Horas este mes", 115), ("kind", "Cuenta", 210),
                                                 ("created", "Alta", 90)), first=("Correo", 250))
        bar = tk.Frame(box.inner, bg=CARD)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Cuenta interna…", command=self.make_internal).pack(side="left")
        tk.Label(bar, text="Una cuenta interna lamina sin límite y sin cobro.", bg=CARD, fg=MUTED,
                 font=self.f_small).pack(side="left", padx=10)

    def show_users(self) -> None:
        rows = []
        for u in self.data.get("users") or []:
            kind = ", ".join(k for k, on in (("administrador", u["admin"]), ("interna sin cobro", u["unlimited"]),
                                             ("deshabilitada", not u["enabled"])) if on) or "normal"
            rows.append((u["email"], (u["plan"], u["paid_until"] or "–", number(u["hours_month"], 1), kind,
                                      u["created_at"]), "muted" if not u["enabled"] else ""))
        self.fill(self.users_view, rows, tree=True)

    def make_internal(self) -> None:
        email = simpledialog.askstring("Cuenta interna", "Correo de la cuenta (lamina sin límite y sin cobro):",
                                       parent=self.root)
        if email:
            self.admin(["user-set", email, "--unlimited"])
            self.refresh(reschedule=False)

    # ---- settings
    def build_settings(self) -> None:
        page = self.pages["settings"]
        for child in page.winfo_children():
            child.destroy()
        self.fields.clear()
        role_box = self.section(page, "Rol de esta máquina")
        role_box.pack(fill="x", pady=(0, 12))
        self.role = tk.StringVar(value=self.m.role())
        row = tk.Frame(role_box.inner, bg=CARD)
        row.pack(fill="x")
        for role in ("master", "node"):
            ttk.Radiobutton(row, text=ROLE_NAMES[role].capitalize(), value=role, variable=self.role,
                            style="Card.TRadiobutton", command=self.on_role).pack(side="left", padx=(0, 24))
        service = self.section(page, "Servicio")
        service.pack(fill="x", pady=(0, 12))
        self.autostart = getattr(self, "autostart", tk.BooleanVar(value=False))
        ttk.Checkbutton(service.inner, text="Iniciar con la máquina", variable=self.autostart, style="Card.TCheckbutton",
                        command=self.on_autostart).pack(anchor="w")
        # Windows: the port of the master in the firewall (Windows Server closes it by default).
        self.firewall = getattr(self, "firewall", tk.BooleanVar(value=False))
        if os.name == "nt" and self.m.role() == "master":
            ttk.Checkbutton(service.inner, text="Abrir el puerto en el firewall", variable=self.firewall,
                            style="Card.TCheckbutton", command=self.on_firewall).pack(anchor="w", pady=(4, 0))
        role = self.m.role()
        if not role:
            return
        box = self.section(page, "Ajustes del maestro" if role == "master" else "Ajustes del nodo",
                           "Los cambios se usan al reiniciar el servicio")
        box.pack(fill="x")
        form = tk.Frame(box.inner, bg=CARD)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        rows: list[tuple[str, str, object, bool]] = []
        if role == "master":
            env = self.m.read_env()
            rows.append(("port", "Puerto", self.m.port(), False))
            rows.append(("master_computes", "El maestro también calcula", self.m.master_computes(), False))
            rows += [(key, label, env.get(key, ""), secret) for key, label, secret in MASTER_FIELDS]
        else:
            node = self.m.read_node()
            rows += [(key, label, node.get(key, ""), secret) for key, label, _kind, secret in NODE_FIELDS]
        for r, (key, label, value, secret) in enumerate(rows):
            tk.Label(form, text=label, bg=CARD, fg=INK, font=self.f_body).grid(row=r, column=0, sticky="w", pady=4,
                                                                               padx=(0, 16))
            if isinstance(value, bool):
                var: tk.Variable = tk.BooleanVar(value=value)
                ttk.Checkbutton(form, variable=var, style="Card.TCheckbutton").grid(row=r, column=1, sticky="w")
            else:
                var = tk.StringVar(value=str(value))
                ttk.Entry(form, textvariable=var, show="•" if secret else "").grid(row=r, column=1, sticky="ew")
                if key in ("tisma_slicer", "tisma_fea"):
                    ttk.Button(form, text="Buscar…", command=lambda v=var: self.browse(v)).grid(row=r, column=2,
                                                                                               padx=(6, 0))
            self.fields[key] = var
        bottom = tk.Frame(box.inner, bg=CARD)
        bottom.pack(fill="x", pady=(14, 0))
        ttk.Button(bottom, text="Guardar", style="Accent.TButton", command=self.save).pack(side="right")
        if role == "node":
            ttk.Button(bottom, text="Buscar Tisma Slicer", command=self.find_programs).pack(side="right", padx=6)

    def find_programs(self) -> None:
        """The programs of Tisma Slicer installed on this machine, for the node."""
        found = self.m.detect_programs(replace=True)
        if found:
            self.build_settings()
            messagebox.showinfo("Tisma Cloud", "Encontrados:\n" + "\n".join(found.values()), parent=self.root)
        else:
            messagebox.showwarning("Tisma Cloud", "No se encontraron tisma-slicer y tisma-fea. Vienen en la carpeta "
                                   "tisma-slicer del paquete de Tisma Cloud: dejala junto a tisma-cloud.exe, o indicá "
                                   "los programas con «Buscar…».", parent=self.root)
        self.load_checks()

    def browse(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(parent=self.root)
        if path:
            var.set(path)

    def save(self) -> None:
        try:
            if self.m.role() == "master":
                self.m.set_port(int(self.fields["port"].get()))
                self.m.set_master_computes(bool(self.fields["master_computes"].get()))
                self.m.write_env({k: str(v.get()) for k, v in self.fields.items() if k not in ("port", "master_computes")})
            else:
                self.m.write_node({k: v.get() for k, v in self.fields.items()})
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", f"No se pudo guardar: {e}", parent=self.root)
            return
        if self.data.get("running") and messagebox.askyesno("Tisma Cloud", "Guardado. ¿Reiniciar el servicio ahora?",
                                                            parent=self.root):
            self.service("restart")

    # ---- diagnosis: what the machine needs to serve
    def build_checks(self) -> None:
        box = self.section(self.pages["checks"], "Lo que esta máquina necesita para dar servicio",
                           "Doble clic en un punto marcado «Falta» para arreglarlo")
        box.pack(fill="both", expand=True)
        self.check_view = self.table(box.inner, (("detail", "Estado", 600),), height=9, first=("Punto", 260))
        self.check_view.bind("<Double-1>", lambda _e: self.fix_selected())
        # The whole text of the chosen point (the list cuts long ones).
        self.check_detail = tk.Label(box.inner, text="", bg=CARD, fg=MUTED, font=self.f_small, wraplength=880,
                                     justify="left", anchor="w")
        self.check_detail.pack(fill="x", pady=(8, 0))
        self.check_view.bind("<<TreeviewSelect>>", lambda _e: self.show_check_detail())
        bar = tk.Frame(box.inner, bg=CARD)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="Arreglar", style="Accent.TButton", command=self.fix_selected).pack(side="left")
        ttk.Button(bar, text="Arreglar todo", command=self.fix_all).pack(side="left", padx=6)
        ttk.Button(bar, text="Volver a comprobar", command=self.load_checks).pack(side="right")
        tk.Label(box.inner, text="Algunos arreglos piden abrir Tisma Cloud como administrador.", bg=CARD, fg=MUTED,
                 font=self.f_small).pack(anchor="w", pady=(8, 0))
        self.checks: list = []
        self.load_checks()

    def load_checks(self) -> None:
        """The checks run apart (some ask the network or PowerShell), then the list is filled."""
        self.fill(self.check_view, [("wait", ("",), "muted")], tree=True, texts={"wait": "Comprobando…"})

        def work() -> None:
            try:
                checks = self.m.checks()
            except Exception as e:  # noqa: BLE001
                from .manager import Check

                checks = [Check("Diagnóstico", False, str(e))]
            self.root.after(0, lambda: self.show_checks(checks))

        threading.Thread(target=work, daemon=True).start()

    def show_checks(self, checks: list) -> None:
        self.checks = checks
        self.check_detail.config(text="")
        rows, texts = [], {}
        for i, c in enumerate(checks):
            word, tag = {True: ("Bien", "good"), False: ("Falta", "bad"), None: ("Aviso", "warn")}[c.ok]
            fix = "  (se puede arreglar)" if c.ok is False and c.fix else ""
            texts[str(i)] = f"{word} · {c.name}"
            rows.append((str(i), (c.detail + fix,), tag))
        self.fill(self.check_view, rows, tree=True, texts=texts)
        if self.data.get("role") == "node":
            self.show_node_state()

    def show_check_detail(self) -> None:
        sel = self.check_view.selection()
        if sel and sel[0].isdigit() and int(sel[0]) < len(self.checks):
            c = self.checks[int(sel[0])]
            self.check_detail.config(text=f"{c.name}: {c.detail}")

    def fix_selected(self) -> None:
        sel = self.check_view.selection()
        check = self.checks[int(sel[0])] if sel and sel[0].isdigit() and int(sel[0]) < len(self.checks) else None
        if check is None or not check.fix:
            messagebox.showinfo("Tisma Cloud", "Elegí un punto que se pueda arreglar.", parent=self.root)
            return
        self.run_fixes([check])

    def fix_all(self) -> None:
        self.run_fixes([c for c in self.checks if c.ok is False and c.fix])

    def run_fixes(self, checks: list) -> None:
        def work() -> None:
            done, errors = [], []
            for c in checks:
                try:
                    done.append(self.m.fix(c.fix))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{c.name}: {e}")
            self.root.after(0, lambda: self.after_fixes(done, errors))

        threading.Thread(target=work, daemon=True).start()

    def after_fixes(self, done: list[str], errors: list[str]) -> None:
        if errors:
            hint = "\n\nEn Windows, abrí Tisma Cloud como administrador (clic derecho > Ejecutar como administrador)."
            messagebox.showerror("Tisma Cloud", "\n".join(errors) + hint, parent=self.root)
        elif done:
            messagebox.showinfo("Tisma Cloud", "\n".join(done), parent=self.root)
        self.build_settings()
        threading.Thread(target=self.load_autostart, daemon=True).start()
        self.load_checks()
        self.refresh(reschedule=False)

    # ---- log and console of the master
    def build_log(self) -> None:
        page = self.pages["log"]
        box = self.section(page, "Registro del servicio")
        box.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(box.inner, height=12, font=self.f_mono, state="disabled", bg=CONSOLE,
                                             fg="#E6E1F0", insertbackground="white", relief="flat", borderwidth=8)
        self.log.pack(fill="both", expand=True)
        console = self.section(page, "Consola del maestro", "Los mismos comandos que tisma-cloud admin por SSH")
        console.pack(fill="x", pady=(12, 0))
        bar = tk.Frame(console.inner, bg=CARD)
        bar.pack(fill="x")
        for label, args in (("Nodos", ["node-list"]), ("Usuarios", ["user-list"]), ("Cola", ["queue"]),
                            ("Planes", ["plan-list"])):
            ttk.Button(bar, text=label, command=lambda a=args: self.admin(a)).pack(side="left", padx=(0, 6))
        line = tk.Frame(console.inner, bg=CARD)
        line.pack(fill="x", pady=(10, 6))
        self.admin_line = tk.StringVar()
        entry = ttk.Entry(line, textvariable=self.admin_line)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.admin(self.admin_line.get().split()))
        ttk.Button(line, text="Ejecutar", style="Accent.TButton",
                   command=lambda: self.admin(self.admin_line.get().split())).pack(side="left", padx=(6, 0))
        self.admin_out = scrolledtext.ScrolledText(console.inner, height=8, font=self.f_mono, bg=CONSOLE, fg="#E6E1F0",
                                                   insertbackground="white", relief="flat", borderwidth=8)
        self.admin_out.pack(fill="x")

    def admin(self, args: list[str]) -> None:
        from .cli import admin_output

        if self.m.role() != "master":
            messagebox.showinfo("Tisma Cloud", "La administración es del maestro.", parent=self.root)
            return
        if not args:
            return
        _code, output = admin_output(self.m, args)
        self.admin_out.delete("1.0", "end")
        self.admin_out.insert("end", f"> {' '.join(args)}\n{output}")

    def open_panel(self) -> None:
        """The panel of the master (servers and what each does, queue, jobs, users and internal accounts) in its own
        window, signed in as the administrator (tisma-cloud panel)."""
        import subprocess

        from .manager import run_command
        from .system import own_env

        if self.m.role() != "master" or not self.m.health():
            messagebox.showinfo("Tisma Cloud", "Iniciá el maestro para abrir el panel.", parent=self.root)
            return
        if os.name == "nt":
            from .cli import PANEL_WITHOUT_WEBVIEW2
            from .system import webview2_version

            if webview2_version() is None:
                messagebox.showwarning("Tisma Cloud", PANEL_WITHOUT_WEBVIEW2.format(port=self.m.port()),
                                       parent=self.root)
                return
        try:
            if not self.m.has_admin():
                email = simpledialog.askstring("Primer administrador", "Correo del administrador:", parent=self.root)
                if not email:
                    return
                password = simpledialog.askstring("Primer administrador", "Contraseña (8 caracteres o más):",
                                                  show="•", parent=self.root)
                if not password:
                    return
                self.m.create_admin(email, password)
            # The window of the panel runs in its own process (the engine of the window needs its main thread).
            subprocess.Popen(run_command()[:-1] + ["panel"], cwd=str(self.m.home), env=own_env())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", f"No se pudo abrir el panel: {e}", parent=self.root)

    # ---- role and service
    def on_role(self) -> None:
        role = self.role.get()
        if role == self.m.role():
            return
        try:
            self.m.set_role(role)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", str(e), parent=self.root)
            self.role.set(self.m.role())
            return
        self.build_settings()
        self.load_checks()
        # One refresh: the periodic one is already scheduled.
        self.refresh(reschedule=False)

    def service(self, action: str) -> None:
        for b in self.buttons.values():
            b.state(["disabled"])
        self.status_text.config(text={"start": "Iniciando…", "stop": "Deteniendo…", "restart": "Reiniciando…"}[action])

        def work() -> None:
            error = None
            try:
                getattr(self.m, action)()
            except Exception as e:  # noqa: BLE001
                error = str(e)
            self.root.after(0, lambda: self.after_service(error))

        threading.Thread(target=work, daemon=True).start()

    def after_service(self, error: str | None) -> None:
        if error:
            messagebox.showerror("Tisma Cloud", error, parent=self.root)
        self.refresh(reschedule=False)
        self.load_checks()

    def load_autostart(self) -> None:
        try:
            enabled = self.m.autostart_enabled()
        except Exception:  # noqa: BLE001
            enabled = False
        try:
            firewall = bool(self.m.firewall_open())
        except Exception:  # noqa: BLE001
            firewall = False
        self.root.after(0, lambda: (self.autostart.set(enabled), self.firewall.set(firewall)))

    def on_firewall(self) -> None:
        try:
            self.m.set_firewall(self.firewall.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", f"{e}\n\nEn Windows hay que abrir Tisma Cloud como administrador.",
                                 parent=self.root)
            self.firewall.set(not self.firewall.get())
        self.load_checks()

    def on_autostart(self) -> None:
        try:
            self.m.set_autostart(self.autostart.get())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Tisma Cloud", f"{e}\n\nEn Windows hay que abrir Tisma Cloud como administrador.",
                                 parent=self.root)
            self.autostart.set(not self.autostart.get())

    # ---- the data, read apart every 3 s
    def collect(self) -> dict:
        st = self.m.status()
        data: dict = {"role": st.role, "running": st.running, "healthy": st.healthy, "detail": st.detail,
                      "logs": self.m.logs(300), "resources": self.m.resources()}
        if st.role == "master":
            try:
                data["board"] = self.m.dashboard(last_jobs=60)
                data["users"] = self.m.users()
            except Exception as e:  # noqa: BLE001
                data["board"] = {"error": str(e)}
        elif st.role == "node":
            data["node"] = self.m.read_node()
        return data

    def refresh(self, reschedule: bool = True) -> None:
        if not self.loading:
            self.loading = True

            def work() -> None:
                try:
                    data = self.collect()
                except Exception as e:  # noqa: BLE001
                    data = {"role": self.m.role(), "running": False, "healthy": None, "detail": str(e), "logs": []}
                self.root.after(0, lambda: self.apply(data))

            threading.Thread(target=work, daemon=True).start()
        if reschedule:
            self.root.after(3000, self.refresh)

    def apply(self, data: dict) -> None:
        self.loading = False
        self.data = data
        role, running = data["role"], data["running"]
        state = "running" if running and (role != "master" or data["healthy"]) else "starting" if running else "stopped"
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 11, 11, fill=STATUS_COLORS[state], outline="")
        self.status_text.config(text=data["detail"])
        self.buttons["start"].state(["!disabled"] if role and not running else ["disabled"])
        self.buttons["stop"].state(["!disabled"] if running else ["disabled"])
        self.buttons["restart"].state(["!disabled"] if running else ["disabled"])
        self.role_label.config(text=ROLE_NAMES[role].capitalize() if role else "Sin rol todavía")
        for key in MASTER_PAGES:
            self.nav[key].config(state="normal" if role == "master" else "disabled",
                                 fg=SIDEBAR_TEXT if role == "master" else "#5E5476")
        if role != "master" and self.page in MASTER_PAGES:
            self.show("state")
        for key in self.nav:
            self.nav_color(key)
        if role == "master" and not self.panel_button.winfo_manager():
            self.panel_button.pack(side="bottom", fill="x", padx=16)
        elif role != "master":
            self.panel_button.pack_forget()
        self.show_state()
        if role == "master":
            self.show_nodes()
            self.show_jobs()
            self.show_users()
        at_end = self.log.yview()[1] > .99
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(data.get("logs") or []) or "(sin registro todavía)")
        if at_end:
            self.log.see("end")
        self.log.config(state="disabled")

    def fill(self, view: ttk.Treeview, rows: list[tuple], tree: bool = False, texts: dict | None = None) -> None:
        """The rows (iid, values, tag) in a list, keeping the chosen row and the scroll."""
        chosen = view.selection()
        top = view.yview()[0]
        view.delete(*view.get_children())
        for iid, values, tag in rows:
            if tree:
                view.insert("", "end", iid=iid, text=(texts or {}).get(iid, iid), values=values, tags=(tag,) if tag else ())
            else:
                view.insert("", "end", iid=iid, values=values, tags=(tag,) if tag else ())
        keep = [i for i in chosen if view.exists(i)]
        if keep:
            view.selection_set(keep)
        view.yview_moveto(top)


def main(manager: Manager | None = None) -> int:
    _hide_own_console()
    _sharp_on_windows()
    manager = manager or Manager()
    os.chdir(manager.home)
    root = tk.Tk()
    App(root, manager)
    root.mainloop()
    return 0
