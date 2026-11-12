"""Capture and plan a dated IBM calibration study over ordered layouts."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from copy import deepcopy
from hashlib import sha256
from json import loads
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import build_grover_search_circuit
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .snapshot_calibration_compilation_bundle import _bundle_key_inventory, _bundle_software_versions
from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from .snapshot_calibration_comparison import (
    MAX_CALIBRATION_BACKEND_QUBITS, SnapshotCalibrationFiles, _PreparedCalibrationComparison,
    _calibration_round_key, _capture_calibration_layout, _capture_calibration_layout_fusion,
    _capture_calibration_layout_reference, _prepare_calibration_comparison,
)
from .snapshot_calibration_fusion import (
    _validate_fusion_probability_tolerance, compare_snapshot_calibration_fusion_budgets,
)
from .snapshot_calibration_planning import (
    _calibration_budget_grid, _check_calibration_budget_work, plan_snapshot_calibration_budgets,
)
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
from .unknown_count_snapshot_profile import _digest, _wires

if TYPE_CHECKING:
    from qiskit import QuantumCircuit

    from .compilation import CompiledProgram
    from .snapshot_calibration_comparison import _PendingCalibrationFusion
    from .snapshot_calibration_overlap_receipt import _VerifiedOverlapReceipt


MAX_COMPILATION_BUNDLE_CANDIDATES = 8
_StudyRequest = tuple[tuple[tuple[int, ...], ...], dict[str, Any], int]


def _prepare_study(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int,
    seed_transpiler: int, validation_trials: int, prefer_native_qasm: bool,
    compilation_cache: SnapshotCalibrationCompilationCache | None, batch_density: bool = False,
    fusion_probability_tolerance: float | None = None,
) -> tuple[_PreparedCalibrationComparison, dict[str, Any], int]:
    request = _preflight_study(
        constraint, initial_layouts=initial_layouts, target_probability=target_probability,
        shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
        seed_transpiler=seed_transpiler, validation_trials=validation_trials,
        paired_fusion=fusion_probability_tolerance is not None,
    )
    return _prepare_preflighted_study(
        constraint, earlier, later, request, seed_transpiler=seed_transpiler, prefer_native_qasm=prefer_native_qasm,
        compilation_cache=compilation_cache, batch_density=batch_density,
    )


def _preflight_study(
    constraint: BooleanConstraint, *, initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int,
    seed_transpiler: int, validation_trials: int, paired_fusion: bool,
) -> _StudyRequest:
    options, windows, required = _calibration_budget_grid(
        constraint, target_probability, shot_caps, query_caps, max_rounds_per_shot, validation_trials,
    )
    _bounded_int(seed_transpiler, 0, 2**32 - 1, "seed_transpiler")
    if not isinstance(initial_layouts, Sequence) or not 1 <= len(initial_layouts) <= MAX_LAYOUT_PROFILES:
        raise ValueError(f"calibration study requires 1 to {MAX_LAYOUT_PROFILES} initial layouts")
    layouts = []
    for layout in initial_layouts:
        if not isinstance(layout, Sequence) or not constraint.qubits <= len(layout) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            raise ValueError("each initial layout must supply physical wires within the active limit")
        layouts.append(tuple(_wires(list(layout), MAX_CALIBRATION_BACKEND_QUBITS, label="initial layout")))
    if len(set(layouts)) != len(layouts):
        raise ValueError("calibration study requires distinct ordered initial layouts")
    if len({len(layout) for layout in layouts}) != 1:
        raise ValueError("each initial layout must contain the same number of oracle wires")
    _check_calibration_budget_work(options, windows, len(layouts) * (2 if paired_fusion else 1))
    return tuple(sorted(layouts)), options, required


def _prepare_preflighted_study(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles,
    request: _StudyRequest, *, seed_transpiler: int, prefer_native_qasm: bool,
    compilation_cache: SnapshotCalibrationCompilationCache | None, batch_density: bool,
) -> tuple[_PreparedCalibrationComparison, dict[str, Any], int]:
    layouts, options, required = request
    prepared = _prepare_calibration_comparison(
        constraint, earlier, later, initial_layouts=layouts, max_rounds=required - 1,
        seed_transpiler=seed_transpiler, compilation_cache=compilation_cache, prefer_native_qasm=prefer_native_qasm,
        batch_density=batch_density,
    )
    return prepared, options, required


def run_snapshot_calibration_study(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
    compilation_cache: SnapshotCalibrationCompilationCache | None = None, batch_density: bool = False,
    fusion_probability_tolerance: float | None = None,
) -> dict[str, Any]:
    """Capture only reachable rounds, then plan and optionally replay shared budgets."""
    if fusion_probability_tolerance is not None:
        fusion_probability_tolerance = _validate_fusion_probability_tolerance(fusion_probability_tolerance)
    prepared, options, required = _prepare_study(
        constraint, earlier, later, initial_layouts=initial_layouts, target_probability=target_probability,
        shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
        seed_transpiler=seed_transpiler, validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm,
        compilation_cache=compilation_cache, batch_density=batch_density,
        fusion_probability_tolerance=fusion_probability_tolerance,
    )
    return _run_prepared_study(
        constraint, prepared, options, required, validation_trials,
        fusion_probability_tolerance=fusion_probability_tolerance,
    )


def _run_prepared_study(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, options: dict[str, Any],
    required: int, validation_trials: int, *,
    round_inputs: dict[tuple[int, ...], tuple[tuple[QuantumCircuit, str | None], ...]] | None = None,
    verified_programs: dict[str, CompiledProgram] | None = None,
    fusion_probability_tolerance: float | None = None,
) -> dict[str, Any]:
    requested = {",".join(map(str, layout)): layout for layout in prepared.layouts}
    captures = {}
    completed = {}
    unavailable = []
    pending: dict[str, _PendingCalibrationFusion] = {}
    reference_blocked = False
    for key, layout in requested.items():
        capture_options = {} if round_inputs is None else {
            "round_inputs": round_inputs[layout], "verified_programs": verified_programs,
        }
        diagnostic = None
        if fusion_probability_tolerance is None:
            report = _capture_calibration_layout(prepared, layout, **capture_options)
        else:
            report, diagnostic = _capture_calibration_layout_reference(prepared, layout, **capture_options)
        captures[key] = _seal_study_capture(report, layout, required)
        if report["status"] == "COMPLETED":
            if diagnostic is not None:
                pending[key] = diagnostic
            elif fusion_probability_tolerance is not None:
                raise RuntimeError("completed reference capture has no prepared fusion diagnostic")
            completed[key] = report
        else:
            unavailable.append(key)
            if report["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT":
                cancelled = report.get("local_job_cancelled")
                if cancelled is False or (fusion_probability_tolerance is not None and cancelled is not True):
                    reference_blocked = True
                    break

    planning = None
    fusion = None
    if fusion_probability_tolerance is not None:
        fusion, planning = _run_study_fusion(
            constraint, prepared, options, required, validation_trials, fusion_probability_tolerance,
            requested=requested, reference_reports=completed, pending={} if reference_blocked else pending,
        )
    status = "CALIBRATION_STUDY_UNAVAILABLE"
    if completed:
        if planning is None:
            planning = plan_snapshot_calibration_budgets(
                list(completed.values()), constraint, validation_trials=validation_trials, **options,
            )
        # Keep the plan's provenance independent of the sealed capture reports.
        planning["snapshots"] = deepcopy(planning["snapshots"])
        planning["versions"] = dict(planning["versions"])
        for key, layout in planning["layouts"].items():
            layout["source_capture_sha256"] = captures[key]["report_sha256"]
        status = "CALIBRATION_STUDY_COMPLETED" if len(completed) == len(requested) else "CALIBRATION_STUDY_PARTIAL"
    inputs = _study_inputs(constraint, prepared, options, validation_trials)
    if prepared.batch_density:
        inputs["batch_density"] = True
    result = {
        "schema_version": 1, "status": status,
        "inputs": inputs,
        "required_rounds": list(range(required)),
        "coverage": {
            "requested_layouts": list(requested), "completed_layouts": list(completed),
            "unavailable_layouts": unavailable, "unattempted_layouts": [key for key in requested if key not in captures],
        },
        "captures": captures, "planning": planning,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if fusion is not None:
        result["fusion"] = fusion
    return result


def _seal_study_capture(report: dict[str, Any], layout: tuple[int, ...], required: int) -> dict[str, Any]:
    if report["initial_layout"] != list(layout):
        raise RuntimeError("captured calibration layout differs from the study request")
    if report["status"] == "COMPLETED" and report["max_rounds"] != required - 1:
        raise RuntimeError("completed calibration capture differs from the requested round range")
    return {"report_sha256": _digest(report), "report": report}


def _run_study_fusion(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, options: dict[str, Any],
    required: int, validation_trials: int, tolerance: float, *, requested: dict[str, tuple[int, ...]],
    reference_reports: dict[str, dict[str, Any]], pending: dict[str, _PendingCalibrationFusion],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    captures = {}
    completed = {}
    unavailable = []
    failures = {}
    for key, layout in requested.items():
        if key not in pending:
            continue
        try:
            report = _capture_calibration_layout_fusion(prepared, pending[key])
            captures[key] = _seal_study_capture(report, layout, required)
        except Exception as error:
            # A genuine QiskitError comes from an already imported SDK. The
            # study module and its mocked capture tests can stay SDK-free.
            qiskit_error = getattr(sys.modules.get("qiskit.exceptions"), "QiskitError", ())
            if not isinstance(error, (RuntimeError, ValueError)) and not isinstance(error, qiskit_error):
                raise
            unavailable.append(key)
            failures[key] = {"error_type": type(error).__name__, "message": str(error)}
            break
        if report["status"] == "COMPLETED":
            completed[key] = report
        else:
            unavailable.append(key)
            if report["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT" and report.get("local_job_cancelled") is not True:
                break

    planning = check = None
    if len(completed) == len(requested):
        comparison = compare_snapshot_calibration_fusion_budgets(
            list(reference_reports.values()), list(completed.values()), constraint, probability_tolerance=tolerance,
            validation_trials=validation_trials, **options,
        )
        check, planning = comparison["fusion_check"], comparison["reference_plan"]
        status = "CALIBRATION_FUSION_STUDY_CHECKED"
    else:
        status = "CALIBRATION_FUSION_STUDY_PARTIAL" if completed else "CALIBRATION_FUSION_STUDY_UNAVAILABLE"
    return {
        "probability_tolerance": tolerance, "status": status,
        "coverage": {
            "requested_layouts": list(requested), "completed_layouts": list(completed),
            "unavailable_layouts": unavailable,
            "unattempted_layouts": [key for key in requested if key not in captures and key not in failures],
        },
        "captures": captures, "failures": failures, "check": check,
    }, planning


def _study_inputs(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, options: dict[str, Any],
    validation_trials: int,
) -> dict[str, Any]:
    inputs = {
        "constraint_sha256": constraint.digest(), "search_qubits": constraint.qubits,
        "seed_transpiler": prepared.scope["seed_transpiler"], "validation_trials": validation_trials, **options,
    }
    if prepared.scope.get("prefer_native_qasm", False):
        inputs["prefer_native_qasm"] = True
    return inputs


def _coverage_request_evidence(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, options: dict[str, Any],
    required: int, validation_trials: int,
) -> dict[str, Any]:
    scope = prepared.compilation_key_scope
    if scope is None:
        raise RuntimeError("calibration compilation coverage requires a prepared compiler scope")
    return {
        "inputs": _study_inputs(constraint, prepared, options, validation_trials),
        "required_rounds": list(range(required)),
        "snapshots": prepared.scope["snapshots"], "versions": loads(scope)["versions"],
        "compiler_scope_sha256": sha256(scope).hexdigest(),
    }


def _logical_study_rounds(
    prepared: _PreparedCalibrationComparison, width: int, required: int,
) -> tuple[QuantumCircuit, ...]:
    return tuple(build_grover_search_circuit(prepared.oracle, width, iterations=index) for index in range(required))


def _compilation_key_grid(
    prepared: _PreparedCalibrationComparison, circuits: Sequence[QuantumCircuit],
) -> dict[tuple[int, ...], tuple[str | None, ...]]:
    return {
        layout: tuple(_calibration_round_key(prepared, layout, circuit) for circuit in circuits)
        for layout in prepared.layouts
    }


def _requested_overlap_cells(
    grid: dict[tuple[int, ...], tuple[str | None, ...]], keys: Collection[str],
) -> dict[str, list[tuple[tuple[int, ...], int]]]:
    cells: dict[str, list[tuple[tuple[int, ...], int]]] = {}
    for layout, rounds in grid.items():
        for index, key in enumerate(rounds):
            if key is not None and key in keys:
                cells.setdefault(key, []).append((layout, index))
    return cells


def _round_key_status(key: str | None, bundle_keys: frozenset[str]) -> str:
    if key is None:
        return "UNKEYABLE"
    return "PRESENT" if key in bundle_keys else "ABSENT"


def _summarize_key_coverage(
    grid: dict[tuple[int, ...], tuple[str | None, ...]], bundle_keys: frozenset[str],
) -> tuple[dict[str, Any], list[str]]:
    counts = dict.fromkeys(("PRESENT", "ABSENT", "UNKEYABLE"), 0)
    requested_keys: set[str] = set()
    for rounds in grid.values():
        for key in rounds:
            counts[_round_key_status(key, bundle_keys)] += 1
            if key is not None:
                requested_keys.add(key)
    total = sum(counts.values())
    present = counts["PRESENT"]
    status = "ALL_PRESENT" if present == total else "PARTIAL" if present else "NONE_PRESENT"
    return {
        "status": status, "requested_round_count": total, "present_round_count": present,
        "absent_round_count": counts["ABSENT"], "unkeyable_round_count": counts["UNKEYABLE"],
        "requested_unique_key_count": len(requested_keys),
        "present_unique_key_count": len(requested_keys & bundle_keys),
    }, sorted(bundle_keys - requested_keys)


def check_snapshot_calibration_compilation_coverage(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], compilation_cache: SnapshotCalibrationCompilationCache,
    max_rounds_per_shot: int = MAX_ITERATIONS, seed_transpiler: int = 7, validation_trials: int = 0,
    prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Check requested round keys against a trusted, pinned compilation bundle."""
    if not isinstance(compilation_cache, SnapshotCalibrationCompilationCache):
        raise TypeError("compilation_cache must be a SnapshotCalibrationCompilationCache")
    bundle_sha256, bundle_keys = compilation_cache._bundle_coverage()
    prepared, options, required = _prepare_study(
        constraint, earlier, later, initial_layouts=initial_layouts, target_probability=target_probability,
        shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
        seed_transpiler=seed_transpiler, validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm,
        compilation_cache=compilation_cache,
    )
    request = _coverage_request_evidence(constraint, prepared, options, required, validation_trials)
    grid = _compilation_key_grid(prepared, _logical_study_rounds(prepared, constraint.qubits, required))
    coverage, unused = _summarize_key_coverage(grid, bundle_keys)
    layouts = {
        ",".join(map(str, layout)): {
            "initial_layout": list(layout),
            "rounds": {str(index): {"key": key, "status": _round_key_status(key, bundle_keys)}
                       for index, key in enumerate(rounds)},
        }
        for layout, rounds in grid.items()
    }
    return {
        "schema_version": 1, "status": "COMPILATION_BUNDLE_COVERAGE_CHECKED",
        "evidence_scope": "REQUESTED_COMPILATION_KEYS_ONLY", **request,
        "bundle": {"sha256": bundle_sha256, "key_count": len(bundle_keys),
                   "unused_key_count": len(unused), "unused_keys": unused},
        "layouts": layouts, "coverage": coverage,
        "oracle_audit_performed": True, "stored_qpy_decoded": False, "snapshot_simulation_performed": False,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def compare_snapshot_calibration_compilation_coverage(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], bundles: Sequence[tuple[str | Path, str]],
    max_rounds_per_shot: int = MAX_ITERATIONS, seed_transpiler: int = 7, validation_trials: int = 0,
    prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Compare pinned bundle key coverage against one audited study request."""
    if not isinstance(prefer_native_qasm, bool):
        raise TypeError("prefer_native_qasm must be a boolean")
    if (not isinstance(bundles, Sequence) or isinstance(bundles, (str, bytes))
            or not 2 <= len(bundles) <= MAX_COMPILATION_BUNDLE_CANDIDATES):
        raise ValueError(f"compilation coverage comparison requires 2 to {MAX_COMPILATION_BUNDLE_CANDIDATES} bundles")
    pins: set[str] = set()
    for candidate in bundles:
        if (not isinstance(candidate, (tuple, list)) or len(candidate) != 2
                or not isinstance(candidate[0], (str, Path)) or not isinstance(candidate[1], str)):
            raise ValueError("each compilation coverage candidate requires a path and SHA-256 pin")
        pin = candidate[1]
        if pin in pins:
            raise ValueError("compilation coverage comparison requires distinct bundle SHA-256 pins")
        pins.add(pin)

    versions = _bundle_software_versions()
    candidates = []
    compatible_keys = {}
    for index, (path, pin) in enumerate(bundles, 1):
        recorded, keys = _bundle_key_inventory(path, pin)
        differences = {name: {"recorded": recorded[name], "current": current}
                       for name, current in sorted(versions.items()) if recorded[name] != current}
        candidate_id = f"bundle_{index}"
        if not differences:
            compatible_keys[candidate_id] = keys
        candidates.append({
            "id": candidate_id, "sha256": pin, "status": "INCOMPATIBLE_SOFTWARE" if differences else "COMPATIBLE",
            "versions": recorded, "differing_versions": differences,
            "coverage": None, "key_count": len(keys), "unused_key_count": None, "unused_keys": None,
            "missing_requested_rounds": None,
        })

    request = {"inputs": None, "required_rounds": [], "snapshots": None, "versions": None,
               "compiler_scope_sha256": None}
    layouts = {}
    requested = None
    rank_groups: dict[int, list[str]] = {}
    if compatible_keys:
        # Preparation only uses this cache to enable the same compiler scope as capture.
        prepared, options, required = _prepare_study(
            constraint, earlier, later, initial_layouts=initial_layouts, target_probability=target_probability,
            shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
            seed_transpiler=seed_transpiler, validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm,
            compilation_cache=SnapshotCalibrationCompilationCache(),
        )
        request = _coverage_request_evidence(constraint, prepared, options, required, validation_trials)
        grid = _compilation_key_grid(prepared, _logical_study_rounds(prepared, constraint.qubits, required))
        layouts = {
            ",".join(map(str, layout)): {
                "initial_layout": list(layout),
                "rounds": {str(index): {"key": key} for index, key in enumerate(rounds)},
            }
            for layout, rounds in grid.items()
        }
        requested = len(grid) * required
        for candidate in candidates:
            candidate_id = candidate["id"]
            keys = compatible_keys.get(candidate_id)
            if keys is None:
                continue
            coverage, unused = _summarize_key_coverage(grid, keys)
            missing = [
                {"layout": layout, "round": int(index), "key": row["key"],
                 "status": _round_key_status(row["key"], keys)}
                for layout, record in layouts.items() for index, row in record["rounds"].items()
                if row["key"] not in keys
            ]
            candidate.update(coverage=coverage, unused_key_count=len(unused), unused_keys=unused,
                             missing_requested_rounds=missing)
            present = coverage["present_round_count"]
            if present:
                rank_groups.setdefault(present, []).append(candidate_id)

    ranks = [
        {"rank": rank, "present_round_count": count, "missing_round_count": requested - count, "bundle_ids": ids}
        for rank, (count, ids) in enumerate(sorted(rank_groups.items(), reverse=True), 1)
    ] if requested is not None else []
    best = ranks[0]["present_round_count"] if ranks else 0
    if ranks:
        ranking_status = "COVERAGE_RANKED"
    elif compatible_keys:
        ranking_status = "NO_COMPATIBLE_COVERAGE"
    else:
        ranking_status = "NO_COMPATIBLE_BUNDLES"
    return {
        "schema_version": 1, "status": "COMPILATION_BUNDLE_COVERAGE_COMPARED",
        "evidence_scope": "REQUESTED_COMPILATION_KEYS_ONLY", **request,
        "current_bundle_software_versions": versions, "request_keys_prepared": bool(compatible_keys),
        "layouts": layouts, "bundles": candidates,
        "ranking": {"status": ranking_status, "best_covered_round_count": None if requested is None else best,
                    "best_missing_round_count": None if requested is None else requested - best,
                    "best_bundle_ids": ranks[0]["bundle_ids"] if ranks else [], "ranks": ranks},
        "oracle_audit_performed": bool(compatible_keys), "stored_qpy_decoded": False,
        "snapshot_simulation_performed": False, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def compose_snapshot_calibration_compilation_for_study(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    destination: str | Path, bundles: Sequence[tuple[str | Path, str]],
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Compose bundles and verify eligible QPY overlaps for the exact study request."""

    from .snapshot_calibration_compilation_bundle import _compose_bundles

    if not isinstance(prefer_native_qasm, bool):
        raise TypeError("prefer_native_qasm must be a boolean")

    study_overlap = {
        "status": "NOT_REQUIRED", "request": None, "differing_versions": {},
        "oracle_audit_performed": False, "snapshot_simulation_performed": False,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }

    def resolve_conflicts(
        conflicts: dict[str, dict[str, dict[str, Any]]], recorded_versions: dict[str, str | None],
    ) -> dict[str, dict[str, Any]]:
        evidence = {
            key: {"status": "REQUEST_KEY_NOT_FOUND", "key": key, "source_sha256s": sorted(sources),
                  "selected_source_sha256": None, "differing_fields": ["qpy"], "stored_qpy_decoded": False}
            for key, sources in conflicts.items()
        }
        current_versions = _bundle_software_versions()
        differences = {
            name: {"recorded": recorded_versions[name], "current": current}
            for name, current in sorted(current_versions.items()) if recorded_versions[name] != current
        }
        if differences:
            study_overlap.update(status="INCOMPATIBLE_SOFTWARE", differing_versions=differences)
            for row in evidence.values():
                row["status"] = "INCOMPATIBLE_SOFTWARE"
            return evidence

        from .snapshot_calibration_overlap import verify_snapshot_calibration_overlap

        prepared, options, required = _prepare_study(
            constraint, earlier, later, initial_layouts=initial_layouts, target_probability=target_probability,
            shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
            seed_transpiler=seed_transpiler, validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm,
            compilation_cache=SnapshotCalibrationCompilationCache(),
        )
        study_overlap.update(
            status="REQUEST_PREPARED", oracle_audit_performed=True,
            request={
                **_coverage_request_evidence(constraint, prepared, options, required, validation_trials),
                "initial_layouts": [list(layout) for layout in prepared.layouts],
            },
        )
        circuits = _logical_study_rounds(prepared, constraint.qubits, required)
        cells = _requested_overlap_cells(_compilation_key_grid(prepared, circuits), conflicts)

        for key, matches in cells.items():
            if len(matches) != 1:
                evidence[key].update(
                    status="AMBIGUOUS_REQUEST_KEY",
                    requested_cells=[{"initial_layout": list(layout), "round": index} for layout, index in matches],
                )
                continue
            layout, index = matches[0]
            verification = verify_snapshot_calibration_overlap(
                key, conflicts[key], circuits[index], earlier_backend=prepared.backends["earlier"],
                later_backend=prepared.backends["later"], layout=layout, seed=seed_transpiler,
                prefer_native_qasm=prefer_native_qasm,
            )
            evidence[key] = {**verification, "initial_layout": list(layout), "round": index}
        return evidence

    report = _compose_bundles(destination, bundles, resolve_conflicts=resolve_conflicts)
    report["study_overlap"] = study_overlap
    return report


def replay_snapshot_calibration_overlap_receipt(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    receipt: str | Path, expected_receipt_sha256: str,
    output_bundle: str | Path, expected_output_sha256: str, sources: Sequence[tuple[str | Path, str]],
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Replay the recorded physical overlap checks for a pinned receipt and study request."""
    return _run_overlap_receipt_workflow(
        constraint, earlier, later, receipt=receipt, expected_receipt_sha256=expected_receipt_sha256,
        output_bundle=output_bundle, expected_output_sha256=expected_output_sha256, sources=sources,
        initial_layouts=initial_layouts, target_probability=target_probability, shot_caps=shot_caps,
        query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot, seed_transpiler=seed_transpiler,
        validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm, run_study=False,
    )


def run_snapshot_calibration_study_with_overlap_receipt(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    receipt: str | Path, expected_receipt_sha256: str,
    output_bundle: str | Path, expected_output_sha256: str, sources: Sequence[tuple[str | Path, str]],
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
    batch_density: bool = False, fusion_probability_tolerance: float | None = None,
) -> dict[str, Any]:
    """Replay the receipt, then capture the same study using the prepared request."""
    return _run_overlap_receipt_workflow(
        constraint, earlier, later, receipt=receipt, expected_receipt_sha256=expected_receipt_sha256,
        output_bundle=output_bundle, expected_output_sha256=expected_output_sha256, sources=sources,
        initial_layouts=initial_layouts, target_probability=target_probability, shot_caps=shot_caps,
        query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot, seed_transpiler=seed_transpiler,
        validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm, run_study=True,
        batch_density=batch_density, fusion_probability_tolerance=fusion_probability_tolerance,
    )


def _run_overlap_receipt_workflow(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    receipt: str | Path, expected_receipt_sha256: str,
    output_bundle: str | Path, expected_output_sha256: str, sources: Sequence[tuple[str | Path, str]],
    initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int,
    seed_transpiler: int, validation_trials: int, prefer_native_qasm: bool, run_study: bool,
    batch_density: bool = False, fusion_probability_tolerance: float | None = None,
) -> dict[str, Any]:
    from .snapshot_calibration_overlap_receipt import _read_verified_overlap_receipt

    if not isinstance(prefer_native_qasm, bool):
        raise TypeError("prefer_native_qasm must be a boolean")

    if type(batch_density) is not bool:
        raise TypeError("batch_density must be a boolean")
    study_request = None
    if fusion_probability_tolerance is not None:
        fusion_probability_tolerance = _validate_fusion_probability_tolerance(fusion_probability_tolerance)
        study_request = _preflight_study(
            constraint, initial_layouts=initial_layouts, target_probability=target_probability,
            shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
            seed_transpiler=seed_transpiler, validation_trials=validation_trials, paired_fusion=True,
        )
    verified = _read_verified_overlap_receipt(
        receipt, expected_receipt_sha256=expected_receipt_sha256, output_bundle=output_bundle,
        expected_output_sha256=expected_output_sha256, sources=sources,
    )
    replay = {
        "schema_version": 1, "status": "NO_OVERLAPS_TO_REPLAY",
        "evidence_scope": "RECORDED_COMPILATION_OVERLAPS_ONLY",
        "receipt_sha256": verified.receipt_sha256, "output_sha256": verified.output_sha256,
        "output_bytes": verified.output_bytes,
        "source_bundle_sha256s": [source["sha256"] for source in verified.report["sources"]],
        "request": None, "compiler_scope_sha256": None, "request_layouts_bound": False,
        "verified_conflict_count": 0, "replayed_overlaps": {},
        "oracle_audit_performed": False, "stored_qpy_decoded": False, "snapshot_simulation_performed": False,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if not verified.resolved_rows and not run_study:
        return replay

    if _json_bytes(verified.report["software_versions"]) != _json_bytes(_bundle_software_versions()):
        raise ValueError("overlap receipt software versions differ from the current environment")
    cache = (
        SnapshotCalibrationCompilationCache._from_verified_bundle(verified.output_entries, verified.output_sha256)
        if run_study else SnapshotCalibrationCompilationCache()
    )
    if study_request is None:
        prepared, options, required = _prepare_study(
            constraint, earlier, later, initial_layouts=initial_layouts, target_probability=target_probability,
            shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
            seed_transpiler=seed_transpiler, validation_trials=validation_trials, prefer_native_qasm=prefer_native_qasm,
            compilation_cache=cache, batch_density=batch_density,
        )
    else:
        prepared, options, required = _prepare_preflighted_study(
            constraint, earlier, later, study_request, seed_transpiler=seed_transpiler,
            prefer_native_qasm=prefer_native_qasm, compilation_cache=cache, batch_density=batch_density,
        )
    request = None
    layouts_bound = False
    if verified.resolved_rows:
        request = _coverage_request_evidence(constraint, prepared, options, required, validation_trials)
        recorded_request = verified.report["study_overlap"]["request"]
        layouts_bound = "initial_layouts" in recorded_request
        if layouts_bound:
            request["initial_layouts"] = [list(layout) for layout in prepared.layouts]
        if _json_bytes(request) != _json_bytes(recorded_request):
            raise ValueError("overlap receipt request does not match the current study scope")

    circuits = _logical_study_rounds(prepared, constraint.qubits, required)
    grid = _compilation_key_grid(prepared, circuits)
    winners: dict[str, CompiledProgram] = {}
    if request is not None:
        replayed, winners = _replay_prepared_overlap(verified, prepared, circuits, grid, retain_programs=run_study)
        replay.update(
            status="COMPILATION_BUNDLE_OVERLAP_RECEIPT_REPLAYED", request=request,
            compiler_scope_sha256=request["compiler_scope_sha256"], request_layouts_bound=layouts_bound,
            verified_conflict_count=len(replayed), replayed_overlaps=replayed,
            oracle_audit_performed=True, stored_qpy_decoded=True,
        )
    if not run_study:
        return replay

    round_inputs = {layout: tuple(zip(circuits, keys, strict=True)) for layout, keys in grid.items()}
    study = _run_prepared_study(
        constraint, prepared, options, required, validation_trials,
        round_inputs=round_inputs, verified_programs=winners, fusion_probability_tolerance=fusion_probability_tolerance,
    )
    status = {
        "CALIBRATION_STUDY_COMPLETED": "COMPILATION_OVERLAP_RECEIPT_STUDY_COMPLETED",
        "CALIBRATION_STUDY_PARTIAL": "COMPILATION_OVERLAP_RECEIPT_STUDY_PARTIAL",
        "CALIBRATION_STUDY_UNAVAILABLE": "COMPILATION_OVERLAP_RECEIPT_STUDY_UNAVAILABLE",
    }[study["status"]]
    return {
        "schema_version": 1, "status": status, "replay": replay, "study": study,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _replay_prepared_overlap(
    verified: _VerifiedOverlapReceipt, prepared: _PreparedCalibrationComparison,
    circuits: Sequence[QuantumCircuit], grid: dict[tuple[int, ...], tuple[str | None, ...]], *,
    retain_programs: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, CompiledProgram]]:
    from .snapshot_calibration_overlap import _verify_snapshot_calibration_overlap, verify_snapshot_calibration_overlap

    cells = _requested_overlap_cells(grid, verified.resolved_rows)
    recorded_overlaps = verified.report["overlap_verification"]
    requested = {}
    for key in sorted(verified.resolved_rows):
        matches = cells.get(key, [])
        if len(matches) != 1:
            raise ValueError(f"overlap receipt key {key} must match exactly one requested layout and round")
        layout, index = matches[0]
        recorded = recorded_overlaps[key]
        if _json_bytes([list(layout), index]) != _json_bytes([recorded["initial_layout"], recorded["round"]]):
            raise ValueError(f"overlap receipt key {key} differs from its recorded layout or round")
        requested[key] = (layout, index)

    replayed = {}
    winners = {}
    for key, (layout, index) in requested.items():
        rows = verified.resolved_rows[key]
        options = {"earlier_backend": prepared.backends["earlier"], "later_backend": prepared.backends["later"],
                   "layout": layout, "seed": prepared.scope["seed_transpiler"],
                   "prefer_native_qasm": prepared.scope.get("prefer_native_qasm", False)}
        if retain_programs:
            checked, program = _verify_snapshot_calibration_overlap(key, rows, circuits[index], **options)
        else:
            checked = verify_snapshot_calibration_overlap(key, rows, circuits[index], **options)
            program = None
        evidence = {**checked, "initial_layout": list(layout), "round": index}
        if checked["status"] != "EQUIVALENT_PHYSICAL_CIRCUITS" or _json_bytes(evidence) != _json_bytes(recorded_overlaps[key]):
            raise ValueError(f"replayed physical overlap evidence differs for key {key} ({checked['status']})")
        if retain_programs:
            selected = rows.get(checked["selected_source_sha256"])
            if program is None or selected is None or _json_bytes(selected) != _json_bytes(verified.output_entries.get(key)):
                raise ValueError(f"replayed physical overlap winner differs from the pinned output for key {key}")
            winners[key] = program
        replayed[key] = evidence
    return replayed, winners
