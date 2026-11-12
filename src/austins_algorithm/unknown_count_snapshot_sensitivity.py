"""Endpoint models for bounded per-round witness-probability sensitivity."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from itertools import islice
from typing import Any

from .unknown_count_planning import _candidate_report, _policy_checkpoints

from .unknown_count_snapshot_selection import _GATES, _QUERIES, _SHOTS, _summarize_budget_candidates


MAX_PROBABILITY_RADII = 16
_TOLERANCE_STEPS = 30
_MAX_TOLERANCE_WIDTH = 2.0 ** -_TOLERANCE_STEPS
# Allow one extra midpoint for rounded endpoints and one domain-limit probe.
_MAX_TOLERANCE_PROBES = _TOLERANCE_STEPS + 2
_ENDPOINT_FIELDS = ("minimum_found_probability", "maximum_failure_probability", _SHOTS, _QUERIES, _GATES, "meets_target")
_DECISION_FIELDS = ("layout", "policy", "max_shots", "max_oracle_queries")
_CLASSIFICATIONS = ("ROBUST_FEASIBLE", "TARGET_POSSIBLE", "TARGET_UNREACHABLE_WITHIN_RADIUS")


def _probability_radius(value: float) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("probability_radius must be a finite absolute probability from zero to one")
    return float(value)


def _probability_radius_axis(values: Iterable[float]) -> list[float]:
    try:
        entries = list(islice(values, MAX_PROBABILITY_RADII + 1))
    except TypeError as exc:
        raise ValueError("probability_radii must be an iterable of absolute probabilities") from exc
    if not 1 <= len(entries) <= MAX_PROBABILITY_RADII:
        raise ValueError(f"probability_radii requires 1 to {MAX_PROBABILITY_RADII} supplied values")
    return sorted({_probability_radius(entry) or 0.0 for entry in entries})


def _round_probability_endpoint(
    probabilities: list[tuple[float, float]], radius: float, *, pessimistic: bool,
) -> list[tuple[float, float]]:
    """Move the hit event by at most radius, preserving small failure tails."""
    endpoints = []
    for hit, miss in probabilities:
        if pessimistic:
            hit, miss = max(0.0, hit - radius), min(1.0, miss + radius)
        else:
            hit, miss = min(1.0, hit + radius), max(0.0, miss - radius)
        # Use the smaller endpoint directly. Its complement can round near
        # zero or one, but the recurrence still receives the small tail.
        if hit <= miss:
            endpoints.append((hit, 1.0 - hit))
        else:
            endpoints.append((1.0 - miss, miss))
    return endpoints


def _sensitive_candidates(
    nominal: list[dict[str, Any]], pessimistic: list[dict[str, Any]], optimistic: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for base, lower, upper in zip(nominal, pessimistic, optimistic, strict=True):
        if lower["meets_target"]:
            classification = "ROBUST_FEASIBLE"
        elif upper["meets_target"]:
            classification = "TARGET_POSSIBLE"
        else:
            classification = "TARGET_UNREACHABLE_WITHIN_RADIUS"
        rows.append({
            **lower,
            "sensitivity": {
                "classification": classification,
                "nominal": {key: base[key] for key in _ENDPOINT_FIELDS},
                "optimistic": {key: upper[key] for key in _ENDPOINT_FIELDS},
                "hypotheses": [
                    {"solution_count": a["solution_count"], "lower_found_probability": b["found_probability"],
                     "nominal_found_probability": a["found_probability"], "upper_found_probability": c["found_probability"]}
                    for a, b, c in zip(base["hypotheses"], lower["hypotheses"], upper["hypotheses"], strict=True)
                ],
            },
        })
    return rows


def _decision_key(row: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if row is None:
        return None
    return tuple(row[key] for key in _DECISION_FIELDS)


def _classification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["sensitivity"]["classification"] for row in rows)
    return {key: counts[key] for key in _CLASSIFICATIONS}


def _sensitivity_summary(
    radius: float, nominal: list[dict[str, Any]], active: list[dict[str, Any]],
    recommendations: dict[str, Any] | None,
) -> dict[str, Any]:
    counts = _classification_counts(active)
    nominal_feasible, nominal_recommendations, nominal_best = (
        _summarize_budget_candidates(nominal) if nominal else (0, None, None)
    )
    changes = None
    if recommendations is not None and nominal_recommendations is not None:
        changes = {
            key: _decision_key(row) != _decision_key(nominal_recommendations[key])
            for key, row in recommendations.items()
        }
    return {
        "status": "EVALUATED" if nominal else "UNAVAILABLE",
        "model": "INDEPENDENT_ABSOLUTE_ROUND_HIT_PROBABILITY_BOUNDS",
        "probability_radius": radius,
        "selection_basis": "PESSIMISTIC_HIT_AND_EXPECTED_WORK_ENDPOINT",
        "nominal_feasible_count": nominal_feasible,
        "robust_feasible_count": counts["ROBUST_FEASIBLE"],
        "possible_feasible_count": counts["ROBUST_FEASIBLE"] + counts["TARGET_POSSIBLE"],
        "classification_counts": counts,
        "nominal_recommendations": nominal_recommendations,
        "nominal_best_attainable": nominal_best,
        "recommendation_changed": changes,
    }


def _tolerance_endpoint(row: dict[str, Any], radius: float) -> dict[str, Any]:
    return {
        "probability_radius": radius,
        "minimum_found_probability": row["minimum_found_probability"],
        "maximum_failure_probability": row["maximum_failure_probability"],
        "meets_target": row["meets_target"],
    }


def _update_tolerance_bounds(
    bounds: dict[tuple[Any, ...], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    rows: list[dict[str, Any]], radius: float,
) -> None:
    for row in rows:
        key = _decision_key(row)
        lower, upper = bounds[key]
        if lower is not None and upper is None:
            point = _tolerance_endpoint(row, radius)
            bounds[key] = (point, None) if point["meets_target"] else (lower, point)


def _fixed_plan_endpoint(
    key: tuple[Any, ...], windows: list[int], round_models: dict[int, tuple[list[tuple[float, float]], list[int]]],
    size: int, target: float, radius: float,
) -> dict[str, Any]:
    _, policy, shots, queries = key
    schedule = windows[:shots]
    effective_queries = min(queries, sum(choices - 1 for choices in schedule))
    choices = min(schedule[-1], effective_queries + 1)
    hypotheses = []
    certain_first_hit = True
    for count, (probabilities, _) in round_models.items():
        probabilities = probabilities[:choices]
        if 0 < count < size:
            probabilities = _round_probability_endpoint(probabilities, radius, pessimistic=True)
        certain_first_hit &= probabilities[0][1] == 0.0
        metrics = _policy_checkpoints(schedule, effective_queries, probabilities, policy, [shots])[shots]
        hypotheses.append(metrics)
    row = _candidate_report(shots, queries, policy, hypotheses, target, certain_first_hit)
    return _tolerance_endpoint(row, radius)


def _fixed_plan_tolerance(
    key: tuple[Any, ...], lower: dict[str, Any] | None, upper: dict[str, Any] | None, windows: list[int],
    round_models: dict[int, tuple[list[tuple[float, float]], list[int]]], size: int, target: float,
) -> dict[str, Any]:
    identity = dict(zip(_DECISION_FIELDS, key, strict=True))
    if lower is None:
        return {**identity, "status": "NOMINAL_TARGET_MISSED", "lower": None, "upper": upper}
    if upper is None:
        limit = _fixed_plan_endpoint(key, windows, round_models, size, target, 1.0)
        if limit["meets_target"]:
            return {**identity, "status": "FULL_RADIUS_DOMAIN", "lower": limit, "upper": None}
        upper = limit
    for _ in range(_MAX_TOLERANCE_PROBES - 1):
        if upper["probability_radius"] - lower["probability_radius"] <= _MAX_TOLERANCE_WIDTH:
            break
        midpoint = (lower["probability_radius"] + upper["probability_radius"]) / 2
        point = _fixed_plan_endpoint(key, windows, round_models, size, target, midpoint)
        if point["meets_target"]:
            lower = point
        else:
            upper = point
    if upper["probability_radius"] - lower["probability_radius"] > _MAX_TOLERANCE_WIDTH:
        raise RuntimeError("fixed-plan tolerance did not converge within its transition budget")
    return {**identity, "status": "BRACKETED", "lower": lower, "upper": upper}
