"""Data-only replay of the browser DimensionalDesign/1 decision export."""

from datetime import datetime
import json
import re


SCHEMA = "DimensionalDesign/1"
PHASES = ("readiness", "outcome", "recovery")
RESULTS = {"pass", "fail", "unknown"}
BASELINE_FIELDS = ("title", "condition", "preserve", "reference", "owner")
PLAN_FIELDS = ("title", "starting_state", "proposal", "starting_conditions", "expected",
               "stop", "recovery", "checker", "kind")
EVIDENCE_FIELDS = ("phase", "target", "result", "basis", "source", "observation", "observed_at")
EVENT_FIELDS = {"project_id", "sequence", "request_id", "action", "actor", "reason", "payload",
                "previous_project_id", "recorded_at"}
STATE_ACTIONS = {"assess", "start", "wait", "pause", "resume", "verify", "accept",
                 "revise", "recover", "revert"}
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _fields(value, names, label):
    _require(isinstance(value, dict) and set(value) == set(names), f"Invalid browser {label} fields.")


def _text(value, key, limit=600):
    text = value.get(key)
    _require(isinstance(text, str) and 0 < len(text.strip()) <= limit and "\x00" not in text,
             f"Invalid browser {key}.")
    return text.strip()


def _identifier(value):
    _require(isinstance(value, str) and ID.fullmatch(value), "Invalid browser project or change identifier.")
    return value


def _plan(payload, prior_ids):
    _require(set(payload) - {"dependencies"} == {*PLAN_FIELDS, "interfaces", "change_id"},
             "Invalid browser plan fields.")
    plan = {key: _text(payload, key, {"checker": 80, "kind": 20}.get(key, 600)) for key in PLAN_FIELDS}
    _require(plan["kind"] in ("patch", "improve", "update", "rebuild"), "Invalid browser change type.")
    dependencies = payload.get("dependencies", [])
    _require(isinstance(dependencies, list) and len(dependencies) <= 15, "Invalid browser dependency list.")
    dependencies = [_identifier(value) for value in dependencies]
    _require(set(dependencies) <= set(prior_ids) and len(set(dependencies)) == len(dependencies),
             "Browser dependencies must name distinct earlier changes.")
    interfaces = payload["interfaces"]
    _require(isinstance(interfaces, list) and 1 <= len(interfaces) <= 12, "Invalid browser interfaces.")
    known = {"general"}
    references = []
    for entry in interfaces:
        _require(isinstance(entry, dict) and {"id", "reference"} <= entry.keys(), "Invalid browser interface.")
        key = _identifier(entry["id"])
        _require(key not in known, "Duplicate or reserved browser interface identifier.")
        known.add(key)
        references.append({"id": key, "reference": _text(entry, "reference")})
    return {**plan, "dependencies": dependencies, "interfaces": references}


def _targets(change):
    return ("general", *(row["id"] for row in change["plan"]["interfaces"]))


def latest_evidence(change):
    return {(row["phase"], row["target"]): row for row in change["evidence"]
            if row["revision"] == change["revision"]}


def _passing(change, phase):
    results = latest_evidence(change)
    return all(results.get((phase, key), {}).get("result") == "pass" for key in _targets(change))


def _prior_ready(change, changes):
    pending, visited = list(change["plan"]["dependencies"]), set()
    while pending:
        key = pending.pop()
        prior = changes[key]
        if key in visited:
            continue
        if (prior["state"], prior["decision"]) != ("verified", "accepted"):
            return False
        visited.add(key)
        pending.extend(prior["plan"]["dependencies"])
    return True


def _record(change, event):
    payload = event["payload"]
    _fields(payload, (*EVIDENCE_FIELDS, "change_id"), "evidence")
    row = {key: _text(payload, key, 40 if key in EVIDENCE_FIELDS[:4] else 600) for key in EVIDENCE_FIELDS}
    phase, target, result, basis = (row[key] for key in EVIDENCE_FIELDS[:4])
    _require(phase in PHASES and target in _targets(change) and result in RESULTS
             and basis in ("simulated", "observed", "reported"), "Invalid browser evidence designation.")
    state = change["state"]
    _require(state != "reverted" and (state != "recovering" or phase == "recovery")
             and (phase != "recovery" or state == "recovering")
             and (phase != "outcome" or state in ("in-progress", "verified", "degraded", "failed")),
             "Browser evidence is out of sequence.")
    change["evidence"].append({**row, "revision": change["revision"], "event": event["sequence"],
                               "checker": event["actor"]})
    if result != "pass":
        consequences = {"verified": "degraded", "in-progress": "failed" if result == "fail" else "waiting",
                        "ready": "blocked" if result == "fail" else "waiting"}
        change["state"] = consequences.get(state, state)
    return "Recorded current-revision evidence."


