"""Tk controls for offline decision review, replay, and practice."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .decision_review import load_decision_document, parse_decision_document, record_check, review_decision, revise_decision
from .decision_review_dimensional import load_dimensional
from .decision_review_project import (
    export_review_project, load_review_project, new_review_project, project_with_document, project_with_replay,
    record_review_decision, save_review_project,
)
from .decision_review_replays import inspect_replay, load_replay
from .decision_review_practice import practice_action, practice_view
from .workbench_project import _file_state


PROJECT_TYPES = [("Decision Review projects", "*.decision.json"), ("JSON files", "*.json")]


def _fill(text: tk.Text, content: str | Iterable[str]) -> None:
    if not isinstance(content, str):
        content = "\n".join(content)
    text.configure(state="normal")
    text.delete("1.0", "end")
    text.insert("1.0", content)
    text.configure(state="disabled")
    text.yview_moveto(0)


def _display(value) -> str:
    if value is None:
        return "Not recorded"
    if isinstance(value, str):
        return value or "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_display(item) for item in value) if value else "None recorded"
    return " · ".join(f"{key.replace('_', ' ').capitalize()}  {_display(item)}" for key, item in value.items())


def _holds(values: list[dict], *, empty: str = "No holds found.") -> list[str]:
    return [f"• {item.get('check_id', 'Check')} · {item.get('reason', '')}"
            + (f" · {item['owner']}" if item.get("owner") else "") for item in values] or [empty]


class DecisionReviewPanel:
    """One project-specific workbench page, with local files and read-only replay."""

    def __init__(self, workbench, page: ttk.Frame) -> None:
        self.workbench = workbench
        self.root = workbench.root
        self.page = page
        self.project: dict | None = None
        self.report: dict | None = None
        self.path: Path | None = None
        self.version: tuple[int, int, int, int] | None = None
        self.dirty = False
        self.editor: tk.Toplevel | None = None
        self.step = 0
        self.evidence_keys: dict[str, str] = {}
        self.current_practice: dict | None = None
        self.practice_options: dict[str, tk.BooleanVar] = {}
        self.practice_key: tuple | None = None
        self.headline = tk.StringVar(self.root)
        self.actor = tk.StringVar(self.root)
        self.reason = tk.StringVar(self.root)
        self.disposition = tk.StringVar(self.root, value="accepted")
        self.evidence_selection = tk.StringVar(self.root)
        self.evidence_status = tk.StringVar(self.root, value="UNKNOWN")
        self.evidence_reference = tk.StringVar(self.root)
        self.evidence_detail = tk.StringVar(self.root)
        self.prediction = tk.StringVar(self.root)
        self.practice_infeasible = tk.BooleanVar(self.root)
        self.stasis_index = tk.StringVar(self.root)
        self.page.rowconfigure(4, weight=1)
        self.workbench._heading(page, "Decision Review", "Review evidence, compare plans across scenarios, and rehearse what happens when a prerequisite fails.")
        controls = ttk.Frame(page, style="Surface.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        self._buttons(controls, (("Check & Optimize", self.optimize), ("Import case…", self.import_case),
                                 ("Import design journal…", self.import_dimensional), ("Edit case…", self.edit_case),
                                 ("Import replay…", self.import_replay)))
        self.workbench._label(page, style="Muted", textvariable=self.headline, wraplength=745).grid(
            row=3, column=0, sticky="ew", pady=(3, 8),
        )
        tabs = ttk.Notebook(page)
        self.tabs = tabs
        tabs.enable_traversal()
        tabs.grid(row=4, column=0, sticky="nsew")
        builders = (
            ("Plan", self._build_plan), ("Evidence", self._build_evidence), ("Handoff", self._build_handoff),
            ("Recovery replay", self._build_containment), ("Stasis trace", self._build_stasis), ("Practice", self._build_practice),
        )
        for label, build in builders:
            tab = ttk.Frame(tabs, style="Card.TFrame", padding=9)
            tab.columnconfigure(0, weight=1)
            tabs.add(tab, text=label)
            build(tab)

    def _buttons(self, row, buttons) -> None:
        for index, (text, command) in enumerate(buttons):
            self.workbench._button(row, text, command, style="Primary.TButton" if index == 0 else "TButton").pack(
                side="left", padx=(0, 6),
            )

    def _text(self, parent, row, *, editable=False, height=8) -> tk.Text:
        wrapper = ttk.Frame(parent, style="Card.TFrame")
        wrapper.grid(row=row, column=0, sticky="nsew", pady=(3, 0))
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(0, weight=1)
        parent.rowconfigure(row, weight=1)
        widget = tk.Text(
            wrapper, height=height, wrap="word", relief="solid", borderwidth=1, padx=10, pady=8,
            background="#ffffff", foreground="#172935", font=("Consolas" if editable else "Segoe UI", 10),
            state="normal" if editable else "disabled", takefocus=True,
        )
        widget.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(wrapper, orient="vertical", command=widget.yview)
        bar.grid(row=0, column=1, sticky="ns")
        widget.configure(yscrollcommand=bar.set)
        return widget

    def _build_plan(self, tab) -> None:
        self.plan_text = self._text(tab, 0)

    def _build_evidence(self, tab) -> None:
        self.evidence_text = self._text(tab, 0)
        row = ttk.Frame(tab, style="Card.TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        row.columnconfigure(1, weight=1)
        self.workbench._label(row, "Check", "Card").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.evidence_combo = ttk.Combobox(row, state="readonly", textvariable=self.evidence_selection)
        self.evidence_combo.grid(row=0, column=1, sticky="ew")
        self.evidence_combo.bind("<<ComboboxSelected>>", self._pick_evidence)
        ttk.Combobox(row, state="readonly", textvariable=self.evidence_status, values=("UNKNOWN", "PASS", "FAIL"), width=11).grid(
            row=0, column=2, padx=(6, 0),
        )
        self.workbench._label(row, "Source / reference", "Card").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(5, 0))
        ttk.Entry(row, textvariable=self.evidence_reference).grid(row=1, column=1, sticky="ew", pady=(5, 0))
        self.workbench._button(row, "Record evidence", self.record_evidence).grid(row=1, column=2, padx=(6, 0), pady=(5, 0))
        self.workbench._label(tab, style="CardMuted", textvariable=self.evidence_detail, wraplength=720).grid(
            row=2, column=0, sticky="ew",
        )

    def _build_handoff(self, tab) -> None:
        self.handoff_text = self._text(tab, 0)
        row = ttk.Frame(tab, style="Card.TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        row.columnconfigure(1, weight=1)
        self.workbench._label(row, "Reviewer name", "Card").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(row, textvariable=self.actor).grid(row=0, column=1, sticky="ew")
        ttk.Combobox(row, state="readonly", textvariable=self.disposition, values=("accepted", "revised", "paused", "reverted"), width=13).grid(
            row=0, column=2, padx=(6, 0),
        )
        self.workbench._label(row, "Decision reason", "Card").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(5, 0))
        ttk.Entry(row, textvariable=self.reason).grid(row=1, column=1, sticky="ew", pady=(5, 0))
        self.workbench._button(row, "Record decision", self.record_disposition).grid(row=1, column=2, padx=(6, 0), pady=(5, 0))
        self.workbench._button(row, "Edit handoff…", self.edit_handoff).grid(row=2, column=1, sticky="w", pady=(6, 0))

    def _build_containment(self, tab) -> None:
        self.workbench._label(tab, "An offline reconciliation of recorded or synthetic case outcomes.", "CardMuted").grid(
            row=0, column=0, sticky="w",
        )
        self.containment_text = self._text(tab, 1)

    def _build_stasis(self, tab) -> None:
        row = ttk.Frame(tab, style="Card.TFrame")
        row.grid(row=0, column=0, sticky="ew")
        self.previous_button = self.workbench._button(row, "Previous step", lambda: self.step_trace(-1))
        self.previous_button.pack(side="left", padx=(0, 6))
        self.next_button = self.workbench._button(row, "Next step", lambda: self.step_trace(1))
        self.next_button.pack(side="left", padx=(0, 6))
        self.workbench._label(row, style="Card", textvariable=self.stasis_index).pack(side="left")
        self.stasis_text = self._text(tab, 1)
        self.stasis_text.bind("<Left>", lambda _event: self._keyboard_step(-1))
        self.stasis_text.bind("<Right>", lambda _event: self._keyboard_step(1))

    def _build_practice(self, tab) -> None:
        self.practice_text = self._text(tab, 0, height=5)
        self.practice_choices = ttk.Frame(tab, style="Card.TFrame")
        self.practice_choices.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        for column in (0, 1):
            self.practice_choices.columnconfigure(column, weight=1)
        row = ttk.Frame(tab, style="Card.TFrame")
        row.grid(row=2, column=0, sticky="ew")
        self.workbench._label(row, "Predict minimum", "Card").pack(side="left")
        ttk.Entry(row, textvariable=self.prediction, width=7).pack(side="left", padx=5)
        self.predict_button = self.workbench._button(row, "Record prediction", lambda: self._practice_action("predict"))
        self.predict_button.pack(side="left", padx=(0, 5))
        self.hint_button = self.workbench._button(row, "Reveal hint", lambda: self._practice_action("hint"))
        self.hint_button.pack(side="left", padx=(0, 5))
        self.submit_button = self.workbench._button(row, "Check answer", lambda: self._practice_action("submit"), style="Primary.TButton")
        self.submit_button.pack(side="left", padx=(0, 5))
        self.continue_button = self.workbench._button(row, "Next exercise", lambda: self._practice_action("continue"))
        self.continue_button.pack(side="left")

    def title(self) -> None:
        if self.workbench.current_page != "Decision Review" or self.project is None:
            return
        title = self.project["document"]["title"]
        marker = " • Unsaved" if self.dirty else ""
        self.workbench.project_title.set(title)
        self.workbench.project_file.set((self.path.name if self.path else "Decision Review draft") + marker)
        self.root.title(f"{title} — Austin’s Algorithm" + (" *" if self.dirty else ""))

    def show(self) -> None:
        if self.project is None:
            self._activate(new_review_project(), None)
        else:
            self.title()

    def _activate(self, project: dict, path: Path | None, version=None) -> None:
        self.project = project
        self.path, self.version = path, version
        self.dirty = False
        self.step = 0
        self.report = None
        self.practice_key = None
        self.evidence_selection.set("")
        self.actor.set("")
        self.reason.set("")
        self._refresh()

    def _refresh(self) -> None:
        assert self.project is not None
        document = self.project["document"]
        self.title()
        state = self.report["status"] if self.report else "Ready to review"
        meanings = {
            "OPTIMUM": "Exact plan found", "INFEASIBLE": "No assignment fits all declared scenarios",
            "HELD": "Held · review the outstanding evidence",
        }
        self.headline.set(f"{document['origin'].title()} case · Revision {document['revision']} · {meanings.get(state, state)}")
        self._render_plan()
        self._render_evidence()
        self._render_handoff()
        self._render_containment()
        self._render_stasis()
        self._render_practice()

    def _render_plan(self) -> None:
        document = self.project["document"]
        names = {item["id"]: item["name"] for item in document["variables"]}

        def choices(selected):
            return ", ".join(names.get(option, option) for option in selected) or "No options selected"

        lines = [document["title"], document["baseline"], ""]
        if self.report:
            result = self.report["result"]
            status = self.report["status"]
            if status == "OPTIMUM":
                lines += [f"Shared plan · {choices(result['selected'])}", f"Exact minimum · {result['cost']} {document['cost_unit']}",
                          f"{result['assignments_checked']} assignments checked · {result['feasible_count']} feasible"]
                for tie in result["ties"][1:]:
                    lines.append(f"Equal-cost alternative · {choices(tie['selected'])}")
            elif status == "HELD":
                lines += _holds(self.report["holds"], empty="Check Evidence for the unresolved requirements.")
                if best := result.get("partial_best"):
                    lines.append(f"Known admissible candidate · {choices(best['selected'])} · {best['cost']} {document['cost_unit']}")
            elif status == "INFEASIBLE":
                lines.append("No shared assignment meets all currently declared hard rules.")
                lines += _holds(result.get("exclusion_checks", []), empty="")
            lines += ["", "Scenario results with independently chosen plans"]
            for scenario in self.report["scenarios"]:
                item = scenario["result"]
                lines.append(f"• {scenario['title']} · {scenario['status']}"
                             + (f" · {item['cost']} {document['cost_unit']} · {choices(item['selected'])}"
                                if item and item.get("cost") is not None else ""))
                if scenario.get("holds"):
                    lines += _holds(scenario["holds"])
            titles = {scenario["id"]: scenario["title"] for scenario in document["scenarios"]}
            if matrix := self.report.get("scenario_matrix"):
                lines += ["", "Check each candidate across all scenarios"]
                for row in matrix:
                    source = titles[row["source_scenario_id"]] if row["source_scenario_id"] else "Shared plan"
                    lines.append(f"• {source} choice · {choices(row['candidate']['selected'])}")
                    for outcome in row["outcomes"]:
                        lines.append(f"  {titles[outcome['scenario_id']]} · {outcome['status']}")
                        lines.extend(f"    {item['reason']}" for item in outcome["failures"])
                        if outcome["holds"]:
                            lines.extend(f"    {note}" for note in _holds(outcome["holds"]))
            if result.get("cheaper_exclusions"):
                lines += ["", "Why cheaper alternatives were excluded"]
                for item in result["cheaper_exclusions"]:
                    lines.append(f"• {choices(item['selected'])} · {item['cost']} {document['cost_unit']}")
                    for fail in item["failures"]:
                        lines.append(f"  {fail['scenario_id']} / {fail['rule_id']} · {fail['reason']} · {fail['source']}")
                    remaining = [scenario for scenario in item.get("failing_scenarios", []) if not item["failures"]
                                 or scenario != item["failures"][0]["scenario_id"]]
                    if remaining:
                        lines.append("  Also excluded in " + ", ".join(titles.get(scenario, scenario) for scenario in remaining))
                    lines.extend("  " + line for line in _holds(item.get("holds", []), empty="") if line)
        else:
            lines.append("Select Check & Optimize to inspect the evidence and compare the scenarios.")
        lines += ["", "Dependency map · prerequisites to the left of the arrow"]
        for item in document["variables"]:
            lines.append(f"  {choices(item['depends_on']) if item['depends_on'] else 'Baseline'} → {item['name']}")
        lines += ["", "Project stages and interfaces"]
        stages = {item["id"]: item for item in self.report["stages"]} if self.report else {}
        for item in document["variables"]:
            lines += ["", f"{item['name']} · {item['cost']} {document['cost_unit']}",
                      "Depends on  " + (choices(item["depends_on"]) if item["depends_on"] else "None")]
            for field, heading in (("starting_state", "Starting state"), ("change", "Change"), ("expected", "Expected"),
                                   ("stop", "Stop"), ("recovery", "Recovery")):
                lines.append(f"{heading}  {item[field]}")
            lines += [f"Interface  {entry['reference']} · {entry['tolerance']} · Owner {entry['owner']}" for entry in item["interfaces"]]
            held = stages.get(item["id"], {}).get("holds")
            if held:
                lines += _holds(held)
        lines += ["", "Hard rules by scenario"]
        for scenario in document["scenarios"]:
            lines.append(scenario["title"])
            lines += [f"• {rule['id']} · {rule['reason']} · {rule['source']}" for rule in scenario["rules"]]
        _fill(self.plan_text, lines)

    def _render_evidence(self) -> None:
        checks = self.project["document"]["checks"]
        effective = {item["id"]: item for item in self.report["checks"]} if self.report else {}
        lines = ["A replaced prerequisite can make an earlier check stale. Record downstream checks again after the prerequisite passes.", ""]
        previous = self.evidence_keys.get(self.evidence_selection.get())
        self.evidence_keys = {}
        for check in checks:
            name = f"{check['label']} ({check['id']})"
            self.evidence_keys[name] = check["id"]
            status = effective.get(check["id"], {})
            lines += [f"{name} · {status.get('effective_status', check['status'])}",
                      f"Owner {check['owner']} · Revision {check['revision']} · Reference {_display(check['reference'])}",
                      f"Depends on  {', '.join(check['depends_on']) or 'None'}"]
            if status.get("reason"):
                lines.append(str(status["reason"]))
            lines.append("")
        self.evidence_combo.configure(values=list(self.evidence_keys))
        selection = next((label for label, identifier in self.evidence_keys.items() if identifier == previous), None)
        if not self.evidence_selection.get() or self.evidence_selection.get() not in self.evidence_keys:
            self.evidence_selection.set(selection or next(iter(self.evidence_keys), ""))
            self._pick_evidence()
        if revisions := self.project.get("revisions"):
            lines += [f"{len(revisions)} prior evidence snapshots in the project and export", ""]
            for prior in reversed(revisions[-8:]):
                lines.append(f"Snapshot {prior['sequence']} · Revision {prior['document']['revision']} · {prior['input_sha256']}")
        _fill(self.evidence_text, lines)

    def _pick_evidence(self, _event=None) -> None:
        key = self.evidence_keys.get(self.evidence_selection.get())
        row = next((item for item in self.project["document"]["checks"] if item["id"] == key), None)
        self.evidence_status.set(row["status"] if row else "UNKNOWN")
        self.evidence_reference.set((row["reference"] or "") if row else "")
        self.evidence_detail.set(f"Named checker · {row['owner']}. Use a current, traceable source." if row else "No check selected.")

    def _render_handoff(self) -> None:
        data = self.project["document"]["handoff"]
        lines = [f"Decision owner  {_display(data['decision_owner'])}", f"Delivery owner  {_display(data['delivery_owner'])}",
                 f"Verifier  {_display(data['verifier'])}", f"Resources / funding  {_display(data['resources'])}", "",
                 "Who is affected and who can amend the proposal"]
        lines += [f"• {item['name']} · {item['role']} · Can amend  {_display(item['can_amend'])}" for item in data["affected"]]
        lines += ["", "Handoff evidence"]
        for key in ("authority_check", "consent_check", "funding_check"):
            identifier = data[key]
            effective = next((row for row in self.report["checks"] if row["id"] == identifier), None) if self.report else None
            lines.append(f"• {key.replace('_check', '').capitalize()} · {_display(identifier)}"
                         + (f" · {effective['effective_status']} · {_display(effective.get('reference'))}" if effective else ""))
        lines += ["", "Decision history"]
        for item in reversed(self.project["decision"]):
            lines.append(f"• {item['recorded_at']} · {item['actor']} · {item['status']} · {item['reason']} "
                         f"· Revision {item['document']['revision']} · {item['result_status']}")
        if not self.project["decision"]:
            lines.append("No human decision recorded.")
        _fill(self.handoff_text, lines)

    def _review_replay(self, kind: str) -> dict | None:
        document = self.project["replays"].get(kind)
        return inspect_replay(document) if document is not None else None

    def _replay_header(self, review: dict) -> list[str]:
        source = review["provenance"]
        return [review["title"], review["scope"], f"{source['origin']} · {source['source']}", f"SHA-256  {source['sha256']}", ""]

    def _render_containment(self) -> None:
        review = self._review_replay("containment")
        if not review:
            _fill(self.containment_text, "Import a Decision Containment record or start a new Decision Review example.")
            return
        lines = self._replay_header(review)
        for scenario in review["scenarios"]:
            lines += [f"{scenario['title']} · {scenario['phase']}",
                      f"Accepted {scenario['accepted']} · Completed once {scenario['completed_once']} · Pending {scenario['pending']}",
                      f"Lost {scenario['lost']} · Duplicated {scenario['duplicated']} · Extra effects {scenario['duplicate_effects']} "
                      f"· Failed {scenario['failed']} · Missing {scenario['missing']}",
                      f"Reconciled {scenario['reconciled']} · Recovery complete  {self._yes(scenario['recovery_complete'])}"]
            lines.extend(f"  {line}" for line in scenario["timeline"])
            if scenario["items"]:
                lines += ["Work items", *[f"  {_display(item)}" for item in scenario["items"]]]
            lines.append("")
        _fill(self.containment_text, lines)

    @staticmethod
    def _yes(value: bool) -> str:
        return "Yes" if value else "No"

    def _render_stasis(self) -> None:
        review = self._review_replay("stasis")
        steps = review["steps"] if review else []
        self.step = min(self.step, len(steps) - 1) if steps else 0
        self.previous_button.configure(state="normal" if self.step else "disabled")
        self.next_button.configure(state="normal" if self.step + 1 < len(steps) else "disabled")
        self.stasis_index.set(f"Step {self.step + 1} of {len(steps)}" if steps else "No trace imported")
        if not steps:
            _fill(self.stasis_text, "Import a Stasis Beam trace or start a new Decision Review example.")
            return
        current = steps[self.step]
        lines = self._replay_header(review)
        lines += [f"Legacy  {current['legacy']} · Fixed  {current['fixed']}", current["description"], "",
                  f"First different state  {_display(review['first_divergence'])}",
                  f"Premature RUN reproduced in legacy model  {self._yes(review['legacy_run_reproduced'])}",
                  f"Fixed trace stays stopped  {self._yes(review['fixed_stays_stopped'])}", "", "Input event"]
        lines += [f"• {key.replace('_', ' ').capitalize()}  {_display(value)}" for key, value in current["event"].items()]
        if checks := review.get("bench_checks"):
            lines += ["", "Proposed offline bench checks"]
            for check in checks:
                lines += [f"• {check['check']} · {check['setup']}",
                          f"  Criterion  {check['criterion']}", f"  Evidence  {check['evidence']}"]
        _fill(self.stasis_text, lines)

    def _keyboard_step(self, delta: int) -> str:
        self.step_trace(delta)
        return "break"

    def step_trace(self, delta: int) -> None:
        self.step = max(0, self.step + delta)
        self._render_stasis()

    def _render_practice(self) -> None:
        view = practice_view(self.project["practice"])
        self.current_practice = view
        cases = view.get("choices", [])
        current = (view.get("kind"), view.get("title"), tuple(choice["id"] for choice in cases))
        if current != self.practice_key:
            self.practice_key = current
            self.prediction.set("" if view.get("prediction") is None else str(view["prediction"]))
            self.practice_infeasible.set(False)
            for child in self.practice_choices.winfo_children():
                child.destroy()
            self.practice_options = {}
            for index, choice in enumerate(cases):
                active = tk.BooleanVar(self.root)
                self.practice_options[choice["id"]] = active
                ttk.Checkbutton(self.practice_choices, text=f"{choice['name']} ({choice['cost']})", variable=active).grid(
                    row=index // 2, column=index % 2, sticky="w",
                )
            if cases:
                ttk.Checkbutton(self.practice_choices, text="No feasible plan", variable=self.practice_infeasible).grid(
                    row=(len(cases) + 1) // 2, column=0, sticky="w",
                )
        self.predict_button.configure(state="normal" if cases else "disabled")
        self.hint_button.configure(state="normal" if view.get("hint_available") else "disabled")
        self.submit_button.configure(state="normal" if cases else "disabled")
        due_at = view.get("due_at")
        self.continue_button.configure(state="normal" if not cases and (view.get("ready") or due_at) else "disabled")
        self.continue_button.configure(text="Start exercise" if view.get("ready") else "Check for due exercise")
        lines = [view.get("title", "Decision practice"), view.get("prompt", "")]
        if cases:
            lines += ["Predict a minimum cost, then select the choices you would include or select No feasible plan."]
        for rule in view.get("rules", []):
            lines.append(f"• {_display(rule)}")
        if view.get("hint"):
            lines += ["", f"Hint  {_display(view['hint'])}"]
        if view.get("feedback"):
            lines += ["", f"Feedback  {_display(view['feedback'])}"]
        evidence = view.get("evidence", {})
        if evidence:
            lines += ["", "Practice evidence"]
            lines += [f"• {key.replace('_', ' ').capitalize()}  {_display(value)}" for key, value in evidence.items()]
        if due_at:
            lines += ["", f"Delayed check due  {due_at}"]
        if history := view.get("history"):
            lines += ["", "Earlier attempts", *[f"• {_display(item)}" for item in history]]
        _fill(self.practice_text, lines)

    def optimize(self) -> bool:
        try:
            self.report = review_decision(self.project["document"])
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self._refresh()
        self.workbench.set_message("Reviewed the declared rules and evidence. The result remains a local planning record.")
        return True

    def open_help(self) -> None:
        status = self.report["status"] if self.report else None
        guidance = {
            None: "Start with Check & Optimize. Plan compares the shared plan with the best candidate for each scenario.",
            "OPTIMUM": "Read why cheaper candidates were excluded, then open Handoff to record a human decision if appropriate.",
            "HELD": "Open Evidence to see who owns the missing or stale checks. Record source-backed observations and review again.",
            "INFEASIBLE": "Use the scenario comparison to locate the conflicting rule. Edit the case only if its rules or assumptions changed.",
        }
        messagebox.showinfo(
            "Decision Review · Local help",
            guidance.get(status, guidance[None])
            + "\n\nRecovery replay and Stasis trace inspect offline records. Practice uses separate learning cases.",
            parent=self.root,
        )

    def record_evidence(self) -> bool:
        key = self.evidence_keys.get(self.evidence_selection.get())
        if not key:
            self._error("Select an evidence check.")
            return False
        try:
            document = record_check(self.project["document"], key, status=self.evidence_status.get(),
                                    reference=self.evidence_reference.get().strip())
            updated = project_with_document(self.project, document)
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self._commit(updated, "Evidence recorded. Dependent checks were reassessed.", reviewed=True)
        return True

    def record_disposition(self) -> bool:
        try:
            status = self.disposition.get()
            updated = record_review_decision(self.project, status, self.actor.get().strip(), self.reason.get().strip())
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self.reason.set("")
        self._commit(updated, f"Recorded human decision · {status}.", reviewed=True)
        return True

    def _practice_action(self, action: str) -> bool:
        try:
            arguments = {}
            if action == "predict":
                arguments["prediction"] = int(self.prediction.get().strip(), 10)
            elif action == "submit":
                selected = [key for key, value in self.practice_options.items() if value.get()]
                if selected and self.practice_infeasible.get():
                    raise ValueError("Choose a plan or mark it infeasible.")
                if self.current_practice.get("prediction") is None and self.prediction.get().strip():
                    raise ValueError("Record your prediction before checking the answer.")
                arguments["choices"] = "infeasible" if self.practice_infeasible.get() else selected
            updated = {**self.project, "practice": practice_action(self.project["practice"], action, **arguments)}
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            self._render_practice()
            return False
        self._commit(updated, "Practice record updated.")
        return True

    def _commit(self, project: dict, message: str, *, reviewed=False) -> None:
        self.project = project
        self.dirty = True
        if reviewed:
            self.report = review_decision(project["document"])
        self._refresh()
        if self.path and self.save(quiet=True):
            self.workbench.set_message(message + f" Checkpoint saved to {self.path.name}.")
        elif not self.path:
            self.workbench.set_message(message + " Select Save to keep a copy.")

    def _error(self, error) -> None:
        self.workbench.set_message(str(error), error=True)

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        choice = messagebox.askyesnocancel(
            "Save Decision Review?", "Save changes to this Decision Review project before continuing?", parent=self.root,
        )
        return self.save() if choice is True else choice is False

    def new(self) -> None:
        if self.confirm_discard():
            self._activate(new_review_project(), None)
            self.workbench.set_message("Opened the offline intake example. Select Check & Optimize to compare its scenarios.")

    def open(self, path: str | Path | None = None) -> bool:
        if not self.confirm_discard():
            return False
        if path is None:
            path = filedialog.askopenfilename(parent=self.root, title="Open Decision Review project", filetypes=PROJECT_TYPES)
        if not path:
            return False
        try:
            path = Path(path).absolute()
            version = _file_state(path.lstat())
            project = load_review_project(path)
            if _file_state(path.lstat()) != version:
                raise ValueError("The Decision Review project changed while opening. Select it again.")
            self._activate(project, path, version)
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self.workbench.set_message(f"Opened {path.name}.")
        return True

    def save(self, path: str | Path | None = None, *, save_as=False, quiet=False) -> bool:
        overwrite = False
        if path is None and self.path and not save_as:
            path = self.path
            overwrite = True
        elif path is None:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="Save Decision Review project", defaultextension=".decision.json",
                initialfile="decision-review.decision.json", filetypes=PROJECT_TYPES, confirmoverwrite=True,
            )
            overwrite = bool(path) and Path(path).exists()
        if not path:
            return False
        path = Path(path).absolute()
        try:
            expected = self.version if self.path and path.resolve() == self.path.resolve() else None
            if expected is not None:
                try:
                    existing = _file_state(path.lstat())
                except FileNotFoundError:
                    existing = None
                if existing != expected:
                    raise ValueError("The Decision Review project changed on disk. Use Save As to keep your local changes.")
            save_review_project(path, self.project, overwrite=overwrite, expected_version=expected)
            version = _file_state(path.lstat())
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self.path, self.version, self.dirty = path, version, False
        self.title()
        if not quiet:
            self.workbench.set_message(f"Saved {path.name}. Evidence, replays, and practice will checkpoint here.")
        return True

    def export(self, path: str | Path | None = None) -> dict | None:
        overwrite = False
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="Export Decision Review evidence", defaultextension=".zip",
                initialfile="decision-review.zip", filetypes=[("Decision Review ZIP", "*.zip")], confirmoverwrite=True,
            )
            overwrite = bool(path) and Path(path).exists()
        if not path:
            return None
        path = Path(path)
        try:
            if self.path and path.resolve() == self.path.resolve():
                raise ValueError("Choose a different path for the export and saved Decision Review project.")
            receipt = export_review_project(path, self.project, overwrite=overwrite)
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return None
        self.workbench.set_message(f"Exported {path.name} · {receipt['files']} files · {receipt['status']}.")
        return receipt

    def import_case(self, path: str | Path | None = None) -> bool:
        return self._import_document(path, "Import Decision Review case", load_decision_document)

    def import_dimensional(self, path: str | Path | None = None) -> bool:
        return self._import_document(path, "Import Dimensional Design journal export", load_dimensional)

    def _import_document(self, path, title, load) -> bool:
        if path is None:
            path = filedialog.askopenfilename(parent=self.root, title=title, filetypes=[("JSON files", "*.json")])
        if not path:
            return False
        try:
            document = load(path)
            fresh = new_review_project(document)
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        if not self.confirm_discard():
            return False
        self._activate(fresh, None)
        self.dirty = True
        self.title()
        self.optimize()
        self.workbench.set_message(f"Imported {Path(path).name} into a new review. Check assumptions and evidence before accepting.")
        return True

    def import_replay(self, path: str | Path | None = None) -> bool:
        if path is None:
            path = filedialog.askopenfilename(
                parent=self.root, title="Import local Containment or Stasis replay", filetypes=[("JSON files", "*.json")],
            )
        if not path:
            return False
        try:
            updated = project_with_replay(self.project, load_replay(path))
        except (OSError, ValueError, RuntimeError) as error:
            self._error(error)
            return False
        self.step = 0
        self._commit(updated, f"Attached offline replay from {Path(path).name}.")
        return True

    def edit_case(self) -> None:
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.lift()
            return
        editor = tk.Toplevel(self.root)
        self.editor = editor
        editor.title("Edit Decision Review case")
        editor.geometry("820x650")
        editor.minsize(480, 330)
        editor.transient(self.root)
        editor.grab_set()
        editor.columnconfigure(0, weight=1)
        content = ttk.Frame(editor, style="Card.TFrame", padding=12)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        editor.rowconfigure(0, weight=1)
        self.workbench._label(
            content, "Edit named stages, interfaces, scenarios, or who can shape and carry out the decision.", "CardTitle",
            wraplength=730,
        ).grid(row=0, column=0, sticky="w")
        self.workbench._label(
            content, "Applying a changed definition starts a new revision. Re-record affected evidence on the Evidence tab.", "CardMuted",
            wraplength=730,
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        widget = self._text(content, 2, editable=True)
        current = self.project["document"]
        widget.insert("1.0", json.dumps(current, indent=2, ensure_ascii=False))
        feedback = tk.StringVar(editor)
        self.workbench._label(content, style="CardMuted", textvariable=feedback, wraplength=730).grid(row=3, column=0, sticky="ew")
        row = ttk.Frame(content, style="Card.TFrame")
        row.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        def apply():
            try:
                draft = parse_decision_document(widget.get("1.0", "end-1c"))
                if draft == current:
                    editor.destroy()
                    return
                revised = revise_decision(current, draft)
                updated = project_with_document(self.project, revised)
            except (OSError, ValueError, RuntimeError) as error:
                feedback.set(str(error))
                return
            editor.destroy()
            self._commit(updated, "Applied case revision. Review any checks marked stale.", reviewed=True)

        self._buttons(row, (("Apply as new revision", apply), ("Cancel", editor.destroy)))
        editor.bind("<Escape>", lambda _event: editor.destroy())
        widget.focus_set()

    def edit_handoff(self) -> None:
        current = self.project["document"]
        draft = deepcopy(current)
        handoff = draft["handoff"]
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit who shapes and delivers the decision")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(True, False)
        dialog.columnconfigure(0, weight=1)
        content = ttk.Frame(dialog, style="Card.TFrame", padding=12)
        content.grid(row=0, column=0, sticky="ew")
        content.columnconfigure(1, weight=1)
        fields = {}
        for row, (key, caption) in enumerate((
            ("decision_owner", "Decision owner"), ("delivery_owner", "Delivery owner"),
            ("verifier", "Verifier"), ("resources", "Resources or funding"),
        )):
            fields[key] = tk.StringVar(dialog, value=handoff[key])
            self.workbench._label(content, caption, "Card").grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(content, textvariable=fields[key], width=49).grid(row=row, column=1, sticky="ew", pady=2)
        check_names = {"No check required": None, **{f"{item['label']} ({item['id']})": item["id"] for item in draft["checks"]}}
        check_fields = {}
        for row, (key, caption) in enumerate((
            ("authority_check", "Authority check"), ("consent_check", "Consent check"), ("funding_check", "Funding check"),
        ), start=4):
            active = next(name for name, identifier in check_names.items() if identifier == handoff[key])
            check_fields[key] = tk.StringVar(dialog, value=active)
            self.workbench._label(content, caption, "Card").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Combobox(content, state="readonly", textvariable=check_fields[key], values=tuple(check_names)).grid(
                row=row, column=1, sticky="ew", pady=2,
            )
        self.workbench._label(content, "Affected people", "CardTitle").grid(row=7, column=0, sticky="w", pady=(9, 2))
        people = tk.Listbox(content, height=4, exportselection=False)
        people.grid(row=8, column=0, columnspan=2, sticky="ew")
        person = tk.StringVar(dialog)
        role = tk.StringVar(dialog)
        amend = tk.StringVar(dialog, value="Unknown")
        options = {"Unknown": None, "Yes": True, "No": False}
        for row, (caption, variable) in enumerate((("Person or group", person), ("Role", role)), start=9):
            self.workbench._label(content, caption, "Card").grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(content, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)
        self.workbench._label(content, "Can amend?", "Card").grid(row=11, column=0, sticky="w", pady=2)
        ttk.Combobox(content, state="readonly", textvariable=amend, values=tuple(options)).grid(row=11, column=1, sticky="ew", pady=2)
        feedback = tk.StringVar(dialog)
        self.workbench._label(content, style="CardMuted", textvariable=feedback, wraplength=500).grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=(8, 0),
        )

        def display_people():
            people.delete(0, "end")
            for item in handoff["affected"]:
                people.insert("end", f"{item['name']} · {item['role']} · {_display(item['can_amend'])}")

        def pick(_event=None):
            if selected := people.curselection():
                item = handoff["affected"][selected[0]]
                person.set(item["name"])
                role.set(item["role"])
                amend.set(next(label for label, value in options.items() if value is item["can_amend"]))

        def update_person(add=False):
            item = {"name": person.get().strip(), "role": role.get().strip(), "can_amend": options[amend.get()]}
            selected = people.curselection()
            if not item["name"] or not item["role"]:
                feedback.set("Enter a name and role for the affected person or group.")
            elif add:
                handoff["affected"].append(item)
                display_people()
            elif selected:
                handoff["affected"][selected[0]] = item
                display_people()
            else:
                feedback.set("Select the person or group to update.")

        def remove_person():
            if selected := people.curselection():
                del handoff["affected"][selected[0]]
                display_people()

        def apply():
            for key, variable in fields.items():
                handoff[key] = variable.get().strip()
            for key, variable in check_fields.items():
                handoff[key] = check_names[variable.get()]
            try:
                if draft != current:
                    updated = project_with_document(self.project, revise_decision(current, draft))
                    self._commit(updated, "Revised the handoff. Reassess its affected checks.", reviewed=True)
            except (OSError, ValueError, RuntimeError) as error:
                feedback.set(str(error))
                return
            dialog.destroy()

        actions = ttk.Frame(content, style="Card.TFrame")
        actions.grid(row=12, column=0, columnspan=2, sticky="w", pady=(5, 0))
        for label, command in (("Add person", lambda: update_person(add=True)), ("Update selected", update_person),
                               ("Remove selected", remove_person)):
            self.workbench._button(actions, label, command).pack(side="left", padx=(0, 6))
        modal_controls = ttk.Frame(content, style="Card.TFrame")
        modal_controls.grid(row=14, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self._buttons(modal_controls, (("Apply handoff revision", apply), ("Cancel", dialog.destroy)))
        people.bind("<<ListboxSelect>>", pick)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        display_people()
