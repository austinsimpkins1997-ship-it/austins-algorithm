"""Inspect bounded local evidence from Decision Containment and Stasis Beam."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .workbench_project import _decode_json, _read_regular


REPLAY_SCHEMA = "austins-algorithm/decision-review-replay/1"
MAX_REPLAY_BYTES = 512 * 1024
_MAX_LANES = 32
_MAX_ITEMS = 128
_MAX_TIMELINE = 512
_STASIS_PHASES = ("STOP", "RESET_HELD", "ARMED", "RUN")
_WORK_STATUSES = {"pending", "processed", "unrecoverable", "failed", "missing"}
_CONTAINMENT_SCOPE = "Recounts supplied synthetic ledgers and accepted IDs. Full event-chain verification belongs to Decision Containment."
_STASIS_SCOPE = "Plays back recorded abstract states. No device, timing, stop or hold measurement."


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _record(value, fields, label):
    _require(isinstance(value, dict) and set(value) == set(fields), f"Invalid {label} fields.")
    return value


def _array(value, maximum, label, *, nonempty=False):
    _require(isinstance(value, list) and int(nonempty) <= len(value) <= maximum,
             f"{label} must contain at most {maximum} items.")
    return value


def _text(value, label, maximum=400):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
             and all(c.isprintable() for c in value), f"Invalid {label}.")
    return value


def _key(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", value) is not None,
             "Invalid replay item or scenario ID.")
    return value


def _integer(value, maximum, label):
    _require(type(value) is int and 0 <= value <= maximum, f"Invalid {label}.")
    return value


def _boolean(value, label):
    _require(type(value) is bool, f"{label} must be true or false.")
    return value


def _digest(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
             "Invalid lowercase SHA-256 record.")
    return value


def _json_bytes(document):
    try:
        raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Replay data must be bounded finite JSON.") from error
    _require(len(raw) <= MAX_REPLAY_BYTES, "The replay exceeds the 512 KiB limit.")
    return raw


def _provenance(origin, source, sha256):
    return {"origin": origin, "source": source, "sha256": sha256}


def _lane(lane_id, title, phase, accepted_ids, ledger, timeline, recovery_observed):
    return {"id": lane_id, "title": title, "phase": phase, "accepted_ids": list(accepted_ids),
            "ledger": [dict(item) for item in ledger], "timeline": timeline, "recovery_observed": recovery_observed}


def _item(row, *, native=False):
    if native:
        _require(isinstance(row, dict), "Invalid native ledger item.")
        row = {key: row.get(key) for key in ("id", "status", "effects")}
    _record(row, ("id", "status", "effects"), "ledger item")
    status = _text(row["status"], "item status", 32)
    _require(status in _WORK_STATUSES, "Unsupported ledger status.")
    effects = _integer(row["effects"], 1_000_000, "effect count")
    _require((effects > 0) == (status == "processed"), "Ledger status and effects disagree.")
    return {"id": _key(row["id"]), "status": status, "effects": effects}


def _containment(lanes):
    scenarios = []
    seen = set()
    for lane in _array(lanes, _MAX_LANES, "Containment scenarios", nonempty=True):
        _record(lane, ("id", "title", "phase", "accepted_ids", "ledger", "timeline", "recovery_observed"), "scenario")
        lane_id = _key(lane["id"])
        _require(lane_id not in seen, f"Duplicate scenario ID {lane_id}.")
        seen.add(lane_id)
        accepted = [_key(key) for key in _array(lane["accepted_ids"], _MAX_ITEMS, "Accepted IDs")]
        _require(len(accepted) == len(set(accepted)), "Duplicate accepted work ID.")
        items = {}
        for raw in _array(lane["ledger"], _MAX_ITEMS, "Scenario ledger"):
            row = _item(raw)
            key = row["id"]
            _require(key in accepted and key not in items, "Ledger IDs must be unique and present in the accepted set.")
            items[key] = row
        missing = {"status": "missing", "effects": 0}
        rows = [items.get(key, {"id": key, **missing}) for key in accepted]
        timeline = [_text(line, "timeline entry") for line in _array(lane["timeline"], _MAX_TIMELINE, "Timeline")]
        counts = {label: sum(row["status"] == status for row in rows) for label, status in
                  (("pending", "pending"), ("lost", "unrecoverable"), ("failed", "failed"), ("missing", "missing"))}
        completed_once = sum(row["status"] == "processed" and row["effects"] == 1 for row in rows)
        observed = _boolean(lane["recovery_observed"], "Recovery observed")
        _require(not observed or completed_once == len(rows) > 0,
                 "Claimed recovery conflicts with the item ledger.")
        scenarios.append({
            "id": lane_id, "title": _text(lane["title"], "scenario title"),
            "phase": _text(lane["phase"], "phase", 40), "accepted": len(rows), "completed_once": completed_once,
            **counts, "duplicated": sum(row["effects"] > 1 for row in rows),
            "duplicate_effects": sum(max(0, row["effects"] - 1) for row in rows),
            "reconciled": len(rows) - counts["pending"] - counts["missing"],
            "recovery_complete": observed, "items": rows, "timeline": timeline,
        })
    return scenarios


def _step_description(event, legacy, fixed, prior_fixed):
    if event["reboot"]:
        return "Reboot event. Compare the recorded phases for restart behavior."
    if prior_fixed == "RESET_HELD" and event["start_raw"] and not event["start"] and fixed == "STOP":
        return "The raw START arrived before RESET release had been accepted. The fixed trace trips immediately."
    if legacy == "RUN" and fixed == "STOP":
        return "The legacy trace reaches RUN. The fixed trace remains stopped for the same input."
    if fixed == "RESET_HELD" and event["reset"]:
        return "The model records the local RESET press with a matching permit."
    return "Compare the recorded legacy and fixed phases with this event."


def _bench_checks(divergence):
    """Give the inspector test objectives from the owning LED bench procedure."""
    early_start = f"Use modeled step {divergence} as the trace comparison point." if divergence else "Record raw and debounced inputs."
    checks = (
        ("Premature START during RESET release", f"Compare raw and debounced inputs on the isolated LED fixture. {early_start}",
         "The LED remains off until a deliberate, ordered RESET and new START under valid readiness."),
        ("Permit withdrawal and reconnect", "Observe LED current after withdrawing permission. Compare wired and enclosed fiber traces.",
         "Owning bench planning target is at most 225 ms including uncertainty, with no automatic restart."),
        ("Independent cutoff", "Capture the guard opening and LED current even while the application keeps requesting the LED.",
         "Owning bench planning target is at most 5 ms from a qualified local trip."),
        ("Controller hang", "Capture the last qualifying heartbeat and independent LED cutoff for static high and static low.",
         "Owning bench planning target is at most 75 ms from the last qualifying heartbeat."),
    )
    return [{"check": label, "setup": setup, "criterion": criterion,
             "evidence": "PENDING PHYSICAL MEASUREMENT · ChronoRealm Stasis Beam docs/BENCH.md"}
            for label, setup, criterion in checks]


def _stasis(data):
    _record(data, ("trace", "phase_encoding", "legacy_run_reproduced", "fixed_stays_stopped"), "Stasis proof")
    phase_ids = _record(data["phase_encoding"], _STASIS_PHASES, "Stasis phase encoding")
    ids = [_integer(phase_ids[key], len(_STASIS_PHASES) - 1, "phase index") for key in _STASIS_PHASES]
    _require(len(set(ids)) == len(ids), "Stasis phase indices must be unique.")
    names = dict(zip(ids, _STASIS_PHASES))
    steps = []
    prior_fixed = "STOP"
    for index, row in enumerate(_array(data["trace"], _MAX_ITEMS, "Stasis trace", nonempty=True), start=1):
        _record(row, ("event", "legacy_phase", "fixed_phase"), "trace step")
        controls = ("reset", "start", "reset_raw", "start_raw", "challenge", "reboot")
        event = _record(row["event"], (*controls, "local", "response"), "trace event")
        for key in controls:
            _boolean(event[key], key)
        _integer(event["local"], 2, "local guard state")
        _integer(event["response"], 3, "response type")
        legacy = names[_integer(row["legacy_phase"], len(ids) - 1, "legacy phase")]
        fixed = names[_integer(row["fixed_phase"], len(ids) - 1, "fixed phase")]
        steps.append({"step": index, "legacy": legacy, "fixed": fixed, "event": dict(event),
                      "description": _step_description(event, legacy, fixed, prior_fixed)})
        prior_fixed = fixed
    legacy_run = _boolean(data["legacy_run_reproduced"], "legacy run reproduced")
    fixed_stopped = _boolean(data["fixed_stays_stopped"], "fixed stays stopped")
    _require(legacy_run == (steps[-1]["legacy"] == "RUN") and fixed_stopped == (steps[-1]["fixed"] == "STOP"),
             "Recorded result contradicts the final trace row.")
    divergence = next((row["step"] for row in steps if row["legacy"] != row["fixed"]), None)
    return {"steps": steps, "first_divergence": divergence, "legacy_run_reproduced": legacy_run,
            "fixed_stays_stopped": fixed_stopped, "bench_checks": _bench_checks(divergence)}


def _incident(document):
    state = document.get("state")
    report = document.get("report")
    _require(isinstance(state, dict) and isinstance(report, dict), "Invalid Decision Containment incident.")
    _require(state.get("mode") == report.get("scope") == "SYNTHETIC_LOCAL", "Unsupported decision incident scope.")
    _require(_digest(state.get("head_sha256")) == _digest(report.get("head_sha256")), "Incident heads disagree.")
    _digest(document.get("fixture_sha256"))
    rows = [_item(row, native=True) for row in _array(state.get("work"), _MAX_ITEMS, "Incident work")]
    reconciliation = state.get("reconciliation")
    observed = False
    missing = []
    if reconciliation is not None:
        _require(isinstance(reconciliation, dict) and report.get("reconciliation") == reconciliation,
                 "Incident reconciliation records disagree.")
        missing = [_key(key) for key in _array(reconciliation.get("missing_ids"), _MAX_ITEMS, "Missing IDs")]
        _require(_integer(reconciliation.get("accepted"), _MAX_ITEMS, "accepted count") == len(rows) + len(missing),
                 "Incident accepted count disagrees.")
        totals = {label: sum(row["status"] == label for row in rows) for label in ("processed", "pending", "failed", "unrecoverable")}
        totals["effects"] = sum(row["effects"] for row in rows)
        totals["duplicate_effects"] = sum(max(0, row["effects"] - 1) for row in rows)
        _require(all(type(reconciliation.get(key)) is int and reconciliation[key] == value for key, value in totals.items()),
                 "Incident summary and ledger disagree.")
        observed = _boolean(reconciliation.get("mailbox_and_read_receipts_verified"), "read receipts verified")
    phase = state.get("phase")
    _require(phase == report.get("phase"), "Incident phases disagree.")
    timeline = []
    for i, event in enumerate(_array(document.get("events"), 128, "Incident events"), start=1):
        request = event.get("request") if isinstance(event, dict) else None
        _require(isinstance(request, dict) and type(request.get("expected_version")) is int
                 and request["expected_version"] == i - 1, "Invalid incident event order.")
        timeline.append(f"{i}. {_text(request.get('action'), 'incident action', 64)}")
    lane = _lane("incident", "Communications rollout", phase, [row["id"] for row in rows] + missing, rows,
                 timeline, phase == "RECOVERED" and observed)
    return [lane]


def _comparison(document):
    _require(document.get("scope") == "SYNTHETIC_LOCAL", "Unsupported Decision Containment comparison scope.")
    lanes = []
    for scenario in _array(document.get("scenarios"), _MAX_LANES // 2, "Matched comparison", nonempty=True):
        _require(isinstance(scenario, dict), "Invalid matched scenario.")
        case = _key(scenario.get("id"))
        title = _text(scenario.get("title"), "matched scenario title", 300)
        workload = _array(scenario.get("workload"), _MAX_ITEMS, "Scenario workload")
        _require(all(isinstance(item, dict) for item in workload), "Invalid workload entry.")
        ids = [_key(item.get("id")) for item in workload]
        for name in ("baseline", "guarded"):
            lane = scenario.get(name)
            _require(isinstance(lane, dict) and isinstance(lane.get("metrics"), dict), "Invalid matched scenario lane.")
            metrics = lane["metrics"]
            rows = [_item(row, native=True) for row in _array(lane.get("ledger"), _MAX_ITEMS, "Comparison ledger")]
            events = _array(lane.get("events"), _MAX_TIMELINE, "Comparison events")
            timeline = []
            for event in events:
                _require(isinstance(event, dict), "Invalid comparison event.")
                step = _integer(event.get("step"), 1_000_000, "simulation step")
                timeline.append(f"Step {step} · {_text(event.get('event'), 'comparison event', 64)}")
            recovered = metrics.get("recovery_step")
            if recovered is not None:
                _integer(recovered, 1_000_000, "recovery step")
            _require(_integer(metrics.get("accepted"), _MAX_ITEMS, "accepted count") == len(ids), "Comparison accepted count disagrees.")
            lanes.append(_lane(f"{case}-{name}", f"{title} ({name})", "RECOVERED" if recovered is not None else "END_OF_RUN",
                               ids, rows, timeline, recovered is not None))
    return lanes


def _envelope(kind, title, provenance, data):
    return {"schema": REPLAY_SCHEMA, "kind": kind, "title": title, "provenance": provenance, "data": data}


def _normalize(document, source=None, source_digest=None):
    canonical = _json_bytes(document)
    _require(isinstance(document, dict), "Replay input must be a JSON object.")
    source_digest = source_digest or hashlib.sha256(canonical).hexdigest()
    schema = document.get("schema")
    if schema == REPLAY_SCHEMA:
        return deepcopy(document)
    if schema == "ChronoRealmDecisionContainment/1":
        return _envelope("containment", "Decision Containment incident", _provenance("synthetic record", source or schema, source_digest),
                         _incident(document))
    if schema == "decision-containment-comparison/1":
        return _envelope("containment", "Decision Containment matched comparison", _provenance("synthetic record", source or schema, source_digest),
                         _comparison(document))
    if "legacy_raw_start_counterexample" in document:
        return _envelope("stasis", "Stasis Beam recorded model trace", _provenance("finite model output", source or "Stasis Beam formal output", source_digest),
                         deepcopy(document["legacy_raw_start_counterexample"]))
    raise ValueError("Choose a Decision Review replay or a supported Decision Containment or Stasis Beam report.")


def _inspect_normalized(document):
    _record(document, ("schema", "kind", "title", "provenance", "data"), "replay envelope")
    provenance = _record(document["provenance"], ("origin", "source", "sha256"), "replay provenance")
    _text(provenance["origin"], "evidence origin", 64)
    _text(provenance["source"], "evidence source")
    _digest(provenance["sha256"])
    kind = document["kind"]
    title = _text(document["title"], "replay title")
    summary = {"kind": kind, "title": title, "provenance": dict(provenance)}
    if kind == "containment":
        _require(provenance["origin"] in ("bundled synthetic fixture", "synthetic record"),
                 "Containment review only accepts labeled synthetic records.")
        return {**summary, "scope": _CONTAINMENT_SCOPE, "scenarios": _containment(document["data"])}
    if kind == "stasis":
        _require(provenance["origin"] == "finite model output", "Stasis review only accepts labeled finite-model records.")
        return {**summary, "scope": _STASIS_SCOPE, **_stasis(document["data"])}
    raise ValueError("Unsupported replay kind.")


def inspect_replay(document):
    """Read an offline evidence record and derive bounded presentation fields."""
    return _inspect_normalized(_normalize(document))


def load_replay(path):
    """Read bounded UTF-8 JSON, preserve source identity, and validate before import."""
    path = Path(path)
    with _read_regular(path, MAX_REPLAY_BYTES, "replay") as (stream, _):
        raw = stream.read(MAX_REPLAY_BYTES + 1)
    _require(len(raw) <= MAX_REPLAY_BYTES, "The replay exceeds the 512 KiB limit.")
    document = _normalize(_decode_json(raw), path.name, hashlib.sha256(raw).hexdigest())
    _inspect_normalized(document)
    return document


def replay_examples():
    """Return a synthetic service example and the recorded four-step Stasis trace."""
    ids = [f"request-{index:02d}" for index in range(1, 6)]
    processed = [{"id": key, "status": "processed", "effects": 1} for key in ids]
    failed = [processed[0], {**processed[1], "effects": 2},
              {**processed[2], "status": "unrecoverable", "effects": 0},
              {**processed[3], "status": "pending", "effects": 0}]
    queued = [{**row, "status": "pending", "effects": 0} for row in processed]
    scenarios = [
        _lane("normal", "Normal service", "COMPLETE", ids, processed,
              ["Five synthetic requests complete on the main route."], False),
        _lane("unguarded-outage", "Outage without prior reconciliation", "PAUSED", ids, failed,
              ["A blind retry duplicates request 02.", "Request 03 is declared lost.", "Request 05 is missing from the ledger."], False),
        _lane("guarded-outage", "Outage with offline capture", "PAUSED", ids, queued,
              ["An offline queue admits five requests with stable IDs.", "Wait for the recovery route."], False),
        _lane("guarded-recovery", "Recovery of the guarded lane", "RECOVERED", ids, processed,
              ["Reconcile the guarded-outage queue by stable ID.", "Check that all five completed exactly once."], True),
    ]
    origin = _provenance("bundled synthetic fixture", "Austin Decision Review · public-office exercise",
                         hashlib.sha256(_json_bytes(scenarios)).hexdigest())
    trace_signals = (
        (False, False, False, False, 0, True, 0, 0),
        (True, False, True, False, 2, True, 1, 1),
        (False, False, False, True, 2, True, 2, 0),
        (False, True, False, True, 2, False, 3, 0),
    )
    trace = []
    for reset, start, reset_raw, start_raw, response, challenge, legacy, fixed in trace_signals:
        event = dict(local=0, reset=reset, start=start, reset_raw=reset_raw, start_raw=start_raw,
                     response=response, challenge=challenge, reboot=False)
        trace.append({"event": event, "legacy_phase": legacy, "fixed_phase": fixed})
    model = {"legacy_run_reproduced": True, "fixed_stays_stopped": True,
             "phase_encoding": {label: index for index, label in enumerate(_STASIS_PHASES)}, "trace": trace}
    model_source = _provenance("finite model output", "ChronoRealm Stasis Beam · output/formal-model.json",
                               "9a85a5d9cab9d9591452239131f5f0d3e8c96041e43d78d759c5da65a44248cb")
    return {"containment": _envelope("containment", "Offline office reconciliation", origin, scenarios),
            "stasis": _envelope("stasis", "Premature START counterexample", model_source, model)}
