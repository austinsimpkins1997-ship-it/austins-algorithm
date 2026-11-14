"""Native Workbench view of the optional Future Reader practice controller."""

import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import sqlite3
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import uuid4


ACTOR = "workbench-practice"


class FutureReaderPanel:
    def __init__(self, workbench, page):
        self.workbench = workbench
        self.page = page
        self.controller = None
        self.pending = None
        self.store = None
        self.active_pointer = None
        self.route_options = {}
        workbench._heading(page, "Future Reader", "Preview a route, see when the choice changes, and verify what happened.")
        card = workbench._card(page, 11)
        card.grid(row=2, column=0, sticky="nsew")
        page.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)
        card.rowconfigure(7, weight=1)
        self.headline = tk.StringVar()
        self.status = tk.StringVar()
        self.route = tk.StringVar()
        self.custom = tk.StringVar()
        self.tick = tk.StringVar(value="0")
        self.check = tk.BooleanVar(value=True)
        workbench._label(card, style="CardLarge", textvariable=self.headline, wraplength=680).grid(row=0, column=0, sticky="w")
        workbench._label(card, style="Card", textvariable=self.status, wraplength=680).grid(row=1, column=0, sticky="w", pady=(3, 6))
        context = ttk.Frame(card, style="Card.TFrame")
        context.grid(row=2, column=0, sticky="ew")
        workbench._label(context, "Practice seconds", "Card").pack(side="left")
        ttk.Entry(context, textvariable=self.tick, width=7).pack(side="left", padx=5)
        ttk.Checkbutton(context, text="Independent check available", variable=self.check).pack(side="left", padx=5)
        workbench._button(context, "Apply context", self.apply_context).pack(side="left", padx=5)
        route_row = ttk.Frame(card, style="Card.TFrame")
        route_row.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        route_row.columnconfigure(1, weight=1)
        workbench._label(route_row, "Route", "Card").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.routes = ttk.Combobox(route_row, state="readonly", textvariable=self.route)
        self.routes.grid(row=0, column=1, sticky="ew")
        self.routes.bind("<<ComboboxSelected>>", self.clear_selection_preview)
        custom_row = ttk.Frame(card, style="Card.TFrame")
        custom_row.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        custom_row.columnconfigure(1, weight=1)
        workbench._label(custom_row, "Custom nodes (S B G)", "Card").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(custom_row, textvariable=self.custom).grid(row=0, column=1, sticky="ew")
        self.custom.trace_add("write", lambda *_: self.clear_selection_preview() if self.route.get() == "Custom" else None)
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        for label, callback in (("Pinpoint", self.refresh), ("Preview Travel", self.preview_travel),
                                ("Preview Reverse", self.preview_reverse), ("Check", self.certify)):
            workbench._button(actions, label, callback).pack(side="left", padx=(0, 5))
        self.confirm_button = workbench._button(actions, "Confirm", self.confirm, style="Primary.TButton")
        self.confirm_button.pack(side="left")
        second = ttk.Frame(card, style="Card.TFrame")
        second.grid(row=6, column=0, sticky="ew", pady=(7, 5))
        for label, callback in (("Wait comparison", self.wait), ("Live recovery check", self.start_recovery),
                                ("Checkpoint…", self.checkpoint_menu), ("New practice", self.reset),
                                ("Offline HTML", self.export_html)):
            workbench._button(second, label, callback).pack(side="left", padx=(0, 5))
        detail = ttk.Frame(card, style="Card.TFrame")
        detail.grid(row=7, column=0, sticky="nsew")
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(0, weight=1)
        self.details = tk.Text(detail, height=6, wrap="word", font=("Consolas", 10), state="disabled")
        self.details.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(detail, command=self.details.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=scrollbar.set)
        self.confirm_button.state(["disabled"])
        self.headline.set("Open this view to start a practice session.")

    def show(self):
        if self.controller is None:
            self.reset(reopen=True)
        else:
            self.refresh(self.status.get())

    def _get_api(self):
        from future_reader import controller

        return controller

    def _record(self, value):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", json.dumps(value, indent=2, ensure_ascii=True))
        self.details.configure(state="disabled")

    def _error(self, error):
        self.pending = None
        self.confirm_button.state(["disabled"])
        self.status.set(str(error))

    def clear_selection_preview(self, _event=None):
        if self.pending:
            self.pending = None
            self.confirm_button.state(["disabled"])
            self.status.set("The selected route changed. Preview the new choice before confirming.")

    def close(self):
        if self.controller is not None:
            self.controller.close()
            self.controller = None

    def reset(self, store=None, *, reopen=False):
        self.close()
        self.pending = None
        self.confirm_button.state(["disabled"])
        try:
            api = self._get_api()
            if store is not None:
                self.store = store
            elif self.store is None:
                data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ChronoRealm" / "AustinWorkbench"
                data.mkdir(parents=True, exist_ok=True)
                self.store = data / "future-reader-practice.sqlite3"
                self.active_pointer = data / "future-reader-active.txt"
            session = "desktop-practice" if reopen else f"desk-{uuid4().hex}"
            if reopen and self.active_pointer is not None and self.active_pointer.is_file():
                with self.active_pointer.open("r", encoding="ascii") as stream:
                    active = stream.read(60).strip()
                if re.fullmatch("desktop-practice|desk-[0-9a-f]{32}", active):
                    session = active
            self.controller = api.Controller(self.store, session=session)
            self.controller.set_grant(ACTOR, True)
            if self.active_pointer is not None:
                temp = self.active_pointer.with_name(f".future-reader-{uuid4().hex}.tmp")
                try:
                    temp.write_text(session, encoding="ascii")
                    temp.replace(self.active_pointer)
                finally:
                    temp.unlink(missing_ok=True)
            self.refresh("Practice started. Choose a route and preview it.")
        except ModuleNotFoundError as error:
            if error.name != "future_reader":
                raise
            self.headline.set("Future Reader runtime is unavailable in this installation.")
            self.status.set("Open the separate offline companion or use a build that includes Future Reader.")
        except (OSError, sqlite3.Error, ValueError) as error:
            self._error(error)

    def refresh(self, message=None):
        if self.controller is None:
            return
        api = self._get_api()
        state = self.controller.pinpoint()
        available = state["evidence"]["independent_check"]
        report = api.opportunity(state["now"], available, start=state["state"])
        options = {}
        for candidate in report["routes"]:
            if candidate["eligible"] and len(candidate["path"]) > 1:
                label = " → ".join(api.domain()["nodes"][node] for node in candidate["path"])
                options[label] = candidate["path"]
        self.route_options = options
        options_with_custom = [*options, "Custom"] if state["state"] != "G" else []
        self.routes.configure(values=options_with_custom)
        if self.route.get() not in options_with_custom:
            self.route.set(options_with_custom[0] if options_with_custom else "")
        self.tick.set(str(state["now"]))
        self.check.set(available)
        best = report["recommended"]
        if state["state"] == "G":
            summary = "Goal reached in the practice simulation. Check or preview a restore."
        elif best:
            summary = (f"{state['label']} at {state['now']} s · {report['status'].title()} window. "
                       f"Latest safe start: {report['latest_safe_start']} s.")
        else:
            summary = f"{state['label']} · No eligible route at {state['now']} s."
        self.headline.set(summary)
        basis = ("Independent check available." if available else "Independent check unavailable.")
        self.status.set(message or f"{basis} Simulated deadline: {report['deadline']} s.")
        self._record({"current": state, "opportunity": report})

    def apply_context(self):
        if self.controller is None:
            return
        try:
            self.controller.set_context(now=int(self.tick.get()), check_available=self.check.get())
            self.refresh("Context applied. Any older preview must pass the current state check again.")
        except (OSError, sqlite3.Error, ValueError) as error:
            self._error(error)

    def _preview(self, callback, **fields):
        if self.controller is None:
            return
        try:
            preview = callback(ACTOR, **fields)
            if preview["status"] != "PREVIEW":
                self._error("The current window has no eligible route.")
                self._record(preview)
                return
            self.pending = preview["preview_id"]
            self.confirm_button.state(["!disabled"])
            self.status.set("Preview only. Review the route or restore below, then choose Confirm.")
            self._record(preview)
        except (OSError, sqlite3.Error, ValueError) as error:
            self._error(error)

    def preview_travel(self):
        if self.controller is None:
            return
        selected = self.route.get()
        if selected == "Custom":
            text = self.custom.get()
            if len(text) > 40:
                self._error("Enter up to four practice node labels separated by spaces.")
                return
            path = text.upper().split()
        else:
            path = self.route_options.get(selected)
        self._preview(self.controller.travel, path=path)

    def preview_reverse(self):
        if self.controller is not None:
            self._preview(self.controller.reverse)

    def confirm(self):
        if self.controller is None or self.pending is None:
            return
        try:
            state = self.controller.pinpoint()
            if (int(self.tick.get()), self.check.get()) != (state["now"], state["evidence"]["independent_check"]):
                raise ValueError("Apply the edited practice context and create a fresh preview.")
            result = self.controller.commit(ACTOR, self.pending)
            self.pending = None
            self.confirm_button.state(["disabled"])
            self.refresh(f"{result['operation'].title()} verified. Observed state: {result['state']} at {result['now']} s.")
            self._record(result)
            return result
        except (OSError, sqlite3.Error, ValueError) as error:
            self._error(error)

    def certify(self):
        if self.controller is None:
            return
        result = self.controller.certify()
        certificate = result["certificate"]
        self.status.set(f"Finite route check: {certificate['status'].replace('_', ' ').title()}.")
        self._record(result)
        return result

    def wait(self):
        if self.controller is None:
            return
        state = self.controller.pinpoint()
        result = self._get_api().compare_wait(state["now"], state["evidence"]["independent_check"],
                                              check_returns=state["now"] + 2, start=state["state"])
        def label(candidate):
            return " → ".join(candidate["path"]) if candidate else "no feasible route"
        self.status.set(f"Act now: {label(result['act_now'])}. Wait until {result['check_returns']} s: "
                        f"{label(result['wait_positive_check'])} if the check passes, "
                        f"{label(result['wait_negative_check'])} otherwise.")
        self._record(result)

    def start_recovery(self):
        if self.store is None or getattr(self, "recovery_queue", None) is not None:
            return
        result = SimpleQueue()
        self.recovery_queue = result
        store = self.store.with_name("future-reader-failover.sqlite3")
        self.status.set("Running a controlled local worker failure. You can continue using the workbench.")

        def run():
            try:
                from future_reader.live_failover import demo

                result.put((demo(store), None))
            except Exception as error:
                result.put((None, error))

        Thread(target=run, daemon=True, name="future-reader-failover").start()
        self.workbench.root.after(80, self._poll_recovery)

    def _poll_recovery(self):
        try:
            result, error = self.recovery_queue.get_nowait()
        except Empty:
            self.workbench.root.after(80, self._poll_recovery)
            return
        self.recovery_queue = None
        if error is not None:
            self._error(error)
            return
        self.show_recovery(result)

    def show_recovery(self, result):
        self.status.set("A local worker exited. Its replacement resumed the practice job and verified the goal.")
        self._record(result)

    def export(self):
        if self.controller is None:
            return
        destination = filedialog.asksaveasfilename(parent=self.workbench.root, title="Export Future Reader checkpoint",
                                                  defaultextension=".json", filetypes=[("JSON", "*.json")])
        if destination:
            try:
                record = self.controller.checkpoint()
                with Path(destination).open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, indent=2)
                self.status.set("Checkpoint exported. It carries the historical record and no execution grant.")
            except (OSError, sqlite3.Error, ValueError) as error:
                self._error(error)

    def checkpoint_menu(self):
        if self.controller is None:
            return
        dialog = tk.Toplevel(self.workbench.root)
        dialog.title("Future Reader checkpoint")
        dialog.transient(self.workbench.root)
        choices = ttk.Frame(dialog, padding=14)
        choices.pack(fill="both", expand=True)
        ttk.Label(choices, text="Export this session or inspect a saved checkpoint.").pack(anchor="w", pady=(0, 8))
        for label, callback in (("Export current checkpoint", self.export), ("Inspect saved checkpoint", self.inspect_checkpoint)):
            ttk.Button(choices, text=label, command=lambda run=callback: (dialog.destroy(), run())).pack(fill="x", pady=2)
        ttk.Button(choices, text="Cancel", command=dialog.destroy).pack(fill="x", pady=2)

    def inspect_checkpoint(self):
        destination = filedialog.askopenfilename(parent=self.workbench.root, title="Inspect a saved checkpoint",
                                                filetypes=[("JSON", "*.json")])
        if destination:
            try:
                from future_reader.codec import read_json
                checkpoint = read_json(destination)
                if not self._get_api().verify_checkpoint(checkpoint):
                    raise ValueError("Checkpoint digest or replay failed.")
                self.status.set("Checkpoint replay verified for inspection. Active practice is unaffected.")
                self._record(checkpoint)
            except (OSError, ValueError) as error:
                self._error(error)

    def export_html(self):
        asset = Path(__file__).with_name("workbench_assets") / "future-reader.html"
        if not asset.is_file():
            asset = Path(__file__).resolve().parents[3] / "future-reader" / "future-reader.html"
        if not asset.is_file():
            self.status.set("The standalone companion is available as a separate local artifact.")
            return
        destination = filedialog.asksaveasfilename(parent=self.workbench.root, title="Save the offline companion",
                                                  initialfile="future-reader.html", defaultextension=".html")
        if destination:
            try:
                with Path(destination).open("xb") as stream:
                    stream.write(asset.read_bytes())
                self.status.set("Offline companion saved. Open the HTML file in your browser.")
            except OSError as error:
                self._error(error)

    def smoke(self, store):
        self.reset(store)
        self.custom.set("S B G")
        self.route.set("Custom")
        self.preview_travel()
        assert self.pending and self.controller.pinpoint()["state"] == "S"
        self.tick.set("1")
        self.apply_context()
        self.confirm()
        assert self.controller.pinpoint()["state"] == "S"
        self.preview_travel()
        moved = self.confirm()
        assert moved["state"] == "G" and moved["verification"] == "OBSERVED_MATCH"
        self.preview_reverse()
        restored = self.confirm()
        assert restored["state"] == "S" and restored["now"] == moved["now"]
        from future_reader.controller import verify_certificate, verify_checkpoint
        assert verify_certificate(self.certify())
        assert verify_checkpoint(self.controller.checkpoint())
        from future_reader.live_failover import demo
        recovery = demo(store.with_name("future-reader-failover.sqlite3"))
        assert recovery["supervisor_outcome"] == "COMPLETE" and recovery["injected_process_loss"]
        self.show_recovery(recovery)
        self.close()
        return {"custom": True, "stale_rejected": True, "moved": moved["state"],
                "restored": restored["state"], "elapsed_preserved": True, "recovery": True,
                "live_worker_generation": recovery["effects"][-1]["generation"],
                "live_failed_pid": recovery["injected_process_loss"]["pid"]}
