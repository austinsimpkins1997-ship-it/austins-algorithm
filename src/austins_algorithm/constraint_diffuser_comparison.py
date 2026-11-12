"""Matched fixed-layout diffuser comparisons on offline IBM snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from functools import partial
from hashlib import sha256
from importlib.metadata import version
import json
from math import fsum
from time import perf_counter
from typing import Any

from .compilation import MEASUREMENT_TOLERANCE
from .constraint_oracle import synthesize_constraint_oracle
from .constraint_snapshot_sweep import _compile_fixed_cell, _layout_pair
from .constraints import BooleanConstraint
from .core import _bounded_int
from .grover_search import audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_CONSTRAINT_SNAPSHOT_SHOTS,
    _compact_snapshot_inputs,
    _local_limits,
    _prepare_density_snapshot,
    _prepared_snapshot_probabilities,
    _sample_snapshot,
    _snapshot_hit_rate_difference,
    _snapshot_identity,
    _snapshot_simulator,
    _snapshot_specs,
    _summarize_snapshot_batches,
    _validate_snapshot_request,
    _validate_snapshot_seed,
)
from .witness_budget import (
    MAX_WITNESS_BATCHES_PER_COVER,
    _apply_witness_validation_power_budget,
    _check_witness_schedule_size,
    _minimum_successful_witness_batches,
    _minimum_witness_schedule,
    _normalize_validation_seeds,
    _validate_budget,
    _validate_minimum_validation_power,
    _witness_futility_stop,
    _witness_target_support_probability,
    plan_witness_budget,
    summarize_witness_batches,
)


COMPARISON_ROUNDS = (1, 2)
COMPARED_DIFFUSERS = ("standard", "clean-ancilla")
_DiffuserExports = dict[tuple[int, int, str], str]
DIFFUSER_SNAPSHOT_METHODS = ("density_matrix", "prepared_density_matrix", "modeled_probabilities")
DEFAULT_DIFFUSER_SHOTS = 64
DEFAULT_EVALUATION_SEEDS = (101, 103, 107, 109)
MAX_DIFFUSER_BATCHES_PER_VARIANT = 8
FAMILY_CONFIDENCE = 0.95
DIFFUSER_VARIANT_COUNT = 2 * len(COMPARISON_ROUNDS) * len(COMPARED_DIFFUSERS)
DIFFUSER_CONFIDENCE_PER_VARIANT = 1 - (1 - FAMILY_CONFIDENCE) / DIFFUSER_VARIANT_COUNT
DIFFUSER_PLAN_PROBABILITY_ROUNDOFF = 1e-12
_MODELED_BUDGET_PROBABILITIES = {
    "modeled_hit_probability", "success_probability_at_cap", "success_probability_at_budget",
    "validation_power_at_cap", "validation_power_at_budget",
}


def _split_diffuser_seeds(seeds: Sequence[int]) -> dict[str, list[int]]:
    return {method: list(seeds[index::2]) for index, method in enumerate(COMPARED_DIFFUSERS)}


def _diffuser_schedule_power(
    *, target_probability: float, assumed_batch_success_probability: float, minimum_validation_power: float | None,
    minimum: int | None, cap: int, proposed_batches: int | None,
) -> dict[str, Any]:
    assumed = Fraction(assumed_batch_success_probability)
    required_power = None if minimum_validation_power is None else Fraction(minimum_validation_power)
    eligible = range(0) if minimum is None else range(minimum, cap + 1)
    sizes = set(eligible) | {cap}
    if proposed_batches is not None:
        sizes.add(proposed_batches)
    powers = {}
    assessments = {}
    for batches in sizes:
        successes = _minimum_successful_witness_batches(
            batches, target_probability, confidence_level=DIFFUSER_CONFIDENCE_PER_VARIANT,
        )
        power = _witness_target_support_probability(batches, successes, assumed)
        powers[batches] = power
        assessments[batches] = {
            "batches_per_variant": batches,
            "validation_seed_count": 2 * batches,
            "required_successful_batches": successes,
            "maximum_failed_batches": None if successes is None else batches - successes,
            "probability_of_target_support": float(power),
        }
        if required_power is not None:
            assessments[batches]["meets_minimum_validation_power"] = power >= required_power

    # At a fixed miss allowance, adding batches can lower power until
    # the confidence rule starts allowing another miss. Search all eligible sizes.
    best = max(eligible, key=powers.__getitem__, default=None)
    smallest = None
    if required_power is not None:
        smallest = next((size for size in eligible if powers[size] >= required_power), None)
    status = "ASSESSED"
    if minimum is None:
        status = "NO_ELIGIBLE_SCHEDULE"
    elif required_power is not None:
        status = "MINIMUM_POWER_MET" if smallest is not None else "MINIMUM_POWER_UNREACHABLE"
    return {
        "status": status,
        "assumed_batch_success_probability": assumed_batch_success_probability,
        "minimum_validation_power": minimum_validation_power,
        "assumption": "independent batches with a fixed success probability within one variant",
        "at_cap": assessments[cap],
        "at_sizing_minimum": assessments.get(minimum),
        "at_proposed_schedule": assessments.get(proposed_batches),
        "best_within_cap": assessments.get(best),
        "smallest_meeting_minimum_power": assessments.get(smallest),
    }


def plan_diffuser_witness_schedule(
    *, target_probability: float, tolerated_misses: int = 0,
    max_batches_per_variant: int = MAX_WITNESS_BATCHES_PER_COVER,
    validation_seeds: Sequence[int] | None = None,
    assumed_batch_success_probability: float | None = None, minimum_validation_power: float | None = None,
) -> dict[str, Any]:
    """Size, check, or estimate power for a diffuser holdout without running circuits."""
    holdout = None if validation_seeds is None else _normalize_validation_seeds(validation_seeds)
    if assumed_batch_success_probability is not None and (
        isinstance(assumed_batch_success_probability, bool)
        or not isinstance(assumed_batch_success_probability, (int, float))
        or not 0 <= assumed_batch_success_probability <= 1
    ):
        raise ValueError("assumed_batch_success_probability must be between zero and one")
    if minimum_validation_power is not None:
        if assumed_batch_success_probability is None:
            raise ValueError("minimum_validation_power requires assumed_batch_success_probability")
        _validate_minimum_validation_power(minimum_validation_power)
    minimum, cap_bound, minimum_bound = _minimum_witness_schedule(
        target_probability, tolerated_misses, max_batches_per_variant,
        confidence_level=DIFFUSER_CONFIDENCE_PER_VARIANT, cap_label="max_batches_per_variant",
    )
    required_seeds = None if minimum is None else 2 * minimum
    report: dict[str, Any] = {
        "status": "BATCH_CAP_EXCEEDED" if minimum is None else "PLANNED",
        "comparison": "diffuser",
        "target_probability": target_probability,
        "tolerated_misses": tolerated_misses,
        "max_batches_per_variant": max_batches_per_variant,
        "family_variant_count": DIFFUSER_VARIANT_COUNT,
        "batch_family_confidence": FAMILY_CONFIDENCE,
        "confidence_per_variant": DIFFUSER_CONFIDENCE_PER_VARIANT,
        "lower_success_probability_at_cap": cap_bound,
        "minimum_batches_per_variant": minimum,
        "minimum_validation_seeds": required_seeds,
        "lower_success_probability_at_minimum": minimum_bound,
        "max_sampling_batches": max_batches_per_variant * DIFFUSER_VARIANT_COUNT,
        "local_sampling_performed": False,
    }
    if holdout is not None:
        batches = len(holdout) // 2
        report["proposed_schedule"] = {
            **_check_witness_schedule_size(
                len(holdout), required_seeds, cap=max_batches_per_variant,
                tolerated_misses=tolerated_misses, confidence_level=DIFFUSER_CONFIDENCE_PER_VARIANT,
            ),
            "validation_seed_schedule": _split_diffuser_seeds(holdout),
            "validation_batches_per_variant": batches,
            "sampling_batch_ceiling": batches * DIFFUSER_VARIANT_COUNT,
        }
    if assumed_batch_success_probability is not None:
        report["validation_power"] = _diffuser_schedule_power(
            target_probability=target_probability, assumed_batch_success_probability=assumed_batch_success_probability,
            minimum_validation_power=minimum_validation_power, minimum=minimum, cap=max_batches_per_variant,
            proposed_batches=None if holdout is None else len(holdout) // 2,
        )
    return report


def _plan_modeled_witness(
    hit_probability: float, *, target_probability: float, max_shots: int, two_qubit_gates: int,
    validation_batches: int = 0, required_successes: int | None = None, minimum_validation_power: float | None = None,
) -> dict[str, Any]:
    plan = plan_witness_budget(hit_probability, target_probability=target_probability, max_shots=max_shots)
    if minimum_validation_power is not None:
        _apply_witness_validation_power_budget(
            plan, hit_probability=hit_probability, batches=validation_batches, required_successes=required_successes,
            minimum_validation_power=minimum_validation_power,
        )
    plan["modeled_hit_probability"] = plan.pop("lower_hit_rate")
    if plan["status"] == "NO_POSITIVE_LOWER_BOUND":
        plan["status"] = "ZERO_MODELED_HIT_PROBABILITY"
    shots = plan["shots_required"]
    plan["nominal_two_qubit_gate_executions"] = None if shots is None else shots * two_qubit_gates
    return plan


def _search_modeled_witness_schedules(report: dict[str, Any]) -> None:
    search = report["modeled_witness_schedule_search"]
    target = report["modeled_witness_target"]
    candidates = search["candidates"]
    for batches in range(1, search["max_batches_per_variant"] + 1):
        required = _minimum_successful_witness_batches(
            batches, target["target_probability"], confidence_level=search["confidence_per_variant"],
        )
        if required is None:
            continue
        rows = []
        for cell in report["cells"]:
            for diffuser, variant in cell["variants"].items():
                budget = variant["modeled_witness_budget"].copy()
                _apply_witness_validation_power_budget(
                    budget, hit_probability=budget["modeled_hit_probability"], batches=batches,
                    required_successes=required, minimum_validation_power=target["minimum_validation_power"],
                )
                shots = budget["shots_required"]
                planned_shots = 0 if shots is None else batches * shots
                gates_per_shot = variant["compilation"]["compiled"]["two_qubit_gates"]
                rows.append({
                    "placement": cell["placement"], "rounds": cell["rounds"], "diffuser": diffuser,
                    "budget_status": budget["status"], "shots_per_batch": shots, "planned_shots": planned_shots,
                    "planned_nominal_two_qubit_gate_executions": planned_shots * gates_per_shot,
                    "validation_power_at_budget": budget["validation_power_at_budget"],
                    "validation_power_at_cap": budget["validation_power_at_cap"],
                })
        feasible = sum(row["shots_per_batch"] is not None for row in rows)
        candidates.append({
            "batches_per_variant": batches, "validation_seed_count": 2 * batches,
            "required_successful_batches": required, "maximum_failed_batches": batches - required,
            "feasible_variants": feasible, "planned_batches": batches * feasible,
            "planned_shots": sum(row["planned_shots"] for row in rows),
            "planned_nominal_two_qubit_gate_executions": sum(
                row["planned_nominal_two_qubit_gate_executions"] for row in rows
            ),
            "variants": rows,
        })

    if not candidates:
        search.update(status="NO_ELIGIBLE_SCHEDULE", max_feasible_variants=0)
        return
    search["minimum_eligible_batches_per_variant"] = candidates[0]["batches_per_variant"]
    search["max_feasible_variants"] = max(candidate["feasible_variants"] for candidate in candidates)
    complete = [candidate for candidate in candidates if candidate["feasible_variants"] == search["required_variant_count"]]
    if not complete:
        search["status"] = "NO_FULLY_FEASIBLE_SCHEDULE"
        return

    def cost_order(candidate: dict[str, Any], primary: str, secondary: str) -> tuple[int, int, int]:
        return candidate[primary], candidate[secondary], candidate["batches_per_variant"]

    shots = "planned_shots"
    gates = "planned_nominal_two_qubit_gate_executions"
    lowest_shots = min(complete, key=lambda row: cost_order(row, shots, gates))
    lowest_gates = min(complete, key=lambda row: cost_order(row, gates, shots))
    search.update(
        status="PLANNED",
        lowest_shot_cost_batches_per_variant=lowest_shots["batches_per_variant"],
        lowest_nominal_gate_cost_batches_per_variant=lowest_gates["batches_per_variant"],
    )


def _diffuser_replay_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _diffuser_circuit_identity(variant: dict[str, Any]) -> dict[str, Any]:
    compilation = variant["compilation"]
    return {
        "openqasm": compilation["openqasm"],
        "initial_logical_to_target_qubit": compilation["initial_logical_to_target_qubit"],
        "two_qubit_gates": compilation["compiled"]["two_qubit_gates"],
    }


def _diffuser_replay_circuit_digest(cells: Sequence[dict[str, Any]]) -> str:
    circuits = [
        {
            "placement": cell["placement"], "rounds": cell["rounds"], "diffuser": diffuser,
            **_diffuser_circuit_identity(variant),
        }
        for cell in cells for diffuser, variant in cell["variants"].items()
    ]
    return _diffuser_replay_digest(circuits)


def _diffuser_replay_plan(report: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "constraint_sha256": report["constraint_sha256"],
        "target": report["modeled_witness_target"],
        "confidence_per_variant": schedule["confidence_per_variant"],
        "required_successful_batches": schedule["required_successful_batches"],
        "seed_schedule": schedule["seed_schedule"],
        "variants": [
            {
                "placement": cell["placement"], "rounds": cell["rounds"], "diffuser": diffuser,
                "budget": variant["modeled_witness_budget"], "modeled_output": variant["modeled_output"],
            }
            for cell in report["cells"] for diffuser, variant in cell["variants"].items()
        ],
    }


def _diffuser_replay_plan_evidence(report: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    plan = _diffuser_replay_plan(report, schedule)
    budgets = [variant["budget"] for variant in plan["variants"]]
    if len(budgets) != DIFFUSER_VARIANT_COUNT or any(budget["shots_required"] is None for budget in budgets):
        raise ValueError("diffuser replay requires a feasible witness budget for all eight variants")
    batches = schedule["batches_per_variant"]
    return {
        "plan_sha256": _diffuser_replay_digest(plan),
        "planned_batches": batches * len(budgets),
        "planned_shots": batches * sum(budget["shots_required"] for budget in budgets),
        "planned_nominal_two_qubit_gate_executions": batches * sum(
            budget["nominal_two_qubit_gate_executions"] for budget in budgets
        ),
    }


def _modeled_plan_probability(path: tuple[str | int, ...]) -> bool:
    if len(path) < 4 or path[0] != "variants" or type(path[1]) is not int:
        return False
    section = path[2:]
    return (
        (len(section) == 2 and section[0] == "budget" and section[1] in _MODELED_BUDGET_PROBABILITIES)
        or section == ("modeled_output", "verified_hit_probability")
        or (len(section) == 3 and section[:2] == ("modeled_output", "outcome_probabilities")
            and isinstance(section[2], str) and bool(section[2]) and set(section[2]) <= {"0", "1"})
    )


def _compare_diffuser_plan_roundoff(reference: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Require identical plan structure and permit only the named finite probability differences."""
    changed = 0
    maximum = 0.0

    def compare(left: Any, right: Any, path: tuple[str | int, ...]) -> None:
        nonlocal changed, maximum
        if type(left) is type(right):
            if isinstance(left, dict) and left.keys() == right.keys():
                for key, value in left.items():
                    compare(value, right[key], (*path, key))
                return
            if isinstance(left, list) and len(left) == len(right):
                for index, (a, b) in enumerate(zip(left, right, strict=True)):
                    compare(a, b, (*path, index))
                return
            if left == right and (type(left) is not float or left.hex() == right.hex()):
                return
            if (type(left) is float and _modeled_plan_probability(path)
                    and 0 <= left <= 1 and 0 <= right <= 1):
                difference = abs(right - left)
                if difference <= DIFFUSER_PLAN_PROBABILITY_ROUNDOFF:
                    changed += 1
                    maximum = max(maximum, difference)
                    return
        location = "/".join(map(str, path))
        raise ValueError(f"diffuser replay modeled plan differs at {location}")

    compare(reference, observed, ())
    return {"maximum_absolute_difference": maximum, "changed_modeled_probability_values": changed}


