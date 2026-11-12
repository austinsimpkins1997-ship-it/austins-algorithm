"""Validate recorded paired study coverage and capture evidence without the SDK."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
import math
from typing import Any

from .constraints import BooleanConstraint
from .core import _bounded_int
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, _MAX_DENSITY_BATCH_ROUNDS
from .snapshot_calibration_comparison import (
    CALIBRATION_COMPARISON, CALIBRATION_MASS_TOLERANCE, CALIBRATION_NOISE_METHOD,
)
from .snapshot_calibration_compilation_bundle import _recorded_calibration_importer
from .snapshot_calibration_fusion import (
    _CAPTURE_CHANGES, _ROUND_CHANGES, _ROUND_EVIDENCE, _compare_capture_pair, _fusion_marker, _unchanged,
)
from .snapshot_calibration_planning import _LANES, _VERSIONS, _profile, _snapshot_pair, _verified_program
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_profile import _digest, _object, _sha256, _wires


_COMPLETED = "COMPLETED"
_TIMED_OUT = "LOCAL_RESULT_WAIT_TIMED_OUT"
_WIDTH_BLOCKED = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
_STATIC_ROUND_FIELDS = {"compilation", "target_calibrations"}
_TIMEOUT_FIELDS = {"blocked_snapshot", "blocked_round", "blocked_rounds", "local_job_cancelled",
                   "local_job_cancellation_error"}
_CAPTURE_FIELDS = {
    "schema_version", "status", "execution_mode", "comparison", "noise_method", "compile_target",
    "initial_layout", "seed_transpiler", "max_rounds", "constraint_sha256", "search_qubits", "audit",
    "snapshots", "configuration_changed", "backend_version_changed", "versions", "limits", "rounds",
    "maximum_absolute_hit_probability_difference", "provider_job_submitted", "quantum_hardware_used",
}
_CAPTURE_REQUEST_FIELDS = {"constraint_sha256", "search_qubits", "seed_transpiler", "prefer_native_qasm",
                           "batch_density"}
_CROSS_LAYOUT_CHANGES = _CAPTURE_CHANGES | _TIMEOUT_FIELDS | {"status", "initial_layout"}


@dataclass(frozen=True)
class _VerifiedStudyCaptures:
    reference_reports: list[dict[str, Any]]
    fused_reports: list[dict[str, Any]]
    reference_report_sha256s: dict[str, str]
    fused_report_sha256s: dict[str, str]
    fusion_complete: bool


def _record(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    record = _object(value, label)
    if record.keys() != fields:
        raise ValueError(f"{label} has invalid fields")
    return record


def _check_coverage(
    recorded: Any, requested: list[str], completed: list[str], unavailable: list[str], label: str,
) -> None:
    attempted = set(completed) | set(unavailable)
    if recorded != {
        "requested_layouts": requested, "completed_layouts": completed, "unavailable_layouts": unavailable,
        "unattempted_layouts": [key for key in requested if key not in attempted],
    }:
        raise ValueError(f"{label} coverage disagrees with the recorded capture attempts")


def _validate_target_calibrations(value: Any, active: list[int], width: int) -> None:
    dates = _record(value, set(_LANES), "target calibrations")
    active_keys = set(map(str, active))
    for label, saved in dates.items():
        calibration = _record(saved, {"qubits", "operations"}, f"{label} target calibration")
        qubits = _record(calibration["qubits"], active_keys, f"{label} target calibration wires")
        for properties in qubits.values():
            times = _record(properties, {"t1_seconds", "t2_seconds"}, f"{label} target calibration lifetimes")
            if any(type(time) not in (float, int) or time <= 0 for time in times.values()):
                raise ValueError(f"{label} target calibration lifetimes must be positive")
        operations = calibration["operations"]
        if not isinstance(operations, list) or not operations:
            raise ValueError(f"{label} target calibration must record compiled operations")
        identities = set()
        for row in operations:
            operation = _record(row, {"operation", "wires", "error", "duration_seconds"},
                                f"{label} target calibration operation")
            name, error, duration = (operation[key] for key in ("operation", "error", "duration_seconds"))
            wires = _wires(operation["wires"], width, label=f"{label} target calibration operation wires")
            if (
                not isinstance(name, str) or not name or not wires or not set(wires).issubset(active)
                or type(error) not in (float, int) or not 0 <= error <= 1
                or type(duration) not in (float, int) or duration < 0
                or (name, tuple(wires)) in identities
            ):
                raise ValueError(f"{label} target calibration has an invalid compiled operation")
            identities.add((name, tuple(wires)))


def _validate_readout(value: Any, active: list[int]) -> None:
    dates = _record(value, set(_LANES), "readout assignment matrices")
    active_keys = set(map(str, active))
    for label, saved in dates.items():
        matrices = _object(saved, f"{label} readout assignment matrices")
        if not matrices.keys() <= active_keys:
            raise ValueError(f"{label} readout assignment matrices contain an inactive wire")
        for matrix in matrices.values():
            if not isinstance(matrix, list) or len(matrix) != 2:
                raise ValueError(f"{label} readout assignment matrix must contain two probability rows")
            for row in matrix:
                if (
                    not isinstance(row, list) or len(row) != 2
                    or any(type(entry) not in (float, int) or not 0 <= entry <= 1 for entry in row)
                    or not math.isclose(math.fsum(row), 1, rel_tol=0, abs_tol=CALIBRATION_MASS_TOLERANCE)
                ):
                    raise ValueError(f"{label} readout assignment matrix rows must be normalized probabilities")


def _validate_width_block(
    row: dict[str, Any], backend: dict[str, Any], layout: tuple[int, ...], inputs: dict[str, Any],
) -> None:
    compilation = _object(row["compilation"], "width-blocked round compilation")
    target = _object(compilation.get("target"), "width-blocked round target")
    export = _object(compilation.get("openqasm"), "width-blocked OpenQASM")
    width = backend["qubits"]
    active = _wires(export.get("export_wire_to_target_qubit"), width, label="width-blocked active wires")
    measured = _wires(export.get("classical_bit_to_target_qubit"), width, label="width-blocked measurement map")
    if (
        compilation.get("status") != "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION"
        or compilation.get("provider_job_submitted") is not False
        or compilation.get("quantum_hardware_used") is not False
        or target.get("kind") != "BACKEND_V2" or target.get("name") != backend["backend_name"]
        or type(target.get("qubits")) is not int or target["qubits"] != width
        or type(compilation.get("seed_transpiler")) is not int or compilation["seed_transpiler"] != inputs["seed_transpiler"]
        or _wires(compilation.get("initial_logical_to_target_qubit"), width, label="width-blocked compiled layout")
        != list(layout)
        or export.get("representation") != "PORTABLE_COMPILED_WIRES"
        or active != sorted(active) or len(active) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
        or len(measured) != inputs["search_qubits"] or not set(measured).issubset(active)
    ):
        raise ValueError("compile-only capture lacks verified active-width blocking evidence")
    _sha256(export.get("sha256"), "width-blocked OpenQASM digest")
    _recorded_calibration_importer(export, inputs.get("prefer_native_qasm", False))


def _validate_timeout(
    report: dict[str, Any], active_wires: list[list[int]], *, fused: bool,
) -> None:
    label = report.get("blocked_snapshot")
    cancelled = report.get("local_job_cancelled")
    single = "blocked_round" in report
    if label not in _LANES or type(cancelled) is not bool or single == ("blocked_rounds" in report):
        raise ValueError("timed-out capture has invalid blocked snapshot, round or cancellation evidence")
    if "local_job_cancellation_error" in report:
        error = report["local_job_cancellation_error"]
        if cancelled or not isinstance(error, str) or not error.isidentifier():
            raise ValueError("timed-out capture has invalid cancellation error evidence")
    blocked = [report["blocked_round"]] if single else report["blocked_rounds"]
    if (
        not isinstance(blocked, list) or len(blocked) < (1 if single else 2)
        or any(type(index) is not int for index in blocked)
    ):
        raise ValueError("timed-out capture has invalid blocked round group")
    limit = _MAX_DENSITY_BATCH_ROUNDS if report.get("batch_density", False) else 1
    for _, group in groupby(range(len(active_wires)), key=active_wires.__getitem__):
        indices = list(group)
        if any(blocked == indices[start:start + limit] for start in range(0, len(indices), limit)):
            break
    else:
        raise ValueError("timed-out capture blocked rounds disagree with the recorded simulator groups")
    if fused:
        marker = _record(report["density_fusion"], {"requested", "observed"}, "partial density fusion")
        observed = _record(marker["observed"], set(_LANES), "partial density fusion observations")
        completed = {"earlier": len(active_wires), "later": 0}
        completed[label] = blocked[0]
        # Validate recorded metadata using the full marker schema after checking which cells can exist.
        expanded = {}
        for lane in _LANES:
            prefix = {str(index) for index in range(completed[lane])}
            saved = _record(observed[lane], prefix, f"partial density fusion observations for {lane}")
            expanded[lane] = {index: saved.get(index) for index in report["rounds"]}
        _fusion_marker({"density_fusion": {**marker, "observed": expanded}, "rounds": report["rounds"]})


def _read_capture(
    sealed: Any, layout: tuple[int, ...], inputs: dict[str, Any], required: int, audit: dict[str, Any], *, fused: bool,
) -> tuple[dict[str, Any], str]:
    label = "fusion capture" if fused else "reference capture"
    envelope = _record(sealed, {"report_sha256", "report"}, f"{label} envelope")
    report = _object(envelope["report"], f"{label} report")
    digest = _sha256(envelope["report_sha256"], f"{label} digest")
    if digest != _digest(report):
        raise ValueError(f"{label} digest disagrees with its recorded report")

    status = report.get("status")
    if status not in (_COMPLETED, _TIMED_OUT) and (fused or status != _WIDTH_BLOCKED):
        raise ValueError(f"{label} has unsupported status")
    fields = _CAPTURE_FIELDS | (inputs.keys() & {"prefer_native_qasm", "batch_density"})
    if fused:
        fields.add("density_fusion")
    if status == _TIMED_OUT:
        fields |= report.keys() & _TIMEOUT_FIELDS
    elif status == _WIDTH_BLOCKED:
        fields.add("blocked_round")
    _record(report, fields, label)
    expected = {
        "schema_version": 1, "execution_mode": "SIMULATOR", "comparison": CALIBRATION_COMPARISON,
        "noise_method": CALIBRATION_NOISE_METHOD, "compile_target": "later",
        "provider_job_submitted": False, "quantum_hardware_used": False, "initial_layout": list(layout),
        "max_rounds": required - 1, "audit": audit, **{key: inputs[key] for key in _CAPTURE_REQUEST_FIELDS & inputs.keys()},
    }
    if _json_bytes({key: report.get(key) for key in expected}) != _json_bytes(expected):
        raise ValueError(f"{label} scope disagrees with the paired study request")
    backend = _snapshot_pair(report)["later"]
    _wires(report["initial_layout"], backend["qubits"], label=f"{label} layout")
    versions = _object(report["versions"], f"{label} SDK versions")
    if any(not isinstance(versions.get(name), str) or not versions[name] for name in _VERSIONS):
        raise ValueError(f"{label} lacks SDK version provenance")
    _object(report["limits"], f"{label} runtime limits")

    if status == _COMPLETED:
        rounds = _record(report["rounds"], {str(index) for index in range(required)}, f"{label} rounds")
        for row in rounds.values():
            _record(row, set(_ROUND_EVIDENCE) | _ROUND_CHANGES, f"{label} completed round")
            compilation = _object(row["compilation"], f"{label} round compilation")
            export = _object(compilation.get("openqasm"), f"{label} OpenQASM")
            active = _wires(export.get("export_wire_to_target_qubit"), backend["qubits"], label=f"{label} active wires")
            _validate_target_calibrations(row["target_calibrations"], active, backend["qubits"])
            _validate_readout(row["readout_assignment_matrices"], active)
        return report, digest

    if report["maximum_absolute_hit_probability_difference"] is not None:
        raise ValueError(f"{label} claims completed distribution evidence after a blocked capture")
    blocked = _bounded_int(report["blocked_round"], 0, required - 1, "width-blocked round") if status == _WIDTH_BLOCKED else None
    count = required if blocked is None else blocked + 1
    rounds = _record(report["rounds"], {str(index) for index in range(count)}, f"{label} rounds")
    active = []
    for index in range(count):
        row = rounds[str(index)]
        if index == blocked:
            _validate_width_block(_record(row, {"compilation"}, f"{label} blocked round"), backend, layout, inputs)
            continue
        row = _record(row, _STATIC_ROUND_FIELDS, f"{label} blocked capture round")
        program = _verified_program(
            row, backend, list(layout), inputs["seed_transpiler"], inputs["search_qubits"],
            prefer_native_qasm=inputs.get("prefer_native_qasm", False),
        )
        _validate_target_calibrations(row["target_calibrations"], program["active_target_wires"], backend["qubits"])
        active.append(program["active_target_wires"])
    if status == _TIMED_OUT:
        _validate_timeout(report, active, fused=fused)
    return report, digest


def _validate_study_captures(
    study: dict[str, Any], constraint: BooleanConstraint, *, required_rounds: int,
    requested_layouts: Sequence[tuple[int, ...]],
) -> _VerifiedStudyCaptures:
    """Verify reference and fusion seals, coverage, and per-layout evidence boundaries."""
    requested = {",".join(map(str, layout)): layout for layout in requested_layouts}
    inputs = study["inputs"]
    outcomes = [format(state, f"0{constraint.qubits}b") for state in range(1 << constraint.qubits)]
    witnesses = {bits for bits in outcomes if constraint.matches(bits)}
    audit = {"states_checked": len(outcomes), "marked_count": len(witnesses), "marked_states": sorted(witnesses)}

    references = _object(study["captures"], "reference captures")
    if references.keys() - requested.keys():
        raise ValueError("reference captures include unrequested layouts")
    reference_reports, reference_pins, reference_unavailable = {}, {}, []
    reference_stopped = False
    scope_reference = None
    for key, layout in requested.items():
        if (key in references) == reference_stopped:
            raise ValueError("reference capture attempts disagree with the recorded cancellation boundary")
        if reference_stopped:
            continue
        report, reference_pins[key] = _read_capture(references[key], layout, inputs, required_rounds, audit, fused=False)
        if scope_reference is not None and not _unchanged(scope_reference, report, _CROSS_LAYOUT_CHANGES):
            raise ValueError("reference captures disagree on the dated snapshots or capture settings")
        scope_reference = report
        if report["status"] == _COMPLETED:
            reference_reports[key] = report
        else:
            reference_unavailable.append(key)
            reference_stopped = report["status"] == _TIMED_OUT and report["local_job_cancelled"] is not True
    reference_complete = len(reference_reports) == len(requested)
    reference_status = ("CALIBRATION_STUDY_COMPLETED" if reference_complete else
                        "CALIBRATION_STUDY_PARTIAL" if reference_reports else "CALIBRATION_STUDY_UNAVAILABLE")
    if study["status"] != reference_status:
        raise ValueError("paired study status disagrees with its reference coverage")
    _check_coverage(study["coverage"], list(requested), list(reference_reports), reference_unavailable, "reference")

    fusion = study["fusion"]
    candidates = _object(fusion["captures"], "fusion captures")
    failures = _object(fusion["failures"], "fusion failures")
    eligible = {} if reference_stopped else reference_reports
    attempted = candidates.keys() | failures.keys()
    if candidates.keys() & failures.keys() or attempted - eligible.keys():
        raise ValueError("fusion capture attempts require completed reference evidence and disjoint outcomes")
    fused_reports, fused_pins, fused_unavailable = {}, {}, []
    fused_stopped = False
    for key, reference in eligible.items():
        if (key in attempted) == fused_stopped:
            raise ValueError("fusion capture attempts disagree with the recorded failure or cancellation boundary")
        if fused_stopped:
            continue
        if key in failures:
            failure = _record(failures[key], {"error_type", "message"}, "fusion failure")
            if (
                not isinstance(failure["error_type"], str) or not failure["error_type"].isidentifier()
                or not isinstance(failure["message"], str)
            ):
                raise ValueError("fusion failure has invalid error evidence")
            fused_unavailable.append(key)
            fused_stopped = True
            continue
        report, fused_pins[key] = _read_capture(candidates[key], requested[key], inputs, required_rounds, audit, fused=True)
        if report["status"] == _COMPLETED:
            fused_reports[key] = report
            continue
        if not _unchanged(reference, report, _CAPTURE_CHANGES | _TIMEOUT_FIELDS | {"status"}):
            raise ValueError("timed-out fusion capture differs from its reference scope")
        for index, row in report["rounds"].items():
            static = {field: reference["rounds"][index][field] for field in _STATIC_ROUND_FIELDS}
            if _json_bytes(row) != _json_bytes(static):
                raise ValueError("timed-out fusion capture differs from its reference compilation or calibration")
        fused_unavailable.append(key)
        fused_stopped = report["local_job_cancelled"] is not True

    fusion_complete = len(fused_reports) == len(requested)
    fusion_status = ("CALIBRATION_FUSION_STUDY_CHECKED" if fusion_complete else
                     "CALIBRATION_FUSION_STUDY_PARTIAL" if fused_reports else "CALIBRATION_FUSION_STUDY_UNAVAILABLE")
    if fusion["status"] != fusion_status or (fusion_complete and not isinstance(fusion["check"], dict)) or (
        not fusion_complete and fusion["check"] is not None
    ):
        raise ValueError("fusion check or status disagrees with the recorded fusion coverage")
    _check_coverage(fusion["coverage"], list(requested), list(fused_reports), fused_unavailable, "fusion")
    if not fusion_complete:
        for key, candidate in fused_reports.items():
            reference = reference_reports[key]
            profile_args = (constraint.qubits, inputs["constraint_sha256"], outcomes, witnesses)
            _compare_capture_pair(reference, candidate, _profile(reference, *profile_args), _profile(candidate, *profile_args))
    return _VerifiedStudyCaptures(
        list(reference_reports.values()), list(fused_reports.values()), reference_pins, fused_pins,
        fusion_complete,
    )
