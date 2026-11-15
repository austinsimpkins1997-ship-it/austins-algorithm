"""Import a local Dimensional Design snapshot as an offline decision draft."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .decision_review import DECISION_SCHEMA, ID, MAX_CHECKS, MAX_VARIABLES, UNASSIGNED_COST_UNIT, _digest, validate_decision
from .decision_review_replays import MAX_REPLAY_BYTES, _json_bytes
from .dimensional_browser_replay import PHASES as BROWSER_PHASES, SCHEMA as BROWSER_SCHEMA, latest_evidence, replay_browser
from .workbench_project import _decode_json, _read_regular


JOURNAL_SCHEMA = "ChronoRealmDimensionalDesign/1"
_PHASES = ("entry", "invariant", "outcome", "recovery")
_STATES = {"unknown", "ready", "in_progress", "waiting", "blocked", "degraded", "failed", "recovering", "verified"}
_REVIEWS = {"open", "accepted", "revised", "paused", "reverted"}
_RESULTS = {"pass": "PASS", "fail": "FAIL", "unknown": "UNKNOWN"}
_ORIGINS = {"simulation": "SYNTHETIC", "observed": "OBSERVED"}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _object(value, label):
    _require(isinstance(value, dict), f"Provide a Dimensional Design {label} object.")
    return value


def _array(value, label, maximum):
    _require(isinstance(value, list) and len(value) <= maximum, f"Too many or invalid {label}.")
    return value


def _text(value, label, maximum=512):
    _require(isinstance(value, str) and 0 < len(value.strip()) <= maximum and all(c.isprintable() for c in value),
             f"Provide a printable {label} of at most {maximum} characters. Export a smaller review if needed.")
    return value.strip()


def _ident(value, label):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None,
             f"Invalid Dimensional Design {label} identifier.")
    return value


def _number(value, label, low, high):
    _require(type(value) is int and low <= value <= high, f"Invalid Dimensional Design {label}.")
    return value


def _mapped_id(prefix, *values):
    readable = f"{prefix}-{'-'.join(values).lower()}"
    if ID.fullmatch(readable) and all(value == value.lower() and "-" not in value for value in values):
        return readable
    return f"{prefix}-{hashlib.sha256('/'.join(values).encode('ascii')).hexdigest()[:20]}"


def _check(key, owner, label, status, reference, revision, *, event=1, prerequisites=(), basis=None):
    return {"id": key, "owner": owner, "label": label, "status": status, "reference": reference,
            "revision": revision, "assessment": max(1, event), "depends_on": list(prerequisites), "basis": basis or {}}


def _browser_excerpt(value, maximum=512):
    # Native text permits line breaks and the canonical editor requires printable text.
    visible = "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in value)
    suffix = " … [see linked journal]"
    return visible if len(visible) <= maximum else visible[:maximum - len(suffix)].rstrip() + suffix


def _browser_observation(change, phase, target, row, events):
    if row is None:
        return f"No {phase} observation for {target} in journal revision {change['revision']}."
    event = events[row["event"] - 1]
    return _browser_excerpt(
        f"Journal revision {row['revision']}, event {row['event']}, {row['basis']}. "
        f"Observed {row['observed_at']} · recorded {event['recorded_at']} · "
        f"actor {row['checker']} · source {row['source']} · {row['observation']}"
    )


def _browser_draft(document, source, fingerprint, mapping):
    project, transitions = replay_browser(document)
    version = document["version"]
    baseline, changes = project["baseline"], project["changes"]
    _require(1 <= len(changes) <= MAX_VARIABLES, f"Import a browser journal with 1 to {MAX_VARIABLES} bounded stages.")
    title = _browser_excerpt(baseline["title"], 120)
    owner = _browser_excerpt(baseline["owner"])
    headline = _browser_excerpt(f"{baseline['condition']} Preserve: {baseline['preserve']}. Reference: {baseline['reference']}")
    modes = {row["basis"] for change in changes for row in change["evidence"]}
    origin = "OBSERVED" if "observed" in modes and "simulated" not in modes else "SYNTHETIC"
    source_note = (f"{BROWSER_SCHEMA} · {source} · project {project['id']} · "
                   f"SHA-256 {fingerprint} · replayed {version} events. "
                   f"Recorded bases: {', '.join(sorted(modes)) or 'none supplied'}. Source assertions need review.")
    variables, checks, admissions = [], [], {}
    for change in changes:
        key, plan, revision = change["id"], change["plan"], change["revision"]
        _require(len(plan["interfaces"]) <= 6, "Import at most six interfaces per stage into Decision Review.")
        rows = latest_evidence(change)
        checks_for_admission = []
        for phase in BROWSER_PHASES:
            for target in ("general", *(field["id"] for field in plan["interfaces"])):
                row = rows.get((phase, target))
                # Keep recorded failures and restoration evidence separate from permission to use the stage.
                record = _check(
                    _mapped_id("check", key, phase, target), _browser_excerpt(plan["checker"]),
                    f"{key} · {phase} · {target}", _RESULTS[row["result"]] if row else "UNKNOWN",
                    _browser_observation(change, phase, target, row, document["events"]), version,
                    event=row["event"] if row else 1,
                )
                checks.append(record)
                if phase != "recovery":
                    checks_for_admission.append(record)
        parents = [admissions[item] for item in plan["dependencies"]]
        prerequisites = [*parents, *checks_for_admission]
        state, decision = change["state"], change["decision"]
        history = transitions[key]
        acceptance = max((seq for seq, action, rev, _, _ in history if action == "accept" and rev == revision),
                         default=0)
        passed = ((state, decision) == ("verified", "accepted") and acceptance > 0
                  and all(check["status"] == "PASS" and check["assessment"] < acceptance for check in prerequisites))
        status = "PASS" if passed else "FAIL" if state in ("failed", "reverted") else "UNKNOWN"
        important = [
            f"{seq} {status_at_event}/{label}" for seq, action, _, status_at_event, label in history
            if action in ("accept", "revert", "revise", "pause") or (action == "evidence" and status_at_event == "failed")
        ]
        detail = f"Journal revision {revision} · current {state}/{decision} · event {history[-1][0]}."
        if (state, decision) == ("verified", "accepted") and not passed:
            detail += " Review and reaccept after the current evidence or prerequisite changed."
        if important:
            detail += f" History (latest first): {', '.join(reversed(important))}."
        event = acceptance if passed else history[-1][0]
        gate = _check(f"admission-{key}", _browser_excerpt(plan["checker"]), f"Stage {key} accepted with current evidence",
                      status, _browser_excerpt(detail), version, event=event,
                      prerequisites=[check["id"] for check in prerequisites],
                      basis={check["id"]: _digest(check) for check in prerequisites} if passed else {})
        checks.append(gate)
        admissions[key] = gate
        variables.append({
            "id": f"stage-{key}", "name": _browser_excerpt(plan["title"], 60), "cost": 0,
            "starting_state": _browser_excerpt(f"{plan['starting_state']} · Conditions: {plan['starting_conditions']}"),
            "change": _browser_excerpt(plan["proposal"]), "expected": _browser_excerpt(plan["expected"]),
            "stop": _browser_excerpt(plan["stop"]), "recovery": _browser_excerpt(plan["recovery"]),
            "interfaces": [{"reference": _browser_excerpt(f"{item['id']} · {item['reference']}"),
                            "tolerance": "See source interface reference and expected outcome",
                            "owner": f"Named checker: {_browser_excerpt(plan['checker'])}"} for item in plan["interfaces"]],
            "depends_on": [f"stage-{item}" for item in plan["dependencies"]],
            "checks": [gate["id"], *(row["id"] for row in checks_for_admission)],
        })
    return _make_draft(title, headline, origin, version, owner, variables, checks, mapping, source_note)


def _ordered(dependencies):
    seen, active, order = set(), set(), []

    def visit(stage):
        _require(stage not in active, "Dimensional Design dependencies contain a cycle.")
        if stage in seen:
            return
        active.add(stage)
        for parent in dependencies[stage]:
            visit(parent)
        active.remove(stage)
        seen.add(stage)
        order.append(stage)

    for stage in dependencies:
        visit(stage)
    return order


def _unpack(document):
    document = _object(document, "export")
    if document.get("schema") == JOURNAL_SCHEMA:
        return document
    export = _object(document.get("export"), "demo export")
    _require(export.get("schema") == JOURNAL_SCHEMA, "Choose a Dimensional Design /1 journal export or demo output.")
    _require(document.get("evidence_origin") == _object(export.get("baseline"), "baseline").get("origin"),
             "The demo and journal disagree about evidence origin.")
    return export


def _history(journal, changes):
    events = _array(journal.get("history"), "journal history events", 2048)
    history = {key: [] for key in changes}
    for index, raw in enumerate(events, start=1):
        event = _object(raw, "history event")
        _number(event.get("event"), "event sequence", index, index)
        key, command = event.get("change_id"), event.get("command")
        if index == 1:
            _require(command == "baseline" and key is None and event.get("baseline") == journal["baseline"],
                     "The export baseline disagrees with the journal history.")
            continue
        _require(isinstance(key, str) and key in changes, "A history event names an unknown stage.")
        _text(command, "history command", 32)
        history[key].append(event)
    _require(bool(events), "Supply the history of the exported journal.")
    return history, len(events)


def _stage(raw, key, origin, timeline, head):
    raw = _object(raw, "change")
    plan = _object(raw.get("plan"), "plan")
    state, review = raw.get("state"), raw.get("review")
    _require(isinstance(state, str) and state in _STATES and isinstance(review, str) and review in _REVIEWS,
             "Unsupported journal stage or review state.")
    revision = _number(raw.get("revision"), "stage revision", 1, head)
    started = _number(raw.get("started_event"), "start event", 0, head)
    recovery = _number(raw.get("recovery_event"), "recovery event", 0, head)
    recheck = _number(raw.get("recheck_event", 0), "recheck event", 0, head)
    plan_events = [event for event in timeline if event["command"] in ("add", "revise")]
    _require(bool(plan_events) and plan_events[-1].get("plan") == plan
             and plan_events[-1].get("revision") == revision, "Stage plan disagrees with the journal history.")
    interfaces = _object(plan.get("interfaces"), "interfaces")
    checks = _object(plan.get("checks"), "checks")
    _require(1 <= len(interfaces) <= 6 and 1 <= len(checks) <= MAX_CHECKS,
             "Stage interfaces or checks exceed the Decision Review bounds.")
    choices = []
    for interface_id, raw_interface in interfaces.items():
        _ident(interface_id, "interface")
        interface = _object(raw_interface, "interface")
        choices.append({field: _text(interface.get(field), field) for field in ("reference", "tolerance", "owner")})
    checks_by_id = {}
    phases = {}
    for check_id, raw_check in checks.items():
        _ident(check_id, "check")
        check = _object(raw_check, "check")
        phase = check.get("phase")
        _require(phase in _PHASES and isinstance(check.get("interface"), str) and check["interface"] in interfaces,
                 "A check needs a known phase and interface.")
        phases[check_id] = phase
        checks_by_id[check_id] = {"owner": _text(check.get("checker"), "checker"),
                                  "label": _text(f"{key} · {phase} · {check.get('interface')} · "
                                                 + _text(check.get("test"), "inspection test"), "check label")}
    _require(set(phases.values()) == set(_PHASES), "Supply entry, invariant, outcome, and recovery checks.")
    observations = _array(raw.get("evidence"), "stage evidence", 2048)
    modeled_observations = [row.get("evidence") for row in timeline if row["command"] == "evidence"]
    _require(observations == modeled_observations, "Stage evidence records disagree with the journal history.")
    latest = {}
    previous = 0
    for event, observation in zip((row for row in timeline if row["command"] == "evidence"), observations):
        obs = _object(observation, "observation")
        number = _number(obs.get("event"), "observation event", previous + 1, head)
        previous = number
        _require(number == event["event"] and obs.get("revision") == event.get("revision") and obs.get("origin") == origin,
                 "Evidence event, revision, or origin disagrees with the journal.")
        if obs["revision"] != revision:
            continue
        check_id, result = obs.get("check"), obs.get("result")
        _require(isinstance(check_id, str) and check_id in phases and isinstance(result, str) and result in _RESULTS,
                 "An observation names an unknown check or result.")
        _require(obs.get("checker") == checks_by_id[check_id]["owner"], "An observation disagrees with the named checker.")
        _text(obs.get("reference"), "source observation reference")
        _text(obs.get("observation"), "source observation")
        latest[check_id] = obs
    eligible = []
    dispositions = []
    for check_id, fields in checks_by_id.items():
        phase = phases[check_id]
        obs = latest.get(check_id)
        needed_after = {"entry": 0, "invariant": max(started, recheck), "outcome": max(started, recheck), "recovery": recovery}[phase]
        fresh = obs is not None and obs["event"] > needed_after and (phase != "outcome" or started) and (phase != "recovery" or recovery)
        status = _RESULTS[obs["result"]] if fresh else "UNKNOWN"
        if obs is not None:
            basis = "" if fresh else "Earlier observation; fresh evidence required. "
            reference = f"{basis}Journal revision {revision}, event {obs['event']}, {obs['reference']} · {obs['observation']}"
        else:
            reference = f"No current {phase} observation in journal revision {revision}."
        check = {"id": _mapped_id("check", key, check_id), **fields, "status": status,
                 "event": obs["event"] if obs is not None else 0, "reference": _text(reference, "evidence reference")}
        dispositions.append(check)
        if phase != "recovery":
            eligible.append(check["id"])
    acceptances = [row["event"] for row in timeline if row["command"] == "accept" and row.get("state") == "verified"
                   and row.get("review") == "accepted" and row.get("revision") == revision]
    stage = {"id": _mapped_id("stage", key), "name": _text(plan.get("title"), "change title", 60),
             "interfaces": choices, "starting_state": _text(plan.get("starting_state"), "starting state"),
             **{field: _text(plan.get(field), field) for field in ("change", "expected", "stop", "recovery")}}
    return stage, dispositions, eligible, max(acceptances, default=0), state, review, _text(plan.get("owner"), "stage owner")


def import_dimensional(document, *, source="Dimensional Design journal", mapping=None, source_sha256=None):
    """Make a held draft. Optional explicit objective and scenarios do not clear evidence review."""
    raw_data = _json_bytes(document)
    fingerprint = source_sha256 or hashlib.sha256(raw_data).hexdigest()
    _require(isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint) is not None,
             "Supply a lowercase source SHA-256.")
    source = _text(source, "source label", 180)
    if isinstance(document, dict) and document.get("schema") == BROWSER_SCHEMA:
        return _browser_draft(document, source, fingerprint, mapping)
    journal = _unpack(document)
    baseline = _object(journal.get("baseline"), "baseline")
    origin = baseline.get("origin")
    _require(isinstance(origin, str) and origin in _ORIGINS, "Label the journal simulation or observed.")
    headline = _text(f"{_text(baseline.get('state'), 'baseline state')} Preserve: {_text(baseline.get('preserve'), 'baseline invariant')} "
                     f"Reference: {_text(baseline.get('reference'), 'baseline reference')}.", "baseline summary")
    owner = _text(baseline.get("owner"), "journal owner")
    changes = _object(journal.get("changes"), "changes")
    _require(1 <= len(changes) <= MAX_VARIABLES, f"Import a journal with 1 to {MAX_VARIABLES} bounded stages.")
    for key in changes:
        _ident(key, "stage")
    history, head = _history(journal, changes)
    dependencies = {}
    for key, change in changes.items():
        plan = _object(_object(change, "change").get("plan"), "plan")
        parents = _array(plan.get("depends_on"), "stage dependencies", MAX_VARIABLES)
        _require(all(isinstance(parent, str) and parent in changes for parent in parents)
                 and len(parents) == len(set(parents)), "Journal dependency IDs must be unique and point to known stages.")
        dependencies[key] = parents
    ordered = _ordered(dependencies)
    revision = head
    checks, variables, by_id = [], [], {}
    eligible_events = {}

    def evidence(key, owner, label, status, reference, *, event=1, prerequisites=(), basis=None):
        record = _check(key, owner, label, status, reference, revision, event=event,
                        prerequisites=prerequisites, basis=basis)
        checks.append(record)
        by_id[key] = record

    for key in ordered:
        stage, rows, eligible, acceptance, state, review, stage_owner = _stage(changes[key], key, origin, history[key], head)
        for row in rows:
            evidence(row["id"], row["owner"], row["label"], row["status"], row["reference"], event=row["event"])
        gate = _mapped_id("admission", key)
        parents = [_mapped_id("admission", parent) for parent in dependencies[key]]
        prerequisites = [*eligible, *parents]
        event = max([acceptance, *(row["event"] for row in rows if row["id"] in eligible)])
        current_parents = all(eligible_events[parent] < acceptance for parent in parents)
        passed = (state == "verified" and review == "accepted" and acceptance and acceptance == event and current_parents
                  and all(by_id[req]["status"] == "PASS" for req in prerequisites))
        status = "PASS" if passed else "FAIL" if review == "reverted" else "UNKNOWN"
        detail = f"Journal records {state}/{review} for stage {key}; current admission requires a fresh accepted outcome."
        if state == "verified" and review == "accepted" and not current_parents:
            detail = f"Stage {key} needs fresh acceptance after its prerequisite changed."
        elif state == "verified" and review == "accepted" and acceptance < event:
            detail = f"Stage {key} needs fresh acceptance of its newer evidence."
        evidence(gate, stage_owner, f"Stage {key} accepted with current evidence", status, detail, event=event,
                 prerequisites=prerequisites, basis={req: _digest(by_id[req]) for req in prerequisites} if passed else {})
        eligible_events[gate] = event
        stage.update(cost=0, depends_on=[_mapped_id("stage", parent) for parent in dependencies[key]], checks=[gate, *eligible])
        variables.append(stage)

    source_note = f"{source} · SHA-256 {fingerprint} · journal event {head}. Imported observations are source assertions."
    return _make_draft("Dimensional Design journal review", headline, _ORIGINS[origin], revision, owner,
                       variables, checks, mapping, source_note)


def _make_draft(title, headline, origin, revision, owner, variables, checks, mapping, source_note):
    def hold(key, label, reference):
        checks.append(_check(key, owner, label, "UNKNOWN", reference, revision))

    hold("source-review", "Review imported journal and evidence scope", source_note)
    for name in ("authority", "consent", "funding"):
        hold(f"handoff-{name}", f"Document {name} for the proposed handoff",
             "No corresponding reviewed decision was supplied by the native journal.")
    if mapping is None:
        hold("author-cost", "Enter a reviewed objective and every stage cost",
             "The native journal has no numeric objective. All imported zero values are placeholders for editing.")
        hold("author-rules", "Enter and approve the required scenario rules",
             "Stage dependency links were imported. No acceptance scenario was inferred from notes.")
        required = ["source-review", "author-cost", "author-rules"]
        unit = UNASSIGNED_COST_UNIT
        scenarios = [{"id": "draft", "title": "Scenario rules awaiting authoring", "checks": [], "rules": []}]
    else:
        _require(isinstance(mapping, dict) and set(mapping) == {"cost_unit", "costs", "scenarios"},
                 "Supply explicit cost_unit, costs, and scenarios for the imported stages.")
        costs = _object(mapping["costs"], "authored costs")
        _require(set(costs) == {stage["id"] for stage in variables}, "Give one explicit cost for every mapped stage ID.")
        for stage in variables:
            stage["cost"] = costs[stage["id"]]
        unit, scenarios = mapping["cost_unit"], mapping["scenarios"]
        required = ["source-review"]
    _require(len(checks) <= MAX_CHECKS, f"Import at most {MAX_CHECKS} checks including authoring and handoff checks.")
    draft = {"schema": DECISION_SCHEMA, "title": title, "origin": origin,
             "baseline": headline, "revision": revision, "cost_unit": unit, "variables": variables,
             "checks": checks, "required_checks": required, "scenarios": scenarios,
             "handoff": {"affected": [{"name": "People affected by the staged change", "role": "Identify and review with the proposed owner",
                                       "can_amend": None}], "decision_owner": "Awaiting assignment", "delivery_owner": owner,
                         "verifier": "See imported named checkers", "resources": "Confirm resources, funding, and dependencies before proceeding.",
                         **{f"{name}_check": f"handoff-{name}" for name in ("authority", "consent", "funding")}}}
    return validate_decision(draft)


def load_dimensional(path, *, mapping=None):
    """Read a selected immutable JSON snapshot, never the active journal database."""
    path = Path(path)
    with _read_regular(path, MAX_REPLAY_BYTES, "Dimensional Design export") as (stream, _):
        raw = stream.read(MAX_REPLAY_BYTES + 1)
    _require(len(raw) <= MAX_REPLAY_BYTES, "The Dimensional Design export exceeds the 512 KiB limit.")
    return import_dimensional(_decode_json(raw), source=path.name, mapping=mapping, source_sha256=hashlib.sha256(raw).hexdigest())