def _plan_modeled_witness_validation(report: dict[str, Any]) -> None:
    plan = report["modeled_witness_plan"]
    batches = plan["batches_per_variant"]
    required_successes = plan["required_successful_batches"]
    if "minimum_validation_power" not in report["modeled_witness_target"]:
        required_successes = _minimum_successful_witness_batches(
            batches, report["modeled_witness_target"]["target_probability"],
            confidence_level=plan["confidence_per_variant"],
        )
    plan.update(
        required_successful_batches=required_successes,
        maximum_failed_batches=None if required_successes is None else batches - required_successes,
        planned_batches=0, planned_shots=0, planned_nominal_two_qubit_gate_executions=0,
    )
    for cell in report["cells"]:
        for variant in cell["variants"].values():
            budget = variant["modeled_witness_budget"]
            shots = budget["shots_required"]
            assumed = budget["success_probability_at_budget"]
            if shots is None:
                row = {
                    "status": "NO_FEASIBLE_BUDGET", "planned_batches": 0, "planned_shots": 0,
                    "planned_nominal_two_qubit_gate_executions": 0,
                    "assumed_batch_success_probability": None, "probability_of_target_support": None,
                }
            else:
                power = budget.get("validation_power_at_budget")
                if power is None:
                    power = float(_witness_target_support_probability(batches, required_successes, Fraction(assumed)))
                row = {
                    "status": "PLANNED", "planned_batches": batches, "planned_shots": shots * batches,
                    "planned_nominal_two_qubit_gate_executions": budget["nominal_two_qubit_gate_executions"] * batches,
                    "assumed_batch_success_probability": assumed, "probability_of_target_support": power,
                }
            variant["modeled_witness_plan"] = row
            for metric in ("planned_batches", "planned_shots", "planned_nominal_two_qubit_gate_executions"):
                plan[metric] += row[metric]
    plan["status"] = "PLANNED" if plan["planned_batches"] else "NO_FEASIBLE_BUDGET"


