"""Persist each completed reference layout and explicitly resume from a pinned generation."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from sys import modules
from typing import TYPE_CHECKING, Any

from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS, _bounded_int
from .snapshot_calibration_comparison import SnapshotCalibrationFiles, _PreparedCalibrationComparison
from .snapshot_calibration_compilation_bundle import _bundle_software_versions
from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from .snapshot_calibration_fusion import _validate_fusion_probability_tolerance
from .snapshot_calibration_incremental_store import (
    _initialize_incremental_calibration_store, _publish_incremental_calibration_generation,
    _read_incremental_calibration_generations,
)
from .snapshot_calibration_study import (
    _FusionCaptures, _StudyRequest, _capture_study_references, _clear_fusion_error_frames, _compilation_key_grid,
    _finish_prepared_study, _logical_study_rounds, _preflight_study, _prepare_preflighted_study,
)
from .snapshot_calibration_study_checkpoint import (
    _checkpoint_request, _required_keys, _restore_fusion_captures, _resume_request, _verify_references,
)
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES

if TYPE_CHECKING:
    from qiskit import QuantumCircuit

    from .snapshot_calibration_incremental_store import _VerifiedIncrementalGeneration


@dataclass(frozen=True)
class _IncrementalStudy:
    constraint: BooleanConstraint
    prepared: _PreparedCalibrationComparison
    options: dict[str, Any]
    required: int
    request: dict[str, Any]
    circuits: tuple[QuantumCircuit, ...]
    grid: dict[tuple[int, ...], tuple[str | None, ...]]
    layouts: dict[str, tuple[int, ...]]
    software_versions: dict[str, str | None]


def _reference_limit(value: int | None) -> int:
    return MAX_LAYOUT_PROFILES if value is None else _bounded_int(value, 1, MAX_LAYOUT_PROFILES, "max_new_reference_layouts")


def _prepare_incremental_study(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles,
    normalized: _StudyRequest, *, seed_transpiler: int, prefer_native_qasm: bool, batch_density: bool,
    validation_trials: int, tolerance: float, software_versions: dict[str, str | None],
    recorded_request: dict[str, Any] | None = None,
) -> _IncrementalStudy:
    prepared, options, required = _prepare_preflighted_study(
        constraint, earlier, later, normalized, seed_transpiler=seed_transpiler, prefer_native_qasm=prefer_native_qasm,
        compilation_cache=SnapshotCalibrationCompilationCache(), batch_density=batch_density,
    )
    request = _checkpoint_request(constraint, prepared, options, required, validation_trials, tolerance)
    if recorded_request is not None and _json_bytes(request) != _json_bytes(recorded_request):
        raise ValueError("incremental generation compiler, archive, or study request differs from the current request")
    circuits = _logical_study_rounds(prepared, constraint.qubits, required)
    grid = _compilation_key_grid(prepared, circuits)
    _required_keys(grid)
    layouts = {",".join(map(str, layout)): layout for layout in prepared.layouts}
    return _IncrementalStudy(constraint, prepared, options, required, request, circuits, grid, layouts, software_versions)


def _restore_generation(
    runtime: _IncrementalStudy, key: str, generation: _VerifiedIncrementalGeneration,
    *, retain_layouts: Collection[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], dict[str, Any]] | None]:
    layout = runtime.layouts[key]
    if _json_bytes(generation.document["layout"]) != _json_bytes(list(layout)):
        raise ValueError(f"incremental generation does not match requested layout {key}")
    if _json_bytes(generation.document["request"]) != _json_bytes(runtime.request):
        raise ValueError(f"incremental generation request differs for layout {key}")
    if _json_bytes(generation.bundle.software_versions) != _json_bytes(runtime.software_versions):
        raise ValueError(f"incremental generation software versions differ for layout {key}")
    grid = {layout: runtime.grid[layout]}
    if generation.bundle.entries.keys() != _required_keys(grid):
        raise ValueError(f"incremental generation bundle does not cover the exact round keys for layout {key}")
    sealed = generation.document["capture"]
    requested = {key: layout}
    references = _verify_references(
        runtime.constraint, runtime.prepared, runtime.required, runtime.request["inputs"], requested, {key: sealed},
    )
    pending = _restore_fusion_captures(
        runtime.prepared, runtime.circuits, grid, generation.bundle.entries, requested, references,
        retain_layouts=retain_layouts,
    )
    return sealed, references[key], pending.get(key)


def _restore_prefix(
    runtime: _IncrementalStudy, generations: tuple[_VerifiedIncrementalGeneration, ...],
    *, retain_layouts: Collection[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], _FusionCaptures]:
    keys = list(runtime.layouts)[:len(generations)]
    captures, completed, pending = {}, {}, {}
    try:
        for key, generation in zip(keys, generations, strict=True):
            captures[key], completed[key], callback = _restore_generation(
                runtime, key, generation, retain_layouts=retain_layouts,
            )
            if callback is not None:
                pending[key] = callback
            del callback
        return captures, completed, pending
    except BaseException:
        pending.clear()
        raise


def _incremental_result(
    status: str, tip: str | None, runtime: _IncrementalStudy, captures: dict[str, dict[str, Any]], *,
    study: dict[str, Any] | None = None, failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1, "status": status, "generation_sha256": tip,
        "completed_layouts": list(captures), "remaining_layouts": [key for key in runtime.layouts if key not in captures],
        "failure": failure, "study": study, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _incremental_failure(
    error: Exception, layout: str, capture: dict[str, Any] | None,
) -> dict[str, Any] | None:
    # A Qiskit error can only come from an already loaded SDK.
    qiskit_error = getattr(modules.get("qiskit.exceptions"), "QiskitError", ())
    if not isinstance(error, (OSError, RuntimeError, ValueError)) and not isinstance(error, qiskit_error):
        return None
    failure: dict[str, Any] = {"layout": layout, "error_type": type(error).__name__, "message": str(error)}
    if capture is not None:
        failure["capture"] = capture
    _clear_fusion_error_frames(error)
    return failure


def _publish_verified_generation(
    runtime: _IncrementalStudy, directory: Path, key: str, capture: dict[str, Any],
    cache: SnapshotCalibrationCompilationCache, ordinal: int, tip: str | None,
    *, retain_layouts: Collection[str] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], Callable[[], dict[str, Any]] | None]:
    layout = runtime.layouts[key]
    restored = None

    def verify(generation: _VerifiedIncrementalGeneration) -> None:
        nonlocal restored
        restored = _restore_generation(runtime, key, generation, retain_layouts=retain_layouts)

    published = _publish_incremental_calibration_generation(
        directory, request=runtime.request, layout=layout, capture=capture, cache=cache,
        ordinal=ordinal, previous_generation_sha256=tip, expected_keys=_required_keys({layout: runtime.grid[layout]}),
        verify_generation=verify,
    )
    if restored is None:
        raise RuntimeError("incremental generation was published without restore verification")
    return published["generation_sha256"], *restored


def _advance_incremental_study(
    runtime: _IncrementalStudy, directory: Path, limit: int, *, tip: str | None,
    captures: dict[str, dict[str, Any]], completed: dict[str, dict[str, Any]], pending: _FusionCaptures,
    reference_only: bool = False,
) -> dict[str, Any]:
    remaining = [key for key in runtime.layouts if key not in captures]
    retain_layouts = () if reference_only or len(remaining) > limit else None
    try:
        for key in remaining[:limit]:
            layout = runtime.layouts[key]
            requested = {key: layout}
            cache = SnapshotCalibrationCompilationCache(max_entries=256, max_bytes=32 * 1024 * 1024)
            prepared = replace(runtime.prepared, compilation_cache=cache)
            capture = None
            try:
                captured, reports, _, _ = _capture_study_references(
                    prepared, requested, runtime.required,
                    round_inputs={layout: tuple(zip(runtime.circuits, runtime.grid[layout], strict=True))},
                )
                capture = captured[key]
                if key not in reports:
                    return _incremental_result(
                        "CALIBRATION_STUDY_INCREMENTAL_REFERENCE_INCOMPLETE", tip, runtime, captures,
                        failure={"layout": key, "capture": capture},
                    )
                _verify_references(
                    runtime.constraint, prepared, runtime.required, runtime.request["inputs"], requested, captured,
                )
            except Exception as error:
                failure = _incremental_failure(error, key, capture)
                if failure is None:
                    raise
                return _incremental_result(
                    "CALIBRATION_STUDY_INCREMENTAL_REFERENCE_INCOMPLETE", tip, runtime, captures, failure=failure,
                )
            try:
                tip, captures[key], completed[key], callback = _publish_verified_generation(
                    runtime, directory, key, capture, cache, len(captures) + 1, tip,
                    retain_layouts=retain_layouts,
                )
                if callback is not None:
                    pending[key] = callback
                del callback
            except Exception as error:
                failure = _incremental_failure(error, key, capture)
                if failure is None:
                    raise
                return _incremental_result(
                    "CALIBRATION_STUDY_INCREMENTAL_PUBLICATION_FAILED", tip, runtime, captures, failure=failure,
                )
            del prepared, cache

        if len(captures) != len(runtime.layouts):
            return _incremental_result("CALIBRATION_STUDY_INCREMENTAL_PAUSED", tip, runtime, captures)
        if reference_only:
            return _incremental_result("CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED", tip, runtime, captures)
        study = _finish_prepared_study(
            runtime.constraint, runtime.prepared, runtime.options, runtime.required,
            runtime.request["inputs"]["validation_trials"], requested=runtime.layouts,
            captures=captures, completed=completed, unavailable=[], pending=pending,
            fusion_probability_tolerance=runtime.request["fusion_probability_tolerance"],
        )
        return _incremental_result("CALIBRATION_STUDY_INCREMENTAL_COMPLETED", tip, runtime, captures, study=study)
    finally:
        pending.clear()


def run_snapshot_calibration_study_incremental(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    directory: str | Path, initial_layouts: Sequence[Sequence[int]], target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    seed_transpiler: int = 7, validation_trials: int = 0, prefer_native_qasm: bool = False,
    batch_density: bool = False, fusion_probability_tolerance: float, max_new_reference_layouts: int | None = None,
    reference_only: bool = False,
) -> dict[str, Any]:
    """Start a study in a new directory and publish a generation per complete reference layout."""
    destination = Path(directory)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"incremental checkpoint directory already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"incremental checkpoint parent directory does not exist: {destination.parent}")
    if type(reference_only) is not bool:
        raise ValueError("reference_only must be a boolean")
    limit = _reference_limit(max_new_reference_layouts)
    tolerance = _validate_fusion_probability_tolerance(fusion_probability_tolerance)
    normalized = _preflight_study(
        constraint, initial_layouts=initial_layouts, target_probability=target_probability, shot_caps=shot_caps,
        query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot, seed_transpiler=seed_transpiler,
        validation_trials=validation_trials, paired_fusion=True,
    )
    runtime = _prepare_incremental_study(
        constraint, earlier, later, normalized, seed_transpiler=seed_transpiler, prefer_native_qasm=prefer_native_qasm,
        batch_density=batch_density, validation_trials=validation_trials, tolerance=tolerance,
        software_versions=_bundle_software_versions(),
    )
    _initialize_incremental_calibration_store(destination)
    return _advance_incremental_study(
        runtime, destination, limit, tip=None, captures={}, completed={}, pending={}, reference_only=reference_only,
    )


def resume_snapshot_calibration_study_incremental(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    directory: str | Path, expected_generation_sha256: str, max_new_reference_layouts: int | None = None,
    reference_only: bool = False,
) -> dict[str, Any]:
    """Verify an explicit generation and its prefix, then capture only pending layouts."""
    source = Path(directory)
    if type(reference_only) is not bool:
        raise ValueError("reference_only must be a boolean")
    limit = _reference_limit(max_new_reference_layouts)
    generations = _read_incremental_calibration_generations(source, expected_generation_sha256)
    request = generations[-1].document["request"]
    normalized, tolerance = _resume_request(constraint, request, earlier, later)
    software_versions = _bundle_software_versions()
    if any(_json_bytes(generation.bundle.software_versions) != _json_bytes(software_versions) for generation in generations):
        raise ValueError("incremental generation compilation bundle software versions differ from the current environment")
    inputs = request["inputs"]
    runtime = _prepare_incremental_study(
        constraint, earlier, later, normalized, seed_transpiler=inputs["seed_transpiler"],
        prefer_native_qasm=inputs.get("prefer_native_qasm", False), batch_density=inputs.get("batch_density", False),
        validation_trials=inputs["validation_trials"], tolerance=tolerance, software_versions=software_versions,
        recorded_request=request,
    )
    retain_layouts = () if reference_only or len(generations) + limit < len(runtime.layouts) else None
    captures, completed, pending = _restore_prefix(runtime, generations, retain_layouts=retain_layouts)
    del generations
    return _advance_incremental_study(
        runtime, source, limit, tip=expected_generation_sha256, captures=captures, completed=completed, pending=pending,
        reference_only=reference_only,
    )
