"""Check a pinned paired calibration study using saved outcomes and standard-library Python."""

from __future__ import annotations

from collections.abc import Set
from functools import partial
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from .constraints import BooleanConstraint, _unique_object
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .replay_receipt import _regular_file
from .snapshot_calibration_compilation_bundle import _MAX_BUNDLE_BYTES, _MAX_COMPOSITION_SOURCES
from .snapshot_calibration_fusion import (
    _validate_fusion_probability_tolerance, compare_snapshot_calibration_fusion_budgets,
)
from .snapshot_calibration_planning import plan_snapshot_calibration_budgets
from .snapshot_calibration_study import _StudyRequest, _preflight_study
from .snapshot_calibration_study_captures import _validate_study_captures
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
from .unknown_count_snapshot_profile import _sha256


MAX_PAIRED_STUDY_BYTES = 64 * 1024 * 1024
_EXECUTION_FIELDS = {"provider_job_submitted", "quantum_hardware_used"}
_STUDY_FIELDS = {
    "schema_version", "status", "inputs", "required_rounds", "coverage", "captures", "planning", "fusion",
    *_EXECUTION_FIELDS,
}
_INPUT_FIELDS = {
    "constraint_sha256", "search_qubits", "seed_transpiler", "validation_trials", "target_probability",
    "shot_caps", "query_caps", "max_rounds_per_shot",
}
_RECEIPT_FIELDS = {
    "schema_version", "status", "evidence_scope", "receipt_sha256", "output_sha256", "output_bytes",
    "source_bundle_sha256s", "request", "compiler_scope_sha256", "request_layouts_bound", "verified_conflict_count",
    "replayed_overlaps", "oracle_audit_performed", "stored_qpy_decoded", "snapshot_simulation_performed",
    *_EXECUTION_FIELDS,
}
_RECEIPT_STATES = {
    "CALIBRATION_STUDY_COMPLETED": "COMPILATION_OVERLAP_RECEIPT_STUDY_COMPLETED",
    "CALIBRATION_STUDY_PARTIAL": "COMPILATION_OVERLAP_RECEIPT_STUDY_PARTIAL",
    "CALIBRATION_STUDY_UNAVAILABLE": "COMPILATION_OVERLAP_RECEIPT_STUDY_UNAVAILABLE",
}


def _record(value: Any, required: set[str], label: str, *, optional: Set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() <= required | optional:
        raise ValueError(f"invalid paired study {label} fields")
    return value


def _expect(value: Any, expected: Any, label: str) -> None:
    if _json_bytes(value) != _json_bytes(expected):
        raise ValueError(f"paired study {label} differs from verified evidence")


def _local_header(report: dict[str, Any], label: str) -> None:
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise ValueError(f"unsupported paired study {label} schema version")
    if any(report[field] is not False for field in _EXECUTION_FIELDS):
        raise ValueError(f"paired study {label} must describe local simulation evidence")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def _read_study(path: Path, expected_sha256: str) -> tuple[dict[str, Any], int]:
    _sha256(expected_sha256, "expected paired study SHA-256")
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_PAIRED_STUDY_BYTES + 1)
    if len(raw) > MAX_PAIRED_STUDY_BYTES:
        raise ValueError(f"paired study exceeds {MAX_PAIRED_STUDY_BYTES} bytes")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("paired study does not match the expected SHA-256 digest")
    try:
        report = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=partial(_unique_object, label="paired study"),
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"invalid paired study JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("paired study file must contain a JSON object")
    return report, len(raw)


