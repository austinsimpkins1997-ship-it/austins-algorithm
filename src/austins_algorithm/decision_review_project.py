"""Versioned local projects and offline review receipts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
import stat
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from .decision_review import decision_digest, offline_service_example, review_decision
from .decision_review_practice import new_practice, practice_view
from .decision_review_replays import inspect_replay, replay_examples
from .focus_session import validate_focus
from .workbench_project import _archive_entry, _decode_json, _json_bytes, _read_regular, _write_atomically

PROJECT_SCHEMA = "AustinDecisionProject/2"
EXPORT_SCHEMA = "AustinDecisionExport/2"
LEGACY_PROJECT_SCHEMA = "AustinDecisionProject/1"
LEGACY_EXPORT_SCHEMA = "AustinDecisionExport/1"
MAX_FILE = 16 * 1024 * 1024
MAX_DOCUMENT = 256 * 1024
MAX_HISTORY = 64
KEYS = {"schema", "document", "replays", "practice", "decision", "revisions"}
LEGACY_KEYS = KEYS - {"revisions"}
DISPOSITIONS = {"accepted", "revised", "paused", "reverted"}
FILES = {"project.json", "plan-review.json", "recovery-and-trace.json", "practice-review.json", "report.html", "README.txt"}


def _digest(raw):
    return sha256(raw).hexdigest()


def _read_json(path, limit, label):
    with _read_regular(Path(path), limit, label) as (stream, _):
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"The {label} exceeds its size limit.")
    return _decode_json(raw)


def _project_payload(project):
    if not isinstance(project, dict) or not (
        (project.get("schema") == PROJECT_SCHEMA and set(project) - {"focus"} == KEYS)
        or (project.get("schema") == LEGACY_PROJECT_SCHEMA and set(project) - {"focus"} in (KEYS, LEGACY_KEYS))
    ):
        raise ValueError("Expected a supported Decision Review project.")
    if len(_json_bytes(project)) > MAX_FILE:
        raise ValueError("The decision project exceeds 16 MiB.")
    if len(_json_bytes(project["document"])) > MAX_DOCUMENT:
        raise ValueError("The decision input exceeds 256 KiB.")
    if "focus" in project:
        validate_focus(project["focus"])
    review = review_decision(project["document"])
    replays = project["replays"]
    if not isinstance(replays, dict) or set(replays) - {"containment", "stasis"}:
        raise ValueError("A decision project can carry one containment and one Stasis replay.")
    inspected = {key: inspect_replay(document) for key, document in replays.items()}
    if any(item["kind"] != key for key, item in inspected.items()):
        raise ValueError("A replay was stored under the wrong type.")
    practice = practice_view(project["practice"])
    history = project["decision"]
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError("The decision history exceeds its record limit.")
    prior = None
    for item in history:
        prior = _verify_disposition(item, prior)
    revisions = project.get("revisions", [])
    if not isinstance(revisions, list) or len(revisions) > MAX_HISTORY:
        raise ValueError("The review revision history exceeds its record limit.")
    for index, item in enumerate(revisions, 1):
        if (not isinstance(item, dict) or set(item) != {"sequence", "document", "input_sha256"}
                or type(item["sequence"]) is not int or item["sequence"] != index
                or item["input_sha256"] != decision_digest(item["document"])):
            raise ValueError("A prior evidence snapshot disagrees with its pin or sequence.")
    return review, inspected, practice


def _verify_disposition(item, prior):
    fields = {"status", "actor", "reason", "recorded_at", "document", "input_sha256", "result_status"}
    if (not isinstance(item, dict) or set(item) != fields or not isinstance(item["status"], str)
            or item["status"] not in DISPOSITIONS):
        raise ValueError("Malformed decision history item.")
    if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > cap
           for key, cap in (("actor", 160), ("reason", 2000), ("recorded_at", 64))):
        raise ValueError("Record a named actor and reason.")
    timestamp = datetime.fromisoformat(item["recorded_at"])
    if timestamp.utcoffset() is None or (prior and timestamp < prior):
        raise ValueError("Decision history requires ordered timestamps with an offset.")
    report = review_decision(item["document"])
    if item["input_sha256"] != report["input_sha256"] or item["result_status"] != report["status"]:
        raise ValueError("A historical decision does not match its source.")
    if item["status"] == "accepted" and report["status"] != "OPTIMUM":
        raise ValueError("Acceptance requires a feasible reviewed optimum.")
    return timestamp


def validate_review_project(project):
    _project_payload(project)
    result = deepcopy(project)
    result["schema"] = PROJECT_SCHEMA
    result.setdefault("revisions", [])
    return result


def new_review_project(document=None):
    result = {"schema": PROJECT_SCHEMA,
              "document": offline_service_example() if document is None else deepcopy(document),
              "replays": replay_examples() if document is None else {},
              "practice": new_practice(), "decision": [], "revisions": []}
    return validate_review_project(result)


def project_with_document(project, document):
    previous = project["document"]
    if document == previous:
        return validate_review_project(project)
    snapshot = {"sequence": len(project["revisions"]) + 1,
                "document": previous, "input_sha256": decision_digest(previous)}
    return validate_review_project({**project, "document": document,
                                    "revisions": [*project["revisions"], snapshot]})


def project_with_replay(project, raw):
    replay = inspect_replay(raw)
    return validate_review_project({**project, "replays": {**project["replays"], replay["kind"]: raw}})


def record_review_decision(project, status, actor, reason):
    report = review_decision(project["document"])
    item = {"status": status, "actor": actor, "reason": reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "document": project["document"],
            "input_sha256": report["input_sha256"], "result_status": report["status"]}
    return validate_review_project({**project, "decision": [*project["decision"], item]})


def load_review_project(path):
    return validate_review_project(_read_json(path, MAX_FILE, "decision project"))


def save_review_project(path, project, overwrite=False, *, expected_version=None):
    raw = _json_bytes(validate_review_project(project), readable=True)
    if len(raw) > MAX_FILE:
        raise ValueError("The decision project exceeds 16 MiB.")
    _write_atomically(Path(path), lambda stream: stream.write(raw), overwrite=overwrite, expected_version=expected_version)


def _stable_practice_view(practice):
    # Replay using the last recorded action. A report describes that snapshot,
    # even if a delayed check comes due on another machine or another day.
    events = practice["events"]
    instant = (datetime.fromisoformat(events[-1]["recorded_at"]) if events
               else datetime(1970, 1, 1, tzinfo=timezone.utc))
    return practice_view(practice, now=instant)


def _readable(project, report, replays, practice):
    parts = [
        ("Decision and inputs", project["document"]),
        ("Scenario results and held checks", report),
        ("Offline evidence", replays),
        ("Practice record", practice),
        ("Human decisions", [{key: value for key, value in item.items() if key != "document"}
                             for item in project["decision"]]),
    ]
    if "revisions" in project:
        parts.append(("Prior review snapshots", [
            {"sequence": row["sequence"], "revision": row["document"]["revision"], "input_sha256": row["input_sha256"]}
            for row in project["revisions"]
        ]))
    if "focus" in project:
        parts.append(("Focus and resumption (user-recorded task state)", project["focus"]))
    title = escape(project["document"]["title"])
    body = "".join(f"<section><h2>{escape(label)}</h2><pre>{escape(_json_bytes(value, readable=True).decode('utf-8'))}</pre></section>"
                   for label, value in parts)
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title><style>body{{max-width:72rem;margin:2rem auto;padding:0 1rem;"
            "font:1rem/1.5 system-ui;color:#18243a}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:1rem;"
            "background:#eef2f6}section{margin-block:2rem}</style>"
            f"<h1>{title}</h1><p>Review outcome <strong>{escape(report['status'])}</strong></p>"
            "<p>Evidence and actor statements remain attributed. This receipt supports local replay of the supplied model.</p>"
            f"{body}</html>\n").encode("utf-8")


def _export_payload(project):
    report, replays, _ = _project_payload(project)
    practice = _stable_practice_view(project["practice"])
    files = {"project.json": _json_bytes(project, readable=True),
             "plan-review.json": _json_bytes(report, readable=True),
             "recovery-and-trace.json": _json_bytes(replays, readable=True),
             "practice-review.json": _json_bytes(practice, readable=True),
             "report.html": _readable(project, report, replays, practice),
             "README.txt": (b"Austin's Algorithm Decision Review archive.\n"
                            b"Open report.html or run python -m austins_algorithm decision-review verify ARCHIVE.zip.\n"
                            b"The verifier loads data and replays it using the installed package. This archive contains no executable code.\n"
                            b"Use --manifest-sha256 with a known digest to verify the expected export.\n")}
    if "focus" in project:
        files["focus.json"] = _json_bytes(project["focus"], readable=True)
    if any(len(raw) > MAX_FILE for raw in files.values()) or sum(map(len, files.values())) > MAX_FILE * 3:
        raise ValueError("The expanded decision export exceeds its size limit.")
    schema = LEGACY_EXPORT_SCHEMA if project["schema"] == LEGACY_PROJECT_SCHEMA else EXPORT_SCHEMA
    manifest = {"schema": schema, "input_sha256": report["input_sha256"],
                "files": {name: {"bytes": len(raw), "sha256": _digest(raw)} for name, raw in files.items()}}
    files["manifest.json"] = _json_bytes(manifest, readable=True)
    return files, report


def export_review_project(path, project, overwrite=False):
    if isinstance(project, dict) and project.get("schema") == LEGACY_PROJECT_SCHEMA:
        project = validate_review_project(project)
    files, report = _export_payload(project)

    def write(stream):
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
        if stream.tell() > MAX_FILE:
            raise ValueError("The decision archive exceeds 16 MiB.")

    _write_atomically(Path(path), write, overwrite=overwrite)
    return {"archive": str(path), "status": report["status"], "files": len(files),
            "manifest_sha256": _digest(files["manifest.json"])}


def verify_review_export(path, *, manifest_sha256=None):
    try:
        with _read_regular(Path(path), MAX_FILE, "Decision Review archive") as (stream, _), ZipFile(stream) as archive:
            members = {}
            total_bytes = 0
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                if (info.filename not in FILES | {"manifest.json", "focus.json"} or info.filename in members
                        or info.filename != info.orig_filename or info.is_dir() or info.external_attr & 0x10
                        or info.flag_bits & 1 or info.compress_type not in (ZIP_DEFLATED, ZIP_STORED)
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError("The decision archive has an unsafe or unexpected entry.")
                total_bytes += info.file_size
                if total_bytes > MAX_FILE * 3:
                    raise ValueError("The expanded decision export exceeds its size limit.")
                members[info.filename] = info
            if not FILES | {"manifest.json"} <= set(members):
                raise ValueError("The decision archive is incomplete.")
            raw = {name: _archive_entry(archive, info, 8192 if name == "manifest.json" else MAX_FILE)
                   for name, info in members.items()}
    except (BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Invalid Decision Review archive.") from exc
    head = _digest(raw["manifest.json"])
    if manifest_sha256 is not None and manifest_sha256 != head:
        raise ValueError("The archive manifest does not match the supplied digest.")
    expected, report = _export_payload(_decode_json(raw["project.json"]))
    if raw != expected:
        raise ValueError("The decision archive does not match the replayed project.")
    return {"verified": True, "archive": str(path), "status": report["status"],
            "manifest_sha256": head, "files": len(raw)}