def _run_modeled_witness_validation(
    report: dict[str, Any], samplers: Mapping[tuple[int, int, str], Callable[..., dict[str, Any]]],
) -> None:
    validation = report["modeled_witness_validation"]
    batches_per_variant = validation["batches_per_variant"]
    jobs = []
    for cell in report["cells"]:
        for diffuser, variant in cell["variants"].items():
            budget = variant["modeled_witness_budget"]
            shots = budget["shots_required"]
            row: dict[str, Any] = {"batches": [], "completed_nominal_two_qubit_gate_executions": 0}
            variant["modeled_witness_validation"] = row
            if shots is None:
                row.update(status="NO_FEASIBLE_BUDGET", planned_batches=0, planned_shots=0)
                continue
            key = (cell["placement"], cell["rounds"], diffuser)
            row["planned_shots"] = shots * batches_per_variant
            gates = variant["compilation"]["compiled"]["two_qubit_gates"]
            jobs.append((samplers[key], validation["seed_schedule"][diffuser], shots, gates, row))

    validation["planned_batches"] = len(jobs) * batches_per_variant
    validation["planned_shots"] = sum(row["planned_shots"] for _, _, _, _, row in jobs)
    if not jobs:
        validation["status"] = "NO_FEASIBLE_BUDGET"
        return

    target = report["modeled_witness_target"]["target_probability"]
    confidence = validation["confidence_per_variant"]
    validation["status"] = "PARTIAL_VALIDATION"
    try:
        for sampler, seeds, shots, _, row in jobs:
            check_futility = validation.get("stop_on_futility", False)
            misses = 0
            for index, seed in enumerate(seeds, start=1):
                started = perf_counter()
                sample = sampler(shots=shots, seed=seed)
                report["timing_seconds"]["sampling"] += perf_counter() - started
                row["batches"].append({"seed": seed, **sample})
                if sample.get("local_job_cancelled") is False:
                    report["local_cancellation_unconfirmed"] = True
                    return
                if sample["status"] != "COMPLETED":
                    check_futility = False
                    continue
                if not check_futility or index == len(seeds):
                    continue

                missed = sample["simulation"]["verified_shots"] == 0
                misses += missed
                if index > 1 and not missed:
                    continue
                futility = _witness_futility_stop(seeds, index, misses, target, confidence_level=confidence)
                if futility is not None:
                    row["futility_stop"] = futility
                    skipped = len(futility["skipped_seeds"])
                    validation["skipped_batches_for_futility"] += skipped
                    validation["skipped_shots_for_futility"] += skipped * shots
                    break
    finally:
        for _, _, _, gates, row in jobs:
            summary = summarize_witness_batches(
                row["batches"], planned_batches=batches_per_variant,
                target_probability=target, confidence_level=confidence,
            )
            row.update(summary)
            if "futility_stop" in row:
                row["status"] = "STOPPED_FOR_FUTILITY"
            gate_executions = summary["completed_shots"] * gates
            row["completed_nominal_two_qubit_gate_executions"] = gate_executions
            validation["completed_batches"] += summary["completed_batches"]
            validation["completed_shots"] += summary["completed_shots"]
            validation["completed_nominal_two_qubit_gate_executions"] += gate_executions
        skipped = validation.get("skipped_batches_for_futility", 0)
        if validation["completed_batches"] + skipped == validation["planned_batches"]:
            validation["status"] = "STOPPED_FOR_FUTILITY" if skipped else "COMPLETED"


