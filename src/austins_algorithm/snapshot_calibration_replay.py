"""Replay frozen dated-calibration search budgets with local seeded draws."""

from __future__ import annotations

from collections import Counter
from random import Random
from typing import TYPE_CHECKING, Any

from .unknown_count_snapshot_profile import _digest
from .unknown_count_snapshot_selection import MAX_VALIDATION_ATTEMPTS, _validate_arm, _validation_interval

if TYPE_CHECKING:
    from .constraints import BooleanConstraint
    from .snapshot_calibration_planning import _CalibrationProfile


_REPLAY_SEED_START = 0x23800000
_CLASSICAL_SEED_XOR = 0x5A5A5A5A
_LANES = ("earlier", "later")
_PLAN_FIELDS = ("layout", "policy", "max_shots", "max_oracle_queries")
_PAIR_OUTCOMES = {
    (True, True): "both_found", (True, False): "earlier_only",
    (False, True): "later_only", (False, False): "neither_found",
}


def _freeze_plans(report: dict[str, Any]) -> dict[tuple[Any, ...], list[str]]:
    selected = {}
    groups = {"shared": report, **report["lane_selection"]}
    for label, group in groups.items():
        choices = {objective: row for objective, row in group["recommendations"].items() if row is not None}
        if not choices:
            choices = {"best_attainable": group["best_attainable"]}
        for objective, row in choices.items():
            key = tuple(row[field] for field in _PLAN_FIELDS)
            selected.setdefault(key, []).append(f"{label}.{objective}")
    return selected


def _replay_classical(
    constraint: BooleanConstraint, caps: list[int], seeds: list[int], forecasts: dict[str, dict[str, float]],
    confidence: float, target: float,
) -> dict[str, Any]:
    size = 1 << constraint.qubits
    effective_caps = {cap: min(size, cap) for cap in caps}
    longest = max(effective_caps.values())
    prefixes = []
    for seed in seeds:
        order = Random(seed ^ _CLASSICAL_SEED_XOR).sample(range(size), longest)
        checked = []
        witness = None
        for value in order:
            bits = format(value, f"0{constraint.qubits}b")
            checked.append(bits)
            if constraint.matches(bits):
                witness = bits
                break
        prefixes.append((checked, witness))

    results = {}
    by_limit = {}
    for cap, limit in effective_caps.items():
        if limit not in by_limit:
            traces = [
                {"checked": checked[:limit], "witness": witness if len(checked) <= limit else None}
                for checked, witness in prefixes
            ]
            hits = sum(trace["witness"] is not None for trace in traces)
            observed = {
                "witnesses": hits, "trials": len(seeds), "found_rate": hits / len(seeds),
                "verifier_calls": sum(len(trace["checked"]) for trace in traces),
            }
            by_limit[limit] = {
                "effective_candidate_cap": limit, "observed": observed,
                "interval": _validation_interval(hits, len(seeds), confidence, target), "traces_sha256": _digest(traces),
            }
        modeled = forecasts[str(cap)]
        row = by_limit[limit]
        interval = row["interval"]
        results[str(cap)] = {
            **row, "modeled": modeled,
            "modeled_probability_in_interval": interval["lower"] <= modeled["found_probability"] <= interval["upper"],
        }
    return results


def _replay_calibration_budgets(
    report: dict[str, Any], profiles: dict[str, _CalibrationProfile], constraint: BooleanConstraint, trials: int,
) -> dict[str, Any]:
    if not report["candidates"]:
        return {"status": "UNAVAILABLE", "reason": "NO_ELIGIBLE_FIXED_PLAN", "trials_per_arm": trials,
                "plans": [], "classical_distinct_uniform": {}}

    selected = _freeze_plans(report)
    forecasts = {tuple(row[field] for field in _PLAN_FIELDS): row for row in report["candidates"]}
    caps = sorted({key[2] for key in selected})
    classical_limit = min(1 << constraint.qubits, caps[-1])
    work = trials * (len(_LANES) * sum(key[2] for key in selected) + classical_limit)
    if work > MAX_VALIDATION_ATTEMPTS:
        raise ValueError(f"calibration replay exceeds {MAX_VALIDATION_ATTEMPTS} possible attempts")

    seeds = list(range(_REPLAY_SEED_START, _REPLAY_SEED_START + trials))
    arms = len(_LANES) * len(selected) + len(caps)
    family_confidence = .95
    confidence = 1 - (1 - family_confidence) / arms
    target = report["inputs"]["target_probability"]
    count = report["inputs"]["solution_count"]
    plans = []
    for index, (key, labels) in enumerate(selected.items(), start=1):
        profile = profiles[key[0]]
        budget = {**dict(zip(_PLAN_FIELDS, key, strict=True)),
                  "max_rounds_per_shot": report["inputs"]["max_rounds_per_shot"]}
        observations = {}
        outcomes = {}
        for lane in _LANES:
            rounds = {"rounds": [
                {"weights": weights, "probabilities": probabilities, "gates": gates}
                for weights, probabilities, gates in zip(
                    profile.weights[lane], profile.probabilities[lane], profile.gate_costs, strict=True,
                )
            ]}
            outcomes[lane] = []
            forecast = {name: value for name, value in forecasts[key][lane].items()
                        if name not in ("solution_count", "meets_target")}
            observed = _validate_arm(
                rounds, constraint.qubits, count, budget, seeds, target, confidence,
                verifier=constraint.matches, modeled=forecast, witness_outcomes=outcomes[lane],
            )
            interval = observed["interval"]
            observed["modeled_probability_in_interval"] = interval["lower"] <= forecast["found_probability"] <= interval["upper"]
            observations[lane] = observed
        pairs = Counter(_PAIR_OUTCOMES[pair] for pair in zip(outcomes["earlier"], outcomes["later"], strict=True))
        plans.append({
            "id": f"plan_{index}", **budget, "selected_for": labels, "target_status": forecasts[key]["target_status"],
            **observations, "paired_witness_outcomes": {label: pairs[label] for label in _PAIR_OUTCOMES.values()},
            "later_minus_earlier_found_rate": (pairs["later_only"] - pairs["earlier_only"]) / trials,
        })
    classical = _replay_classical(constraint, caps, seeds, report["classical_distinct_uniform"], confidence, target)
    return {
        "status": "COMPLETED", "method": "MATCHED_SEEDED_CATEGORICAL_REPLAY_OF_TWO_SAVED_SNAPSHOT_MODELS",
        "classical_method": "SEEDED_DISTINCT_UNIFORM_PREFIX",
        "trials_per_arm": trials, "seeds": seeds, "maximum_possible_attempts": work,
        "confidence": {"method": "CLOPPER_PEARSON_BONFERRONI", "family": family_confidence,
                       "per_arm": confidence, "arms": arms},
        "plans": plans, "classical_distinct_uniform": classical,
    }

