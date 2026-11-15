"""Explicit, portable work checkpoints. Entries are user reports and task metadata."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

SCHEMA = "AustinFocus/1"
MAX_EVENTS = 128
MAX_INTERRUPTS = 64
FIELDS = {"schema", "status", "goal", "next_action", "checkpoint", "basis_sha256", "interruptions", "events"}
STATUSES = {"ACTIVE", "PAUSED", "COMPLETE"}
ACTIONS = {"START", "CHECKPOINT", "PARK", "INTERRUPT", "RESUME", "COMPLETE", "RESOLVE"}


def basis_digest(project: dict) -> str:
    """Pin meaningful task inputs without including editable focus notes."""
    if "document" in project:
        basis = {"kind": "decision-review", "document": project["document"]}
    else:
        basis = {"kind": "weighted-search", "problem": project["problem"], "settings": project["settings"]}
    raw = json.dumps(basis, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def _text(value, label, limit=2000, *, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{label} requires text of at most {limit} characters.")
    return value


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("A focus basis requires a SHA-256 digest.")


def _instant(value):
    _text(value, "Recorded time", 64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Recorded time requires an ISO timestamp with an offset.") from error
    if parsed.utcoffset() is None:
        raise ValueError("Recorded time requires an ISO timestamp with an offset.")


def validate_focus(value):
    if not isinstance(value, dict) or set(value) != FIELDS or value["schema"] != SCHEMA:
        raise ValueError("Unsupported focus record.")
    if not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise ValueError("Invalid focus status.")
    for field in ("goal", "next_action", "checkpoint"):
        _text(value[field], field, required=field != "checkpoint")
    _digest(value["basis_sha256"])
    interruptions = value["interruptions"]
    if not isinstance(interruptions, list) or len(interruptions) > MAX_INTERRUPTS:
        raise ValueError("Focus supports at most 64 recorded interruptions per project.")
    for sequence, item in enumerate(interruptions, 1):
        if (not isinstance(item, dict) or set(item) != {"id", "text", "priority", "resolved", "recorded_at"}
                or type(item["id"]) is not int or item["id"] != sequence
                or type(item["priority"]) is not bool or type(item["resolved"]) is not bool):
            raise ValueError("Malformed focus interruption.")
        _text(item["text"], "Interruption")
        _instant(item["recorded_at"])
    events = value["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError("Focus supports 1 to 128 history entries per project.")
    for sequence, event in enumerate(events, 1):
        if (not isinstance(event, dict)
                or set(event) != {"sequence", "action", "status", "note", "next_action", "basis_sha256", "recorded_at"}
                or type(event["sequence"]) is not int or event["sequence"] != sequence
                or not isinstance(event["action"], str) or event["action"] not in ACTIONS
                or not isinstance(event["status"], str) or event["status"] not in STATUSES):
            raise ValueError("Malformed focus history entry.")
        _text(event["note"], "History note", required=False)
        _text(event["next_action"], "History next action")
        _digest(event["basis_sha256"])
        _instant(event["recorded_at"])
    if events[0]["action"] != "START" or any(events[-1][key] != value[key]
                                              for key in ("status", "next_action", "basis_sha256")):
        raise ValueError("Focus history and current checkpoint disagree.")
    return deepcopy(value)


def _event(record, action, note):
    record["events"].append({
        "sequence": len(record["events"]) + 1, "action": action, "status": record["status"],
        "note": note, "next_action": record["next_action"], "basis_sha256": record["basis_sha256"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    return validate_focus(record)


def start_focus(project, goal, next_action):
    record = {"schema": SCHEMA, "status": "ACTIVE", "goal": goal, "next_action": next_action,
              "checkpoint": "", "basis_sha256": basis_digest(project), "interruptions": [], "events": []}
    return _event(record, "START", "User started a focus session.")


def focus_action(value, action, project, *, note="", next_action=None, reviewed_changes=False, interruption_id=None):
    """Return a validated copy. No operation changes optimizer or approval state."""
    record = validate_focus(value)
    if not isinstance(action, str) or action not in ACTIONS - {"START"}:
        raise ValueError("Choose a supported focus action.")
    _text(note, "Note", required=action in {"CHECKPOINT", "PARK", "INTERRUPT"})
    if type(reviewed_changes) is not bool:
        raise ValueError("Changed input review must be an explicit boolean.")
    if record["status"] == "COMPLETE":
        raise ValueError("This focus session is complete. Its history is retained in this project.")
    if next_action is not None:
        record["next_action"] = _text(next_action, "Next action")
    if action in {"PARK", "INTERRUPT"}:
        record["interruptions"].append({"id": len(record["interruptions"]) + 1, "text": note,
                                        "priority": action == "INTERRUPT", "resolved": False,
                                        "recorded_at": datetime.now(timezone.utc).isoformat()})
    if action in {"CHECKPOINT", "INTERRUPT"}:
        record["checkpoint"] = note
        record["status"] = "PAUSED"
    elif action == "RESUME":
        current = basis_digest(project)
        if record["basis_sha256"] != current and not reviewed_changes:
            raise ValueError("Task inputs changed. Review the current problem or evidence, then explicitly confirm before resuming.")
        record["basis_sha256"] = current
        record["status"] = "ACTIVE"
    elif action == "COMPLETE":
        record["status"] = "COMPLETE"
    elif action == "RESOLVE":
        if type(interruption_id) is not int or not 1 <= interruption_id <= len(record["interruptions"]):
            raise ValueError("Select a recorded interruption.")
        item = record["interruptions"][interruption_id - 1]
        if item["resolved"]:
            raise ValueError("This interruption is already resolved.")
        item["resolved"] = True
        note = f"Resolved interruption {interruption_id}."
    return _event(record, action, note)


def focus_view(value, project):
    record = validate_focus(value)
    stale = record["basis_sha256"] != basis_digest(project)
    return {"status": record["status"], "inputs_changed": stale, "goal": record["goal"],
            "next_action": record["next_action"], "checkpoint": record["checkpoint"],
            "pending_interruptions": sum(not item["resolved"] for item in record["interruptions"]),
            "evidence_kind": "USER_RECORDED_TASK_STATE"}
