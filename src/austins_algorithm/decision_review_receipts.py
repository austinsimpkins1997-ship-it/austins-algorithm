"""Verify and inspect bounded synthetic receipts from their owning planners."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json
import math
from pathlib import Path
import re

from .workbench_project import _decode_json, _read_regular


MAX_RECEIPT_BYTES = 512 * 1024
SCHEMAS = {
    "decision-containment-rehearsal-plan/1": "rehearsal",
    "chronorealm-eligible-recovery-plan/1": "recovery",
}
_SCOPE = "SYNTHETIC_REVIEW_ONLY"
_CAPABILITIES = {
    "UNRESTRICTED_FAILOVER": "essential-service",
    "METERED_ESSENTIAL_FAILOVER": "essential-service",
    "REGIONAL_ISOLATION": "regional-isolation",
}
_MODES = {*_CAPABILITIES, "SAFE_STOP"}
_APPROVAL = "AWAITING_OPERATOR_APPROVAL"
_OPTION_FIELDS = (
    "mode", "result_mode", "state", "ingress_cap_rps", "interlocks",
    "verified_surviving_rps", "prerequisites", "sector_prerequisites",
    "rollback_steps", "preview_sha256",
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _fields(value, names, label):
    _require(type(value) is dict and set(value) == set(names), f"Invalid {label} fields.")


def _list(value, limit, label):
    _require(type(value) is list and len(value) <= limit, f"Invalid or oversized {label}.")
    return value


def _text(value, label, limit=512):
    _require(type(value) is str and 0 < len(value) <= limit and all(c.isprintable() for c in value)
             and bool(value.strip()), f"Invalid {label}.")
    return value


def _texts(value, limit, label):
    return [_text(row, label) for row in _list(value, limit, label)]


def _digest(value, label):
    _require(type(value) is str and re.fullmatch(r"[a-fA-F0-9]{64}", value) is not None,
             f"Provide a valid SHA-256 for {label}.")
    return value.lower()


def _nonnegative(value, label):
    _require(type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value),
             f"Invalid {label}.")


def receipt_sha256(document: object) -> str:
    """Compute the producer-compatible canonical SHA-256 for a JSON document."""
    try:
        raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Receipt must contain finite JSON.") from error
    _require(len(raw) <= MAX_RECEIPT_BYTES, "The canonical receipt exceeds its size limit.")
    return sha256(raw).hexdigest()


def _verify_proposal(proposal, *, exclusive):
    _fields(proposal, ("candidate_basis", "objective", "required", "candidates", "candidate_sha256",
                       "action_authority", "status", "uncovered", "selected", "equally_small",
                       "hold_reason", "direct_check", "austin"), "proposal")
    _require(proposal["action_authority"] is False, "The proposal must be read-only.")
    _require(proposal["candidate_basis"] == "already eligible local fixture candidates"
             and proposal["objective"] == "minimum number of selected candidate records",
             "Unsupported coverage objective.")
    required = _texts(proposal["required"], 6 if exclusive else 12, "required check")
    _require(required and len(set(required)) == len(required), "Required checks must be distinct and nonempty.")
    candidates = _list(proposal["candidates"], 4 if exclusive else 8, "candidate set")
    ids, coverage = [], []
    for row in candidates:
        field = "preview" if exclusive else "guarded_evidence_sha256"
        _fields(row, ("id", "covers", field), "candidate")
        ids.append(_text(row["id"], "candidate ID", 128))
        covers = _texts(row["covers"], 12, "covered check")
        _require(len(covers) == len(set(covers)), "Candidate checks must be distinct.")
        coverage.append(set(covers))
        if not exclusive:
            _digest(row[field], "candidate evidence")
    _require(len(ids) == len(set(ids)), "Candidate IDs must be distinct.")
    expected = receipt_sha256({"required": required, "candidates": candidates, "exclusive": exclusive})
    _require(_digest(proposal["candidate_sha256"], "candidate set") == expected,
             "The candidate fingerprint does not match.")

    uncovered = [check for check in required if all(check not in covers for covers in coverage)]
    matches = []
    if not uncovered:
        for count in range(1, (1 if exclusive else len(ids)) + 1):
            matches = [subset for subset in combinations(range(len(ids)), count)
                       if set(required) <= set().union(*(coverage[index] for index in subset))]
            if matches:
                break
    # The owner enumerates unsigned masks and retains every tied minimum.
    matches.sort(key=lambda subset: sum(1 << index for index in subset))
    ties = [[ids[index] for index in subset] for subset in matches]
    minimum = len(ties[0]) if ties else None
    reason = ("REQUIRED_COVERAGE_NOT_OBSERVED" if uncovered else
              None if ties else "NO_COMPATIBLE_CANDIDATE_SET")
    _require((proposal["status"], proposal["uncovered"], proposal["selected"], proposal["equally_small"],
              proposal["hold_reason"]) == ("PROPOSED" if ties else "HELD", uncovered, ties[0] if ties else [],
                                          ties, reason),
             "The proposal disagrees with independent coverage replay.")
    direct = proposal["direct_check"]
    solver = proposal["austin"]
    if uncovered:
        _require(direct is None and solver is None, "Uncovered requirements cannot have a solver verdict.")
    else:
        _fields(direct, ("minimum_count", "all_minimum_choices_matched"), "direct check")
        _require(direct["minimum_count"] == minimum
                 and (minimum is None or type(direct["minimum_count"]) is int)
                 and direct["all_minimum_choices_matched"] is True,
                 "The recorded minimum does not match coverage replay.")
        _fields(solver, ("version", "assignments_checked", "source_files_sha256"), "Austin solver record")
        _text(solver["version"], "solver version", 50)
        _require(type(solver["assignments_checked"]) is int and solver["assignments_checked"] == 2 ** len(ids),
                 "The number of checked assignments does not match the candidate set.")
        _require(type(solver["source_files_sha256"]) is dict and 0 < len(solver["source_files_sha256"]) <= 32,
                 "Invalid Austin solver source fingerprints.")
        for name, value in solver["source_files_sha256"].items():
            _text(name, "solver source filename", 128)
            _digest(value, "solver source")
    return minimum


def _verify_recovery(document, proposal):
    scenario = document["scenario"]
    keys = {"scenario", "node_id", "status", "shared_asset", "observation"}
    _require(type(scenario) is dict and set(scenario) in (keys, keys | {"shared_path_evidence"}),
             "Invalid recovery scenario fields.")
    for key in keys - {"shared_asset"}:
        _text(scenario[key], "recovery " + key)
    _require(scenario["status"] == "MODELED", "Only a modeled recovery scenario is supported.")
    if scenario["shared_asset"] is not None:
        _text(scenario["shared_asset"], "shared asset")
    if "shared_path_evidence" in scenario:
        evidence = scenario["shared_path_evidence"]
        _fields(evidence, ("primary_node_id", "backup_node_id", "shared_asset_id", "prior_backup_asset_id",
                           "provenance_type"), "shared path evidence")
        for key in evidence:
            _text(evidence[key], "shared path evidence")
        _require(evidence["provenance_type"] == "SYNTHETIC"
                 and evidence["shared_asset_id"] == scenario["shared_asset"],
                 "The shared asset evidence disagrees with the modeled scenario.")
    _nonnegative(document["as_of_s"], "synthetic recovery time")
    existing = document["existing_recovery"]
    _fields(existing, ("status", "available_modes", "fresh_probe_signals"), "existing recovery")
    _texts(existing["fresh_probe_signals"], 32, "recovery probe")
    options = _list(document["existing_options"], 4, "recovery options")
    by_mode = {}
    for option in options:
        _fields(option, _OPTION_FIELDS, "recovery option")
        mode = _text(option["mode"], "option mode", 64)
        result = _text(option["result_mode"], "option result mode", 64)
        _require(mode in _MODES and result in _MODES and mode not in by_mode, "Invalid recovery option mode.")
        by_mode[mode] = option
        _digest(option["preview_sha256"], "recovery preview")
        _nonnegative(option["ingress_cap_rps"], "ingress cap")
        surviving = option["verified_surviving_rps"]
        if surviving is not None:
            _nonnegative(surviving, "surviving capacity")
        _require(option["ingress_cap_rps"] == 0
                 or (surviving is not None and option["ingress_cap_rps"] <= surviving),
                 "Positive ingress requires sufficient verified surviving capacity.")
        interlocks = _texts(option["interlocks"], 256, "interlock")
        _texts(option["prerequisites"], 32, "prerequisite")
        _texts(option["rollback_steps"], 32, "rollback step")
        sector = option["sector_prerequisites"]
        _require(type(sector) is dict and len(sector) <= 32, "Invalid sector prerequisites.")
        for key, value in sector.items():
            _text(key, "sector prerequisite ID")
            _text(value, "sector prerequisite")
        if option["state"] == _APPROVAL:
            _require(not interlocks and result == mode, "A proposed mode disagrees with its interlocks.")
        else:
            _require(option["state"] == "HALT_AUTOMATED_FAILOVER" and bool(interlocks)
                     and result == "SAFE_STOP" and option["ingress_cap_rps"] == 0,
                     "A held recovery mode disagrees with its interlocks.")
    _require(set(by_mode) == _MODES, "The owner receipt must retain all recovery options and their interlocks.")
    available = [mode for mode, option in by_mode.items() if option["state"] == _APPROVAL and mode != "SAFE_STOP"]
    _require(existing["available_modes"] == available
             and existing["status"] == (_APPROVAL if available else "HELD"),
             "Available modes disagree with the approval state.")
    _require(set(proposal["required"]) <= set(_CAPABILITIES.values()), "Unsupported recovery goal.")
    for row in proposal["candidates"]:
        mode = row["id"]
        _require(mode in available and row["preview"] == by_mode[mode] and row["covers"] == [_CAPABILITIES[mode]]
                 and (_CAPABILITIES[mode] != "essential-service" or by_mode[mode]["ingress_cap_rps"] > 0),
                 "An eligible candidate disagrees with the owner's recovery preview.")
    _require(document["review_bundles"] == [by_mode[mode] for mode in proposal["selected"]],
             "The presented recovery bundle disagrees with the verified selection.")


def verify_receipt(document: object, *, expected_sha256: str, kind: str | None = None) -> dict:
    """Return read-only view data after an external pin check and independent replay.

    The caller gets the pin separately from the owning workflow. The receipt's
    unbundled source material and real-world freshness stay with that owner.
    """
    expected = _digest(expected_sha256, "the separately obtained expected receipt")
    actual = receipt_sha256(document)
    _require(actual == expected, "Receipt differs from the expected pin. Refresh the evidence with its owner.")
    _require(type(document) is dict and type(document.get("schema")) is str, "Unsupported receipt schema.")
    received_kind = SCHEMAS.get(document["schema"])
    _require(received_kind is not None and (kind is None or kind == received_kind), "Unexpected receipt kind.")
    common = ("schema", "scope", "action_authority")
    _require(document.get("scope") == _SCOPE and document.get("action_authority") is False,
             "The receipt must be scoped to synthetic, read-only review.")
    source = {}
    other_cases, options, bundles = [], [], []
    scenario = as_of = recovery = None
    if received_kind == "rehearsal":
        _fields(document, (*common, "source_comparison_sha256", "cascade_comparison_sha256",
                           "other_fixture_cases", "rehearsal"), "rehearsal receipt")
        source["source_comparison_sha256"] = _digest(document["source_comparison_sha256"], "source comparison")
        cascade = document["cascade_comparison_sha256"]
        source["cascade_comparison_sha256"] = None if cascade is None else _digest(cascade, "Cascade comparison")
        other_cases = _texts(document["other_fixture_cases"], 64, "other rehearsal cases")
        proposal = document["rehearsal"]
        minimum = _verify_proposal(proposal, exclusive=False)
        _require(cascade is not None or all(not row["id"].startswith("cascade:") for row in proposal["candidates"]),
                 "A Cascade rehearsal requires its source comparison fingerprint.")
    else:
        _fields(document, (*common, "scenario", "as_of_s", "tabletop_sha256", "existing_recovery",
                           "existing_options", "proposal", "review_bundles"), "recovery receipt")
        source["tabletop_sha256"] = _digest(document["tabletop_sha256"], "source tabletop")
        proposal = document["proposal"]
        minimum = _verify_proposal(proposal, exclusive=True)
        _verify_recovery(document, proposal)
        scenario, as_of = document["scenario"], document["as_of_s"]
        recovery, options, bundles = document["existing_recovery"], document["existing_options"], document["review_bundles"]
    source["candidate_sha256"] = proposal["candidate_sha256"]
    return deepcopy({
        "kind": received_kind, "status": proposal["status"], "scope": _SCOPE, "receipt_sha256": actual,
        "verification": {
            "expected_pin_matched": True, "coverage_replay_matched": True,
            "source_freshness": "RELATIVE_TO_SUPPLIED_PIN",
        },
        "source_sha256": source, "required": proposal["required"], "candidates": proposal["candidates"],
        "selected": proposal["selected"], "equally_small": proposal["equally_small"],
        "uncovered": proposal["uncovered"], "hold_reason": proposal["hold_reason"], "minimum_count": minimum,
        "other_fixture_cases": other_cases, "scenario": scenario, "as_of_s": as_of,
        "existing_recovery": recovery, "existing_options": options, "review_bundles": bundles,
        "action_authority": False,
    })


def load_receipt(path: str | Path, *, expected_sha256: str, kind: str | None = None) -> dict:
    """Load one bounded local UTF-8 JSON receipt."""
    with _read_regular(Path(path), MAX_RECEIPT_BYTES, "receipt") as (stream, _):
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    _require(len(raw) <= MAX_RECEIPT_BYTES, "The receipt file exceeds its size limit.")
    return verify_receipt(_decode_json(raw), expected_sha256=expected_sha256, kind=kind)
