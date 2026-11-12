"""Compare verified IBM snapshot profile bundles on a common workload."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, _unknown_round_choices
from .unknown_count_planning import _selection_axis, _selection_grid
from .unknown_count_snapshot_profile import MAX_VALIDATION_TRIALS, _case_profile, _read_bundle, _source_trial_seeds, _suite_parts
from .unknown_count_snapshot_selection import (
    MAX_VALIDATION_ATTEMPTS, _GATES, _QUERIES, _SHOTS, _model_inputs, _summarize_budget_candidates, _validate_arm,
)


_COMPARISON_SEED_START = 0x23500000
_BUDGET_FIELDS = ("policy", "max_shots", "max_oracle_queries")
_DELTA_FIELDS = ("minimum_found_probability", _SHOTS, _QUERIES, _GATES)


@dataclass(frozen=True)
class _BundleSuite:
    digest: str
    source_digest: str
    configuration: dict[str, Any]
    width: int
    declared: list[int]
    cases: dict[int, dict[str, Any]]

    def evidence(self) -> dict[str, Any]:
        case = self.cases[self.declared[0]]["comparison"]
        return {
            "bundle_sha256": self.digest, "source_observations_sha256": self.source_digest,
            "snapshot": case["snapshot"], "versions": case["versions"],
            "seed_transpiler": self.configuration["seed_transpiler"],
        }


def _open_suite(bundle: dict[str, Any], suite: str, expected: str | None) -> _BundleSuite:
    digest, source, configuration, observations = _read_bundle(bundle, expected)
    if not isinstance(suite, str) or suite not in observations:
        raise ValueError("suite must be present in the snapshot profile bundle")
    _, width, declared, cases = _suite_parts(observations[suite])
    return _BundleSuite(digest, source["observations_sha256"], configuration, width, declared, cases)


def _case_coverage(case: dict[str, Any] | None, required: int) -> dict[str, Any]:
    if case is None:
        return {"status": "CASE_MISSING", "available_rounds": []}
    comparison = case["comparison"]
    available = comparison["rounds_to_prepare"]
    status = comparison["status"]
    if status == "COMPLETED" and len(available) < required:
        status = "INSUFFICIENT_ROUND_PROFILES"
    return {"status": status, "available_rounds": available}


def _coverage(left: _BundleSuite, right: _BundleSuite, required: int) -> dict[str, Any]:
    if left.width != right.width or left.declared != right.declared:
        raise ValueError("snapshot suites must have matching search width and declared solution counts")
    matched, gaps = [], {}
    for count in sorted(left.cases.keys() | right.cases.keys()):
        a, b = left.cases.get(count), right.cases.get(count)
        if a is not None and b is not None and (
            a["role"] != b["role"] or a["reference"]["constraint_sha256"] != b["reference"]["constraint_sha256"]
        ):
            raise ValueError(f"snapshot case {count} has mismatched role or constraint identity")
        coverage = {"left": _case_coverage(a, required), "right": _case_coverage(b, required)}
        if all(side["status"] == "COMPLETED" for side in coverage.values()):
            matched.append(count)
        else:
            gaps[str(count)] = {"role": (a if a is not None else b)["role"], **coverage}
    return {
        "required_rounds": list(range(required)), "matched_counts": matched,
        "matched_holdout_counts": [count for count in matched if count not in left.declared],
        "unmatched_declared_counts": [count for count in left.declared if count not in matched],
        "gaps": gaps,
    }


def _round_comparison(left: dict[str, Any], right: dict[str, Any], required: int) -> dict[str, Any]:
    rows = {}
    for rounds in range(required):
        a, b = left["rounds"][rounds], right["rounds"][rounds]
        x, y = a["weights"], b["weights"]
        mass_x, mass_y = math.fsum(x), math.fsum(y)
        rows[str(rounds)] = {
            "left": a["evidence"], "right": b["evidence"],
            "recorded_compiled_fields_match": all(
                a["evidence"][key] == b["evidence"][key]
                for key in ("openqasm_sha256", "active_target_wires", "measurement_map", "compiled_two_qubit_gates")
            ),
            "right_minus_left_hit_probability": b["probabilities"][0] - a["probabilities"][0],
            "outcome_total_variation_distance": min(
                1.0, math.fsum(abs(i / mass_x - j / mass_y) for i, j in zip(x, y, strict=True)) / 2,
            ),
        }
    return {
        "constraint_sha256": left["constraint_sha256"],
        "anchor_layout": {"left": left["anchor_layout"], "right": right["anchor_layout"]},
        "rounds": rows,
    }


def _candidate_pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    budget = {key: left[key] for key in _BUDGET_FIELDS}
    if any(right[key] != value for key, value in budget.items()):
        raise RuntimeError("snapshot comparison produced mismatched candidate grids")
    shared = {
        **budget, "meets_target": left["meets_target"] and right["meets_target"],
        "minimum_found_probability": min(left["minimum_found_probability"], right["minimum_found_probability"]),
        "maximum_failure_probability": max(left["maximum_failure_probability"], right["maximum_failure_probability"]),
        **{key: max(left[key], right[key]) for key in (_SHOTS, _QUERIES, _GATES)},
    }
    return {
        **budget, "left": left, "right": right, "shared": shared,
        "right_minus_left": {key: right[key] - left[key] for key in _DELTA_FIELDS},
    }


def _replay_budgets(
    recommendations: dict[str, Any], best: dict[str, Any], max_rounds: int,
) -> list[dict[str, Any]]:
    choices = {}
    for side, ranking in recommendations.items():
        chosen = {name: row for name, row in ranking.items() if row is not None}
        for name, row in (chosen or {"best_attainable": best[side]}).items():
            key = tuple(row[field] for field in _BUDGET_FIELDS)
            choices.setdefault(key, []).append(f"{side}.{name}")
    return [
        {"id": f"plan_{index}", "labels": labels, **dict(zip(_BUDGET_FIELDS, key, strict=True)),
         "max_rounds_per_shot": max_rounds}
        for index, (key, labels) in enumerate(choices.items(), start=1)
    ]


def _paired_validation(
    left: _BundleSuite, right: _BundleSuite, profiles: dict[str, dict[int, dict[str, Any]]],
    report: dict[str, Any], trials: int,
) -> dict[str, Any]:
    inputs, coverage = report["inputs"], report["coverage"]
    matched = coverage["matched_counts"]
    budgets = _replay_budgets(report["recommendations"], report["best_attainable"], inputs["max_rounds_per_shot"])
    if 2 * trials * len(matched) * sum(row["max_shots"] for row in budgets) > MAX_VALIDATION_ATTEMPTS:
        raise ValueError(f"snapshot comparison replay exceeds {MAX_VALIDATION_ATTEMPTS} possible attempts")
    source_seeds = {seed for side in (left, right) for case in side.cases.values() for seed in _source_trial_seeds(case)}
    seed_blocks = {}
    for offset, count in enumerate(matched):
        start = _COMPARISON_SEED_START + offset * MAX_VALIDATION_TRIALS
        seeds = list(range(start, start + trials))
        if not source_seeds.isdisjoint(seeds):
            raise ValueError("comparison replay seed blocks overlap a source snapshot suite")
        seed_blocks[count] = seeds

    family_confidence = .95
    arms = 2 * len(matched) * len(budgets)
    confidence = 1 - (1 - family_confidence) / arms
    cases = {}
    labels = {(True, True): "both_found", (True, False): "left_only_found",
              (False, True): "right_only_found", (False, False): "neither_found"}
    for count, seeds in seed_blocks.items():
        plans = {}
        for budget in budgets:
            results, outcomes = {}, {}
            for side, side_profiles in profiles.items():
                outcomes[side] = []
                results[side] = _validate_arm(
                    side_profiles[count], left.width, count, budget, seeds, inputs["target_probability"],
                    confidence, witness_outcomes=outcomes[side],
                )
            pairs = Counter(labels[pair] for pair in zip(outcomes["left"], outcomes["right"], strict=True))
            plans[budget["id"]] = {
                **results, "paired_witness_outcomes": {name: pairs[name] for name in labels.values()},
                "right_minus_left_found_rate": (pairs["right_only_found"] - pairs["left_only_found"]) / trials,
            }
        cases[str(count)] = {"role": left.cases[count]["role"], "seeds": seeds, "plans": plans}
    return {
        "status": "PARTIAL_MATCHED_COVERAGE_REPLAYED" if coverage["gaps"] else "MATCHED_CASES_REPLAYED",
        "method": "PAIRED_CATEGORICAL_REPLAY_OF_SAVED_SNAPSHOT_DISTRIBUTIONS",
        "confidence": {"method": "CLOPPER_PEARSON_BONFERRONI", "family": family_confidence, "per_arm": confidence},
        "trials_per_arm": trials, "budgets": budgets, "cases": cases,
    }


def compare_snapshot_profile_bundles(
    left: dict[str, Any], right: dict[str, Any], suite: str, *, target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    validation_trials: int = 0, expected_left_bundle_sha256: str | None = None,
    expected_right_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Use one budget grid and matched held-out cases to compare two saved models."""
    _bounded_int(validation_trials, 0, MAX_VALIDATION_TRIALS, "validation_trials")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")
    if type(target_probability) not in (int, float) or not 0 < target_probability <= 1:
        raise ValueError("target_probability must be greater than zero and at most one")
    shots = _selection_axis(shot_caps, 1, MAX_SEARCH_SHOTS, "shot_caps")
    queries = _selection_axis(query_caps, 0, MAX_ORACLE_QUERIES, "query_caps")
    a = _open_suite(left, suite, expected_left_bundle_sha256)
    b = _open_suite(right, suite, expected_right_bundle_sha256)
    required = min(max(islice(_unknown_round_choices(a.width, max_rounds_per_shot), shots[-1])), queries[-1] + 1)
    coverage = _coverage(a, b, required)
    left_evidence, right_evidence = a.evidence(), b.evidence()
    report = {
        "status": "SNAPSHOT_PROFILE_COMPARISON_UNAVAILABLE",
        "inputs": {
            "suite": suite, "search_qubits": a.width, "solution_counts": a.declared,
            "target_probability": float(target_probability), "shot_caps": shots, "query_caps": queries,
            "max_rounds_per_shot": max_rounds_per_shot,
        },
        "artifacts": {"left": left_evidence, "right": right_evidence},
        "recorded_changes": {
            "calibration_digest_changed": left_evidence["snapshot"]["configuration_properties_sha256"] !=
                                          right_evidence["snapshot"]["configuration_properties_sha256"],
            "compiler_seed_changed": left_evidence["seed_transpiler"] != right_evidence["seed_transpiler"],
            "software_versions_changed": left_evidence["versions"] != right_evidence["versions"],
        },
        "coverage": coverage,
        "round_comparisons": {}, "candidates": [], "feasibility": None,
        "recommendations": None, "best_attainable": None, "validation": None,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if coverage["unmatched_declared_counts"]:
        return report

    profiles = {
        name: {count: _case_profile(side.cases[count], a.width, count, side.configuration)
               for count in coverage["matched_counts"]}
        for name, side in (("left", a), ("right", b))
    }
    sides = {}
    for name, side_profiles in profiles.items():
        _, sides[name] = _selection_grid(
            a.width, a.declared, target_probability=target_probability, shot_caps=shots, query_caps=queries,
            max_rounds_per_shot=max_rounds_per_shot,
            round_model=lambda count, choices: _model_inputs(side_profiles[count], choices, count),
        )
    pairs = [_candidate_pair(x, y) for x, y in zip(sides["left"], sides["right"], strict=True)]
    sides["shared"] = [row["shared"] for row in pairs]
    ranked = {name: _summarize_budget_candidates(rows) for name, rows in sides.items()}
    both = ranked["shared"][0]
    only_left, only_right = ranked["left"][0] - both, ranked["right"][0] - both
    report.update(
        status="SNAPSHOT_PROFILE_BUNDLES_COMPARED",
        round_comparisons={str(count): _round_comparison(profiles["left"][count], profiles["right"][count], required)
                           for count in coverage["matched_counts"]},
        candidates=pairs,
        feasibility={"both": both, "left_only": only_left, "right_only": only_right,
                     "neither": len(pairs) - both - only_left - only_right},
        recommendations={name: row[1] for name, row in ranked.items()},
        best_attainable={name: row[2] for name, row in ranked.items()},
    )
    if validation_trials:
        report["validation"] = _paired_validation(a, b, profiles, report, validation_trials)
    return report
