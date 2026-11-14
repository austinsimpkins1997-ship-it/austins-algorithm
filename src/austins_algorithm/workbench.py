"""Native desktop interface for weighted constraint search."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .optimization import (
    MAX_QUERIES, MAX_ROUNDS, MAX_SHOTS, MAX_VARIABLES, MAX_WEIGHT,
    SearchSession, exact_minimum,
)
from .workbench_assistant import assist, build_overview
from .future_reader_panel import FutureReaderPanel
from .workbench_release import DESKTOP_VERSION
from .workbench_project import (
    export_project, generated_program, load_project, new_project,
    restore_session, save_project,
)


INK = "#172935"
MUTED = "#506572"
BG = "#eef3f5"
WHITE = "#ffffff"
LINE = "#d0dce2"
NAV = "#112a32"
NAV_ACTIVE = "#21444a"
TEAL = "#086653"
TEAL_HOVER = "#075343"
GREEN = "#0b5f4e"
GREEN_BG = "#dbf3e8"
BLUE = "#244e7c"
BLUE_BG = "#e4effb"
DIM = "#e9eff2"

STATUS_LABELS = {
    "RUNNING": "Search in progress",
    "OPTIMUM": "Optimum proven",
    "INFEASIBLE": "Infeasibility proven",
    "BUDGET_EXHAUSTED": "Shot budget exhausted",
}

TWO_BIT = {
    "name": "Two choices, different costs",
    "variables": [{"name": "Choice A", "cost": 5}, {"name": "Choice B", "cost": 1}],
    "clauses": [[1, 2]],
}
AUSTIN = {
    "name": "Original Austin circuit",
    "variables": [{"name": f"q{i}", "cost": 0} for i in range(3)],
    "clauses": [[1], [2, 3], [-2, -3]],
}
PRESETS = ("Weighted field sensor kit", "Two choices, different costs", "Original Austin circuit")
FILETYPES = [("Austin projects", "*.austin.json"), ("JSON files", "*.json"), ("All files", "*.*")]
ASSETS = Path(__file__).with_name("workbench_assets") / "icons"


def _integer(value: str, title: str) -> int:
    try:
        return int(value.strip(), 10)
    except ValueError as error:
        raise ValueError(f"{title} requires a whole number.") from error


def _rule_text(clauses: list[list[int]]) -> str:
    return "\n".join(" ".join(map(str, clause)) if clause else "false" for clause in clauses)


def _parse_rules(text: str) -> list[list[int]]:
    clauses = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        if line.casefold() == "false":
            clauses.append([])
            continue
        try:
            clauses.append([int(token, 10) for token in line.replace(",", " ").split()])
        except ValueError as error:
            raise ValueError(f"Rule {line_number} must contain signed integers, such as 1 -2.") from error
    return clauses


def _disk_version(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class StateBoard(ttk.Frame):
    """Scrollable, keyboard-selectable assignment tiles for one simulation frame."""

    def __init__(self, parent: tk.Misc, on_select) -> None:
        super().__init__(parent, style="Card.TFrame")
        self.on_select = on_select
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self, height=255, background=WHITE, highlightthickness=1,
            highlightbackground=LINE, highlightcolor=TEAL, takefocus=True, yscrollincrement=23,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(3, "units"))
        for key in ("Left", "Right", "Up", "Down", "Home", "End"):
            self.canvas.bind(f"<{key}>", self._key)
        self.states: list[dict] = []
        self.probabilities: list[float] = []
        self.amplitudes: list[float] = []
        self.selected = 0
        self.columns = 1
        self.row_height = 46
        self.tile_width = 80

    def update_frame(self, trace: dict, frame: dict, selected: int) -> None:
        changed = len(self.states) != len(trace["states"])
        self.states = trace["states"]
        self.probabilities = frame["probabilities"]
        self.amplitudes = frame["amplitudes"]
        self.selected = min(selected, len(self.states) - 1)
        self.draw()
        if changed:
            self.canvas.yview_moveto(0)

    def draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(120, canvas.winfo_width())
        self.columns = max(1, (width - 12) // 83)
        self.tile_width = (width - 12) / self.columns
        height = ((len(self.states) + self.columns - 1) // self.columns) * self.row_height + 12
        canvas.configure(scrollregion=(0, 0, width, height))
        for index, (state, prob, amp) in enumerate(zip(self.states, self.probabilities, self.amplitudes)):
            row, column = divmod(index, self.columns)
            x, y = 6 + column * self.tile_width, 6 + row * self.row_height
            right = x + self.tile_width - 8
            fill = GREEN_BG if state["marked"] else BLUE_BG if state["feasible"] else DIM
            text_color = GREEN if state["marked"] else BLUE if state["feasible"] else MUTED
            outline = TEAL if index == self.selected else LINE
            canvas.create_rectangle(x, y, right, y + 39, fill=fill, outline=outline,
                                    width=2 if index == self.selected else 1)
            canvas.create_text(x + 6, y + 10, text=state["bits"], fill=INK,
                               anchor="w", font=("Consolas", 9, "bold"))
            canvas.create_text(x + 6, y + 24, text=f"{prob:.1%}", fill=text_color,
                               anchor="w", font=("Segoe UI", 8))
            if amp < -1e-12:
                canvas.create_text(right - 4, y + 24, text="−", anchor="e", fill=INK,
                                   font=("Segoe UI", 9, "bold"))
            if prob > 0:
                bar = (right - x - 6) * prob
                canvas.create_rectangle(x + 3, y + 34, x + 3 + max(1, bar), y + 36,
                                        fill=text_color, outline="")

    def _click(self, event) -> None:
        self.canvas.focus_set()
        column = int((self.canvas.canvasx(event.x) - 6) // self.tile_width)
        row = int((self.canvas.canvasy(event.y) - 6) // self.row_height)
        if 0 <= column < self.columns and row >= 0:
            index = row * self.columns + column
            if index < len(self.states):
                self.on_select(index)

    def _wheel(self, event) -> str:
        if event.delta:
            self.canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _key(self, event) -> str:
        if self.states:
            if event.keysym == "Home":
                index = 0
            elif event.keysym == "End":
                index = len(self.states) - 1
            else:
                offsets = {"Left": -1, "Right": 1, "Up": -self.columns, "Down": self.columns}
                index = max(0, min(len(self.states) - 1, self.selected + offsets[event.keysym]))
            self.on_select(index)
            row = index // self.columns
            top = row * self.row_height + 6
            visible_top = self.canvas.canvasy(0)
            visible_height = self.canvas.winfo_height()
            total_height = ((len(self.states) + self.columns - 1) // self.columns) * self.row_height + 12
            if top < visible_top:
                self.canvas.yview_moveto(top / total_height)
            elif top + self.row_height > visible_top + visible_height:
                self.canvas.yview_moveto((top + self.row_height - visible_height) / total_height)
        return "break"


class Workbench:
    """Project editor, ideal simulator, result browser, and export controls."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.project = new_project()
        self.session = restore_session(self.project)
        self.events: list[dict] = []
        self.path: Path | None = None
        self.disk_version: tuple[int, int, int, int] | None = None
        self.assistant_window: tk.Toplevel | None = None
        self.suggested_action: str | None = None
        self.dirty = False
        self.loading = False
        self.auto_running = False
        self.auto_after: str | None = None
        self.trace: dict | None = None
        self.trace_event: int | None = None
        self.frame_index = 0
        self.selected_state = 0
        self.variable_names: list[tk.StringVar] = []
        self.variable_costs: list[tk.StringVar] = []
        self.images: dict[str, tk.PhotoImage] = {}
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.current_page = ""
        self.settings_vars = {key: tk.StringVar(root) for key in ("seed", "max_shots", "max_queries", "max_rounds")}
        self.prove_var = tk.BooleanVar(root)
        self.project_title = tk.StringVar(root)
        self.project_file = tk.StringVar(root)
        self.message = tk.StringVar(root)
        self.problem_name = tk.StringVar(root)
        self.preset_var = tk.StringVar(root, value=PRESETS[0])
        self.rounds_var = tk.StringVar(root, value="1")
        self.history_var = tk.StringVar(root, value="Next shot preview")
        self.exact_result: dict | None = None
        self.code_cache: str | None = None
        self.root.title("Austin’s Algorithm · Workbench")
        self.root.geometry("1280x820")
        self.root.minsize(1000, 720)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._styles()
        self._layout()
        self._shortcuts()
        self._activate_project(self.project, None)
        self.show_page("Overview")

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.option_add("*Font", ("Segoe UI", 10))
        style.configure(".", font=("Segoe UI", 10), foreground=INK, background=BG)
        for name, background in (("Surface", BG), ("Card", WHITE), ("Nav", NAV), ("Top", WHITE)):
            style.configure(f"{name}.TFrame", background=background)
        for name, foreground, background, size, weight in (
            ("Body", INK, BG, 10, "normal"),
            ("Muted", MUTED, BG, 10, "normal"),
            ("Heading", INK, BG, 25, "bold"),
            ("Eyebrow", TEAL, BG, 10, "bold"),
            ("Card", INK, WHITE, 10, "normal"),
            ("CardTitle", INK, WHITE, 12, "bold"),
            ("CardMuted", MUTED, WHITE, 9, "normal"),
            ("CardLarge", INK, WHITE, 19, "bold"),
            ("CardMono", INK, WHITE, 11, "normal"),
            ("TopTitle", INK, WHITE, 12, "bold"),
            ("TopMuted", MUTED, WHITE, 9, "normal"),
            ("Brand", WHITE, NAV, 15, "bold"),
            ("NavMuted", "#bad1d6", NAV, 9, "normal"),
            ("Footer", MUTED, BG, 9, "normal"),
            ("FooterError", "#952a38", BG, 9, "bold"),
        ):
            style.configure(f"{name}.TLabel", foreground=foreground, background=background,
                            font=("Consolas" if name == "CardMono" else "Segoe UI", size, weight))
        style.configure("TButton", padding=(10, 5), background=WHITE, foreground=INK, bordercolor=LINE)
        style.map("TButton", background=[("active", "#dfebf0")], bordercolor=[("focus", TEAL)])
        style.configure("Primary.TButton", padding=(11, 6), background=TEAL, foreground=WHITE, bordercolor=TEAL)
        style.map("Primary.TButton", background=[("disabled", "#57756e"), ("active", TEAL_HOVER)],
                  foreground=[("disabled", "#f0f5f3")], bordercolor=[("focus", "#9ce3c6")])
        for selected, background in ((False, NAV), (True, NAV_ACTIVE)):
            name = "NavSelected.TButton" if selected else "Nav.TButton"
            style.configure(name, anchor="w", padding=(15, 9), background=background, foreground=WHITE,
                            borderwidth=0, font=("Segoe UI", 11, "bold" if selected else "normal"))
            style.map(name, background=[("active", NAV_ACTIVE)], bordercolor=[("focus", "#8dcdbd")])
        style.configure("TEntry", fieldbackground=WHITE, padding=4)
        style.configure("Compact.TEntry", fieldbackground=WHITE, padding=(4, 2))
        style.configure("TCombobox", padding=4, fieldbackground=WHITE)
        style.configure("TSpinbox", padding=4, fieldbackground=WHITE)
        style.configure("TCheckbutton", background=WHITE)
        style.map("TCheckbutton", background=[("active", WHITE)])
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, rowheight=27, foreground=INK)
        style.configure("Treeview.Heading", background=DIM, font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", TEAL)], foreground=[("selected", WHITE)])

    def _icon(self, name: str, dark: bool = False) -> tk.PhotoImage | str:
        key = name + ("-dark" if dark else "")
        if key not in self.images:
            path = ASSETS / f"{key}.png"
            if path.is_file():
                with suppress(tk.TclError):
                    self.images[key] = tk.PhotoImage(master=self.root, file=str(path))
        return self.images.get(key, "")

    def _button(self, parent, text, command, *, icon=None, style="TButton", **options) -> ttk.Button:
        image = self._icon(icon, dark=style == "TButton") if icon else ""
        return ttk.Button(parent, text=text, command=command, style=style, image=image,
                          compound="left", **options)

    def _label(self, parent, text="", style="Body", **options) -> ttk.Label:
        return ttk.Label(parent, text=text, style=f"{style}.TLabel", **options)

    def _layout(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)
        nav = ttk.Frame(self.root, style="Nav.TFrame", padding=(12, 20, 12, 14), width=166)
        nav.grid(row=0, column=0, rowspan=3, sticky="ns")
        nav.grid_propagate(False)
        nav.columnconfigure(0, weight=1)
        nav.rowconfigure(10, weight=1)
        brand = self._label(nav, " Austin’s", "Brand", image=self._icon("boxes"), compound="left")
        brand.grid(row=0, column=0, sticky="w", padx=6)
        self._label(nav, "Algorithm", "Brand").grid(row=1, column=0, sticky="w", padx=6)
        self._label(nav, "WORKBENCH", "NavMuted").grid(row=2, column=0, sticky="w", padx=6, pady=(5, 25))
        for row, name in enumerate(("Overview", "Build", "Simulate", "Results", "Future Reader", "Guide"), start=3):
            item = self._button(nav, name, lambda page=name: self.show_page(page), style="Nav.TButton")
            item.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[name] = item
        self._label(nav, f"Ideal simulator\n{DESKTOP_VERSION}", "NavMuted", justify="left").grid(
            row=11, column=0, sticky="sw", padx=6,
        )

        top = ttk.Frame(self.root, style="Top.TFrame", padding=(16, 8))
        top.grid(row=0, column=1, sticky="ew")
        top.columnconfigure(0, weight=1)
        self._label(top, style="TopTitle", textvariable=self.project_title, width=26).grid(row=0, column=0, sticky="w")
        self._label(top, style="TopMuted", textvariable=self.project_file, width=29).grid(row=1, column=0, sticky="w")
        for column, text, command, icon, button_style in (
            (1, "New", self.new, None, "TButton"),
            (2, "Open", self.open, "folder-open", "TButton"),
            (3, "Save", self.save, "save", "TButton"),
            (4, "AI Assistant", self.open_assistant, "circle-help", "TButton"),
            (5, "Export", self.export, "download", "Primary.TButton"),
        ):
            button = self._button(top, text, command, icon=icon, style=button_style)
            button.grid(row=0, column=column, rowspan=2, padx=(6, 0), sticky="e")
            if text == "Export":
                self.export_button = button

        self.content = ttk.Frame(self.root, style="Surface.TFrame", padding=(16, 11, 16, 3))
        self.content.grid(row=1, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        for name, builder in (
            ("Overview", self._overview_page), ("Build", self._build_page), ("Simulate", self._simulation_page),
            ("Results", self._results_page), ("Future Reader", self._future_reader_page), ("Guide", self._guide_page),
        ):
            frame = ttk.Frame(self.content, style="Surface.TFrame")
            frame.grid(row=0, column=0, sticky="nsew")
            frame.columnconfigure(0, weight=1)
            self.pages[name] = frame
            builder(frame)
        footer = ttk.Frame(self.root, style="Surface.TFrame", padding=(16, 4, 16, 8))
        footer.grid(row=2, column=1, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_line = self._label(footer, style="Footer", textvariable=self.message)
        self.status_line.grid(row=0, column=0, sticky="ew")

    def _future_reader_page(self, page) -> None:
        self.future_reader = FutureReaderPanel(self, page)

    def _heading(self, page, title: str, subtitle: str) -> None:
        self._label(page, title, "Heading").grid(row=0, column=0, sticky="w")
        self._label(page, subtitle, "Muted", wraplength=740).grid(row=1, column=0, sticky="w", pady=(0, 10))

    def _card(self, parent, padding=12) -> ttk.Frame:
        return ttk.Frame(parent, style="Card.TFrame", padding=padding)

    def _overview_page(self, page) -> None:
        self.overview = {
            key: tk.StringVar(self.root)
            for key in ("goal", "heading", "explanation", "next_step", "outcome", "budget")
        }
        self._label(page, "Project overview", "Heading").grid(row=0, column=0, sticky="w")
        self._label(page, style="Muted", textvariable=self.overview["goal"], wraplength=740).grid(
            row=1, column=0, sticky="w", pady=(0, 10),
        )
        work = self._card(page, (16, 13))
        work.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        work.columnconfigure(0, weight=1)
        for index, (key, style) in enumerate((("heading", "CardLarge"), ("explanation", "Card"), ("next_step", "CardMuted"))):
            self._label(work, style=style, textvariable=self.overview[key], wraplength=680).grid(
                row=index, column=0, sticky="w", pady=(0, 6),
            )
        actions = self._card(work, 0)
        actions.grid(row=3, column=0, sticky="w", pady=(2, 0))
        self.optimize_button = self._button(actions, "Optimize", self.optimize, icon="play", style="Primary.TButton")
        self.optimize_button.pack(side="left")
        self._button(actions, "Explore simulation", lambda: self.show_page("Simulate")).pack(side="left", padx=7)
        self._button(actions, "Edit problem", lambda: self.show_page("Build")).pack(side="left")
        answer = self._card(page, (16, 13))
        answer.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self._label(answer, "Outcome", "CardTitle").pack(anchor="w")
        self._label(answer, style="Card", textvariable=self.overview["outcome"], wraplength=680).pack(anchor="w", pady=(4, 5))
        self._label(answer, style="CardMuted", textvariable=self.overview["budget"], wraplength=680).pack(anchor="w")
        self._button(answer, "See recorded results", lambda: self.show_page("Results")).pack(anchor="w", pady=(8, 0))
        note = self._card(page, (16, 10))
        note.grid(row=4, column=0, sticky="ew")
        self._label(note, "How to use this project", "CardTitle").pack(anchor="w")
        self._label(
            note, "Edit choices and rules in Build. Optimize finds the exact answer locally. "
            "Simulate shows how a Grover search would evolve, one stage at a time.",
            "Card", wraplength=680,
        ).pack(anchor="w", pady=(3, 0))

    def _assistant_context(self) -> dict:
        return {
            "problem": self.session.problem,
            "settings": self.session.settings,
            "preview": self.trace,
            "run": {
                "status": self.session.status, "best": self.session.best,
                "shots_used": self.session.shots_used, "queries_used": self.session.queries_used,
                "shots_remaining": self.session.shots_remaining,
                "queries_remaining": self.session.queries_remaining,
                "events": self.events[-1:],
            },
        }

    def _refresh_overview(self) -> None:
        overview = build_overview(
            self._assistant_context(), frame_index=self.frame_index,
            historical=self.trace_event is not None, exact=self.exact_result,
        )
        for key, value in self.overview.items():
            value.set(overview[key])

    def open_assistant(self) -> None:
        if self.assistant_window is not None and self.assistant_window.winfo_exists():
            self.assistant_window.lift()
            self.assistant_entry.focus_set()
            return
        window = tk.Toplevel(self.root, background=BG)
        self.assistant_window = window
        window.title("Austin’s Algorithm · AI Assistant")
        window.geometry("630x540")
        window.minsize(500, 440)
        window.transient(self.root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        content = ttk.Frame(window, style="Surface.TFrame", padding=14)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(5, weight=1)
        self._label(content, "AI Assistant", "Heading").grid(row=0, column=0, sticky="w")
        self._label(
            content, "Built-in rule-based help · Offline · Answers use the active simulation.",
            "Muted", wraplength=565,
        ).grid(row=1, column=0, sticky="w", pady=(0, 5))
        self.assistant_context_line = tk.StringVar(window)
        self._label(content, style="Muted", textvariable=self.assistant_context_line, wraplength=565).grid(
            row=2, column=0, sticky="ew", pady=(0, 7),
        )
        choices = ttk.Frame(content, style="Surface.TFrame")
        choices.grid(row=3, column=0, sticky="w", pady=(0, 7))
        for text, question in (
            ("What’s happening?", "What is happening right now?"),
            ("Explain this state", "Explain the selected state."),
            ("How do I optimize?", "How can I optimize this project?"),
        ):
            self._button(choices, text, lambda query=question: self._ask_assistant(query)).pack(side="left", padx=(0, 6))
        query = ttk.Frame(content, style="Surface.TFrame")
        query.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        query.columnconfigure(0, weight=1)
        self.assistant_question = tk.StringVar(window)
        self.assistant_entry = ttk.Entry(query, textvariable=self.assistant_question)
        self.assistant_entry.grid(row=0, column=0, sticky="ew")
        self.assistant_entry.bind("<Return>", lambda _e: self._ask_assistant())
        self._button(query, "Ask", self._ask_assistant, style="Primary.TButton").grid(row=0, column=1, padx=(6, 0))
        reply = self._card(content, 0)
        reply.grid(row=5, column=0, sticky="nsew")
        reply.columnconfigure(0, weight=1)
        reply.rowconfigure(0, weight=1)
        self.assistant_answer = tk.Text(
            reply, wrap="word", height=8, font=("Segoe UI", 11), padx=10, pady=9,
            foreground=INK, background=WHITE, relief="flat", state="disabled",
        )
        self.assistant_answer.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(reply, orient="vertical", command=self.assistant_answer.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.assistant_answer.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(content, style="Surface.TFrame")
        actions.grid(row=6, column=0, sticky="ew", pady=(7, 0))
        self.assistant_action_button = self._button(actions, "Run suggested action", self._run_suggested_action)
        self.assistant_action_button.pack(side="left")
        self._button(actions, "Close", window.destroy).pack(side="right")
        window.bind("<Escape>", lambda _e: window.destroy())
        self._ask_assistant("What is happening right now?")
        self.assistant_entry.focus_set()

    def _ask_assistant(self, question: str | None = None) -> None:
        if question is None:
            question = self.assistant_question.get()
        self.assistant_question.set(question)
        label = self.session.problem["name"]
        try:
            draft_problem, draft_settings = self._editor_values()
            edited = (draft_problem, draft_settings) != (self.session.problem, self.session.settings)
        except ValueError:
            edited = True
        self.assistant_context_line.set(
            f"Active project · {label}" + (" · Build has unapplied edits" if edited else "")
        )
        try:
            result = assist(
                self._assistant_context(), question, frame_index=self.frame_index,
                historical=self.trace_event is not None, exact=self.exact_result,
                selected_state=self.selected_state,
            )
            answer = result["answer"]
            self.suggested_action = result.get("action")
        except ValueError as error:
            answer = str(error)
            self.suggested_action = None
        self.assistant_answer.configure(state="normal")
        self.assistant_answer.delete("1.0", "end")
        self.assistant_answer.insert("1.0", answer)
        self.assistant_answer.configure(state="disabled")
        title = {
            "optimize": "Optimize", "build": "Open Build", "simulate": "Open Simulate",
            "results": "Open Results", "guide": "Open Guide",
            "preview": "Preview next shot", "export": "Export project",
        }.get(self.suggested_action)
        self.assistant_action_button.configure(text=title or "Run suggested action")
        self.assistant_action_button.state(["!disabled"] if title else ["disabled"])

    def _run_suggested_action(self) -> None:
        action = self.suggested_action
        if action in {"build", "simulate", "results", "guide"}:
            self.show_page(action.title())
        elif action == "optimize":
            self.optimize()
        elif action == "preview":
            self.show_page("Simulate")
            if self.current_page == "Simulate":
                self.preview_current()
        elif action == "export":
            self.export()
        if self.assistant_window is not None and self.assistant_window.winfo_exists():
            self._ask_assistant("What is happening right now?")

    def _build_page(self, page) -> None:
        self._heading(page, "Build a problem", "Define binary choices, assign costs, and enter the rules a valid solution must satisfy.")
        page.rowconfigure(3, weight=1)
        setup = self._card(page, (11, 8))
        setup.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        setup.columnconfigure(1, weight=1)
        self._label(setup, "Project name", "CardTitle").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(setup, textvariable=self.problem_name).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        preset = ttk.Combobox(setup, textvariable=self.preset_var, values=PRESETS, state="readonly", width=23)
        preset.grid(row=0, column=2, sticky="ew", padx=(0, 5))
        self._button(setup, "Use example", self.use_preset).grid(row=0, column=3)

        body = ttk.Frame(page, style="Surface.TFrame")
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=5, uniform="build")
        body.columnconfigure(1, weight=6, uniform="build")
        body.rowconfigure(0, weight=1)
        variables = self._card(body, (11, 8))
        variables.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        variables.columnconfigure(0, weight=1)
        self._label(variables, "1  Variables and costs", "CardTitle").grid(row=0, column=0, sticky="w")
        self._label(variables, "An enabled choice contributes its cost.", "CardMuted").grid(row=1, column=0, sticky="w", pady=(2, 4))
        self.variables_panel = self._card(variables, 0)
        self.variables_panel.grid(row=2, column=0, sticky="ew")
        variable_actions = self._card(variables, 0)
        variable_actions.grid(row=3, column=0, sticky="w", pady=(5, 0))
        self._button(variable_actions, "+ Variable", self.add_variable).pack(side="left")
        self._button(variable_actions, "Remove last", self.remove_variable).pack(side="left", padx=(6, 0))

        right = self._card(body, (11, 8))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self._label(right, "2  Rules", "CardTitle").grid(row=0, column=0, sticky="w")
        self._label(right, "One clause per line. 1 means q0 is on, −2 means q1 is off.\nAt least one choice on each line must be true.", "CardMuted", wraplength=390).grid(
            row=1, column=0, sticky="w", pady=(2, 5),
        )
        self.rules = tk.Text(right, height=6, wrap="none", undo=True, font=("Consolas", 11), padx=8, pady=5,
                             foreground=INK, background="#f5f8fa", insertbackground=INK,
                             relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL)
        self.rules.grid(row=2, column=0, sticky="nsew")
        rule_scroll = ttk.Scrollbar(right, orient="vertical", command=self.rules.yview)
        rule_scroll.grid(row=2, column=1, sticky="ns")
        self.rules.configure(yscrollcommand=rule_scroll.set)
        self.rules.bind("<<Modified>>", self._rules_edited)
        self._label(right, "3  Search budget", "CardTitle").grid(row=3, column=0, sticky="w", pady=(8, 3))
        settings = self._card(right, 0)
        settings.grid(row=4, column=0, sticky="ew")
        for column in range(2):
            settings.columnconfigure(column, weight=1, uniform="settings")
        for index, (key, label) in enumerate((
            ("seed", "Random seed"), ("max_shots", "Shots"),
            ("max_queries", "Oracle queries"), ("max_rounds", "Rounds per shot"),
        )):
            row, column = divmod(index, 2)
            self._label(settings, label, "CardMuted").grid(row=row * 2, column=column, sticky="w", padx=(0, 8))
            ttk.Entry(settings, textvariable=self.settings_vars[key], width=8).grid(
                row=row * 2 + 1, column=column, sticky="ew", padx=(0, 8), pady=(0, 2),
            )
        ttk.Checkbutton(right, text="Check for an exact optimum", variable=self.prove_var).grid(
            row=5, column=0, sticky="w", pady=(4, 0),
        )
        footer = ttk.Frame(page, style="Surface.TFrame", padding=(0, 6, 0, 0))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self._label(footer, "Each shot starts from a fresh uniform state.", "Muted").grid(row=0, column=0, sticky="w")
        self.apply_button = self._button(footer, "Apply & simulate", self.apply_and_simulate, icon="play", style="Primary.TButton")
        self.apply_button.grid(row=0, column=1, sticky="e")

    def _simulation_page(self, page) -> None:
        self._heading(page, "Simulation workspace", "Inspect amplitude, phase and probability at each step, then choose where to measure.")
        page.rowconfigure(3, weight=1)
        metrics = ttk.Frame(page, style="Surface.TFrame")
        metrics.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.metric_values = [tk.StringVar(self.root) for _ in range(4)]
        for column, title in enumerate(("BASIS STATES", "VERIFIED BEST", "SHOTS USED", "ORACLE QUERIES")):
            metrics.columnconfigure(column, weight=1, uniform="metrics")
            card = self._card(metrics, (10, 5))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 6 if column < 3 else 0))
            self._label(card, title, "CardMuted").pack(anchor="w")
            self._label(card, style="CardLarge", textvariable=self.metric_values[column]).pack(anchor="w")

        middle = ttk.Frame(page, style="Surface.TFrame")
        middle.grid(row=3, column=0, sticky="nsew")
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)
        board_card = self._card(middle, (10, 8))
        board_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        board_card.columnconfigure(0, weight=1)
        board_card.rowconfigure(4, weight=1)
        board_head = self._card(board_card, 0)
        board_head.grid(row=0, column=0, sticky="ew")
        board_head.columnconfigure(0, weight=1)
        self._label(board_head, "State explorer", "CardTitle").grid(row=0, column=0, sticky="w")
        self.history = ttk.Combobox(board_head, textvariable=self.history_var, state="readonly", width=18)
        self.history.grid(row=0, column=1, sticky="e")
        self.history.bind("<<ComboboxSelected>>", self._history_selected)
        self.stage_title = tk.StringVar(self.root)
        self.stage_caption = tk.StringVar(self.root)
        self._label(board_card, style="CardTitle", textvariable=self.stage_title).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self._label(board_card, style="CardMuted", textvariable=self.stage_caption, wraplength=510).grid(
            row=2, column=0, sticky="w", pady=(0, 2),
        )
        playback = self._card(board_card, 0)
        playback.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        playback.columnconfigure(1, weight=1)
        self.back_button = self._button(playback, "Prev", lambda: self._frame_delta(-1), icon="chevron-left")
        self.back_button.grid(row=0, column=0, padx=(0, 7))
        self.slider = ttk.Scale(playback, from_=0, to=2, orient="horizontal", command=self._scrub, takefocus=True)
        self.slider.grid(row=0, column=1, sticky="ew")
        self.next_button = self._button(playback, "Next", lambda: self._frame_delta(1), icon="chevron-right")
        self.next_button.grid(row=0, column=2, padx=(7, 0))
        self.board = StateBoard(board_card, self._select_state)
        self.board.grid(row=4, column=0, sticky="nsew")
        self._label(board_card, "Green · marked    Blue · feasible    Gray · invalid    − · negative phase", "CardMuted",
                    wraplength=490).grid(row=5, column=0, sticky="w", pady=(3, 0))

        inspector = self._card(middle, (12, 8))
        inspector.grid(row=0, column=1, sticky="ns")
        inspector.columnconfigure(0, weight=1)
        self.inspector_text = [tk.StringVar(self.root) for _ in range(6)]
        self._label(inspector, "State inspector", "CardTitle").grid(row=0, column=0, sticky="w")
        self._label(inspector, style="CardLarge", textvariable=self.inspector_text[0], width=13).grid(
            row=1, column=0, sticky="w", pady=(1, 2),
        )
        for row, value in enumerate(self.inspector_text[1:5], start=2):
            self._label(inspector, style="Card", textvariable=value, wraplength=205).grid(row=row, column=0, sticky="w", pady=1)
        self._label(inspector, style="CardMuted", textvariable=self.inspector_text[5], wraplength=205).grid(
            row=6, column=0, sticky="w", pady=(4, 7),
        )
        self.target_text = tk.StringVar(self.root)
        self._label(inspector, "Marked probability", "CardTitle").grid(row=7, column=0, sticky="w")
        self.target_bar = ttk.Progressbar(inspector, mode="determinate", maximum=100, length=190)
        self.target_bar.grid(row=8, column=0, sticky="ew", pady=(4, 2))
        self._label(inspector, style="CardMuted", textvariable=self.target_text, wraplength=205).grid(
            row=9, column=0, sticky="w",
        )
        self.session_hint = tk.StringVar(self.root)
        self._label(inspector, style="CardMuted", textvariable=self.session_hint, wraplength=205).grid(
            row=10, column=0, sticky="w", pady=(7, 0),
        )

        controls = self._card(page, (10, 6))
        controls.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._label(controls, "Rounds", "CardMuted").grid(row=0, column=0, padx=(0, 5))
        self.round_spin = ttk.Spinbox(controls, from_=0, to=MAX_ROUNDS, textvariable=self.rounds_var, width=3,
                                     command=self.preview_current)
        self.round_spin.grid(row=0, column=1, padx=(0, 5))
        self.round_spin.bind("<Return>", lambda _e: self.preview_current())
        self.rounds_var.trace_add("write", lambda *_args: self._refresh_controls() if self.trace else None)
        self.preview_button = self._button(controls, "Preview", self.preview_current)
        self.preview_button.grid(row=0, column=2, padx=(0, 5))
        self.measure_button = self._button(controls, "Measure here", self.measure_here, style="Primary.TButton")
        self.measure_button.grid(row=0, column=3, padx=(0, 5))
        self.run_button = self._button(controls, "Auto run", self.toggle_auto, icon="play")
        self.run_button.grid(row=0, column=4, padx=(0, 5))
        self.reset_button = self._button(controls, "Reset run", self.reset_run)
        self.reset_button.grid(row=0, column=5)
        self.measure_hint = tk.StringVar(self.root)
        self._label(controls, style="CardMuted", textvariable=self.measure_hint, wraplength=720).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(3, 0),
        )

    def _results_page(self, page) -> None:
        self._heading(page, "Results and replay", "Compare measured candidates with the exact answer and inspect the recorded search.")
        page.rowconfigure(3, weight=1)
        summary = ttk.Frame(page, style="Surface.TFrame")
        summary.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.result_vars = [tk.StringVar(self.root) for _ in range(3)]
        for column, title in enumerate(("SEARCH STATUS", "BEST VERIFIED CANDIDATE", "EXACT CLASSICAL RESULT")):
            summary.columnconfigure(column, weight=1, uniform="results")
            card = self._card(summary, (10, 6))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 6 if column < 2 else 0))
            self._label(card, title, "CardMuted").pack(anchor="w")
            self._label(card, style="CardTitle", textvariable=self.result_vars[column], wraplength=230).pack(anchor="w")
        notebook = ttk.Notebook(page)
        notebook.grid(row=3, column=0, sticky="nsew")
        self.results_notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", lambda _e: self._render_code_if_visible())
        trace_tab = self._card(notebook, (10, 8))
        code_tab = self._card(notebook, (10, 8))
        notebook.add(trace_tab, text="Measurement history")
        notebook.add(code_tab, text="Generated Python")
        self.code_tab = code_tab
        trace_tab.columnconfigure(0, weight=1)
        trace_tab.rowconfigure(1, weight=1)
        actions = self._card(trace_tab, 0)
        actions.grid(row=0, column=0, sticky="ew", columnspan=2, pady=(0, 5))
        self._button(actions, "Compare exact", self.compare_exact).pack(side="left")
        self._button(actions, "Inspect selected shot", self.inspect_selected_shot).pack(side="left", padx=(6, 0))
        self._label(actions, "Double-click a shot to inspect its stages.", "CardMuted").pack(side="left", padx=(8, 0))
        columns = ("shot", "rounds", "bits", "cost", "result")
        self.history_tree = ttk.Treeview(trace_tab, columns=columns, show="headings", selectmode="browse", height=8)
        for column, label, width in zip(columns, ("Shot", "Rounds", "Assignment", "Cost", "Outcome"),
                                        (68, 90, 145, 82, 235)):
            self.history_tree.heading(column, text=label)
            self.history_tree.column(column, width=width, minwidth=width - 6, stretch=column == "result", anchor="w")
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        self.history_tree.bind("<Double-1>", lambda _e: self.inspect_selected_shot())
        self.history_tree.bind("<Return>", lambda _e: self.inspect_selected_shot())
        scroll = ttk.Scrollbar(trace_tab, orient="vertical", command=self.history_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.exact_caption = tk.StringVar(self.root)
        self._label(trace_tab, style="CardMuted", textvariable=self.exact_caption, wraplength=740).grid(
            row=2, column=0, sticky="w", pady=(4, 0),
        )

        code_tab.columnconfigure(0, weight=1)
        code_tab.rowconfigure(1, weight=1)
        code_actions = self._card(code_tab, 0)
        code_actions.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        self._button(code_actions, "Copy Python", self.copy_code).pack(side="left")
        self._label(code_actions, "Export includes this program and the local files needed to replay it.", "CardMuted").pack(
            side="left", padx=(8, 0),
        )
        self.code_text = tk.Text(code_tab, wrap="none", height=10, font=("Consolas", 10), padx=9, pady=7,
                                 foreground=INK, background="#f5f8fa", relief="flat", state="disabled",
                                 highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL)
        self.code_text.grid(row=1, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(code_tab, orient="vertical", command=self.code_text.yview)
        xscroll = ttk.Scrollbar(code_tab, orient="horizontal", command=self.code_text.xview)
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")
        self.code_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

    def _guide_page(self, page) -> None:
        self._heading(page, "Workbench guide", "Start with an example and follow the same steps for your own project.")
        page.rowconfigure(2, weight=1)
        card = self._card(page, (12, 8))
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        guide = tk.Text(card, wrap="word", font=("Segoe UI", 11), background=WHITE, foreground=INK, relief="flat",
                        padx=5, pady=3, borderwidth=0, highlightthickness=0)
        guide.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(card, orient="vertical", command=guide.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        guide.configure(yscrollcommand=scroll.set)
        guide.tag_configure("title", font=("Segoe UI", 13, "bold"), foreground=TEAL, spacing1=7, spacing3=3)
        guide.tag_configure("body", spacing3=5, lmargin1=0, lmargin2=0)
        for title, detail in (
            ("1  Build a problem",
             "Choose an example on Build or enter 1–8 binary variables. A cost is counted when its variable is on. "
             f"Use nonnegative whole numbers up to {MAX_WEIGHT:,}. Each rule line is an OR clause, and every rule line must hold. "
             "Use 1 for q0 on, −2 for q1 off (type a keyboard hyphen), or false for an impossible clause. "
             "Blank lines and text following # are ignored. The two-choice example has a first feasible answer of 01 (cost 5) "
             "and an exact minimum of 10 (cost 1)."),
            ("2  Explore the state board",
             "Preview starts with equal amplitudes for all assignments. The oracle reverses the sign of marked amplitudes. "
             "Diffusion reflects amplitudes around their mean. The resulting probabilities are their squared magnitudes. "
             "A minus sign records a relative phase. Click a tile or use its arrow keys to inspect one assignment. "
             "The bit string is written with q0 on the right."),
            ("3  Decide and measure",
             f"Choose up to {MAX_ROUNDS} rounds per shot. Use Previous or Next to reach an initial or diffusion frame, "
             "then select Measure here. A preview uses no shots. A measurement spends one shot and one query per full Grover round. "
             "Each shot starts fresh. Auto run chooses affordable round counts and stops at the shared limits or an exact proof. "
             f"Budgets support up to {MAX_SHOTS} shots and {MAX_QUERIES} queries. Zero-round shots still work after queries run out."),
            ("4  Read the result",
             "A feasible measurement sets the current best cost. After that, only strictly cheaper feasible assignments are marked. "
             "The exact classical check can prove optimality or infeasibility. A budget stop retains the best verified candidate so far. "
             "Past shots remain available in the state explorer and in Results. Local ideal simulation uses a classical phase lookup."),
            ("5  Save and export",
             "Save keeps the problem, settings, preview and every recorded measurement in a reusable project file. "
             "Open restores the seeded search so you can continue an unfinished run. The Export button creates a ZIP with "
             "the project, exact result, run report, frame data, replay program and file hashes. In Results, open Generated Python "
             "to view or copy the replay program. The ZIP contains the files needed to run it locally."),
            ("Keyboard",
             "Ctrl+N opens a new project. Ctrl+O opens a saved project. Ctrl+S saves, Ctrl+Shift+S chooses a new path, "
             "and Ctrl+E exports. F1 opens this guide. Use Tab to move through controls and arrow keys on the state board."),
        ):
            guide.insert("end", title + "\n", "title")
            guide.insert("end", detail + "\n\n", "body")
        guide.configure(state="disabled")

    def _shortcuts(self) -> None:
        for key, callback in (
            ("<Control-n>", self.new), ("<Control-o>", self.open), ("<Control-s>", self.save),
            ("<Control-Shift-S>", lambda: self.save(save_as=True)), ("<Control-e>", self.export),
            ("<F1>", lambda: self.show_page("Guide")),
        ):
            def invoke(_event, action=callback):
                action()
                return "break"
            self.root.bind(key, invoke)

    def set_message(self, text: str, *, error: bool = False) -> None:
        self.message.set(text)
        self.status_line.configure(style="FooterError.TLabel" if error else "Footer.TLabel")

    def _failure(self, error: Exception) -> None:
        self.set_message(str(error), error=True)

    def _mark_dirty(self) -> None:
        if not self.loading:
            self.dirty = True
            self._title()

    def _title(self) -> None:
        self.project_title.set(self.project["problem"]["name"])
        self.project_file.set((self.path.name if self.path else "Untitled project") + (" • Unsaved" if self.dirty else ""))
        self.root.title(f"{self.project['problem']['name']} — Austin’s Algorithm" + (" *" if self.dirty else ""))

    def _rules_edited(self, _event) -> None:
        if self.rules.edit_modified():
            self.rules.edit_modified(False)
            self._mark_dirty()

    def _load_editor(self, project: dict) -> None:
        self.loading = True
        try:
            problem, settings = project["problem"], project["settings"]
            self.problem_name.set(problem["name"])
            self.variable_names = [tk.StringVar(self.root, value=row["name"]) for row in problem["variables"]]
            self.variable_costs = [tk.StringVar(self.root, value=str(row["cost"])) for row in problem["variables"]]
            self._draw_variables()
            self.rules.delete("1.0", "end")
            self.rules.insert("1.0", _rule_text(problem["clauses"]))
            self.rules.edit_modified(False)
            for key, variable in self.settings_vars.items():
                variable.set(str(settings[key]))
            self.prove_var.set(settings["prove"])
            for variable in (self.problem_name, self.prove_var, *self.settings_vars.values()):
                if not variable.trace_info():
                    variable.trace_add("write", lambda *_args: self._mark_dirty())
        finally:
            self.loading = False

    def _draw_variables(self) -> None:
        for child in self.variables_panel.winfo_children():
            child.destroy()
        self.variables_panel.columnconfigure(1, weight=1)
        for column, label in enumerate(("Bit", "Choice name", "Cost")):
            self._label(self.variables_panel, label, "CardMuted").grid(row=0, column=column, sticky="w", padx=3)
        for index, (name, cost) in enumerate(zip(self.variable_names, self.variable_costs)):
            self._label(self.variables_panel, f"q{index}", "CardMono").grid(row=index + 1, column=0, sticky="w", padx=3)
            ttk.Entry(self.variables_panel, textvariable=name, width=14, style="Compact.TEntry").grid(
                row=index + 1, column=1, sticky="ew", padx=(3, 6), pady=1,
            )
            ttk.Entry(self.variables_panel, textvariable=cost, width=7, style="Compact.TEntry").grid(
                row=index + 1, column=2, sticky="ew", padx=3, pady=1,
            )
            for variable in (name, cost):
                if not variable.trace_info():
                    variable.trace_add("write", lambda *_args: self._mark_dirty())

    def add_variable(self) -> None:
        if len(self.variable_names) >= MAX_VARIABLES:
            self.set_message(f"This simulator supports at most {MAX_VARIABLES} variables.", error=True)
            return
        labels = {variable.get().strip().casefold() for variable in self.variable_names}
        index = len(self.variable_names)
        while f"choice {index + 1}" in labels:
            index += 1
        self.variable_names.append(tk.StringVar(self.root, value=f"Choice {index + 1}"))
        self.variable_costs.append(tk.StringVar(self.root, value="0"))
        self._draw_variables()
        self._mark_dirty()

    def remove_variable(self) -> None:
        if len(self.variable_names) == 1:
            self.set_message("Keep at least one variable.", error=True)
            return
        removed = len(self.variable_names) - 1
        self.variable_names.pop()
        self.variable_costs.pop()
        self._draw_variables()
        self._mark_dirty()
        self.set_message(f"Removed q{removed}. Check rules that refer to this choice.")

    def _editor_values(self) -> tuple[dict, dict]:
        document = {
            "name": self.problem_name.get(),
            "variables": [
                {"name": name.get(), "cost": _integer(cost.get(), f"q{index} cost")}
                for index, (name, cost) in enumerate(zip(self.variable_names, self.variable_costs))
            ],
            "clauses": _parse_rules(self.rules.get("1.0", "end-1c")),
        }
        settings = {
            key: _integer(var.get(), label) for key, var, label in (
                ("seed", self.settings_vars["seed"], "Random seed"),
                ("max_shots", self.settings_vars["max_shots"], "Shot budget"),
                ("max_queries", self.settings_vars["max_queries"], "Oracle query budget"),
                ("max_rounds", self.settings_vars["max_rounds"], "Rounds per shot"),
            )
        }
        settings["prove"] = self.prove_var.get()
        return document, settings

    def apply_editor(self, *, confirm: bool = True) -> bool:
        try:
            document, settings = self._editor_values()
            candidate = SearchSession(document, **settings)
        except ValueError as error:
            self._failure(error)
            self.show_page("Build", apply_draft=False)
            return False
        if (candidate.problem, candidate.settings) == (self.project["problem"], self.project["settings"]):
            return True
        if confirm and self.events and not messagebox.askokcancel(
            "Apply new search settings",
            "These changes start a new run and replace the measurements in this project. Continue?",
            parent=self.root,
        ):
            self.set_message("The current project is still available. Apply or revert the changes on Build.")
            return False
        self.stop_auto()
        self.project = new_project(candidate.problem)
        self.project["settings"] = candidate.settings
        self.session = candidate
        self.events = []
        self.exact_result = None
        self.code_cache = None
        self.rounds_var.set(str(min(1, self.session.settings["max_rounds"])))
        self.round_spin.configure(to=self.session.settings["max_rounds"])
        self._mark_dirty()
        self._preview(self._rounds(), announce=False)
        self._refresh_history()
        self._refresh_results()
        return True

    def apply_and_simulate(self) -> bool:
        if self.apply_editor():
            self.show_page("Simulate", apply_draft=False)
            self.set_message("Problem and budget applied. Use the stage controls to inspect the next shot.")
            return True
        return False

    def _activate_project(self, project: dict, path: Path | None) -> None:
        self.stop_auto()
        self.session = restore_session(project)
        self.project = project
        self.events = list(project["run"]["events"]) if project["run"] else []
        self.path = path
        self.disk_version = _disk_version(path) if path else None
        self.dirty = False
        self.exact_result = None
        self.code_cache = None
        self._load_editor(project)
        saved_trace = project.get("preview")
        self.round_spin.configure(to=self.session.settings["max_rounds"])
        history_index = next((i for i, event in enumerate(self.events) if event["trace"] == saved_trace), None)
        is_historical = (
            saved_trace is not None and history_index is not None
            and saved_trace != self.session.preview(saved_trace["rounds"])
        )
        rounds = saved_trace["rounds"] if saved_trace else min(1, self.session.settings["max_rounds"])
        self.rounds_var.set(str(rounds))
        self._set_trace(saved_trace or self.session.preview(rounds),
                        event_index=history_index if is_historical else None, last=False)
        self._refresh_history()
        self._refresh_results()
        self._title()
        self.set_message("Ready. Explore a phase or choose an example on Build.")

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        decision = messagebox.askyesnocancel(
            "Save project?", "Save your changes before continuing?", parent=self.root,
        )
        if decision is None:
            return False
        return self.save() if decision else True

    def use_preset(self) -> None:
        if not self._confirm_discard():
            return
        name = self.preset_var.get()
        selected = TWO_BIT if name == PRESETS[1] else AUSTIN if name == PRESETS[2] else None
        self._activate_project(new_project(selected), None)
        self._mark_dirty()
        self.set_message(f"Loaded example {name}. Review its rules or open Simulate.")

    def new(self) -> None:
        if self._confirm_discard():
            self.preset_var.set(PRESETS[0])
            self._activate_project(new_project(), None)
            self.show_page("Build", apply_draft=False)

    def open(self, path: str | Path | None = None) -> bool:
        self.stop_auto()
        if not self._confirm_discard():
            return False
        if path is None:
            path = filedialog.askopenfilename(parent=self.root, title="Open Austin project", filetypes=FILETYPES)
        if not path:
            return False
        try:
            project = load_project(path)
            self._activate_project(project, Path(path))
        except (OSError, ValueError, RuntimeError) as error:
            self._failure(error)
            messagebox.showerror("Unable to open project", str(error), parent=self.root)
            return False
        self.show_page("Simulate", apply_draft=False)
        self.set_message(f"Opened {Path(path).name}.")
        return True

    def _snapshot(self) -> dict:
        return {
            "schema_version": 1,
            "problem": self.session.problem,
            "settings": self.session.settings,
            "run": self.session.report() if self.events or self.session.status != "RUNNING" else None,
            "preview": self.trace,
        }

    def _filename(self, suffix: str) -> str:
        clean = "".join(c if c.isalnum() or c in "-_ " else "-" for c in self.project["problem"]["name"])
        return (clean.strip() or "Austin-project") + suffix

    def save(self, path: str | Path | None = None, *, save_as: bool = False) -> bool:
        self.stop_auto()
        if not self.apply_editor():
            return False
        overwrite = False
        if path is None:
            if self.path is not None and not save_as:
                path = self.path
                overwrite = True
                try:
                    unchanged = _disk_version(path) == self.disk_version
                except OSError:
                    unchanged = False
                if not unchanged:
                    self.set_message("The saved file changed on disk. Use Save As to keep both versions.", error=True)
                    return False
            else:
                path = filedialog.asksaveasfilename(
                    parent=self.root, title="Save Austin project", filetypes=FILETYPES,
                    defaultextension=".austin.json", initialfile=self._filename(".austin.json"),
                    confirmoverwrite=True,
                )
                overwrite = bool(path) and Path(path).exists()
        if not path:
            return False
        try:
            project = self._snapshot()
            save_project(path, project, overwrite=overwrite)
            version = _disk_version(Path(path))
        except (OSError, ValueError, RuntimeError) as error:
            self._failure(error)
            messagebox.showerror("Unable to save project", str(error), parent=self.root)
            return False
        self.path = Path(path)
        self.disk_version = version
        self.project = project
        self.dirty = False
        self._title()
        self.set_message(f"Saved {self.path.name}.")
        return True

    def export(self, path: str | Path | None = None) -> dict | None:
        self.stop_auto()
        if not self.apply_editor():
            return None
        overwrite = False
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="Export complete project", defaultextension=".zip",
                initialfile=self._filename(".zip"), filetypes=[("Complete project ZIP", "*.zip")],
                confirmoverwrite=True,
            )
            overwrite = bool(path) and Path(path).exists()
        if not path:
            return None
        if self.path and Path(path).resolve() == self.path.resolve():
            self.set_message("Choose an export path separate from the saved project.", error=True)
            return None
        try:
            receipt = export_project(path, self._snapshot(), overwrite=overwrite)
        except (OSError, ValueError, RuntimeError) as error:
            self._failure(error)
            messagebox.showerror("Unable to export project", str(error), parent=self.root)
            return None
        self.set_message(f"Exported {Path(path).name} · {receipt['entries']} files · {receipt['size_bytes']:,} bytes.")
        return receipt

    def close(self) -> None:
        self.stop_auto()
        if self._confirm_discard():
            self.future_reader.close()
            self.root.destroy()

    def show_page(self, name: str, *, apply_draft: bool = True) -> None:
        if self.current_page == "Build" and name == "Simulate" and apply_draft and not self.apply_editor():
            return
        if name != "Simulate":
            self.stop_auto()
        self.current_page = name
        self.pages[name].tkraise()
        for label, button in self.nav_buttons.items():
            button.configure(style="NavSelected.TButton" if label == name else "Nav.TButton")
        if name == "Results":
            self._refresh_results()
            self._render_code_if_visible()
        elif name == "Overview":
            self._refresh_overview()
        elif name == "Future Reader":
            self.future_reader.show()

    def _rounds(self) -> int:
        return _integer(self.rounds_var.get(), "Grover rounds")

    def _preview(self, rounds: int, *, announce: bool = True) -> bool:
        try:
            trace = self.session.preview(rounds)
        except ValueError as error:
            self._failure(error)
            return False
        self.rounds_var.set(str(rounds))
        self._set_trace(trace, event_index=None, last=announce)
        self.history_var.set("Next shot preview")
        if announce:
            self._mark_dirty()
            self.set_message("Preview ready. Pick a stage to inspect or measure.")
        return True

    def preview_current(self) -> bool:
        if self.auto_running:
            return False
        try:
            rounds = self._rounds()
        except ValueError as error:
            self._failure(error)
            return False
        return self._preview(rounds)

    def _set_trace(self, trace: dict, *, event_index: int | None, last: bool) -> None:
        if trace != self.trace:
            self.code_cache = None
        self.trace = trace
        self.trace_event = event_index
        self.frame_index = len(trace["frames"]) - 1 if last else 0
        self.selected_state = min(self.selected_state, len(trace["states"]) - 1)
        self.slider.configure(to=max(1, len(trace["frames"]) - 1))
        self.slider.set(self.frame_index)
        self._render_frame()

    def _scrub(self, value: str) -> None:
        if self.trace:
            index = min(len(self.trace["frames"]) - 1, max(0, round(float(value))))
            if index != self.frame_index:
                self.frame_index = index
                self._render_frame()

    def _frame_delta(self, delta: int) -> None:
        if self.trace:
            self.slider.set(max(0, min(len(self.trace["frames"]) - 1, self.frame_index + delta)))

    def _render_frame(self) -> None:
        if not self.trace:
            return
        frame = self.trace["frames"][self.frame_index]
        stage = frame["stage"]
        if stage == "prepare":
            title, caption = "Uniform preparation", "Every state starts equally likely."
        elif stage == "oracle":
            title = f"Round {frame['round']} · Oracle"
            caption = "The oracle flips marked phases. Probabilities stay fixed."
        else:
            title = f"Round {frame['round']} · Diffusion"
            caption = "Reflection around the mean redistributes probabilities."
        self.stage_title.set(f"{title}   {self.frame_index + 1}/{len(self.trace['frames'])}")
        self.stage_caption.set(caption)
        self.board.update_frame(self.trace, frame, self.selected_state)
        self.back_button.state(["!disabled"] if self.frame_index else ["disabled"])
        self.next_button.state(["!disabled"] if self.frame_index + 1 < len(self.trace["frames"]) else ["disabled"])
        self._render_inspector(frame)
        self._refresh_metrics()
        self._refresh_controls()

    def _select_state(self, index: int) -> None:
        self.selected_state = index
        self.board.selected = index
        self.board.draw()
        if self.trace:
            self._render_inspector(self.trace["frames"][self.frame_index])

    def _render_inspector(self, frame: dict) -> None:
        state = self.trace["states"][self.selected_state]
        bits = state["bits"]
        amp, prob = frame["amplitudes"][self.selected_state], frame["probabilities"][self.selected_state]
        kind = "Marked · passes threshold" if state["marked"] else (
            "Feasible · outside threshold" if state["feasible"] else "Invalid · fails a rule"
        )
        enabled = [row["name"] for bit, row in zip(reversed(bits), self.session.problem["variables"]) if bit == "1"]
        phase = "undefined at zero" if abs(amp) < 1e-12 else ("negative (π)" if amp < 0 else "positive (0)")
        for variable, value in zip(self.inspector_text, (
            f"|{bits}⟩", kind, f"Cost {state['cost']:,}   •   Probability {prob:.2%}",
            f"Amplitude {amp:+.5f}", f"Phase {phase}",
            "On: " + (", ".join(enabled) if enabled else "none"),
        )):
            variable.set(value)
        marked_probability = frame["marked_probability"]
        self.target_bar.configure(value=100 * marked_probability)
        incumbent = self.trace["incumbent"]
        threshold = "feasible states" if incumbent is None else f"cost below {incumbent:,}"
        self.target_text.set(f"{marked_probability:.2%} · {self.trace['marked_count']} marked\nTarget: {threshold}")
        if self.trace_event is not None:
            event = self.events[self.trace_event]
            self.session_hint.set(f"Recorded shot {event['shot']} measured {event['bits']} at cost {event['cost']:,}.")
        else:
            self.session_hint.set("Preview leaves the budget intact. Measurement samples one assignment.")

    def _refresh_metrics(self) -> None:
        settings = self.session.settings
        best = self.session.best
        for variable, value in zip(self.metric_values, (
            str(1 << len(self.project["problem"]["variables"])),
            f"{best['cost']:,} · {best['bits']}" if best else "—",
            f"{self.session.shots_used} / {settings['max_shots']}",
            f"{self.session.queries_used} / {settings['max_queries']}",
        )):
            variable.set(value)

    def _refresh_controls(self) -> None:
        frame = self.trace["frames"][self.frame_index]
        rounds = frame["round"]
        try:
            pending_rounds = self.trace_event is None and self._rounds() != self.trace["rounds"]
        except ValueError:
            pending_rounds = True
        if self.auto_running:
            message = "Auto run is measuring within the remaining budget."
        elif self.session.status != "RUNNING":
            message = STATUS_LABELS[self.session.status] + ". Reset the run to measure again."
        elif self.trace_event is not None:
            message = "This frame is from a recorded shot. Preview to prepare the next measurement."
        elif pending_rounds:
            message = "Preview your new round count to see and measure its stages."
        elif frame["stage"] == "oracle":
            message = "Step to diffusion or preparation to measure at a supported boundary."
        elif rounds > self.session.queries_remaining:
            message = f"Choose at most {self.session.queries_remaining} rounds with the remaining query budget."
        else:
            message = f"Measure this frame using 1 shot and {rounds} oracle {'query' if rounds == 1 else 'queries'}."
        enabled = (
            not self.auto_running and not pending_rounds and self.session.status == "RUNNING" and self.trace_event is None
            and frame["stage"] in {"prepare", "diffusion"} and rounds <= self.session.queries_remaining
        )
        self.measure_hint.set(message)
        self.measure_button.state(["!disabled"] if enabled else ["disabled"])
        self.preview_button.state(["disabled"] if self.auto_running else ["!disabled"])
        self.round_spin.state(["disabled"] if self.auto_running else ["!disabled"])
        self.run_button.configure(text="Pause" if self.auto_running else "Auto run")
        self.run_button.state(["!disabled"] if self.auto_running or self.session.status == "RUNNING" else ["disabled"])
        self.reset_button.state(["disabled"] if self.auto_running else ["!disabled"])

    def _refresh_history(self) -> None:
        labels = ["Next shot preview"] + [
            f"Shot {event['shot']} · {event['bits']}" for event in self.events
        ]
        self.history.configure(values=labels)
        self.history.current(self.trace_event + 1 if self.trace_event is not None else 0)

    def _history_selected(self, _event=None) -> None:
        if self.auto_running:
            self._refresh_history()
            return
        index = self.history.current() - 1
        if index < 0:
            self.preview_current()
        else:
            self._set_trace(self.events[index]["trace"], event_index=index, last=True)
            self._mark_dirty()

    def _record(self, event: dict) -> None:
        self.events.append(event)
        self.code_cache = None
        self._mark_dirty()
        measured = int(event["bits"], 2)
        self.selected_state = measured
        self._set_trace(event["trace"], event_index=len(self.events) - 1, last=True)
        self._refresh_history()
        self._refresh_results()

    def measure_here(self) -> dict | None:
        if not self.trace or self.measure_button.instate(["disabled"]):
            return None
        rounds = self.trace["frames"][self.frame_index]["round"]
        try:
            event = self.session.measure(rounds)
        except (ValueError, RuntimeError) as error:
            self._failure(error)
            return None
        self._record(event)
        result = "accepted as the new best" if event["accepted"] else (
            "feasible, with no lower cost" if event["feasible"] else "failed a rule"
        )
        self.set_message(f"Shot {event['shot']} measured {event['bits']} at cost {event['cost']:,} · {result}.")
        return event

    def toggle_auto(self) -> None:
        if self.auto_running:
            self.stop_auto()
            self.set_message(f"Paused after {self.session.shots_used} shots.")
        elif self.session.status == "RUNNING":
            self.auto_running = True
            self._refresh_controls()
            self.set_message("Automatic search started. Pause keeps all completed measurements.")
            self.auto_after = self.root.after(1, self._auto_tick)

    def _auto_tick(self) -> None:
        self.auto_after = None
        if not self.auto_running:
            return
        try:
            if self.session.status == "RUNNING":
                self._record(self.session.auto_step())
        except (RuntimeError, ValueError) as error:
            self.stop_auto()
            self._failure(error)
            return
        if self.session.status == "RUNNING":
            self.auto_after = self.root.after(25, self._auto_tick)
        else:
            self.stop_auto()
            self.set_message(f"{STATUS_LABELS[self.session.status]}. {self.session.shots_used} shots recorded.")

    def stop_auto(self) -> None:
        if self.auto_after is not None:
            self.root.after_cancel(self.auto_after)
            self.auto_after = None
        if self.auto_running:
            self.auto_running = False
            if self.trace:
                self._refresh_controls()

    def reset_run(self, *, confirm: bool = True) -> None:
        self.stop_auto()
        if confirm and self.events and not messagebox.askokcancel(
            "Reset search run", "Clear the measurements and restart with the current seed and budget?", parent=self.root,
        ):
            return
        self.session = SearchSession(self.project["problem"], **self.project["settings"])
        self.events = []
        self.code_cache = None
        self._mark_dirty()
        self.rounds_var.set(str(min(1, self.session.settings["max_rounds"])))
        self._preview(self._rounds(), announce=False)
        self._refresh_history()
        self._refresh_results()
        self.set_message("Search reset. The saved project file is available until you save again.")

    def compare_exact(self) -> None:
        self.exact_result = exact_minimum(self.session.problem)
        self._refresh_results()
        self.set_message("Exact classical comparison completed.")

    def optimize(self) -> bool:
        self.stop_auto()
        if not self.apply_editor():
            return False
        self.compare_exact()
        self.show_page("Overview", apply_draft=False)
        if self.exact_result["bits"] is None:
            self.set_message("Exact optimizer found no valid configuration.")
        else:
            self.set_message(f"Exact optimum found. Configuration {self.exact_result['bits']} costs {self.exact_result['cost']:,}.")
        return True

    def _refresh_results(self) -> None:
        best = self.session.best
        status = STATUS_LABELS[self.session.status]
        self.result_vars[0].set("Ready to search" if self.session.status == "RUNNING" and not self.events else status)
        self.result_vars[1].set(f"{best['bits']} · cost {best['cost']:,}" if best else "Awaiting a verified candidate")
        reference = self.exact_result
        if reference is None and self.session.settings["prove"]:
            reference = exact_minimum(self.session.problem)
            self.exact_result = reference
        if reference:
            self.result_vars[2].set(
                f"{reference['bits']} · cost {reference['cost']:,}" if reference["bits"] else "No feasible assignment"
            )
            suffix = f" · {len(reference['ties'])} tied optimum assignments" if reference["ties"] else ""
            self.exact_caption.set(
                f"Exact check: {reference['assignments_checked']} assignments, {reference['feasible_count']} feasible{suffix}."
            )
        else:
            self.result_vars[2].set("Choose Compare exact")
            self.exact_caption.set("The exact comparison uses every assignment in the declared search space.")
        previous = self.history_tree.selection()
        self.history_tree.delete(*self.history_tree.get_children())
        for index, event in enumerate(self.events):
            result = "New best" if event["accepted"] else "Feasible" if event["feasible"] else "Fails a rule"
            self.history_tree.insert("", "end", iid=str(index),
                                     values=(event["shot"], event["rounds"], event["bits"], event["cost"], result))
        if previous and self.history_tree.exists(previous[0]):
            self.history_tree.selection_set(previous[0])

    def inspect_selected_shot(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            self.set_message("Select a recorded shot first.")
            return
        index = int(selection[0])
        self.show_page("Simulate", apply_draft=False)
        self.selected_state = int(self.events[index]["bits"], 2)
        self._set_trace(self.events[index]["trace"], event_index=index, last=True)
        self._refresh_history()
        self._mark_dirty()

    def _program(self) -> str:
        if self.code_cache is None:
            self.code_cache = generated_program(self._snapshot())
        return self.code_cache

    def _render_code_if_visible(self) -> None:
        if self.current_page != "Results" or self.results_notebook.select() != str(self.code_tab):
            return
        try:
            program = self._program()
        except (OSError, ValueError, RuntimeError) as error:
            self._failure(error)
            return
        self.code_text.configure(state="normal")
        self.code_text.delete("1.0", "end")
        self.code_text.insert("1.0", program)
        self.code_text.configure(state="disabled")

    def copy_code(self) -> None:
        try:
            program = self._program()
            self.root.clipboard_clear()
            self.root.clipboard_append(program)
        except (OSError, ValueError, RuntimeError, tk.TclError) as error:
            self._failure(error)
            return
        self.set_message("Generated Python copied.")

    def smoke(self, directory: Path) -> dict:
        """Exercise the same interactive handlers with disposable local output."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.show_page("Build", apply_draft=False)
        self.prove_var.set(False)
        self.settings_vars["max_shots"].set("4")
        self.settings_vars["max_queries"].set("8")
        self.apply_button.invoke()
        if self.current_page != "Simulate":
            raise RuntimeError("The workbench could not apply the smoke-test problem.")
        self.rounds_var.set("1")
        self.preview_button.invoke()
        self.back_button.invoke()
        if self.stage_title.get().find("Oracle") < 0 or not self.measure_button.instate(["disabled"]):
            raise RuntimeError("The oracle frame did not enforce its measurement boundary.")
        self.next_button.invoke()
        self.measure_button.invoke()
        if self.session.shots_used != 1 or self.session.queries_used != 1:
            raise RuntimeError("A manual diffusion-frame measurement did not update the shared budget.")
        self.run_button.invoke()
        while self.auto_running:
            if self.auto_after is not None:
                self.root.after_cancel(self.auto_after)
            self._auto_tick()
        if self.session.shots_used != 4:
            raise RuntimeError("The automatic run did not finish within its shot budget.")
        self.show_page("Results")
        self.compare_exact()
        self.results_notebook.select(self.code_tab)
        self._render_code_if_visible()
        if not self.code_text.get("1.0", "end-1c").strip():
            raise RuntimeError("The workbench did not generate a replay program.")
        self.show_page("Overview")
        self.optimize_button.invoke()
        if self.exact_result is None or self.exact_result["cost"] != 4 or self.session.shots_used != 4:
            raise RuntimeError("Optimize did not find the exact cost while preserving the measured run.")
        self.open_assistant()
        if self.overview["heading"].get() not in self.assistant_answer.get("1.0", "end-1c"):
            raise RuntimeError("The assistant opening answer did not explain the current status.")
        self._ask_assistant("What can I do with the query budget?")
        if not self.assistant_answer.get("1.0", "end-1c").strip() or self.session.shots_used != 4:
            raise RuntimeError("The offline assistant did not return contextual help without changing the run.")
        self.assistant_window.destroy()
        project_path = directory / "smoke-project.austin.json"
        export_path = directory / "smoke-export.zip"
        if not self.save(project_path):
            raise RuntimeError("The smoke-test project could not be saved.")
        expected = self.session.report()
        if not self.open(project_path) or self.session.report() != expected:
            raise RuntimeError("Loading the smoke-test project did not reproduce the recorded run.")
        receipt = self.export(export_path)
        if not receipt:
            raise RuntimeError("The complete export could not be written.")
        oracle_receipt = None
        try:
            self.future_reader._get_api()
        except ModuleNotFoundError as error:
            if error.name != "future_reader" or getattr(sys, "frozen", False):
                raise
        else:
            oracle_receipt = self.future_reader.smoke(directory / "future-reader.sqlite3")
        return {
            "project": str(project_path),
            "export": str(export_path),
            "export_entries": receipt["entries"],
            "shots": self.session.shots_used,
            "queries": self.session.queries_used,
            "exact_cost": self.exact_result["cost"] if self.exact_result else exact_minimum(self.session.problem)["cost"],
            "status": self.session.status,
            "overview": True,
            "assistant": True,
            "future_reader": oracle_receipt,
        }


def main() -> None:
    root = tk.Tk()
    Workbench(root)
    root.mainloop()
