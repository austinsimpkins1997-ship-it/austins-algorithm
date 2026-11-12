"""Stop after complete reference capture and explicitly resume paired fusion from pinned evidence."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS
from .ibm_models import _reduced_noise_target
from .snapshot_calibration_checkpoint_store import _publish_calibration_checkpoint, _read_calibration_checkpoint
from .snapshot_calibration_comparison import (
    SnapshotCalibrationFiles, _PreparedCalibrationComparison, _capture_restored_calibration_layout_fusion,
    _program_calibration,
)
from .snapshot_calibration_compilation_bundle import _bundle_software_versions, _restore_compiled_round
from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from .snapshot_calibration_fusion import _validate_fusion_probability_tolerance
from .snapshot_calibration_planning import _calibration_profiles
from .snapshot_calibration_study import (
    _FusionCaptures, _StudyRequest, _capture_study_references, _compilation_key_grid, _coverage_request_evidence,
    _finish_prepared_study, _logical_study_rounds, _preflight_study, _prepare_preflighted_study,
)
from .snapshot_calibration_study_captures import _read_capture
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_profile import _object, _sha256

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2


_REQUEST_FIELDS = {
    "inputs", "required_rounds", "snapshots", "versions", "compiler_scope_sha256", "initial_layouts",
    "fusion_probability_tolerance",
}
_BUDGET_FIELDS = ("target_probability", "shot_caps", "query_caps", "max_rounds_per_shot", "validation_trials")
_INPUT_FIELDS = {"constraint_sha256", "search_qubits", "seed_transpiler", *_BUDGET_FIELDS}
_OPTIONAL_FLAGS = {"prefer_native_qasm", "batch_density"}
_CAPTURE_SCOPE_FIELDS = ("snapshots", "versions", "limits", "configuration_changed", "backend_version_changed")


def _checkpoint_request(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, options: dict[str, Any],
    required: int, validation_trials: int, tolerance: float,
) -> dict[str, Any]:
    request = _coverage_request_evidence(constraint, prepared, options, required, validation_trials)
    if prepared.batch_density:
        request["inputs"]["batch_density"] = True
    return {
        **request, "initial_layouts": [list(layout) for layout in prepared.layouts],
        "fusion_probability_tolerance": tolerance,
    }


def _resume_request(
    constraint: BooleanConstraint, request: dict[str, Any], earlier: SnapshotCalibrationFiles,
    later: SnapshotCalibrationFiles,
) -> tuple[_StudyRequest, float]:
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if request.keys() != _REQUEST_FIELDS:
        raise ValueError("invalid checkpoint request fields")
    inputs = _object(request["inputs"], "checkpoint inputs")
    if not _INPUT_FIELDS <= inputs.keys() <= _INPUT_FIELDS | _OPTIONAL_FLAGS:
        raise ValueError("invalid checkpoint input fields")
    if any(inputs[flag] is not True for flag in _OPTIONAL_FLAGS & inputs.keys()):
        raise ValueError("checkpoint optional input flags must be true when recorded")
    if (
        inputs["constraint_sha256"] != constraint.digest() or type(inputs["search_qubits"]) is not int
        or inputs["search_qubits"] != constraint.qubits
    ):
        raise ValueError("checkpoint constraint differs from the supplied constraint")
    if any(not isinstance(inputs[field], list) for field in ("shot_caps", "query_caps")):
        raise ValueError("checkpoint budget axes must be JSON arrays")
    _sha256(request["compiler_scope_sha256"], "checkpoint compiler scope SHA-256")
    tolerance = _validate_fusion_probability_tolerance(request["fusion_probability_tolerance"])
    budget = {key: inputs[key] for key in _BUDGET_FIELDS}
    normalized = _preflight_study(
        constraint, initial_layouts=request["initial_layouts"], seed_transpiler=inputs["seed_transpiler"],
        paired_fusion=True, **budget,
    )
    layouts, options, required = normalized
    canonical = [list(layout) for layout in layouts], list(range(required)), {**options, "validation_trials": inputs["validation_trials"]}, tolerance
    recorded = request["initial_layouts"], request["required_rounds"], budget, request["fusion_probability_tolerance"]
    if _json_bytes(canonical) != _json_bytes(recorded):
        raise ValueError("checkpoint request is not in canonical study form")
    snapshots = _object(request["snapshots"], "checkpoint archives")
    if snapshots.keys() != {"earlier", "later"}:
        raise ValueError("checkpoint request requires both dated archives")
    for label, archive in (("earlier", earlier), ("later", later)):
        saved = _object(snapshots[label], f"checkpoint {label} archive")
        if not isinstance(archive, SnapshotCalibrationFiles):
            raise TypeError(f"{label} archive must be a SnapshotCalibrationFiles")
        if (
            saved.get("configuration_sha256") != archive.configuration_sha256
            or saved.get("properties_sha256") != archive.properties_sha256
        ):
            raise ValueError(f"checkpoint {label} archive pins differ from the supplied archives")
    return normalized, tolerance


def _required_keys(grid: dict[tuple[int, ...], tuple[str | None, ...]]) -> frozenset[str]:
    keys = [key for rounds in grid.values() for key in rounds]
    unique = frozenset(key for key in keys if key is not None)
    if len(unique) != len(keys):
        raise ValueError("checkpoint requires a distinct compilation key for every requested round")
    return unique


def _verify_references(
    constraint: BooleanConstraint, prepared: _PreparedCalibrationComparison, required: int,
    inputs: dict[str, Any], requested: dict[str, tuple[int, ...]], captures: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if captures.keys() != requested.keys():
        raise ValueError("checkpoint does not contain every requested reference layout")
    reports = {}
    for key, layout in requested.items():
        report, _ = _read_capture(captures[key], layout, inputs, required, prepared.scope["audit"], fused=False)
        if report["status"] != "COMPLETED":
            raise ValueError(f"checkpoint reference capture is incomplete for layout {key}")
        if any(_json_bytes(report[field]) != _json_bytes(prepared.scope[field]) for field in _CAPTURE_SCOPE_FIELDS):
            raise ValueError(f"checkpoint reference capture scope differs for layout {key}")
        reports[key] = report
    _calibration_profiles(list(reports.values()), constraint)
    return reports


def save_snapshot_calibration_study_checkpoint(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    destination: str | Path, initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
    batch_density: bool = False, fusion_probability_tolerance: float,
) -> dict[str, Any]:
    """Publish a checkpoint only after all default captures and their exact compiled rounds are available."""
    directory = Path(destination)
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(f"checkpoint directory already exists: {directory}")
    if not directory.parent.is_dir():
        raise FileNotFoundError(f"checkpoint parent directory does not exist: {directory.parent}")
    tolerance = _validate_fusion_probability_tolerance(fusion_probability_tolerance)
    normalized = _preflight_study(
        constraint, initial_layouts=initial_layouts, target_probability=target_probability, shot_caps=shot_caps,
        query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot, seed_transpiler=seed_transpiler,
        validation_trials=validation_trials, paired_fusion=True,
    )
    cache = SnapshotCalibrationCompilationCache(max_entries=256, max_bytes=32 * 1024 * 1024)
    prepared, options, required = _prepare_preflighted_study(
        constraint, earlier, later, normalized, seed_transpiler=seed_transpiler, prefer_native_qasm=prefer_native_qasm,
        compilation_cache=cache, batch_density=batch_density,
    )
    request = _checkpoint_request(constraint, prepared, options, required, validation_trials, tolerance)
    circuits = _logical_study_rounds(prepared, constraint.qubits, required)
    grid = _compilation_key_grid(prepared, circuits)
    expected_keys = _required_keys(grid)
    round_inputs = {layout: tuple(zip(circuits, keys, strict=True)) for layout, keys in grid.items()}
    requested = {",".join(map(str, layout)): layout for layout in prepared.layouts}
    captures, completed, unavailable, _ = _capture_study_references(
        prepared, requested, required, round_inputs=round_inputs,
    )
    flags = {"provider_job_submitted": False, "quantum_hardware_used": False}
    summary = {
        "schema_version": 1, "status": "CALIBRATION_STUDY_CHECKPOINT_UNAVAILABLE",
        "checkpoint_directory": str(directory),
        "coverage": {
            "requested_layouts": list(requested), "completed_layouts": list(completed),
            "unavailable_layouts": unavailable, "unattempted_layouts": [key for key in requested if key not in captures],
        },
        **flags,
    }
    if len(completed) != len(requested):
        return {**summary, "reference_captures": captures}
    _verify_references(constraint, prepared, required, request["inputs"], requested, captures)
    document = {
        "schema_version": 1, "phase": "REFERENCE_CAPTURE_COMPLETED", "request": request,
        "reference_captures": captures, **flags,
    }
    saved = _publish_calibration_checkpoint(directory, document, cache, expected_keys=expected_keys)
    return {**summary, "status": "CALIBRATION_STUDY_CHECKPOINT_SAVED", **saved}


def _restore_fusion_captures(
    prepared: _PreparedCalibrationComparison, circuits: tuple[QuantumCircuit, ...],
    grid: dict[tuple[int, ...], tuple[str | None, ...]], entries: dict[str, dict[str, Any]],
    requested: dict[str, tuple[int, ...]], references: dict[str, dict[str, Any]],
    *, retain_layouts: Collection[str] | None = None,
) -> _FusionCaptures:
    pending = {}
    later = prepared.backends["later"]
    seed = prepared.scope["seed_transpiler"]
    native = prepared.scope.get("prefer_native_qasm", False)
    programs = {}
    try:
        for key, layout in requested.items():
            reference = references[key]
            retain = retain_layouts is None or key in retain_layouts
            programs = {}
            for index, (circuit, round_key) in enumerate(zip(circuits, grid[layout], strict=True)):
                if round_key is None or round_key not in entries:
                    raise ValueError(f"checkpoint missing exact compilation for layout {key}, round {index}")
                entry = entries[round_key]
                row = reference["rounds"][str(index)]
                if _json_bytes(entry["report"]) != _json_bytes(row["compilation"]):
                    raise ValueError(f"checkpoint bundle report differs for layout {key}, round {index}")
                program = _restore_compiled_round(entry, circuit, later, layout, seed, prefer_native_qasm=native)
                try:
                    if _json_bytes(program.report) != _json_bytes(row["compilation"]):
                        raise ValueError(f"checkpoint restored compilation differs for layout {key}, round {index}")
                    calibrations = {label: _program_calibration(program, backend) for label, backend in prepared.backends.items()}
                    if _json_bytes(calibrations) != _json_bytes(row["target_calibrations"]):
                        raise ValueError(f"checkpoint target calibrations differ for layout {key}, round {index}")
                    if retain:
                        programs[index] = program
                finally:
                    del program
            _verify_checkpoint_readout(prepared, key, reference["rounds"])
            if retain:
                pending[key] = partial(_capture_restored_calibration_layout_fusion, prepared, reference, programs)
        return pending
    except BaseException:
        programs.clear()
        pending.clear()
        raise


def _checkpoint_readout_channels(backend: BackendV2, active: tuple[int, ...]) -> dict[int, list[list[float]]]:
    """Derive the same nonideal readout channels used by the compact model."""
    from qiskit_aer.noise.device import basic_device_readout_errors

    source_target = backend.target
    target = source_target if len(active) == source_target.num_qubits else _reduced_noise_target(source_target, active)
    channels = {}
    for wires, error in basic_device_readout_errors(target=target):
        if len(wires) != 1:
            raise ValueError("checkpoint requires single-wire device readout channels")
        if not error.ideal():
            channels[active[wires[0]]] = error.to_dict()["probabilities"]
    return channels


def _verify_checkpoint_readout(
    prepared: _PreparedCalibrationComparison, layout_key: str, rounds: dict[str, dict[str, Any]],
) -> None:
    round_wires = {index: row["compilation"]["openqasm"]["export_wire_to_target_qubit"] for index, row in rounds.items()}
    union = tuple(sorted({wire for wires in round_wires.values() for wire in wires}))
    channels = {label: _checkpoint_readout_channels(backend, union) for label, backend in prepared.backends.items()}
    for index, wires in round_wires.items():
        readout = {
            label: {str(wire): by_wire[wire] for wire in wires if wire in by_wire}
            for label, by_wire in channels.items()
        }
        if _json_bytes(readout) != _json_bytes(rounds[index]["readout_assignment_matrices"]):
            raise ValueError(f"checkpoint readout assignment matrices differ for layout {layout_key}, round {index}")


def resume_snapshot_calibration_study_checkpoint(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    checkpoint: str | Path, expected_checkpoint_sha256: str,
) -> dict[str, Any]:
    """Verify the exact saved scope, then run only the fused diagnostics and reference planning."""
    document, bundle = _read_calibration_checkpoint(checkpoint, expected_checkpoint_sha256)
    request = document["request"]
    normalized, tolerance = _resume_request(constraint, request, earlier, later)
    if _json_bytes(bundle.software_versions) != _json_bytes(_bundle_software_versions()):
        raise ValueError("checkpoint compilation bundle software versions differ from the current environment")
    inputs = request["inputs"]
    validation_trials = inputs["validation_trials"]
    # Supply a cache object to build the compiler scope. Restore from the already
    # pinned entries directly so a missing round cannot enter a compile fallback.
    prepared, options, required = _prepare_preflighted_study(
        constraint, earlier, later, normalized, seed_transpiler=inputs["seed_transpiler"],
        prefer_native_qasm=inputs.get("prefer_native_qasm", False), compilation_cache=SnapshotCalibrationCompilationCache(),
        batch_density=inputs.get("batch_density", False),
    )
    current = _checkpoint_request(constraint, prepared, options, required, validation_trials, tolerance)
    if _json_bytes(request) != _json_bytes(current):
        raise ValueError("checkpoint compiler, archive, or study request scope differs from the current request")
    captures = document["reference_captures"]
    requested = {",".join(map(str, layout)): layout for layout in prepared.layouts}
    references = _verify_references(constraint, prepared, required, inputs, requested, captures)
    circuits = _logical_study_rounds(prepared, constraint.qubits, required)
    grid = _compilation_key_grid(prepared, circuits)
    if bundle.entries.keys() != _required_keys(grid):
        raise ValueError("checkpoint compilation bundle does not cover the exact requested round keys")
    pending = _restore_fusion_captures(prepared, circuits, grid, bundle.entries, requested, references)
    del bundle
    return _finish_prepared_study(
        constraint, prepared, options, required, validation_trials, requested=requested,
        captures=captures, completed=references, unavailable=[], pending=pending, fusion_probability_tolerance=tolerance,
    )
