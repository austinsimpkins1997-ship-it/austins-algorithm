"""Select adaptive budgets from a saved IBM snapshot panel or profile bundle."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from itertools import accumulate, islice
from random import Random
from typing import Any

from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import _run_search_attempts, _unknown_round_choices
from .placement_statistics import _placement_exact_interval
from .unknown_count_planning import _cap_order, _policy_checkpoints, _probability_order, _selection_grid, _summary
from .unknown_count_snapshot_profile import (
    MAX_SNAPSHOT_PANEL_BYTES, MAX_VALIDATION_TRIALS, _case_profile, _digest, _object, _selection_suite,
    _source_plan_budgets, _source_trial_seeds, load_snapshot_selection_panel,
)


MAX_VALIDATION_ATTEMPTS = 100_000
_VALIDATION_SEED_START = 0x23400000
_MODEL = "SAVED_IBM_SNAPSHOT_INDEPENDENT_DRAWS"
_SHOTS = "worst_case_expected_shots"
_QUERIES = "worst_case_expected_oracle_queries"
_GATES = "worst_case_expected_compiled_two_qubit_gates"


def _distinct_classical(width: int, count: int, cap: int) -> dict[str, float]:
    size = 1 << width
    survival = 1.0
    checks = 0.0
    for tried in range(min(size, cap)):
        checks += survival
        survival *= max(0, size - count - tried) / (size - tried)
    return {"found_probability": 1.0 - survival, "expected_verifier_calls": checks}


def _model_inputs(profile: dict[str, Any], choices: int, count: int) -> tuple[list[tuple[float, float]], list[int]]:
    rounds = profile["rounds"]
    if len(rounds) < choices:
        raise ValueError(f"solution count {count} is missing required snapshot round {choices - 1}")
    return [row["probabilities"] for row in rounds[:choices]], [row["gates"] for row in rounds[:choices]]


def _validation_budgets(
    entry: dict[str, Any], recommendations: dict[str, Any], best: dict[str, Any], max_rounds: int,
) -> list[dict[str, Any]]:
    budgets: dict[tuple[str, int, int, int], list[str]] = {}
    selected = {name: row for name, row in recommendations.items() if row is not None}
    for name, row in (selected or {"best_attainable": best}).items():
        key = row["policy"], row["max_shots"], row["max_oracle_queries"], max_rounds
        budgets.setdefault(key, []).append(name)
    for name, budget in _source_plan_budgets(entry).items():
        key = tuple(budget[field] for field in ("policy", "max_shots", "max_oracle_queries", "max_rounds_per_shot"))
        budgets.setdefault(key, []).append(f"source_ideal_{name}")
    return [
        {"id": f"plan_{index}", "labels": labels, "policy": policy, "max_shots": shots,
         "max_oracle_queries": queries, "max_rounds_per_shot": rounds}
        for index, ((policy, shots, queries, rounds), labels) in enumerate(budgets.items(), start=1)
    ]


def _validation_interval(hits: int, trials: int, confidence: float, target: float) -> dict[str, Any]:
    lower, upper = _placement_exact_interval(hits, trials, confidence)
    if lower >= target:
        relation = "INTERVAL_AT_OR_ABOVE_TARGET"
    elif upper < target:
        relation = "INTERVAL_BELOW_TARGET"
    else:
        relation = "INTERVAL_OVERLAPS_TARGET"
    return {"lower": lower, "upper": upper, "target_comparison": relation}


def _validate_arm(
    profile: dict[str, Any], width: int, count: int, budget: dict[str, Any], seeds: list[int], target: float,
    confidence: float, *, witness_outcomes: list[bool] | None = None,
    verifier: Callable[[str], bool] | None = None, modeled: dict[str, float] | None = None,
) -> dict[str, Any]:
    caps = {key: budget[key] for key in ("max_shots", "max_oracle_queries", "max_rounds_per_shot")}
    shots, queries = caps["max_shots"], caps["max_oracle_queries"]
    windows = list(islice(_unknown_round_choices(width, caps["max_rounds_per_shot"]), shots))
    choices = min(max(windows), queries + 1)
    probabilities, costs = _model_inputs(profile, choices, count)
    cumulative = [list(accumulate(row["weights"])) for row in profile["rounds"][:choices]]
    totals = [row[-1] for row in cumulative]
    high = [math.nextafter(total, 0.0) for total in totals]
    policy = budget["policy"]

    def measure(rounds: int, seed: int) -> str:
        draw = min(Random(seed).random() * totals[rounds], high[rounds])
        index = bisect_right(cumulative[rounds], draw)
        return format(index, f"0{width}b")

    def verify_prefix(bits: str) -> bool:
        return int(bits, 2) < count

    verify = verify_prefix if verifier is None else verifier
    traces = [_run_search_attempts(width, verify, measure, fixed_rounds=None, seed=seed,
                                  query_budget_policy=policy, **caps) for seed in seeds]
    if witness_outcomes is not None:
        witness_outcomes.extend(trace["status"] == "FOUND" for trace in traces)
    terminal = Counter(trace["exhaustion_reason"] or "FOUND" for trace in traces)
    rounds_used = Counter(attempt["rounds"] for trace in traces for attempt in trace["attempts"])
    hits = terminal["FOUND"]
    if modeled is None:
        modeled = _policy_checkpoints(windows, queries, probabilities, policy, [shots], gate_costs=costs)[shots]
    return {
        "modeled": modeled,
        "observed": {
            "witnesses": hits, "trials": len(seeds), "found_rate": hits / len(seeds),
            "terminal_outcomes": dict(sorted(terminal.items())),
            "shots": sum(rounds_used.values()),
            "oracle_queries": sum(rounds * used for rounds, used in rounds_used.items()),
            "nominal_compiled_two_qubit_gates": sum(costs[rounds] * used for rounds, used in rounds_used.items()),
        },
        "interval": _validation_interval(hits, len(seeds), confidence, target),
        "traces_sha256": _digest(traces),
    }


def _validation(
    cases: dict[int, dict[str, Any]], get_profile: Callable[[int], dict[str, Any]],
    entry: dict[str, Any], inputs: dict[str, Any],
    recommendations: dict[str, Any], best: dict[str, Any], trials: int,
) -> dict[str, Any]:
    width, max_rounds = inputs["search_qubits"], inputs["max_rounds_per_shot"]
    budgets = _validation_budgets(entry, recommendations, best, max_rounds)
    attempts = trials * len(cases) * sum(row["max_shots"] for row in budgets)
    if attempts > MAX_VALIDATION_ATTEMPTS:
        raise ValueError(f"snapshot validation exceeds {MAX_VALIDATION_ATTEMPTS} possible attempts")
    source_seeds = set()
    case_seeds = {}
    profiles = {}
    for offset, (count, case) in enumerate(sorted(cases.items())):
        profiles[count] = get_profile(count)
        source_seeds.update(_source_trial_seeds(case))
        start = _VALIDATION_SEED_START + offset * MAX_VALIDATION_TRIALS
        case_seeds[count] = list(range(start, start + trials))
        for budget in budgets:
            windows = list(islice(_unknown_round_choices(width, budget["max_rounds_per_shot"]), budget["max_shots"]))
            _model_inputs(profiles[count], min(max(windows), budget["max_oracle_queries"] + 1), count)
    if not source_seeds.isdisjoint(seed for seeds in case_seeds.values() for seed in seeds):
        raise ValueError("validation seed blocks overlap the source panel")

    family_confidence = .95
    confidence = 1 - (1 - family_confidence) / (len(cases) * len(budgets))
    evaluated = {}
    for count, seeds in case_seeds.items():
        arms = {budget["id"]: _validate_arm(profiles[count], width, count, budget, seeds,
                                            inputs["target_probability"], confidence) for budget in budgets}
        profile = profiles[count]
        evaluated[str(count)] = {
            "role": cases[count]["role"], "seeds": seeds, "plans": arms,
            "constraint_sha256": profile["constraint_sha256"], "anchor_layout": profile["anchor_layout"],
            "round_openqasm_sha256": [row["evidence"]["openqasm_sha256"] for row in profile["rounds"]],
        }
    return {
        "method": "SEEDED_CATEGORICAL_REPLAY_OF_SAVED_SNAPSHOT_DISTRIBUTIONS",
        "confidence": {"method": "CLOPPER_PEARSON_BONFERRONI", "family": family_confidence, "per_arm": confidence},
        "trials_per_arm": trials, "budgets": budgets, "cases": evaluated,
    }


def _summarize_budget_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    feasible = [row for row in candidates if row["meets_target"]]
    objectives = {
        "fewest_expected_shots": (_SHOTS, _GATES, _QUERIES),
        "fewest_expected_oracle_queries": (_QUERIES, _GATES, _SHOTS),
        "fewest_expected_compiled_two_qubit_gates": (_GATES, _SHOTS, _QUERIES),
    }
    recommendations = {}
    for name, fields in objectives.items():
        best_for_objective = min(feasible, key=lambda row: (
            *(row[field] for field in fields), *_probability_order(row), *_cap_order(row),
        ), default=None)
        recommendations[name] = _summary(best_for_objective) if best_for_objective is not None else None
    best = _summary(min(candidates, key=lambda row: (
        *_probability_order(row), row[_GATES], row[_SHOTS], row[_QUERIES], *_cap_order(row),
    )))
    return len(feasible), recommendations, best


def select_snapshot_unknown_count_budgets(
    panel: dict[str, Any], suite: str, *, target_probability: float, shot_caps: Iterable[int],
    query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS, validation_trials: int = 0,
    expected_observations_sha256: str | None = None, expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Rank budgets from declared saved round profiles and optionally replay them on held-out cases."""
    _bounded_int(validation_trials, 0, MAX_VALIDATION_TRIALS, "validation_trials")
    claimed, bundle_digest, configuration, entry, width, counts, cases = _selection_suite(
        _object(panel, "snapshot panel or bundle"), suite, expected_observations_sha256, expected_bundle_sha256,
    )
    profiles: dict[int, dict[str, Any]] = {}
    identity = None

    def get_profile(count: int) -> dict[str, Any]:
        nonlocal identity
        if count not in profiles:
            profile = _case_profile(cases[count], width, count, configuration)
            scope = profile["snapshot"], profile["versions"]
            if identity is not None and identity != scope:
                raise ValueError("snapshot profiles must share one calibration identity and software stack")
            identity = scope
            profiles[count] = profile
        return profiles[count]

    inputs, candidates = _selection_grid(
        width, counts, target_probability=target_probability, shot_caps=shot_caps, query_caps=query_caps,
        max_rounds_per_shot=max_rounds_per_shot,
        round_model=lambda count, choices: _model_inputs(get_profile(count), choices, count),
    )
    assert identity is not None
    feasible_count, recommendations, best = _summarize_budget_candidates(candidates)
    validation = None
    if validation_trials:
        validation = _validation(cases, get_profile, entry, inputs, recommendations, best, validation_trials)
    return {
        "status": "UNKNOWN_COUNT_TARGET_MET" if feasible_count else "UNKNOWN_COUNT_TARGET_INFEASIBLE",
        "model": _MODEL, "inputs": inputs, "candidate_count": len(candidates), "feasible_count": feasible_count,
        "source": {
            "suite": suite, "observations_sha256": claimed, "snapshot": identity[0], "versions": identity[1],
            **({"bundle_sha256": bundle_digest} if bundle_digest is not None else {}),
            "seed_transpiler": configuration["seed_transpiler"],
            "declared_cases": {
                str(count): {
                    "constraint_sha256": profiles[count]["constraint_sha256"],
                    "anchor_layout": profiles[count]["anchor_layout"],
                    "rounds": [row["evidence"] for row in profiles[count]["rounds"]],
                } for count in counts
            },
        },
        "recommendations": recommendations, "best_attainable": best, "candidates": candidates,
        "classical_distinct_uniform": {
            str(shot_cap): {str(count): _distinct_classical(width, count, shot_cap) for count in counts}
            for shot_cap in inputs["shot_caps"]
        },
        "validation": validation, "provider_job_submitted": False, "quantum_hardware_used": False,
    }
