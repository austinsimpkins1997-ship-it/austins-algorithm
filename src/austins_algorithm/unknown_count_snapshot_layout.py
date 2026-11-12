"""Rank fixed snapshot layouts and adaptive search budgets together."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import islice
from typing import Any

from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, QUERY_BUDGET_POLICIES, _unknown_round_choices
from .unknown_count_planning import (
    MAX_SELECTION_PROFILES, MAX_SELECTION_TRANSITIONS, _selection_axis, _selection_grid, _selection_transition_bound,
)
from .unknown_count_snapshot_profile import MAX_VALIDATION_TRIALS, _case_profile, _source_trial_seeds
from .unknown_count_snapshot_profile_comparison import _BUDGET_FIELDS, _BundleSuite, _case_coverage, _open_suite
from .unknown_count_snapshot_selection import (
    MAX_VALIDATION_ATTEMPTS, _model_inputs, _summarize_budget_candidates, _validate_arm,
)
from .unknown_count_snapshot_sensitivity import (
    _MAX_TOLERANCE_PROBES, _MAX_TOLERANCE_WIDTH, _classification_counts, _decision_key, _fixed_plan_tolerance,
    _probability_radius, _probability_radius_axis, _round_probability_endpoint, _sensitive_candidates,
    _sensitivity_summary, _tolerance_endpoint, _update_tolerance_bounds,
)


MAX_LAYOUT_PROFILES = 8
_LAYOUT_VALIDATION_SEED_START = 0x23700000


def _ordered_anchor(suite: _BundleSuite) -> tuple[int, ...]:
    anchors = {
        tuple(case["comparison"]["anchor_layout"]) for case in suite.cases.values()
        if case["comparison"]["status"] == "COMPLETED"
    }
    if len(anchors) != 1:
        raise ValueError("each bundle must record one fixed ordered layout across its completed cases")
    return next(iter(anchors))


def _aligned_cases(suites: list[_BundleSuite]) -> tuple[dict[int, str], dict[str, Any]]:
    reference = suites[0]
    shared = reference.evidence()
    scope_fields = ("snapshot", "versions", "seed_transpiler")
    cases: dict[int, tuple[str, str]] = {}
    for suite in suites:
        evidence = suite.evidence()
        if any(evidence[key] != shared[key] for key in scope_fields):
            raise ValueError("layout bundles must share one snapshot calibration, software stack, and compiler seed")
        if suite.width != reference.width or suite.declared != reference.declared:
            raise ValueError("layout bundles must share search width and declared solution counts")
        for count, case in suite.cases.items():
            identity = case["role"], case["reference"]["constraint_sha256"]
            if count in cases and cases[count] != identity:
                raise ValueError(f"layout case {count} has a mismatched role or constraint identity")
            cases[count] = identity
    return {count: cases[count][0] for count in sorted(cases)}, {key: shared[key] for key in scope_fields}


def _layout_coverage(suite: _BundleSuite, roles: dict[int, str], required: int) -> dict[str, Any]:
    matched, gaps = [], {}
    for count, role in roles.items():
        coverage = _case_coverage(suite.cases.get(count), required)
        if coverage["status"] == "COMPLETED":
            matched.append(count)
        else:
            gaps[str(count)] = {"role": role, **coverage}
    return {
        "matched_counts": matched, "unmatched_declared_counts": [count for count in suite.declared if count not in matched],
        "gaps": gaps,
    }


def _check_grid_work(
    windows: list[int], shots: list[int], queries: list[int], model_count: int, endpoint_passes: int, *,
    fixed_tolerances: bool,
) -> None:
    maximum_queries = sum(choices - 1 for choices in windows)
    effective_queries = {min(cap, maximum_queries) for cap in queries}
    policy_count = len(QUERY_BUDGET_POLICIES)
    transitions = endpoint_passes * sum(
        _selection_transition_bound(windows, cap) * (1 if cap == maximum_queries else policy_count)
        for cap in effective_queries
    )
    if fixed_tolerances:
        transitions += _MAX_TOLERANCE_PROBES * policy_count * sum(
            _selection_transition_bound(windows[:shot_cap], cap) for shot_cap in shots for cap in queries
        )
    if model_count * transitions > MAX_SELECTION_TRANSITIONS:
        raise ValueError(f"layout grid exceeds {MAX_SELECTION_TRANSITIONS} modeled transition updates")


def _frozen_budgets(report: dict[str, Any]) -> list[dict[str, Any]]:
    selected = {name: row for name, row in report["recommendations"].items() if row is not None}
    arms: dict[tuple[str, int, int], dict[str, str]] = {}
    for name, row in (selected or {"best_attainable": report["best_attainable"]}).items():
        key = tuple(row[field] for field in _BUDGET_FIELDS)
        arms.setdefault(key, {})[name] = row["layout"]
    return [
        {"id": f"plan_{index}", **dict(zip(_BUDGET_FIELDS, key, strict=True)),
         "max_rounds_per_shot": report["inputs"]["max_rounds_per_shot"], "selected_layouts": choices}
        for index, (key, choices) in enumerate(arms.items(), start=1)
    ]


def _replay(
    report: dict[str, Any], eligible: dict[str, _BundleSuite], suites: list[_BundleSuite],
    roles: dict[int, str], trials: int,
) -> dict[str, Any]:
    counts = report["coverage"]["matched_counts"]
    budgets = _frozen_budgets(report)
    work = trials * len(eligible) * len(counts) * sum(row["max_shots"] for row in budgets)
    if work > MAX_VALIDATION_ATTEMPTS:
        raise ValueError(f"layout validation exceeds {MAX_VALIDATION_ATTEMPTS} possible attempts")
    source_seeds = {seed for suite in suites for case in suite.cases.values() for seed in _source_trial_seeds(case)}
    blocks = {}
    for offset, count in enumerate(counts):
        start = _LAYOUT_VALIDATION_SEED_START + offset * MAX_VALIDATION_TRIALS
        seeds = list(range(start, start + trials))
        if not source_seeds.isdisjoint(seeds):
            raise ValueError("layout validation seed blocks overlap a source snapshot suite")
        blocks[count] = seeds

    family_confidence = .95
    arms = len(eligible) * len(counts) * len(budgets)
    confidence = 1 - (1 - family_confidence) / arms
    target = report["inputs"]["target_probability"]
    cases = {}
    for count, seeds in blocks.items():
        profiles = {key: _case_profile(suite.cases[count], suite.width, count, suite.configuration)
                    for key, suite in eligible.items()}
        cases[str(count)] = {
            "role": roles[count], "seeds": seeds,
            "constraint_sha256": next(iter(profiles.values()))["constraint_sha256"],
            "round_openqasm_sha256": {
                key: [row["evidence"]["openqasm_sha256"] for row in profile["rounds"]]
                for key, profile in profiles.items()
            },
            "plans": {
                budget["id"]: {
                    key: _validate_arm(profile, eligible[key].width, count, budget, seeds, target, confidence)
                    for key, profile in profiles.items()
                }
                for budget in budgets
            },
        }
    return {
        "status": "PARTIAL_MATCHED_COVERAGE_REPLAYED" if report["coverage"]["omitted_holdout_counts"]
                  else "MATCHED_CASES_REPLAYED",
        "method": "MATCHED_CATEGORICAL_REPLAY_OF_FIXED_LAYOUT_PROFILES",
        "confidence": {"method": "CLOPPER_PEARSON_BONFERRONI", "family": family_confidence, "per_arm": confidence},
        "trials_per_arm": trials, "budgets": budgets, "cases": cases,
    }


def _layout_grid(
    suite: _BundleSuite, layout: str, round_models: dict[int, tuple[list[tuple[float, float]], list[int]]],
    inputs: dict[str, Any], *,
    radius: float = 0.0, pessimistic: bool = True,
) -> list[dict[str, Any]]:
    def round_model(count: int, choices: int) -> tuple[list[tuple[float, float]], list[int]]:
        probabilities, gate_costs = round_models[count]
        probabilities, gate_costs = probabilities[:choices], gate_costs[:choices]
        if radius and 0 < count < (1 << suite.width):
            probabilities = _round_probability_endpoint(probabilities, radius, pessimistic=pessimistic)
        return probabilities, gate_costs

    _, rows = _selection_grid(
        suite.width, suite.declared, target_probability=inputs["target_probability"],
        shot_caps=inputs["shot_caps"], query_caps=inputs["query_caps"],
        max_rounds_per_shot=inputs["max_rounds_per_shot"], round_model=round_model,
    )
    for row in rows:
        row["layout"] = layout
    return rows


def _rank_layouts(
    eligible: dict[str, _BundleSuite], round_models: dict[str, dict[int, tuple[list[tuple[float, float]], list[int]]]],
    inputs: dict[str, Any], nominal: dict[str, list[dict[str, Any]]], radius: float | None,
) -> dict[str, list[dict[str, Any]]]:
    if radius is None:
        return nominal
    ranked = {}
    for key, item in eligible.items():
        baseline = nominal[key]
        lower = upper = baseline
        if radius:
            lower = _layout_grid(item, key, round_models[key], inputs, radius=radius)
            upper = _layout_grid(item, key, round_models[key], inputs, radius=radius, pessimistic=False)
        ranked[key] = _sensitive_candidates(baseline, lower, upper)
    return ranked


def plan_snapshot_layout_budgets(
    bundles: Sequence[dict[str, Any]], suite: str, *, target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    validation_trials: int = 0, expected_bundle_sha256: Sequence[str | None] | None = None,
    probability_radius: float | None = None, probability_radii: Iterable[float] | None = None,
) -> dict[str, Any]:
    """Rank layout and budget pairs with optional per-round bounds and a fixed-plan tolerance sweep."""
    if not isinstance(bundles, Sequence) or not 2 <= len(bundles) <= MAX_LAYOUT_PROFILES:
        raise ValueError(f"layout planning requires 2 to {MAX_LAYOUT_PROFILES} snapshot profile bundles")
    if expected_bundle_sha256 is None:
        pins = [None] * len(bundles)
    elif not isinstance(expected_bundle_sha256, Sequence) or isinstance(expected_bundle_sha256, (str, bytes)) or (
        len(expected_bundle_sha256) != len(bundles)
    ):
        raise ValueError("expected bundle digests must align with the supplied layout bundles")
    else:
        pins = expected_bundle_sha256
    _bounded_int(validation_trials, 0, MAX_VALIDATION_TRIALS, "validation_trials")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")
    radii = None
    if probability_radii is not None:
        if probability_radius is not None:
            raise ValueError("probability_radius and probability_radii are mutually exclusive")
        radii = _probability_radius_axis(probability_radii)
        probability_radius = radii[-1]
    elif probability_radius is not None:
        probability_radius = _probability_radius(probability_radius)
    if type(target_probability) not in (int, float) or not 0 < target_probability <= 1:
        raise ValueError("target_probability must be greater than zero and at most one")
    shots = _selection_axis(shot_caps, 1, MAX_SEARCH_SHOTS, "shot_caps")
    queries = _selection_axis(query_caps, 0, MAX_ORACLE_QUERIES, "query_caps")

    suites = [_open_suite(bundle, suite, pin) for bundle, pin in zip(bundles, pins, strict=True)]
    reference = suites[0]
    roles, scope = _aligned_cases(suites)
    anchors = {_ordered_anchor(item): item for item in suites}
    if len(anchors) != len(suites):
        raise ValueError("layout bundles must have distinct ordered layouts")
    policies = len(QUERY_BUDGET_POLICIES)
    profiles = len(suites) * len(reference.declared) * len(shots) * len(queries) * policies
    if profiles > MAX_SELECTION_PROFILES:
        raise ValueError(f"layout grid exceeds {MAX_SELECTION_PROFILES} candidate/hypothesis profiles")
    windows = list(islice(_unknown_round_choices(reference.width, max_rounds_per_shot), shots[-1]))
    required = min(max(windows), queries[-1] + 1)
    layouts, eligible = {}, {}
    for anchor, item in sorted(anchors.items()):
        key = ",".join(map(str, anchor))
        coverage = _layout_coverage(item, roles, required)
        layouts[key] = {
            "initial_layout": list(anchor), "bundle_sha256": item.digest,
            "source_observations_sha256": item.source_digest, "coverage": coverage,
            "status": "UNAVAILABLE" if coverage["unmatched_declared_counts"] else "ELIGIBLE",
            "candidate_count": 0, "feasible_count": 0, "recommendations": None, "best_attainable": None,
        }
        if not coverage["unmatched_declared_counts"]:
            eligible[key] = item

    matched = [
        count for count in roles if eligible and all(count in layouts[key]["coverage"]["matched_counts"] for key in eligible)
    ]
    holdouts = [count for count, role in roles.items() if role == "HOLDOUT"]
    report = {
        "status": "SNAPSHOT_LAYOUT_PLANNING_UNAVAILABLE",
        "inputs": {
            "suite": suite, "search_qubits": reference.width, "solution_counts": reference.declared,
            "target_probability": float(target_probability), "shot_caps": shots, "query_caps": queries,
            "max_rounds_per_shot": max_rounds_per_shot,
        },
        **scope, "layouts": layouts,
        "coverage": {"required_rounds": list(range(required)), "matched_counts": matched,
                     "matched_holdout_counts": [count for count in holdouts if count in matched],
                     "omitted_holdout_counts": [count for count in holdouts if count not in matched]},
        "candidate_count": 0, "feasible_count": 0, "recommendations": None, "best_attainable": None,
        "candidates": [], "validation": None, "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    grid = None
    if radii is not None:
        grid = {
            "status": "EVALUATED" if eligible else "UNAVAILABLE", "radii": radii,
            "maximum_bracket_width": _MAX_TOLERANCE_WIDTH, "results": [], "fixed_plan_tolerances": [],
        }
    if not eligible:
        if probability_radius is not None:
            report["sensitivity"] = _sensitivity_summary(probability_radius, [], [], None)
        if grid is not None:
            report["sensitivity_grid"] = grid
        return report

    passes = radii if radii is not None else [probability_radius]
    _check_grid_work(
        windows, shots, queries, len(eligible) * len(reference.declared), 1 + 2 * sum(bool(radius) for radius in passes),
        fixed_tolerances=grid is not None,
    )
    round_models, declared_rounds = {}, {}
    for key, item in eligible.items():
        cases = {count: _case_profile(item.cases[count], item.width, count, item.configuration) for count in item.declared}
        round_models[key] = {count: _model_inputs(profile, required, count) for count, profile in cases.items()}
        declared_rounds[key] = {
            str(count): [row["evidence"] for row in profile["rounds"][:required]] for count, profile in cases.items()
        }
    nominal = {key: _layout_grid(item, key, round_models[key], report["inputs"]) for key, item in eligible.items()}
    nominal_rows = [row for local in nominal.values() for row in local]
    bounds = {}
    if grid is not None:
        for row in nominal_rows:
            point = _tolerance_endpoint(row, 0.0)
            bounds[_decision_key(row)] = (point, None) if point["meets_target"] else (None, point)
    for radius in passes:
        ranked = _rank_layouts(eligible, round_models, report["inputs"], nominal, radius)
        rows = [row for local in ranked.values() for row in local]
        if grid is not None:
            _update_tolerance_bounds(bounds, rows, radius)
            _, recommendations, best = _summarize_budget_candidates(rows)
            grid["results"].append({
                "probability_radius": radius, "classification_counts": _classification_counts(rows),
                "recommendations": recommendations, "best_attainable": best,
            })
    for key, local in ranked.items():
        feasible, recommendations, best = _summarize_budget_candidates(local)
        if probability_radius is not None:
            layouts[key]["sensitivity"] = _sensitivity_summary(probability_radius, nominal[key], local, recommendations)
        layouts[key].update(
            candidate_count=len(local), feasible_count=feasible, recommendations=recommendations, best_attainable=best,
            declared_rounds=declared_rounds[key],
        )
    feasible, recommendations, best = _summarize_budget_candidates(rows)
    report.update(status="SNAPSHOT_LAYOUT_BUDGETS_PLANNED", candidate_count=len(rows), feasible_count=feasible,
                  recommendations=recommendations, best_attainable=best, candidates=rows)
    if probability_radius is not None:
        report["sensitivity"] = _sensitivity_summary(probability_radius, nominal_rows, rows, recommendations)
    if validation_trials:
        report["validation"] = _replay(report, eligible, suites, roles, validation_trials)
        if probability_radius is not None:
            report["validation"]["probability_basis"] = "NOMINAL_SAVED_DISTRIBUTIONS"
    if grid is not None:
        grid["fixed_plan_tolerances"] = [
            _fixed_plan_tolerance(key, lower, upper, windows, round_models[key[0]],
                                  1 << reference.width, target_probability)
            for key, (lower, upper) in bounds.items()
        ]
        report["sensitivity_grid"] = grid
        if validation_trials:
            report["validation"]["probability_radius"] = probability_radius
    return report
