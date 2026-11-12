"""Forecast bounded unknown-count Grover search under ideal independent draws."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from itertools import accumulate, islice
from typing import Any

from .core import MAX_ITERATIONS, AustinAlgorithmError, _bounded_int, theoretical_success_probability
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, MAX_SEARCH_WIDTH, QUERY_BUDGET_POLICIES, _unknown_round_choices


MAX_SELECTION_PROFILES = 8192
MAX_SELECTION_TRANSITIONS = 250_000_000
_IDEAL_MODEL = "IDEAL_INDEPENDENT_GROVER_DRAWS"


def _round_probabilities(search_qubits: int, solution_count: int, choices: int) -> list[tuple[float, float]]:
    size = 1 << search_qubits
    probabilities = [(solution_count / size, (size - solution_count) / size)]
    for rounds in range(1, choices):
        hit = theoretical_success_probability(search_qubits, solution_count, rounds)
        # With an odd-angle Grover rotation, exchanging marked and unmarked
        # states gives the miss probability without subtracting a near-one hit.
        miss = theoretical_success_probability(search_qubits, size - solution_count, rounds)
        total = hit + miss
        probabilities.append((hit / total, miss / total))
    return probabilities


def _policy_checkpoints(
    windows: list[int], query_cap: int, probabilities: list[tuple[float, float]], policy: str,
    shot_caps: list[int], *, gate_costs: list[int] | None = None,
) -> dict[int, dict[str, float]]:
    """Propagate surviving probability mass by completed logical queries."""
    reports = {}
    checkpoints = set(shot_caps)
    hit_prefix = list(accumulate((hit for hit, _ in probabilities), initial=0.0))
    misses = [miss for _, miss in probabilities]
    gate_prefix = None if gate_costs is None else list(accumulate(gate_costs, initial=0))
    gate_work = 0.0
    active = [1.0]
    found, query_exhausted, shots, queries, adjustments = 0.0, 0.0, 0.0, 0.0, 0.0
    affordable = policy == "affordable"
    for attempt, choices in enumerate(windows, start=1):
        # States above this spend cannot be reached after the current shot.
        continued = [0.0] * min(query_cap + 1, len(active) + choices - 1)
        for spent, mass in enumerate(active):
            if not mass:
                continue
            feasible = min(choices, query_cap - spent + 1)
            overflow = mass * (choices - feasible) / choices
            if affordable:
                # Each affordable round gets its original mass plus an equal
                # share of the unaffordable draws that are replaced.
                weight = mass / feasible
                adjustments += overflow
            else:
                weight = mass / choices
                query_exhausted += overflow

            found += weight * hit_prefix[feasible]
            shots += weight * feasible
            queries += weight * (feasible * (feasible - 1) // 2)
            if gate_prefix is not None:
                gate_work += weight * gate_prefix[feasible]
            for rounds in range(feasible):
                continued[spent + rounds] += weight * misses[rounds]
        active = continued
        if attempt in checkpoints:
            reports[attempt] = _policy_metrics(
                attempt, query_cap, active, found, query_exhausted, shots, queries, adjustments,
                None if gate_prefix is None else gate_work,
            )
        if not any(active):
            for cap in shot_caps:
                if cap > attempt:
                    reports[cap] = _policy_metrics(
                        cap, query_cap, active, found, query_exhausted, shots, queries, adjustments,
                        None if gate_prefix is None else gate_work,
                    )
            break
    return reports


def _policy_metrics(
    shot_cap: int, query_cap: int, active: list[float], found: float,
    query_exhausted: float, shots: float, queries: float, adjustments: float,
    gate_work: float | None,
) -> dict[str, float]:
    shot_exhausted = math.fsum(active)
    total = math.fsum((found, shot_exhausted, query_exhausted))
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise RuntimeError("unknown-count budget model lost terminal probability mass")
    # Normalize the floating-point totals and keep expectations inside their
    # exact logical bounds in cases where all paths reach a budget cap.
    expected_shots = min(float(shot_cap), shots / total)
    result = {
        "found_probability": found / total,
        "shot_budget_exhaustion_probability": shot_exhausted / total,
        "query_budget_exhaustion_probability": query_exhausted / total,
        "expected_shots": expected_shots,
        "expected_oracle_queries": min(float(query_cap), queries / total),
        "expected_verifier_calls": expected_shots,
        "expected_round_adjustments": min(expected_shots, adjustments / total),
    }
    if gate_work is not None:
        result["expected_compiled_two_qubit_gates"] = gate_work / total
    return result


def plan_unknown_count_search(
    search_qubits: int, solution_count: int, *, max_shots: int = 64,
    max_oracle_queries: int = 256, max_rounds_per_shot: int = MAX_ITERATIONS,
) -> dict[str, Any]:
    """Model both policies for a supplied solution-count hypothesis.

    Each modeled attempt starts in the ideal uniform state. Round draws and
    measurements are independent, and verification agrees with the oracle.
    """
    _bounded_int(search_qubits, 1, MAX_SEARCH_WIDTH, "search_qubits")
    _bounded_int(solution_count, 0, 1 << search_qubits, "solution_count")
    _bounded_int(max_shots, 1, MAX_SEARCH_SHOTS, "max_shots")
    _bounded_int(max_oracle_queries, 0, MAX_ORACLE_QUERIES, "max_oracle_queries")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")

    windows = list(islice(_unknown_round_choices(search_qubits, max_rounds_per_shot), max_shots))
    probabilities = _round_probabilities(search_qubits, solution_count, min(max(windows), max_oracle_queries + 1))
    return {
        "status": "UNKNOWN_COUNT_PLAN_MODELED",
        "model": _IDEAL_MODEL,
        "inputs": {
            "search_qubits": search_qubits,
            "solution_count": solution_count,
            "max_shots": max_shots,
            "max_oracle_queries": max_oracle_queries,
            "max_rounds_per_shot": max_rounds_per_shot,
        },
        "round_choices_by_attempt": windows,
        "policies": {
            policy: _policy_checkpoints(windows, max_oracle_queries, probabilities, policy, [max_shots])[max_shots]
            for policy in QUERY_BUDGET_POLICIES
        },
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }


def _selection_axis(values: Iterable[int], lower: int, upper: int, label: str) -> list[int]:
    try:
        entries = list(islice(values, MAX_SELECTION_PROFILES + 1))
    except TypeError as exc:
        raise AustinAlgorithmError(f"{label} must be an iterable of integers") from exc
    if not entries:
        raise AustinAlgorithmError(f"{label} must contain at least one value")
    if len(entries) > MAX_SELECTION_PROFILES:
        raise AustinAlgorithmError(f"{label} must contain at most {MAX_SELECTION_PROFILES} entries")
    return sorted({_bounded_int(value, lower, upper, label) for value in entries})


def _selection_transition_bound(windows: list[int], query_cap: int) -> int:
    reachable = 0
    updates = 0
    for choices in windows:
        updates += (reachable + 1) * min(choices, query_cap + 1)
        reachable = min(query_cap, reachable + choices - 1)
    return updates


def _candidate_report(
    shot_cap: int, query_cap: int, policy: str, hypotheses: list[dict[str, Any]],
    target: float, certain_first_hit: bool,
) -> dict[str, Any]:
    minimum_found = min(row["found_probability"] for row in hypotheses)
    maximum_failure = max(
        math.fsum((row["shot_budget_exhaustion_probability"], row["query_budget_exhaustion_probability"]))
        for row in hypotheses
    )
    if target == 1.0:
        # A sequence of zero-round misses has positive probability if
        # round zero has any miss mass, even if the tail underflows.
        meets_target = certain_first_hit
    elif target > 0.5:
        meets_target = maximum_failure <= 1.0 - target
    else:
        meets_target = minimum_found >= target
    result = {
        "policy": policy,
        "max_shots": shot_cap,
        "max_oracle_queries": query_cap,
        "minimum_found_probability": minimum_found,
        "maximum_failure_probability": maximum_failure,
        "worst_case_expected_shots": max(row["expected_shots"] for row in hypotheses),
        "worst_case_expected_oracle_queries": max(row["expected_oracle_queries"] for row in hypotheses),
        "meets_target": meets_target,
        "hypotheses": hypotheses,
    }
    if "expected_compiled_two_qubit_gates" in hypotheses[0]:
        result["worst_case_expected_compiled_two_qubit_gates"] = max(
            row["expected_compiled_two_qubit_gates"] for row in hypotheses
        )
    return result


def _probability_order(row: dict[str, Any]) -> tuple[float, float]:
    hit = row["minimum_found_probability"]
    failure = row["maximum_failure_probability"]
    # Order by the smaller probability near the endpoints, where rounding
    # in its complement could hide a difference between candidates.
    if hit > 0.5:
        return failure, -hit
    return 1.0 - hit, -hit


def _cap_order(row: dict[str, Any]) -> tuple[int, int, int]:
    return row["max_shots"], row["max_oracle_queries"], QUERY_BUDGET_POLICIES.index(row["policy"])


def _shot_order(row: dict[str, Any]) -> tuple[float, float]:
    return row["worst_case_expected_shots"], row["worst_case_expected_oracle_queries"]


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "hypotheses"}


def _selection_grid(
    search_qubits: int, solution_counts: Iterable[int], *, target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int,
    round_model: Callable[[int, int], tuple[list[tuple[float, float]], list[int] | None]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate all candidates using the supplied per-round measurement model."""
    _bounded_int(search_qubits, 1, MAX_SEARCH_WIDTH, "search_qubits")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")
    if (isinstance(target_probability, bool) or not isinstance(target_probability, (int, float))
            or not 0 < target_probability <= 1):
        raise AustinAlgorithmError("target_probability must be greater than zero and at most one")
    target = float(target_probability)
    size = 1 << search_qubits
    counts = _selection_axis(solution_counts, 0, size, "solution_counts")
    shots = _selection_axis(shot_caps, 1, MAX_SEARCH_SHOTS, "shot_caps")
    queries = _selection_axis(query_caps, 0, MAX_ORACLE_QUERIES, "query_caps")
    candidate_count = len(shots) * len(queries) * len(QUERY_BUDGET_POLICIES)
    if candidate_count * len(counts) > MAX_SELECTION_PROFILES:
        raise AustinAlgorithmError(f"selection grid exceeds {MAX_SELECTION_PROFILES} candidate/hypothesis profiles")

    windows = list(islice(_unknown_round_choices(search_qubits, max_rounds_per_shot), shots[-1]))
    max_reachable = sum(choices - 1 for choices in windows)
    effective_queries = sorted({min(cap, max_reachable) for cap in queries})
    transition_bound = len(counts) * sum(
        _selection_transition_bound(windows, cap) * (1 if cap == max_reachable else len(QUERY_BUDGET_POLICIES))
        for cap in effective_queries
    )
    if transition_bound > MAX_SELECTION_TRANSITIONS:
        raise AustinAlgorithmError(f"selection grid exceeds {MAX_SELECTION_TRANSITIONS} modeled transition updates")

    tables = {}
    certain_first_hit = True
    choices = min(max(windows), effective_queries[-1] + 1)
    for count in counts:
        probabilities, gate_costs = round_model(count, choices)
        certain_first_hit &= probabilities[0][1] == 0.0
        for cap in effective_queries:
            stop = _policy_checkpoints(windows, cap, probabilities, "stop", shots, gate_costs=gate_costs)
            # At this cap every scheduled draw is affordable for the entire horizon.
            affordable = stop if cap == max_reachable else _policy_checkpoints(
                windows, cap, probabilities, "affordable", shots, gate_costs=gate_costs,
            )
            tables[count, cap] = {"stop": stop, "affordable": affordable}

    candidates = []
    for shot_cap in shots:
        for query_cap in queries:
            cap = min(query_cap, max_reachable)
            for policy in QUERY_BUDGET_POLICIES:
                hypotheses = [{"solution_count": count, **tables[count, cap][policy][shot_cap]} for count in counts]
                candidates.append(_candidate_report(shot_cap, query_cap, policy, hypotheses, target, certain_first_hit))

    inputs = {
        "search_qubits": search_qubits,
        "solution_counts": counts,
        "target_probability": target,
        "shot_caps": shots,
        "query_caps": queries,
        "max_rounds_per_shot": max_rounds_per_shot,
    }
    return inputs, candidates