def _study_request(study: dict[str, Any], constraint: BooleanConstraint) -> _StudyRequest:
    inputs = _record(study["inputs"], _INPUT_FIELDS, "inputs", optional={"prefer_native_qasm", "batch_density"})
    if any(flag in inputs and inputs[flag] is not True for flag in ("prefer_native_qasm", "batch_density")):
        raise ValueError("paired study optional input flags must be true when recorded")
    _expect(inputs["constraint_sha256"], constraint.digest(), "constraint digest")
    _expect(inputs["search_qubits"], constraint.qubits, "constraint width")
    if any(not isinstance(inputs[field], list) for field in ("shot_caps", "query_caps")):
        raise ValueError("paired study budget axes must be JSON arrays")
    coverage = _record(
        study["coverage"], {"requested_layouts", "completed_layouts", "unavailable_layouts", "unattempted_layouts"},
        "reference coverage",
    )
    keys = coverage["requested_layouts"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_LAYOUT_PROFILES:
        raise ValueError("paired study requested layouts exceed their limits")
    initial_layouts = []
    for key in keys:
        if not isinstance(key, str) or len(key) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS * 4:
            raise ValueError("invalid paired study requested layout key")
        parts = key.split(",")
        if any(not part.isascii() or not part.isdecimal() for part in parts):
            raise ValueError("invalid paired study requested layout key")
        initial_layouts.append(tuple(map(int, parts)))
    budget = {name: inputs[name] for name in (
        "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot", "validation_trials",
    )}
    layouts, options, required = _preflight_study(
        constraint, initial_layouts=initial_layouts, **budget, seed_transpiler=inputs["seed_transpiler"],
        paired_fusion=True,
    )
    _expect(keys, [",".join(map(str, layout)) for layout in layouts], "requested layout order")
    _expect(study["required_rounds"], list(range(required)), "required rounds")
    _expect(budget, {**options, "validation_trials": inputs["validation_trials"]}, "budget inputs")
    return layouts, budget, required


def _check_receipt_replay(
    wrapper: dict[str, Any], study: dict[str, Any], layouts: tuple[tuple[int, ...], ...],
) -> str:
    _expect(wrapper["status"], _RECEIPT_STATES[study["status"]], "receipt-backed status")
    replay = _record(wrapper["replay"], _RECEIPT_FIELDS, "recorded receipt replay")
    _local_header(replay, "recorded receipt replay")
    _expect(replay["evidence_scope"], "RECORDED_COMPILATION_OVERLAPS_ONLY", "receipt replay evidence scope")
    _expect(replay["snapshot_simulation_performed"], False, "receipt replay simulation flag")
    for field in ("receipt_sha256", "output_sha256"):
        _sha256(replay[field], f"recorded receipt replay {field}")
    size = replay["output_bytes"]
    if type(size) is not int or not 1 <= size <= _MAX_BUNDLE_BYTES:
        raise ValueError("invalid paired study recorded output bundle size")
    pins = replay["source_bundle_sha256s"]
    if not isinstance(pins, list) or not 2 <= len(pins) <= _MAX_COMPOSITION_SOURCES:
        raise ValueError("invalid paired study recorded source bundle count")
    for pin in pins:
        _sha256(pin, "recorded receipt replay source bundle SHA-256")
    _expect(pins, sorted(set(pins)), "recorded source bundle order")

    status = replay["status"]
    resolved = status == "COMPILATION_BUNDLE_OVERLAP_RECEIPT_REPLAYED"
    if not resolved and status != "NO_OVERLAPS_TO_REPLAY":
        raise ValueError("invalid paired study recorded receipt replay status")
    for field in ("oracle_audit_performed", "stored_qpy_decoded"):
        _expect(replay[field], resolved, f"recorded replay {field}")
    if not resolved:
        for field, expected in (
            ("request", None), ("compiler_scope_sha256", None), ("request_layouts_bound", False),
            ("verified_conflict_count", 0), ("replayed_overlaps", {}),
        ):
            _expect(replay[field], expected, f"recorded replay {field}")
        return status

    bound = replay["request_layouts_bound"]
    if type(bound) is not bool:
        raise ValueError("invalid paired study recorded layout binding")
    request_fields = {"inputs", "required_rounds", "snapshots", "versions", "compiler_scope_sha256"}
    if bound:
        request_fields.add("initial_layouts")
    request = _record(replay["request"], request_fields, "recorded replay request")
    _sha256(replay["compiler_scope_sha256"], "recorded receipt replay compiler scope SHA-256")
    _expect(request["compiler_scope_sha256"], replay["compiler_scope_sha256"], "recorded compiler scope")
    _expect(request["inputs"], {key: val for key, val in study["inputs"].items() if key != "batch_density"},
            "recorded replay inputs")
    _expect(request["required_rounds"], study["required_rounds"], "recorded replay rounds")
    if bound:
        _expect(request["initial_layouts"], [list(layout) for layout in layouts], "recorded replay layouts")
    first_report = next(iter(study["captures"].values()))["report"]
    _expect(request["snapshots"], first_report["snapshots"], "recorded replay snapshots")
    versions = _record(request["versions"], {*first_report["versions"], "qiskit-qasm3-import"}, "replay versions")
    _expect({key: versions[key] for key in first_report["versions"]}, first_report["versions"], "recorded replay versions")
    importer = versions["qiskit-qasm3-import"]
    if importer is not None and (not isinstance(importer, str) or not importer):
        raise ValueError("invalid paired study recorded OpenQASM importer version")
    rows = replay["replayed_overlaps"]
    if not isinstance(rows, dict) or not 1 <= len(rows) <= len(layouts) * len(study["required_rounds"]):
        raise ValueError("invalid paired study recorded replay overlap count")
    _expect(replay["verified_conflict_count"], len(rows), "recorded replay overlap count")
    cells = set()
    for key, row in rows.items():
        _sha256(key, "recorded replay overlap key")
        if not isinstance(row, dict) or row.get("key") != key or row.get("status") != "EQUIVALENT_PHYSICAL_CIRCUITS":
            raise ValueError("invalid paired study recorded replay overlap row")
        layout, round_ = row.get("initial_layout"), row.get("round")
        if (
            not isinstance(layout, list) or any(type(wire) is not int for wire in layout)
            or tuple(layout) not in layouts or type(round_) is not int or round_ not in study["required_rounds"]
        ):
            raise ValueError("paired study recorded replay overlap is outside the requested layout and rounds")
        cells.add((tuple(layout), round_))
    if len(cells) != len(rows):
        raise ValueError("paired study recorded replay repeats a layout and round")
    return status


def verify_snapshot_calibration_study(
    constraint: BooleanConstraint, path: str | Path, *, expected_sha256: str,
) -> dict[str, Any]:
    """Verify the saved paired study and recompute its budget and fusion results offline."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    document, size = _read_study(Path(path), expected_sha256)
    try:
        return _verify_study_document(document, constraint, expected_sha256, size)
    except RecursionError as exc:
        raise ValueError("paired study JSON nesting exceeds verification limits") from exc


def _verify_study_document(
    document: dict[str, Any], constraint: BooleanConstraint, expected_sha256: str, size: int,
) -> dict[str, Any]:
    wrapper = None
    if "study" in document:
        wrapper = _record(document, {"schema_version", "status", "replay", "study", *_EXECUTION_FIELDS}, "receipt wrapper")
        _local_header(wrapper, "receipt wrapper")
    study = _record(document if wrapper is None else wrapper["study"], _STUDY_FIELDS, "document")
    _local_header(study, "document")
    layouts, budget, required = _study_request(study, constraint)
    fusion = _record(
        study["fusion"], {"probability_tolerance", "status", "coverage", "captures", "failures", "check"}, "fusion",
    )
    tolerance = _validate_fusion_probability_tolerance(fusion["probability_tolerance"])
    _expect(fusion["probability_tolerance"], tolerance, "probability tolerance")
    captures = _validate_study_captures(study, constraint, required_rounds=required, requested_layouts=layouts)
    replay_status = None if wrapper is None else _check_receipt_replay(wrapper, study, layouts)

    plan = check = None
    if captures.fusion_complete:
        audit = compare_snapshot_calibration_fusion_budgets(
            captures.reference_reports, captures.fused_reports, constraint, probability_tolerance=tolerance, **budget,
        )
        plan, check = audit["reference_plan"], audit["fusion_check"]
    elif captures.reference_reports:
        plan = plan_snapshot_calibration_budgets(captures.reference_reports, constraint, **budget)
    if plan is not None:
        for layout, row in plan["layouts"].items():
            row["source_capture_sha256"] = captures.reference_report_sha256s[layout]
    _expect(study["planning"], plan, "reference plan and seeded replay")
    _expect(fusion["check"], check, "fusion check")
    return {
        "schema_version": 1,
        "status": "PAIRED_CALIBRATION_STUDY_VERIFIED",
        "evidence_scope": "SAVED_PAIRED_CALIBRATION_STUDY",
        "source_sha256": expected_sha256,
        "source_bytes": size,
        "constraint_sha256": constraint.digest(),
        "study_status": study["status"],
        "fusion_status": fusion["status"],
        "coverage": study["coverage"],
        "fusion_coverage": fusion["coverage"],
        "reference_capture_sha256s": captures.reference_report_sha256s,
        "fusion_capture_sha256s": captures.fused_report_sha256s,
        "reference_plan_verified": plan is not None,
        "seeded_replay_verified": plan is not None and "validation" in plan,
        "fusion_check_status": None if check is None else check["status"],
        "receipt_replay_status": replay_status,
        "receipt_replay_evidence": None if wrapper is None else "RECORDED_ONLY",
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
