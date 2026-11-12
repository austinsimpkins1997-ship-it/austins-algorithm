"""Audit paired dated calibration reports for observed fusion drift and decision parity."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from copy import deepcopy
from typing import Any

from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS
from .ibm_models import _PREPARED_DENSITY_FUSION_OPTIONS
from .snapshot_calibration_planning import (
    _LANES, _CalibrationProfile, _calibration_budget_grid, _calibration_profiles, _check_calibration_budget_work,
    _plan_calibration_profiles,
)
from .snapshot_calibration_replay import _PLAN_FIELDS
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_profile import _digest, _object


_CAPTURE_CHANGES = {"rounds", "density_fusion", "maximum_absolute_hit_probability_difference"}
_ROUND_CHANGES = {
    *_LANES, "right_minus_left_outcome_probabilities", "right_minus_left_hit_probability",
    "outcome_total_variation_distance",
}
_ROUND_EVIDENCE = ("compilation", "target_calibrations", "readout_assignment_matrices")
_ROUND_EXCLUDED = _ROUND_CHANGES.union(_ROUND_EVIDENCE)
_OBSERVED_FIELDS = {
    "enabled": bool, "applied": bool, "threshold": int, "method": str,
    "max_fused_qubits": int, "parallelization": int,
}
_MAXIMUM_FIELDS = {
    "maximum_total_variation_distance": "total_variation_distance",
    "maximum_absolute_outcome_probability_difference": "maximum_absolute_outcome_probability_difference",
    "maximum_absolute_hit_probability_difference": "absolute_hit_probability_difference",
    "maximum_absolute_miss_probability_difference": "absolute_miss_probability_difference",
}


def _batch_policy(report: dict[str, Any]) -> bool:
    if "batch_density" not in report:
        return False
    if report["batch_density"] is not True:
        raise ValueError("calibration report batch_density must be true when recorded")
    return True


def _fusion_marker(report: dict[str, Any]) -> dict[str, Any]:
    marker = _object(report.get("density_fusion"), "density fusion provenance")
    if marker.keys() != {"requested", "observed"} or _json_bytes(marker["requested"]) != _json_bytes(
        _PREPARED_DENSITY_FUSION_OPTIONS,
    ):
        raise ValueError("fused calibration report must record the supported density fusion options")
    observed = _object(marker["observed"], "observed density fusion")
    if observed.keys() != set(_LANES):
        raise ValueError("density fusion observations must identify both snapshots")
    for label in _LANES:
        rounds = _object(observed[label], f"observed density fusion for {label}")
        if rounds.keys() != report["rounds"].keys():
            raise ValueError(f"density fusion observations must cover every saved round for {label}")
        for metadata in rounds.values():
            if metadata is None:
                continue
            if (
                not isinstance(metadata, dict) or not metadata or metadata.keys() - _OBSERVED_FIELDS.keys()
                or any(type(value) is not _OBSERVED_FIELDS[key] for key, value in metadata.items())
                or any(metadata.get(key, 1) < 1 for key in ("max_fused_qubits", "parallelization"))
                or metadata.get("threshold", 0) < 0 or ("method" in metadata and not metadata["method"])
            ):
                raise ValueError(f"invalid observed density fusion metadata for {label}")
    return marker


def _unchanged(reference: dict[str, Any], fused: dict[str, Any], excluded: set[str]) -> bool:
    return _json_bytes({key: reference[key] for key in reference.keys() - excluded}) == _json_bytes(
        {key: fused[key] for key in fused.keys() - excluded},
    )


def _round_delta(reference: _CalibrationProfile, fused: _CalibrationProfile, label: str, index: int) -> dict[str, float]:
    left, right = reference.weights[label][index], fused.weights[label][index]
    left_mass, right_mass = math.fsum(left), math.fsum(right)
    bin_deltas = [b / right_mass - a / left_mass for a, b in zip(left, right, strict=True)]
    # Largest absolute event difference. Summing each side separately preserves
    # subnormal differences if normalization rounded the largest bin to one.
    increase = math.fsum(delta for delta in bin_deltas if delta > 0)
    decrease = -math.fsum(delta for delta in bin_deltas if delta < 0)
    left_hit, left_miss = reference.probabilities[label][index]
    right_hit, right_miss = fused.probabilities[label][index]
    return {
        "total_variation_distance": min(1.0, max(increase, decrease)),
        "maximum_absolute_outcome_probability_difference": max(map(abs, bin_deltas)),
        "absolute_hit_probability_difference": abs(right_hit - left_hit),
        "absolute_miss_probability_difference": abs(right_miss - left_miss),
    }


def _compare_capture_pair(
    reference: dict[str, Any], fused: dict[str, Any], left: _CalibrationProfile, right: _CalibrationProfile,
) -> dict[str, Any]:
    if reference["max_rounds"] != fused["max_rounds"]:
        raise ValueError("paired fusion reports must have the same saved round horizon")
    if not _unchanged(reference, fused, _CAPTURE_CHANGES):
        raise ValueError("paired fusion reports must have identical non-fusion capture scope")
    marker = _fusion_marker(fused)
    deltas = {}
    for index in range(len(left.programs)):
        key = str(index)
        a, b = reference["rounds"][key], fused["rounds"][key]
        for field in _ROUND_EVIDENCE:
            for row in (a, b):
                evidence = _object(row.get(field), f"round {key} {field}")
                if field != "compilation" and evidence.keys() != set(_LANES):
                    raise ValueError(f"round {key} {field} must identify both snapshots")
            if _json_bytes(a[field]) != _json_bytes(b[field]):
                raise ValueError(f"paired fusion reports must have identical round {key} {field}")
        if not _unchanged(a, b, _ROUND_EXCLUDED):
            raise ValueError(f"paired fusion reports differ in round {key} execution evidence")
        deltas[key] = {label: _round_delta(left, right, label, index) for label in _LANES}
    return {
        "reference_report_sha256": _digest(reference), "fused_report_sha256": _digest(fused),
        "density_fusion": deepcopy(marker), "rounds": deltas,
    }


def _plan_identity(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {key: row[key] for key in _PLAN_FIELDS} if row is not None else None


def _decision_changes(reference: dict[str, Any], fused: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target_changes = []
    for left, right in zip(reference["candidates"], fused["candidates"], strict=True):
        identity = _plan_identity(left)
        if identity != _plan_identity(right):
            raise RuntimeError("paired fusion budget candidates are not aligned")
        if left["target_status"] != right["target_status"]:
            target_changes.append({**identity, "reference": left["target_status"], "fused": right["target_status"]})
    selection_changes = []
    left_groups = {"shared": reference, **reference["lane_selection"]}
    right_groups = {"shared": fused, **fused["lane_selection"]}
    for label, left in left_groups.items():
        right = right_groups[label]
        left_choices = {**left["recommendations"], "best_attainable": left["best_attainable"]}
        right_choices = {**right["recommendations"], "best_attainable": right["best_attainable"]}
        for objective, left_row in left_choices.items():
            a, b = _plan_identity(left_row), _plan_identity(right_choices[objective])
            if a != b:
                selection_changes.append({"scope": label, "objective": objective, "reference": a, "fused": b})
    return target_changes, selection_changes


def _validate_fusion_probability_tolerance(value: float) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("probability_tolerance must be a finite absolute probability from zero to one")
    return float(value) or 0.0


def compare_snapshot_calibration_fusion_budgets(
    reference_reports: Sequence[dict[str, Any]], fused_reports: Sequence[dict[str, Any]], constraint: BooleanConstraint,
    *, probability_tolerance: float, target_probability: float, shot_caps: Iterable[int], query_caps: Iterable[int],
    max_rounds_per_shot: int = MAX_ITERATIONS, validation_trials: int = 0,
) -> dict[str, Any]:
    """Compare saved reference/fused outcomes and exact decisions without selecting from fused evidence."""
    tolerance = _validate_fusion_probability_tolerance(probability_tolerance)
    options, windows, required = _calibration_budget_grid(
        constraint, target_probability, shot_caps, query_caps, max_rounds_per_shot, validation_trials,
    )
    references, witnesses = _calibration_profiles(reference_reports, constraint)
    fused, _ = _calibration_profiles(fused_reports, constraint, fusion_diagnostic=True)
    reference_pairs = {profile.layout: (profile, report) for profile, report in zip(references, reference_reports, strict=True)}
    fused_pairs = {profile.layout: (profile, report) for profile, report in zip(fused, fused_reports, strict=True)}
    if reference_pairs.keys() != fused_pairs.keys():
        raise ValueError("fusion comparison requires the same ordered layouts in both report sets")
    if len({_batch_policy(report) for report in (*reference_reports, *fused_reports)}) != 1:
        raise ValueError("paired fusion reports must share the same batch_density policy")

    layouts = {}
    for layout in sorted(reference_pairs):
        left, reference = reference_pairs[layout]
        right, candidate = fused_pairs[layout]
        layouts[",".join(map(str, layout))] = _compare_capture_pair(reference, candidate, left, right)
    eligible = sum(len(profile.programs) >= required for profile in references)
    complete = eligible == len(references)
    _check_calibration_budget_work(options, windows, 2 * eligible if complete else eligible)
    reference_plan = _plan_calibration_profiles(references, constraint, len(witnesses), options, required, validation_trials)
    reference_plan["snapshots"] = deepcopy(reference_plan["snapshots"])
    reference_plan["versions"] = deepcopy(reference_plan["versions"])

    rows = [row for layout in layouts.values() for round_ in layout["rounds"].values() for row in round_.values()]
    maxima = {key: max(row[field] for row in rows) for key, field in _MAXIMUM_FIELDS.items()}
    within_tolerance = maxima["maximum_total_variation_distance"] <= tolerance
    target_changes = selection_changes = decisions_match = None
    status = "INSUFFICIENT_ROUND_PROFILES"
    if complete:
        diagnostic = _plan_calibration_profiles(fused, constraint, len(witnesses), options, required, 0)
        target_changes, selection_changes = _decision_changes(reference_plan, diagnostic)
        decisions_match = not (target_changes or selection_changes)
        if not within_tolerance:
            status = "TOLERANCE_EXCEEDED"
        elif not decisions_match:
            status = "DECISIONS_CHANGED"
        else:
            status = "MATCHED"
    return {
        "schema_version": 1, "status": "SNAPSHOT_CALIBRATION_FUSION_COMPARED", "reference_plan": reference_plan,
        "fusion_check": {
            "status": status, "probability_tolerance": tolerance, "within_probability_tolerance": within_tolerance,
            "decisions_match": decisions_match, **maxima, "candidate_target_status_changes": target_changes,
            "selection_changes": selection_changes, "layouts": layouts, "fused_plan_used_for_selection": False,
        },
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