def select_unknown_count_budgets(
    search_qubits: int, solution_counts: Iterable[int], *, target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
) -> dict[str, Any]:
    """Compare explicit budget candidates across every supplied count hypothesis."""
    inputs, candidates = _selection_grid(
        search_qubits, solution_counts, target_probability=target_probability,
        shot_caps=shot_caps, query_caps=query_caps, max_rounds_per_shot=max_rounds_per_shot,
        round_model=lambda count, choices: (_round_probabilities(search_qubits, count, choices), None),
    )
    feasible = sorted(
        (row for row in candidates if row["meets_target"]),
        key=lambda row: (*_shot_order(row), *_probability_order(row), *_cap_order(row)),
    )
    frontier = []
    lowest_query_work = math.inf
    for row in feasible:
        work = row["worst_case_expected_oracle_queries"]
        if work < lowest_query_work:
            frontier.append(_summary(row))
            lowest_query_work = work

    best = min(candidates, key=lambda row: (*_probability_order(row), *_shot_order(row), *_cap_order(row)))
    return {
        "status": "UNKNOWN_COUNT_TARGET_MET" if feasible else "UNKNOWN_COUNT_TARGET_INFEASIBLE",
        "model": _IDEAL_MODEL,
        "inputs": inputs,
        "candidate_count": len(candidates),
        "feasible_count": len(feasible),
        "recommendations": {
            "fewest_expected_shots": frontier[0] if frontier else None,
            "fewest_expected_oracle_queries": frontier[-1] if frontier else None,
        },
        "feasible_work_frontier": frontier,
        "best_attainable": _summary(best),
        "candidates": candidates,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
