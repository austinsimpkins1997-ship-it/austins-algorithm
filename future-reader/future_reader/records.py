"""Time-bounded, audience-scoped views over append-only claim revisions."""

from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit
import re

from .codec import digest

VIEWS = {
    "at_event": "At the event",
    "as_known_then": "As known then",
    "as_understood_now": "As understood now",
    "possible_future": "From a possible future",
    "reconstruction": "Beyond the surviving timeline",
}
KINDS = {"OBSERVATION", "REPORTED_OBSERVATION", "INFERENCE", "CALCULATION",
         "SIMULATION", "FORECAST", "HYPOTHESIS", "VERIFIED_FULFILLMENT"}
VALUE_STATES = {"KNOWN", "ESTIMATE", "UNKNOWN", "NOT_APPLICABLE", "CONTRADICTION"}
TIME_ROLES = {"event", "recorded", "available", "valid"}
ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)\Z")
REQUIRED = {"id", "topic", "title", "kind", "value", "source", "times",
            "audience", "domain", "scope", "frame", "units", "dependencies", "next_check"}


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid record identifier")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1200:
        raise ValueError("Expected bounded, nonempty record text")


def instant(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError("Use an offset-qualified ISO timestamp with seconds")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _bound(data):
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != {"from", "to", "clock"}:
        raise ValueError("Time requires from, to and clock, or explicit null")
    _identifier(data["clock"])
    start, end = instant(data["from"]), instant(data["to"])
    if start > end:
        raise ValueError("Time bounds are reversed")
    return start, end


def validate(document):
    if not isinstance(document, dict) or set(document) != {"schema_version", "frame", "claims", "order"}:
        raise ValueError("Invalid timeline object")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported timeline schema")
    _identifier(document["frame"])
    claims, order = document["claims"], document["order"]
    if not isinstance(claims, list) or len(claims) > 256 or not isinstance(order, list) or len(order) > 1024:
        raise ValueError("Timeline exceeds claim or edge bounds")
    by_id, revised = {}, set()
    for row in claims:
        if not isinstance(row, dict) or not REQUIRED <= row.keys() or set(row) - REQUIRED - {"parent", "context"}:
            raise ValueError("Invalid claim fields")
        key, topic = _identifier(row["id"]), _identifier(row["topic"])
        if key in by_id:
            raise ValueError("Duplicate claim identity")
        for field in ("title", "domain", "scope", "frame", "units", "next_check"):
            _text(row[field])
        if "context" in row:
            if not isinstance(row["context"], dict) or set(row["context"]) - {
                "method", "calibration", "model_version", "assumptions", "acceptance",
                "alternatives", "conditions", "reproduction", "decision_dependencies",
            }:
                raise ValueError("Invalid optional claim context")
            for value in row["context"].values():
                _text(value)
        if not isinstance(row["kind"], str) or row["kind"] not in KINDS:
            raise ValueError("Unknown claim evidence class")
        value = row["value"]
        if (not isinstance(value, dict) or not isinstance(value.get("status"), str)
                or value["status"] not in VALUE_STATES or set(value) - {"status", "value", "basis"}):
            raise ValueError("Invalid value status")
        if value["status"] in {"KNOWN", "ESTIMATE"}:
            _text(value.get("value"))
        elif "value" in value:
            raise ValueError("Unresolved values require a basis without a substitute value")
        if "basis" in value or value["status"] != "KNOWN":
            _text(value.get("basis"))
        source = row["source"]
        if not isinstance(source, dict) or not {"title", "actor", "version"} <= source.keys() or set(source) - {"title", "actor", "version", "url"}:
            raise ValueError("Invalid source fields")
        for entry in source.values():
            _text(entry)
        if "url" in source:
            url = urlsplit(source["url"])
            if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
                raise ValueError("Source link must use HTTPS without credentials")
        times = row["times"]
        if not isinstance(times, dict) or set(times) != TIME_ROLES:
            raise ValueError("Missing timestamp role")
        bounds = {name: _bound(value) for name, value in times.items()}
        if (bounds["recorded"] and bounds["available"] and times["recorded"]["clock"] == times["available"]["clock"]
                and bounds["available"][1] < bounds["recorded"][0]):
            raise ValueError("Evidence is declared available before recording in the same clock")
        audience, deps = row["audience"], row["dependencies"]
        if not isinstance(audience, list) or not audience or len(audience) > 16:
            raise ValueError("A claim needs an explicit audience")
        for actor in audience:
            if actor != "*":
                _identifier(actor)
        if len(audience) != len(set(audience)) or "*" in audience and audience != ["*"]:
            raise ValueError("Invalid audience")
        if not isinstance(deps, list) or len(deps) > 32:
            raise ValueError("Invalid claim dependencies")
        for dep in deps:
            if _identifier(dep) not in by_id:
                raise ValueError("Dependency must refer to a preceding claim")
        if len(set(deps)) != len(deps):
            raise ValueError("Repeated claim dependency")
        if "parent" in row:
            parent = _identifier(row["parent"])
            if parent not in by_id or parent in revised or by_id[parent]["topic"] != topic:
                raise ValueError("Revision must extend a unique tip in the same topic")
            revised.add(parent)
        by_id[key] = row
    acyclic = {"causal": {}, "temporal": {}}
    seen_edges = set()
    for edge in order:
        if not isinstance(edge, dict) or set(edge) != {"from", "to", "relation", "evidence"}:
            raise ValueError("Invalid sequence edge")
        start, end, evidence = (_identifier(edge[field]) for field in ("from", "to", "evidence"))
        relation = edge["relation"]
        if start == end or not {start, end, evidence} <= by_id.keys() or relation not in ("causal", "temporal", "adjacent", "disputed"):
            raise ValueError("Invalid edge identity or relation")
        identity = (relation, start, end, evidence)
        if identity in seen_edges:
            raise ValueError("Duplicate sequence edge")
        seen_edges.add(identity)
        if relation in acyclic:
            acyclic[relation].setdefault(start, []).append(end)
    for graph in acyclic.values():
        visiting, done = set(), set()

        def walk(node):
            if node in visiting:
                raise ValueError("Cycle in established ordering")
            if node not in done:
                visiting.add(node)
                for child in graph.get(node, ()):
                    walk(child)
                visiting.remove(node)
                done.add(node)

        for node in graph:
            walk(node)
    return by_id


def view(document, mode, *, actor, as_of, event_at=None, surviving=None):
    """Filter before deriving revisions, conflicts, edges or projected output."""
    if mode not in VIEWS:
        raise ValueError("Unsupported temporal view")
    _identifier(actor)
    cutoff = instant(as_of)
    target = instant(event_at) if event_at is not None else cutoff
    by_id = validate(document)
    if surviving is not None:
        if mode != "reconstruction" or not isinstance(surviving, list) or len(surviving) > 256:
            raise ValueError("Survivors apply only to reconstruction")
        surviving = {_identifier(key) for key in surviving}
    usable = {}
    for key, row in by_id.items():
        if "*" not in row["audience"] and actor not in row["audience"]:
            continue
        if surviving is not None and key not in surviving:
            continue
        availability = [_bound(row["times"][name]) if row["times"][name]
                        and row["times"][name]["clock"] == document["frame"] else None
                        for name in ("recorded", "available")]
        if mode == "reconstruction":
            if any(bound and bound[1] > cutoff for bound in availability):
                continue
        elif any(bound is None or bound[1] > cutoff for bound in availability):
            continue
        if any(dep not in usable for dep in row["dependencies"]):
            continue
        if "parent" in row and row["parent"] not in usable:
            continue
        usable[key] = row
    visible = list(usable.values())
    if mode == "at_event":
        visible = [row for row in visible if row["times"]["event"]
                   and row["times"]["event"]["clock"] == document["frame"]
                   and (bound := _bound(row["times"]["event"])) and bound[0] <= target <= bound[1]]
    if mode in {"as_known_then", "as_understood_now", "possible_future"}:
        valid = [row for row in visible if row["times"]["valid"] is None
                 or (row["times"]["valid"]["clock"] == document["frame"]
                     and (bound := _bound(row["times"]["valid"])) and bound[0] <= cutoff <= bound[1])]
        superseded = {row["parent"] for row in valid if "parent" in row}
        visible = [row for row in valid if row["id"] not in superseded]
    if mode == "possible_future":
        visible = [row for row in visible if row["kind"] in {"FORECAST", "HYPOTHESIS"}]
    ids = {row["id"] for row in visible}
    topics = {}
    superseded = {row.get("parent") for row in visible}
    for row in visible:
        if row["id"] not in superseded and row["value"]["status"] in {"KNOWN", "ESTIMATE"}:
            topics.setdefault(row["topic"], []).append(row)
    conflicts = [[row["id"] for row in group] for group in topics.values()
                 if len({row["value"]["value"] for row in group}) > 1]
    result = deepcopy(visible)
    for row in result:
        row.pop("audience")
    return {"mode": mode, "label": VIEWS[mode], "as_of": as_of, "frame": document["frame"],
            "summary": f"{len(result)} eligible claim records. Review evidence classes and unresolved values before acting.",
            "claims": result, "conflicts": conflicts, "order": [deepcopy(edge) for edge in document["order"]
                if {edge["from"], edge["to"], edge["evidence"]} <= ids]}


def propose_append(document, claim, *, actor, as_of):
    """Preview one authored append against a pinned document."""
    before = view(document, "reconstruction", actor=actor, as_of=as_of)
    if len(before["claims"]) != len(document["claims"]):
        raise PermissionError("Timeline editing requires access to the complete local record")
    candidate = deepcopy(document)
    candidate["claims"].append(deepcopy(claim))
    after = view(candidate, "reconstruction", actor=actor, as_of=as_of)
    if len(after["claims"]) != len(candidate["claims"]):
        raise PermissionError("The proposed claim falls outside the actor's editable scope")
    impacted = {claim.get("parent")} - {None}
    for row in document["claims"]:
        if impacted.intersection(row["dependencies"]):
            impacted.add(row["id"])
    payload = {"schema_version": 1, "base": digest(document), "claim": deepcopy(claim),
               "actor": actor, "as_of": as_of, "impacted": sorted(impacted),
               "candidate": digest(candidate)}
    return {**payload, "review_digest": digest(payload)}


def apply_append(document, preview, *, actor, as_of, approved_digest):
    """Recheck authority, preview, dependencies and state before returning the new value."""
    expected = propose_append(document, preview["claim"], actor=actor, as_of=as_of)
    if preview != expected or approved_digest != expected["review_digest"]:
        raise ValueError("Revision preview is stale or approval does not match")
    return {**deepcopy(document), "claims": deepcopy(document["claims"] + [expected["claim"]])}