def run_constraint_diffuser_comparison(
    constraint: BooleanConstraint, *, model: str, placements: Sequence[Sequence[int]],
    synthesis: str = "auto", shots: int = DEFAULT_DIFFUSER_SHOTS,
    evaluation_seeds: Sequence[int] = DEFAULT_EVALUATION_SEEDS,
    transpiler_seed: int = 7, simulation_method: str | None = None, prefer_native_qasm: bool = False,
    witness_probability: float | None = None, witness_max_shots: int | None = None,
    witness_seed_schedule: Sequence[int] | None = None,
    witness_stop_on_futility: bool = False, witness_plan_only: bool = False,
    witness_validation_power: float | None = None, witness_search_max_batches: int | None = None,
    _capture_witness_replay: bool = False, _expected_witness_replay: Mapping[str, Any] | None = None,
    _openqasm_exports: _DiffuserExports | None = None,
    _expected_witness_replay_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare fixed diffusers and optionally plan or run a modeled witness holdout."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if not isinstance(prefer_native_qasm, bool):
        raise ValueError("prefer_native_qasm must be a boolean")
    _validate_snapshot_request(model, shots, transpiler_seed)
    if model == "all":
        raise ValueError("diffuser comparison requires one named model")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"diffuser comparison shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS} per batch")
    if simulation_method is not None and simulation_method not in DIFFUSER_SNAPSHOT_METHODS:
        raise ValueError(f"simulation_method must be one of {', '.join(DIFFUSER_SNAPSHOT_METHODS)}")
    modeled = simulation_method == "modeled_probabilities"
    prepared = modeled or simulation_method == "prepared_density_matrix"
    if not isinstance(witness_plan_only, bool):
        raise ValueError("witness_plan_only must be a boolean")
    search_schedules = witness_search_max_batches is not None
    if search_schedules:
        _bounded_int(witness_search_max_batches, 1, MAX_WITNESS_BATCHES_PER_COVER, "witness_search_max_batches")
        if witness_seed_schedule is not None:
            raise ValueError("witness_search_max_batches cannot be used with witness_seed_schedule")
        if witness_probability is None or witness_validation_power is None:
            raise ValueError("witness_search_max_batches requires witness_probability and witness_validation_power")
    if witness_plan_only and witness_seed_schedule is None and not search_schedules:
        raise ValueError("witness_plan_only requires witness_seed_schedule")
    if witness_plan_only and witness_stop_on_futility:
        raise ValueError("witness_plan_only cannot be used with witness_stop_on_futility")
    if not isinstance(witness_stop_on_futility, bool):
        raise ValueError("witness_stop_on_futility must be a boolean")
    if witness_stop_on_futility and witness_seed_schedule is None:
        raise ValueError("witness_stop_on_futility requires witness_seed_schedule")
    if witness_validation_power is not None:
        _validate_minimum_validation_power(witness_validation_power)
        if witness_seed_schedule is None and not search_schedules:
            raise ValueError("witness_validation_power requires witness_seed_schedule")
    if witness_seed_schedule is not None and witness_probability is None:
        raise ValueError("witness_seed_schedule requires witness_probability")
    if witness_max_shots is not None and witness_probability is None:
        raise ValueError("witness_max_shots requires witness_probability")
    if witness_probability is not None:
        if not modeled:
            raise ValueError("witness budgets require modeled_probabilities")
        witness_max_shots = MAX_CONSTRAINT_SNAPSHOT_SHOTS if witness_max_shots is None else witness_max_shots
        _validate_budget(witness_probability, witness_max_shots)
    validation_seeds = None if witness_seed_schedule is None else _normalize_validation_seeds(witness_seed_schedule)
    validation_batches = 0 if validation_seeds is None else len(validation_seeds) // 2
    track_replay = _capture_witness_replay or _expected_witness_replay is not None
    if _expected_witness_replay_plan is not None and (
        _expected_witness_replay is None
        or _diffuser_replay_digest(_expected_witness_replay_plan) != _expected_witness_replay["plan_sha256"]
    ):
        raise ValueError("diffuser roundoff requires the verified baseline plan")
    if track_replay and (validation_seeds is None or witness_validation_power is None):
        raise ValueError("diffuser replay requires a fixed seed schedule and validation power")

    max_seeds = 2 * MAX_DIFFUSER_BATCHES_PER_VARIANT
    if (
        not isinstance(evaluation_seeds, Sequence)
        or not 4 <= len(evaluation_seeds) <= max_seeds
        or len(evaluation_seeds) % 2
    ):
        raise ValueError(f"diffuser comparison requires an even number of evaluation seeds from 4 to {max_seeds}")
    evaluation_seeds = tuple(evaluation_seeds)
    for seed in evaluation_seeds:
        _validate_snapshot_seed(seed)
    if len(set(evaluation_seeds)) != len(evaluation_seeds):
        raise ValueError("evaluation seeds must be distinct")
    if modeled and (shots != DEFAULT_DIFFUSER_SHOTS or evaluation_seeds != DEFAULT_EVALUATION_SEEDS):
        raise ValueError("modeled_probabilities uses no shot or evaluation seed options")
    seeds = _split_diffuser_seeds(evaluation_seeds)
    batches_per_variant = len(evaluation_seeds) // 2
    layouts = _layout_pair(placements)

    started = perf_counter()
    oracle, synthesis_report = synthesize_constraint_oracle(constraint, synthesis=synthesis)
    if any(len(layout) != oracle.num_qubits for layout in layouts):
        raise ValueError(f"each placement requires {oracle.num_qubits} indices for the selected oracle")
    audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    num_variants = DIFFUSER_VARIANT_COUNT
    confidence = DIFFUSER_CONFIDENCE_PER_VARIANT
    report: dict[str, Any] = {
        "status": "PROVEN_EMPTY" if audit["marked_count"] == 0 else "PARTIAL",
        "experiment": "LOCAL_IBM_SNAPSHOT_CONSTRAINT_DIFFUSER_COMPARISON",
        "execution_mode": "SIMULATOR",
        "requested_model": model,
        "constraint_sha256": constraint.digest(),
        "oracle": {**synthesis_report, "qubits": oracle.num_qubits, "work_qubits": oracle.num_qubits - constraint.qubits},
        "oracle_audit": audit,
        "round_candidates": list(COMPARISON_ROUNDS),
        "placements": [list(layout) for layout in layouts],
        "transpiler_seed": transpiler_seed,
        "limits": {
            **_local_limits(), "shots": 0 if modeled else MAX_CONSTRAINT_SNAPSHOT_SHOTS,
            "max_sampling_batches": 0 if modeled else batches_per_variant * num_variants,
        },
        "cells": [],
        "local_cancellation_unconfirmed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    timing = {"noise_model_setup": 0.0, "state_preparation": 0.0, "sampling": 0.0}
    report["timing_seconds"] = timing
    if modeled:
        report["result_kind"] = "MODELED_PROBABILITIES"
        timing["probability_evaluation"] = 0.0
    else:
        report.update(
            shots_per_batch=shots, evaluation_seeds=seeds,
            family_confidence_per_snapshot=FAMILY_CONFIDENCE, confidence_per_variant=confidence,
            interval_method="Bonferroni-adjusted two-sided Clopper-Pearson; subtract opposite endpoints",
        )
    if witness_probability is not None:
        report["modeled_witness_target"] = {
            "target_probability": witness_probability,
            "max_shots": witness_max_shots,
            "assumption": "independent shots with fixed snapshot hit probability",
        }
    if witness_validation_power is not None:
        report["modeled_witness_target"]["minimum_validation_power"] = witness_validation_power
    if search_schedules:
        report["result_kind"] = "MODELED_PROBABILITIES_WITH_WITNESS_SCHEDULE_SEARCH"
        report["modeled_witness_schedule_search"] = {
            "status": "NOT_STARTED",
            "max_batches_per_variant": witness_search_max_batches,
            "required_variant_count": num_variants,
            "batch_family_confidence": FAMILY_CONFIDENCE,
            "confidence_per_variant": confidence,
            "power_assumption": "independent batches with a fixed success probability within one variant",
            "local_sampling_performed": False,
            "minimum_eligible_batches_per_variant": None,
            "max_feasible_variants": None,
            "lowest_shot_cost_batches_per_variant": None,
            "lowest_nominal_gate_cost_batches_per_variant": None,
            "candidates": [],
        }
    schedule_key = "modeled_witness_plan" if witness_plan_only else "modeled_witness_validation"
    if validation_seeds is not None:
        schedule = {
            "status": "NOT_STARTED",
            "seed_schedule": _split_diffuser_seeds(validation_seeds),
            "batches_per_variant": validation_batches,
            "batch_family_confidence": FAMILY_CONFIDENCE,
            "confidence_per_variant": confidence,
            "interval_method": "One-sided Clopper-Pearson with Bonferroni across eight variants",
            "planned_batches": None,
            "planned_shots": None,
        }
        if witness_plan_only:
            report["result_kind"] = "MODELED_PROBABILITIES_WITH_WITNESS_PLAN"
            schedule.update(
                required_successful_batches=None, maximum_failed_batches=None,
                planned_nominal_two_qubit_gate_executions=None, local_sampling_performed=False,
                power_assumption="independent batches with a fixed success probability within one variant",
            )
        else:
            report["result_kind"] = "MODELED_PROBABILITIES_WITH_WITNESS_VALIDATION"
            schedule.update(completed_batches=0, completed_shots=0, completed_nominal_two_qubit_gate_executions=0)
            if witness_stop_on_futility:
                schedule.update(stop_on_futility=True, skipped_batches_for_futility=0, skipped_shots_for_futility=0)
            report["limits"].update(shots=witness_max_shots, max_sampling_batches=num_variants * validation_batches)
        report[schedule_key] = schedule
    if simulation_method is not None:
        report.update(
            snapshot_method=simulation_method, wire_representation="compact" if prepared else "physical",
        )
        report["limits"]["max_state_preparation_jobs"] = num_variants if prepared else 0

    try:
        if audit["marked_count"] == 0:
            return report

        from qiskit_aer import AerSimulator

        factory, default_method = _snapshot_specs()[model]
        simulator_method = "density_matrix" if prepared else simulation_method or default_method
        backend = factory()
        properties = backend.properties()
        report.update({
            "backend": backend.name,
            "backend_version": backend.backend_version,
            "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
            "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
        })
        if modeled:
            report["versions"]["numpy"] = version("numpy")
        if track_replay:
            snapshot = _snapshot_identity(backend)
            if _expected_witness_replay is not None:
                if snapshot != _expected_witness_replay["snapshot"]:
                    raise ValueError("diffuser replay snapshot identity differs")
                if report["versions"] != _expected_witness_replay["versions"]:
                    raise ValueError("diffuser replay software versions differ")
            report["witness_replay_evidence"] = {"snapshot": snapshot, "versions": report["versions"]}
        if oracle.num_qubits > backend.num_qubits:
            report.update(status="INSUFFICIENT_TARGET_WIDTH", target_qubits=backend.num_qubits)
            return report
        if any(index >= backend.num_qubits for layout in layouts for index in layout):
            raise ValueError(f"placement indices must be below the {backend.num_qubits}-qubit target width")

        circuits = {
            (rounds, method): build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds, diffuser=method)
            for rounds in COMPARISON_ROUNDS for method in COMPARED_DIFFUSERS
        }
        programs = {}
        difference_key = "modeled_hit_rate_difference" if modeled else "difference"
        witness_samplers = {}
        for placement, layout in enumerate(layouts, start=1):
            for rounds in COMPARISON_ROUNDS:
                variants = {}
                for method in COMPARED_DIFFUSERS:
                    program, record = _compile_fixed_cell(
                        circuits[rounds, method], backend=backend, layout=layout, seed=transpiler_seed,
                        constraint=constraint, marked_count=audit["marked_count"], rounds=rounds,
                        prefer_native_qasm=prefer_native_qasm,
                    )
                    programs[placement, rounds, method] = program
                    record["compilation_timing_seconds"] = dict(program.timing_seconds)
                    for stage, elapsed in program.timing_seconds.items():
                        timing[stage] = timing.get(stage, 0.0) + elapsed
                    if modeled:
                        variants[method] = {**record, "modeled_output": None}
                        if witness_probability is not None:
                            variants[method]["modeled_witness_budget"] = None
                        if validation_seeds is not None:
                            variants[method][schedule_key] = None
                    else:
                        variants[method] = {**record, "batches": [], "summary": None}
                standard, clean = (variants[method] for method in COMPARED_DIFFUSERS)
                a, b = (variant["compilation"]["verification"]["roundtrip_probabilities"]
                        for variant in (standard, clean))
                tvd = fsum(abs(a.get(bits, 0) - b.get(bits, 0)) for bits in a.keys() | b.keys()) / 2
                if tvd > MEASUREMENT_TOLERANCE:
                    raise RuntimeError(f"{backend.name} placement {placement} round {rounds} diffuser distributions differ")
                cell = {
                    "placement": placement, "rounds": rounds, "ideal_distribution_total_variation_distance": tvd,
                    "two_qubit_gate_savings": (
                        standard["compilation"]["compiled"]["two_qubit_gates"]
                        - clean["compilation"]["compiled"]["two_qubit_gates"]
                    ),
                    "variants": variants, difference_key: None,
                }
                if witness_probability is not None:
                    cell["modeled_witness_cost_comparison"] = None
                report["cells"].append(cell)
        cells = report["cells"]
        if track_replay:
            circuit_digest = _diffuser_replay_circuit_digest(cells)
            if _expected_witness_replay is not None and circuit_digest != _expected_witness_replay["circuits_sha256"]:
                raise ValueError("diffuser replay compiled circuit identity differs")
            report["witness_replay_evidence"]["circuits_sha256"] = circuit_digest
        if any(not variant["fits_active_qubit_limit"] for cell in cells for variant in cell["variants"].values()):
            report["status"] = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
            return report

        setup_started = perf_counter()
        compact_inputs = None
        if prepared:
            compact_inputs = _compact_snapshot_inputs(backend, programs, AerSimulator)
        else:
            simulator = _snapshot_simulator(backend, AerSimulator, simulator_method)
        timing["noise_model_setup"] = perf_counter() - setup_started

        required_successes = None
        if witness_validation_power is not None and not search_schedules:
            required_successes = _minimum_successful_witness_batches(
                validation_batches, witness_probability, confidence_level=confidence,
            )
            report[schedule_key].update(
                required_successful_batches=required_successes,
                maximum_failed_batches=None if required_successes is None else validation_batches - required_successes,
            )
        for cell in cells:
            for method, variant in cell["variants"].items():
                key = (cell["placement"], cell["rounds"], method)
                program = programs[key]
                options = {}
                if compact_inputs is not None:
                    prepared_input, preparation = _prepare_density_snapshot(*compact_inputs[key], backend.name)
                    variant["state_preparation"] = preparation
                    timing["state_preparation"] += preparation["wall_seconds"]
                    if prepared_input is None:
                        report["status"] = "STATE_PREPARATION_INCOMPLETE"
                        report["local_cancellation_unconfirmed"] = not preparation["local_job_cancelled"]
                        return report
                    if modeled:
                        probability_started = perf_counter()
                        variant["modeled_output"] = _prepared_snapshot_probabilities(
                            *prepared_input, verifier=constraint.matches,
                        )
                        timing["probability_evaluation"] += perf_counter() - probability_started
                        if witness_probability is not None:
                            budget = _plan_modeled_witness(
                                variant["modeled_output"]["verified_hit_probability"],
                                target_probability=witness_probability, max_shots=witness_max_shots,
                                two_qubit_gates=variant["compilation"]["compiled"]["two_qubit_gates"],
                                validation_batches=validation_batches, required_successes=required_successes,
                                minimum_validation_power=None if search_schedules else witness_validation_power,
                            )
                            variant["modeled_witness_budget"] = budget
                            if validation_seeds is not None and not witness_plan_only and budget["shots_required"] is not None:
                                sampling_circuit, readout_simulator = prepared_input
                                witness_samplers[key] = partial(
                                    _sample_snapshot, program, backend, readout_simulator, method=simulator_method,
                                    verifier=constraint.matches, sampling_circuit=sampling_circuit,
                                )
                        continue
                    options["sampling_circuit"], simulator = prepared_input
                for seed in seeds[method]:
                    batch_started = perf_counter()
                    sample = _sample_snapshot(
                        program, backend, simulator, method=simulator_method, shots=shots, seed=seed,
                        verifier=constraint.matches, **options,
                    )
                    timing["sampling"] += perf_counter() - batch_started
                    variant["batches"].append({"seed": seed, **sample})
                    if sample.get("local_job_cancelled") is False:
                        report["local_cancellation_unconfirmed"] = True
                        return report
                variant["summary"] = _summarize_snapshot_batches(
                    variant["batches"], confidence, expected_batches=batches_per_variant,
                )
            standard, clean = (cell["variants"][method] for method in COMPARED_DIFFUSERS)
            if modeled:
                cell[difference_key] = (
                    clean["modeled_output"]["verified_hit_probability"]
                    - standard["modeled_output"]["verified_hit_probability"]
                )
                if witness_probability is not None:
                    costs = {
                        diffuser: row["modeled_witness_budget"]["nominal_two_qubit_gate_executions"]
                        for diffuser, row in cell["variants"].items()
                    }
                    standard_cost, clean_cost = (costs[diffuser] for diffuser in COMPARED_DIFFUSERS)
                    cell["modeled_witness_cost_comparison"] = {
                        "feasible_diffusers": [diffuser for diffuser, cost in costs.items() if cost is not None],
                        "nominal_two_qubit_gate_savings": (
                            standard_cost - clean_cost if standard_cost is not None and clean_cost is not None else None
                        ),
                    }
            else:
                cell[difference_key] = _snapshot_hit_rate_difference(standard["summary"], clean["summary"])
        if all(cell[difference_key] is not None for cell in cells):
            report["status"] = "COMPLETED"
        if track_replay:
            evidence = report["witness_replay_evidence"]
            schedule = report[schedule_key]
            evidence.update(_diffuser_replay_plan_evidence(report, schedule))
            if _expected_witness_replay_plan is not None:
                if _diffuser_replay_digest({**evidence, "plan_sha256": _expected_witness_replay["plan_sha256"]}) != (
                    _diffuser_replay_digest(dict(_expected_witness_replay))
                ):
                    raise ValueError("diffuser replay circuit, snapshot, software, or planned totals differ")
                _compare_diffuser_plan_roundoff(_expected_witness_replay_plan, _diffuser_replay_plan(report, schedule))
            elif _expected_witness_replay is not None and evidence != _expected_witness_replay:
                raise ValueError("diffuser replay modeled budget, seed schedule, or output differs")
        if search_schedules:
            search_started = perf_counter()
            _search_modeled_witness_schedules(report)
            timing["schedule_search"] = perf_counter() - search_started
        elif witness_plan_only:
            _plan_modeled_witness_validation(report)
        elif validation_seeds is not None:
            _run_modeled_witness_validation(report, witness_samplers)
            validation_status = report["modeled_witness_validation"]["status"]
            if validation_status == "PARTIAL_VALIDATION":
                report["status"] = "PARTIAL"
            elif validation_status == "STOPPED_FOR_FUTILITY":
                report["status"] = validation_status
        if _openqasm_exports is not None and witness_plan_only and report["status"] == "COMPLETED":
            _openqasm_exports.update({key: program.openqasm for key, program in programs.items()})
        return report
    finally:
        report["wall_seconds"] = perf_counter() - started

