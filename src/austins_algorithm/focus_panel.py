"""Optional work checkpoints for the two portable Workbench project types."""

import tkinter as tk
from tkinter import ttk

from .focus_session import focus_action, focus_view, start_focus


class FocusPanel:
    def __init__(self, workbench):
        self.app = workbench
        self.project = self.app._focus_project()
        self.window = tk.Toplevel(workbench.root)
        if workbench.root.state() == "withdrawn":
            self.window.withdraw()
        self.window.title("Focus and resume — Austin’s Algorithm")
        self.window.geometry("720x690")
        self.window.minsize(580, 620)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        body = ttk.Frame(self.window, padding=16)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(8, weight=1)
        ttk.Label(body, text="Focus and resume", font=("Segoe UI", 17, "bold")).grid(sticky="w")
        instruction = ttk.Label(body, text=("Name one useful action. Park other requests, or interrupt and leave a checkpoint. "
                              "Save and Export in the main toolbar include this record."),
                                wraplength=640)
        instruction.grid(row=1, sticky="ew", pady=(6, 10))
        self.goal = tk.StringVar(self.window)
        self.next_action = tk.StringVar(self.window)
        self.note = tk.StringVar(self.window)
        self.reviewed = tk.BooleanVar(self.window)
        fields = ttk.Frame(body)
        fields.grid(row=2, sticky="ew")
        fields.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((("Goal", self.goal), ("Next action", self.next_action),
                                                ("Checkpoint or interruption", self.note))):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            entry = ttk.Entry(fields, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if row == 0:
                self.goal_entry = entry
        self.status = tk.StringVar(self.window)
        status_label = ttk.Label(body, textvariable=self.status, wraplength=640)
        status_label.grid(row=3, sticky="ew", pady=8)
        actions = ttk.Frame(body)
        actions.grid(row=4, sticky="w")
        for column, (text, action) in enumerate((("Start", "START"), ("Park request", "PARK"),
                                                ("Pause + checkpoint", "CHECKPOINT"))):
            ttk.Button(actions, text=text, command=lambda key=action: self.act(key)).grid(
                row=0, column=column, padx=(0, 6), pady=4)
        for column, (text, action) in enumerate((("Priority interrupt", "INTERRUPT"), ("Resume", "RESUME"),
                                                ("Finish session", "COMPLETE"))):
            ttk.Button(actions, text=text, command=lambda key=action: self.act(key)).grid(
                row=1, column=column, padx=(0, 6), pady=4)
        ttk.Checkbutton(body, text="I reviewed the current inputs and next action after a change",
                        variable=self.reviewed).grid(row=5, sticky="w", pady=6)
        ttk.Label(body, text="Parked requests and priority interruptions").grid(row=6, sticky="w")
        parked = ttk.Frame(body)
        parked.grid(row=7, sticky="ew")
        parked.columnconfigure(0, weight=1)
        self.pending = tk.Listbox(parked, height=5, exportselection=False)
        self.pending.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(parked, orient="vertical", command=self.pending.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.pending.configure(yscrollcommand=scrollbar.set)
        ttk.Button(parked, text="Mark selected resolved", command=self.resolve).grid(row=1, sticky="w", pady=5)
        history = ttk.Frame(body)
        history.grid(row=8, sticky="nsew", pady=(5, 8))
        history.columnconfigure(0, weight=1)
        history.rowconfigure(0, weight=1)
        self.history = tk.Text(history, height=7, wrap="word", state="disabled")
        self.history.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(history, command=self.history.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.history.configure(yscrollcommand=scroll.set)
        boundary = ttk.Label(body, text=("Focus describes recorded task choices. Review errors, evidence and approvals in their own screens. "
                                         "The session does not grant execution authority."), wraplength=640)
        boundary.grid(row=9, sticky="ew")
        self.wrapped_labels = (instruction, status_label, boundary)
        body.bind("<Configure>", self._fit_labels)
        ttk.Button(body, text="Close", command=self.window.destroy).grid(row=10, sticky="e", pady=(8, 0))
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.refresh()

    def _fit_labels(self, event):
        for label in self.wrapped_labels:
            label.configure(wraplength=max(320, event.width - 32))

    def _current(self):
        current = self.app._focus_project()
        if current is not self.project:
            raise ValueError("The project changed. Close and reopen Focus to use the current project.")
        return current

    def refresh(self):
        record = self.project.get("focus")
        self.pending.delete(0, "end")
        self.ids = []
        lines = []
        if record:
            self.goal_entry.configure(state="readonly")
            view = focus_view(record, self.project)
            self.goal.set(record["goal"])
            self.next_action.set(record["next_action"])
            changed = " · Inputs changed — review before resume" if view["inputs_changed"] else " · Inputs match checkpoint"
            self.status.set(record["status"] + changed)
            for item in record["interruptions"]:
                if not item["resolved"]:
                    self.ids.append(item["id"])
                    prefix = "PRIORITY" if item["priority"] else "Parked"
                    self.pending.insert("end", f"{prefix} · {item['text']}")
            lines = [f"Checkpoint\n{record['checkpoint'] or 'No pause recorded.'}\n"]
            lines += [f"{event['sequence']} · {event['action']} · {event['recorded_at']}\n"
                      f"{event['note']}\nNext action · {event['next_action']}\n" for event in record["events"]]
        else:
            self.status.set("No session started. Enter a goal and next action, then select Start.")
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.insert("1.0", "\n".join(lines))
        self.history.configure(state="disabled")
        self.app._refresh_focus_status()

    def act(self, action, *, interruption_id=None):
        try:
            project = self._current()
            if action == "START":
                if "focus" in project:
                    raise ValueError("This project already has a focus session. Its history is retained.")
                record = start_focus(project, self.goal.get(), self.next_action.get())
            else:
                if "focus" not in project:
                    raise ValueError("Start a focus session first.")
                record = focus_action(project["focus"], action, project, note=self.note.get(),
                                      next_action=self.next_action.get(), reviewed_changes=self.reviewed.get(),
                                      interruption_id=interruption_id)
            project["focus"] = record
            if self.app.current_page == "Decision Review":
                self.app.decision_review.dirty = True
                self.app.decision_review.title()
            else:
                self.app._mark_dirty()
            self.note.set("")
            self.reviewed.set(False)
            self.refresh()
            self.app.set_message("Focus checkpoint updated. Save the project to keep it across restarts.")
            return True
        except ValueError as error:
            self.status.set(str(error))
            return False

    def resolve(self):
        selected = self.pending.curselection()
        if not selected:
            self.status.set("Select a parked request first.")
            return False
        return self.act("RESOLVE", interruption_id=self.ids[selected[0]])
