"""Continue immutable fusion attempts bound to one complete reference generation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constraints import BooleanConstraint
from .core import _bounded_int
from .snapshot_calibration_comparison import SnapshotCalibrationFiles
from .snapshot_calibration_compilation_bundle import _bundle_software_versions
from .snapshot_calibration_fusion import _CAPTURE_CHANGES, _compare_capture_pair, _unchanged
from .snapshot_calibration_fusion_checkpoint_store import (
    _fusion_generation_terminal, _open_calibration_fusion_context, _publish_calibration_fusion_generation,
)
from .snapshot_calibration_incremental_store import _read_incremental_calibration_generations
from .snapshot_calibration_planning import _calibration_profiles
from .snapshot_calibration_study import _capture_study_fusion_attempt, _finish_prepared_study
from .snapshot_calibration_study_captures import _STATIC_ROUND_FIELDS, _TIMEOUT_FIELDS, _read_capture
from .snapshot_calibration_study_checkpoint import _resume_request
from .snapshot_calibration_study_incremental import (
    _IncrementalStudy, _incremental_failure, _prepare_incremental_study, _restore_prefix,
)
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES

if TYPE_CHECKING:
    from .snapshot_calibration_fusion_checkpoint_store import _VerifiedFusionGeneration
    from .snapshot_calibration_study import _FusionCaptures


def _verify_fusion_generation(
    runtime: _IncrementalStudy, references: dict[str, dict[str, Any]], generation: _VerifiedFusionGeneration,
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
    document = generation.document
    index = document["ordinal"] - 1
    key = list(runtime.layouts)[index]
    layout = runtime.layouts[key]
    if _json_bytes(document["layout"]) != _json_bytes(list(layout)):
        raise ValueError("fusion generation layout differs from its canonical attempt ordinal")
    if "failure" in document:
        return key, None, document["failure"]
    sealed = document["capture"]
    reference = references[key]
    report, _ = _read_capture(
        sealed, layout, runtime.request["inputs"], runtime.required, runtime.prepared.scope["audit"], fused=True,
    )
    if report["status"] == "COMPLETED":
        left, _ = _calibration_profiles([reference], runtime.constraint)
        right, _ = _calibration_profiles([report], runtime.constraint, fusion_diagnostic=True)
        _compare_capture_pair(reference, report, left[0], right[0])
    else:
        if not _unchanged(reference, report, _CAPTURE_CHANGES | _TIMEOUT_FIELDS | {"status"}):
            raise ValueError("timed-out fusion generation differs from its reference scope")
        for index, row in report["rounds"].items():
            static = {field: reference["rounds"][index][field] for field in _STATIC_ROUND_FIELDS}
            if _json_bytes(row) != _json_bytes(static):
                raise ValueError("timed-out fusion generation differs from its reference compilation or calibration")
    return key, sealed, None


def _fusion_checkpoint_result(
    status: str, reference_tip: str, fusion_tip: str | None, runtime: _IncrementalStudy,
    captures: dict[str, dict[str, Any]], failures: dict[str, dict[str, str]], *,
    study: dict[str, Any] | None = None, failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempted = [key for key in runtime.layouts if key in captures or key in failures]
    completed = [key for key in runtime.layouts if key in captures and captures[key]["report"]["status"] == "COMPLETED"]
    return {
        "schema_version": 1, "status": status, "reference_generation_sha256": reference_tip,
        "generation_sha256": fusion_tip, "attempted_layouts": attempted, "completed_layouts": completed,
        "remaining_layouts": [key for key in runtime.layouts if key not in attempted],
        "study": study, "failure": failure, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _finish_fusion_checkpoint(
    runtime: _IncrementalStudy, reference_tip: str, fusion_tip: str, reference_captures: dict[str, dict[str, Any]],
    references: dict[str, dict[str, Any]], captures: dict[str, dict[str, Any]], failures: dict[str, dict[str, str]], *,
    terminal: bool,
) -> dict[str, Any]:
    study = _finish_prepared_study(
        runtime.constraint, runtime.prepared, runtime.options, runtime.required,
        runtime.request["inputs"]["validation_trials"], requested=runtime.layouts,
        captures=reference_captures, completed=references, unavailable=[], pending={},
        fusion_probability_tolerance=runtime.request["fusion_probability_tolerance"], fusion_attempts=(captures, failures),
    )
    status = "CALIBRATION_STUDY_FUSION_CHECKPOINT_TERMINAL" if terminal else "CALIBRATION_STUDY_FUSION_CHECKPOINT_COMPLETED"
    return _fusion_checkpoint_result(status, reference_tip, fusion_tip, runtime, captures, failures, study=study)


def _restore_reference_tip(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles,
    directory: Path, reference_tip: str,
    *, retain_fusion_slice: slice | None = None,
) -> tuple[_IncrementalStudy, dict[str, dict[str, Any]], dict[str, dict[str, Any]], _FusionCaptures]:
    generations = _read_incremental_calibration_generations(directory, reference_tip)
    request = generations[-1].document["request"]
    normalized, tolerance = _resume_request(constraint, request, earlier, later)
    if len(generations) != len(normalized[0]):
        raise ValueError("fusion continuation requires a complete reference generation chain")
    versions = _bundle_software_versions()
    if any(_json_bytes(generation.bundle.software_versions) != _json_bytes(versions) for generation in generations):
        raise ValueError("fusion continuation reference software versions differ from the current environment")
    inputs = request["inputs"]
    runtime = _prepare_incremental_study(
        constraint, earlier, later, normalized, seed_transpiler=inputs["seed_transpiler"],
        prefer_native_qasm=inputs.get("prefer_native_qasm", False), batch_density=inputs.get("batch_density", False),
        validation_trials=inputs["validation_trials"], tolerance=tolerance, software_versions=versions,
        recorded_request=request,
    )
    retain_layouts = None if retain_fusion_slice is None else tuple(runtime.layouts)[retain_fusion_slice]
    reference_captures, references, pending = _restore_prefix(runtime, generations, retain_layouts=retain_layouts)
    return runtime, reference_captures, references, pending


def continue_snapshot_calibration_study_fusion_checkpoint(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    directory: str | Path, expected_reference_generation_sha256: str,
    expected_fusion_generation_sha256: str | None = None, max_new_fusion_layouts: int | None = None,
) -> dict[str, Any]:
    """Verify pinned progress and run only the remaining canonical fusion attempts."""
    limit = MAX_LAYOUT_PROFILES if max_new_fusion_layouts is None else _bounded_int(
        max_new_fusion_layouts, 1, MAX_LAYOUT_PROFILES, "max_new_fusion_layouts",
    )
    root = Path(directory)
    context = _open_calibration_fusion_context(
        root, expected_reference_generation_sha256, expected_fusion_generation_sha256,
    )
    history = context.history
    if any(generation.document["reference_generation_sha256"] != expected_reference_generation_sha256 for generation in history):
        raise ValueError("fusion generation history belongs to a different pinned reference chain")
    terminal = bool(history) and _fusion_generation_terminal(history[-1].document)
    retain_fusion_slice = slice(0, 0) if terminal else slice(len(history), len(history) + limit)
    runtime, reference_captures, references, pending = _restore_reference_tip(
        constraint, earlier, later, root, expected_reference_generation_sha256,
        retain_fusion_slice=retain_fusion_slice,
    )
    captures, failures = {}, {}
    tip = expected_fusion_generation_sha256
    terminal = False
    try:
        for generation in history:
            key, capture, failure = _verify_fusion_generation(runtime, references, generation)
            if capture is not None:
                captures[key] = capture
            else:
                failures[key] = failure
            pending.pop(key, None)
            terminal = _fusion_generation_terminal(generation.document)
        del history
        if terminal:
            pending.clear()
            return _finish_fusion_checkpoint(
                runtime, expected_reference_generation_sha256, tip, reference_captures, references,
                captures, failures, terminal=True,
            )

        for key in list(pending)[:limit]:
            capture, failure = _capture_study_fusion_attempt(pending.pop(key), runtime.layouts[key], runtime.required)
            attempt = {"capture": capture} if capture is not None else {"failure": failure}

            def verify(generation: _VerifiedFusionGeneration) -> None:
                _verify_fusion_generation(runtime, references, generation)

            try:
                published = _publish_calibration_fusion_generation(
                    root, reference_generation_sha256=expected_reference_generation_sha256,
                    previous_generation_sha256=tip, ordinal=len(captures) + len(failures) + 1,
                    layout=runtime.layouts[key], verify_generation=verify, _context=context, **attempt,
                )
            except Exception as error:
                publication_failure = _incremental_failure(error, key, None)
                if publication_failure is None:
                    raise
                publication_failure["attempt"] = attempt
                return _fusion_checkpoint_result(
                    "CALIBRATION_STUDY_FUSION_CHECKPOINT_PUBLICATION_FAILED", expected_reference_generation_sha256,
                    tip, runtime, captures, failures, failure=publication_failure,
                )
            tip = published["generation_sha256"]
            if capture is not None:
                captures[key] = capture
            else:
                failures[key] = failure
            terminal = _fusion_generation_terminal(attempt)
            if terminal:
                break

        attempted = len(captures) + len(failures)
        if not terminal and attempted < len(runtime.layouts):
            return _fusion_checkpoint_result(
                "CALIBRATION_STUDY_FUSION_CHECKPOINT_PAUSED", expected_reference_generation_sha256,
                tip, runtime, captures, failures,
            )
        pending.clear()
        return _finish_fusion_checkpoint(
            runtime, expected_reference_generation_sha256, tip, reference_captures, references,
            captures, failures, terminal=terminal,
        )
    finally:
        pending.clear()