def _act(changes, change, event, earlier):
    action, payload = event["action"], event["payload"]
    if action != "revise":
        _fields(payload, {"change_id"}, action)
    _require(action not in ("accept", "verify", "revert") or change["plan"]["checker"] == event["actor"],
             "Browser review action requires its assigned checker.")
    state, started = change["state"], change["attempt_started"]
    ready = _prior_ready(change, changes) and _passing(change, "readiness")
    if action == "assess":
        _require(state in ("unknown", "blocked", "waiting") and not started, "Browser assessment is out of sequence.")
        change["state"] = "ready" if ready else "blocked"
        if ready:
            return "Ready for the bounded attempt."
        if not _prior_ready(change, changes):
            return "Hold until the prerequisite decisions are accepted and verified."
        return "Hold until general and interface readiness evidence passes."
    if action == "start":
        _require(state == "ready" and ready, "Browser attempt needs current readiness and prerequisites.")
        change.update(state="in-progress", decision="pending", attempt_started=True)
    elif action in ("pause", "wait"):
        _require(state in ("unknown", "ready", "in-progress", "blocked", "waiting"), "Browser pause is out of sequence.")
        change["state"] = "waiting"
        if action == "pause":
            change["decision"] = "paused"
    elif action == "resume":
        _require(state == "waiting" and started and ready, "Browser resume needs a ready current attempt.")
        change.update(state="in-progress", decision="pending")
    elif action in ("verify", "accept"):
        _require(state == ("in-progress" if action == "verify" else "verified") and ready
                 and _passing(change, "outcome"), "Browser verification or acceptance needs passing current checks.")
        change.update(state="verified", decision="accepted" if action == "accept" else "pending")
    elif action == "revise":
        _require(state in ("unknown", "ready", "blocked", "verified", "reverted")
                 or (state == "waiting" and not started), "Browser revision requires a resolved attempt.")
        plan = _plan(payload, earlier)
        change["revisions"].append({"version": change["revision"], "plan": change["plan"],
                                    "last_state": state, "decision": change["decision"]})
        change.update(plan=plan, revision=change["revision"] + 1, state="unknown", decision="revised",
                      attempt_started=False)
    elif action == "recover":
        _require(state in ("waiting", "in-progress", "blocked", "failed", "degraded", "verified"),
                 "Browser recovery requires an affected change.")
        change["state"] = "recovering"
    elif action == "revert":
        _require(state == "recovering" and _passing(change, "recovery"), "Browser reversion needs restoration evidence.")
        change.update(state="reverted", decision="reverted")
    return f"Recorded {action}."


def replay_browser(document):
    """Rebuild each transition and compare the derived state to the exported view."""
    _fields(document, {"schema", "project", "events", "version"}, "export")
    _require(document["schema"] == SCHEMA, "Choose a DimensionalDesign/1 browser export.")
    view, events, version = document["project"], document["events"], document["version"]
    _require(isinstance(events, list) and type(version) is int and 1 <= version <= 500 and len(events) == version,
             "Browser journal event count and version must agree.")
    _require(isinstance(view, dict), "Invalid browser project.")
    project_id = _identifier(view.get("id"))
    changes, transitions = {}, {}
    requests = set()
    for sequence, event in enumerate(events, start=1):
        _fields(event, EVENT_FIELDS, "event")
        _require(type(event["sequence"]) is int and event["sequence"] == sequence and event["project_id"] == project_id
                 and event["previous_project_id"] == (None if sequence == 1 else project_id),
                 "Browser journal event sequence or project ID disagrees.")
        action = _text(event, "action", 80)
        request_id = _text(event, "request_id", 80)
        _require(action == event["action"] and request_id == event["request_id"] and request_id not in requests
                 and _text(event, "actor", 80) == event["actor"] and _text(event, "reason") == event["reason"],
                 "Browser event identifiers, actor or reason disagree.")
        requests.add(request_id)
        try:
            recorded_at = datetime.fromisoformat(_text(event, "recorded_at", 64).replace("Z", "+00:00"))
            _require(recorded_at.utcoffset() is not None, "Browser journal recording time must have an offset.")
        except ValueError as error:
            raise ValueError(f"Invalid browser recording time at event {sequence}.") from error
        payload = event["payload"]
        _require(isinstance(payload, dict), "Invalid browser event payload.")
        if sequence == 1:
            _require(action == "baseline", "Browser journal must start with its original baseline.")
            _fields(payload, BASELINE_FIELDS, "baseline")
            baseline = {key: _text(payload, key) for key in BASELINE_FIELDS}
            message = "Saved the baseline."
            continue
        key = _identifier(payload.get("change_id"))
        if action == "propose":
            _require(key not in changes and len(changes) < 40, "Browser proposal has a duplicate ID or exceeds its limit.")
            changes[key] = {"id": key, "plan": _plan(payload, changes), "revision": 1, "evidence": [],
                            "attempt_started": False, "state": "unknown", "decision": "pending", "revisions": []}
            transitions[key] = []
            message = "Saved the proposed change."
        else:
            _require(key in changes and action in STATE_ACTIONS | {"evidence"}, "Unknown browser change or event action.")
            change = changes[key]
            if action == "evidence":
                message = _record(change, event)
            else:
                earlier = list(changes)[:list(changes).index(key)] if action == "revise" else ()
                message = _act(changes, change, event, earlier)
        current = changes[key]
        transitions[key].append((sequence, action, current["revision"], current["state"], current["decision"]))
    for change in changes.values():
        change["prerequisites_currently_verified"] = _prior_ready(change, changes)
    reproduced = {"id": project_id, "baseline": baseline, "changes": list(changes.values()), "last_result": message}
    _require(json.dumps(reproduced, sort_keys=True) == json.dumps(view, sort_keys=True),
             "Independently replayed browser project differs from the exported view.")
    return reproduced, transitions
