"""Conservative bounded budgets for at least one verified simulator witness."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import comb
from typing import Any

from .core import _bounded_int
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS, _validate_snapshot_seed


DEFAULT_COVER_EVALUATION_SEEDS = (167, 173, 179, 181)
DEFAULT_WITNESS_VALIDATION_SEEDS = (233, 239)
MAX_WITNESS_BATCHES_PER_COVER = 128
WITNESS_BATCH_CONFIDENCE_PER_COVER = 0.975


def _validate_target_probability(target_probability: float) -> None:
    if (
        isinstance(target_probability, bool) or not isinstance(target_probability, (int, float))
        or not 0 < target_probability < 1
    ):
        raise ValueError("target_probability must be strictly between zero and one")


def _validate_minimum_validation_power(minimum_validation_power: float) -> None:
    if (
        isinstance(minimum_validation_power, bool) or not isinstance(minimum_validation_power, (int, float))
        or not 0 < minimum_validation_power <= 1
    ):
        raise ValueError("minimum_validation_power must be greater than zero and at most one")


def _validate_budget(target_probability: float, max_shots: int) -> None:
    _validate_target_probability(target_probability)
    _bounded_int(max_shots, 1, MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_shots")


def _normalize_evaluation_seeds(seeds: Sequence[int]) -> tuple[int, ...]:
    if not isinstance(seeds, Sequence) or len(seeds) != 4:
        raise ValueError("phase cover sampling requires four evaluation seeds")
    normalized = tuple(seeds)
    for seed in normalized:
        _validate_snapshot_seed(seed)
    if len(set(normalized)) != 4:
        raise ValueError("evaluation seeds must be distinct")
    return normalized


def _normalize_validation_seeds(seeds: Sequence[int]) -> tuple[int, ...]:
    if (
        not isinstance(seeds, Sequence)
        or not 2 <= len(seeds) <= 2 * MAX_WITNESS_BATCHES_PER_COVER
        or len(seeds) % 2
    ):
        raise ValueError(
            f"witness validation requires an even number of seeds from 2 to {2 * MAX_WITNESS_BATCHES_PER_COVER}"
        )
    normalized = tuple(seeds)
    for seed in normalized:
        _bounded_int(seed, 0, 2**32 - 1, "validation seed")
    if len(set(normalized)) != len(normalized):
        raise ValueError("witness validation seeds must be distinct")
    return normalized


def _validate_seed_overlap(validation_seeds: Sequence[int], evaluation_seeds: Sequence[int]) -> None:
    if not set(validation_seeds).isdisjoint(evaluation_seeds):
        raise ValueError("witness validation seeds must differ from all calibration seeds")


def _split_cover_seeds(seeds: Sequence[int]) -> dict[str, list[int]]:
    return {"direct": list(seeds[::2]), "complement": list(seeds[1::2])}


@dataclass(frozen=True)
class WitnessBudgetRequest:
    """A probability target, single-batch cap, and prespecified fresh seed pairs."""

    target_probability: float
    max_shots: int = MAX_CONSTRAINT_SNAPSHOT_SHOTS
    validation_seeds: Sequence[int] = DEFAULT_WITNESS_VALIDATION_SEEDS
    stop_on_futility: bool = False

    def __post_init__(self) -> None:
        _validate_budget(self.target_probability, self.max_shots)
        if not isinstance(self.stop_on_futility, bool):
            raise TypeError("stop_on_futility must be a boolean")
        object.__setattr__(self, "validation_seeds", _normalize_validation_seeds(self.validation_seeds))


def plan_witness_budget(
    lower_hit_rate: float | None, *, target_probability: float, max_shots: int,
) -> dict[str, Any]:
    """Plan the smallest capped batch using a conservative per-shot rate."""
    _validate_budget(target_probability, max_shots)
    if lower_hit_rate is not None and (
        isinstance(lower_hit_rate, bool) or not isinstance(lower_hit_rate, (int, float))
        or not 0 <= lower_hit_rate <= 1
    ):
        raise ValueError("lower_hit_rate must be between zero and one")
    report: dict[str, Any] = {
        "status": "UNASSESSED",
        "lower_hit_rate": lower_hit_rate,
        "target_probability": target_probability,
        "max_shots": max_shots,
        "shots_required": None,
        "success_probability_at_cap": None,
        "success_probability_at_budget": None,
    }
    if lower_hit_rate is None:
        return report
    if lower_hit_rate == 0:
        report.update(status="NO_POSITIVE_LOWER_BOUND", success_probability_at_cap=0.0)
        return report
    if lower_hit_rate == 1:
        report.update(status="PLANNED", shots_required=1, success_probability_at_cap=1.0,
                      success_probability_at_budget=1.0)
        return report

    failure = 1 - Fraction(lower_hit_rate)
    target_failure = 1 - Fraction(target_probability)
    failure_at_cap = failure ** max_shots
    report["success_probability_at_cap"] = float(1 - failure_at_cap)
    if failure_at_cap > target_failure:
        report["status"] = "BUDGET_EXCEEDED"
        return report

    # Compare exact binary-float inputs as fractions at integer shot counts.
    low, high = 1, max_shots
    while low < high:
        middle = (low + high) // 2
        if failure ** middle <= target_failure:
            high = middle
        else:
            low = middle + 1
    report.update(status="PLANNED", shots_required=low, success_probability_at_budget=float(1 - failure ** low))
    return report


def _witness_lower_bound(
    successful_batches: int, planned_batches: int, *, confidence_level: float = WITNESS_BATCH_CONFIDENCE_PER_COVER,
) -> float:
    from scipy.stats import binomtest

    interval = binomtest(successful_batches, planned_batches, alternative="greater").proportion_ci(
        confidence_level=confidence_level, method="exact",
    )
    return float(interval.low)


def _minimum_successful_witness_batches(
    batches: int, target_probability: float, *, confidence_level: float,
) -> int | None:
    if _witness_lower_bound(batches, batches, confidence_level=confidence_level) < target_probability:
        return None
    low, high = 1, batches
    while low < high:
        middle = (low + high) // 2
        if _witness_lower_bound(middle, batches, confidence_level=confidence_level) >= target_probability:
            high = middle
        else:
            low = middle + 1
    return low


def _witness_target_support_probability(
    batches: int, required_successes: int | None, assumed_success_probability: Fraction,
) -> Fraction:
    """Evaluate the binomial upper tail exactly for a rational batch probability."""
    if required_successes is None:
        return Fraction(0)
    if required_successes == 1:
        return 1 - (1 - assumed_success_probability)**batches
    if required_successes == batches:
        return assumed_success_probability**batches
    successes, denominator = assumed_success_probability.as_integer_ratio()
    failures = denominator - successes
    use_complement = required_successes <= (batches + 1) // 2
    counts = range(required_successes) if use_complement else range(required_successes, batches + 1)
    numerator = sum(comb(batches, hits) * successes**hits * failures**(batches - hits) for hits in counts)
    tail = Fraction(numerator, denominator**batches)
    return 1 - tail if use_complement else tail


def _assess_witness_validation_power(
    batches: int, required_successes: int | None, batch_probability: Fraction, target: Fraction,
) -> tuple[float, bool]:
    if required_successes is None:
        return 0.0, False

    # The modeled input is dyadic. Bound a long numerator with shorter
    # dyadics before evaluating the monotone tail, which raises it to batch-sized powers.
    numerator, denominator = batch_probability.as_integer_ratio()
    shift = numerator.bit_length() - 96
    if shift > 0:
        lower_numerator, short_denominator = numerator >> shift, denominator >> shift
        lower = _witness_target_support_probability(
            batches, required_successes, Fraction(lower_numerator, short_denominator),
        )
        upper = _witness_target_support_probability(
            batches, required_successes, Fraction(lower_numerator + 1, short_denominator),
        )
        # A reduced dyadic numerator is odd, so a truncated probability is
        # strictly below the upper endpoint. The binomial tail is strictly increasing.
        displayed = float(lower)
        if displayed == float(upper) and (lower >= target or upper <= target):
            return displayed, lower >= target

    power = _witness_target_support_probability(batches, required_successes, batch_probability)
    return float(power), power >= target


def _apply_witness_validation_power_budget(
    budget: dict[str, Any], *, hit_probability: float, batches: int, required_successes: int | None,
    minimum_validation_power: float,
) -> None:
    failure = 1 - Fraction(hit_probability)
    target = Fraction(minimum_validation_power)

    def assess(shots: int) -> tuple[float, float, bool]:
        batch_probability = 1 - failure**shots
        power, meets_target = _assess_witness_validation_power(batches, required_successes, batch_probability, target)
        return float(batch_probability), power, meets_target

    cap = budget["max_shots"]
    best_probability, best_power, cap_meets_target = assess(cap)
    budget.update(
        validation_power_at_cap=best_power, validation_power_at_budget=None,
        meets_minimum_validation_power_at_cap=cap_meets_target,
    )
    minimum_shots = budget["shots_required"]
    if minimum_shots is None:
        return
    if not cap_meets_target:
        status = "VALIDATION_SCHEDULE_TOO_SHORT" if required_successes is None else "VALIDATION_POWER_CAP_EXCEEDED"
        budget.update(status=status, shots_required=None, success_probability_at_budget=None)
        return

    # The one-batch minimum is already known. Holdout power increases with shots.
    low, high = minimum_shots, cap
    while low < high:
        middle = (low + high) // 2
        probability, power, meets_target = assess(middle)
        if meets_target:
            high = middle
            best_probability, best_power = probability, power
        else:
            low = middle + 1
    budget.update(shots_required=low, success_probability_at_budget=best_probability, validation_power_at_budget=best_power)


def _witness_futility_stop(
    seeds: Sequence[int], attempted_batches: int, misses: int, target_probability: float,
    *, confidence_level: float = WITNESS_BATCH_CONFIDENCE_PER_COVER,
) -> dict[str, Any] | None:
    # With the same planned denominator, treating every remaining
    # seed as successful gives the largest possible final lower bound.
    best_hits = len(seeds) - misses
    best_lower = _witness_lower_bound(best_hits, len(seeds), confidence_level=confidence_level)
    if best_lower >= target_probability:
        return None
    return {
        "observed_failed_batches": misses,
        "best_case_successful_batches": best_hits,
        "best_case_lower_success_probability": best_lower,
        "skipped_seeds": list(seeds[attempted_batches:]),
    }


def _minimum_witness_schedule(
    target_probability: float, tolerated_misses: int, cap: int, *, confidence_level: float, cap_label: str,
) -> tuple[int | None, float, float | None]:
    _validate_target_probability(target_probability)
    _bounded_int(tolerated_misses, 0, MAX_WITNESS_BATCHES_PER_COVER, "tolerated_misses")
    _bounded_int(cap, 1, MAX_WITNESS_BATCHES_PER_COVER, cap_label)

    def lower_bound(batches: int) -> float:
        return _witness_lower_bound(max(0, batches - tolerated_misses), batches, confidence_level=confidence_level)

    cap_bound = lower_bound(cap)
    if cap_bound < target_probability:
        return None, cap_bound, None

    # A positive target requires at least one success. At a fixed miss
    # allowance, adding one successful batch raises the lower bound.
    low, high = tolerated_misses + 1, cap
    while low < high:
        middle = (low + high) // 2
        if lower_bound(middle) >= target_probability:
            high = middle
        else:
            low = middle + 1
    return low, cap_bound, cap_bound if low == cap else lower_bound(low)


def plan_witness_schedule(
    *, target_probability: float, tolerated_misses: int = 0,
    max_batches_per_cover: int = MAX_WITNESS_BATCHES_PER_COVER,
) -> dict[str, Any]:
    """Find the smallest phase-cover schedule that supports the target at the miss allowance."""
    minimum, cap_bound, minimum_bound = _minimum_witness_schedule(
        target_probability, tolerated_misses, max_batches_per_cover,
        confidence_level=WITNESS_BATCH_CONFIDENCE_PER_COVER, cap_label="max_batches_per_cover",
    )
    return {
        "status": "BATCH_CAP_EXCEEDED" if minimum is None else "PLANNED",
        "target_probability": target_probability,
        "tolerated_misses": tolerated_misses,
        "max_batches_per_cover": max_batches_per_cover,
        "batch_confidence_per_cover": WITNESS_BATCH_CONFIDENCE_PER_COVER,
        "lower_success_probability_at_cap": cap_bound,
        "minimum_batches_per_cover": minimum,
        "minimum_validation_seeds": None if minimum is None else 2 * minimum,
        "lower_success_probability_at_minimum": minimum_bound,
    }


def _check_witness_schedule_size(
    seed_count: int, required_seeds: int | None, *, cap: int, tolerated_misses: int, confidence_level: float,
) -> dict[str, Any]:
    batches = seed_count // 2
    additional_seeds = None
    if batches > cap:
        status = "SCHEDULE_EXCEEDS_CAP"
    elif required_seeds is None:
        status = "BATCH_CAP_EXCEEDED"
    else:
        additional_seeds = max(0, required_seeds - seed_count)
        status = "SCHEDULE_TOO_SHORT" if additional_seeds else "SCHEDULE_FEASIBLE"
    return {
        "status": status,
        "additional_validation_seeds": additional_seeds,
        "lower_success_probability_at_proposed_size": _witness_lower_bound(
            max(0, batches - tolerated_misses), batches, confidence_level=confidence_level,
        ),
    }


def check_witness_schedule(
    *, target_probability: float, validation_seeds: Sequence[int],
    evaluation_seeds: Sequence[int] = DEFAULT_COVER_EVALUATION_SEEDS, tolerated_misses: int = 0,
    max_batches_per_cover: int = MAX_WITNESS_BATCHES_PER_COVER,
) -> dict[str, Any]:
    """Check a proposed schedule against runtime seed rules and a hypothetical confidence target."""
    calibration = _normalize_evaluation_seeds(evaluation_seeds)
    holdout = _normalize_validation_seeds(validation_seeds)
    _validate_seed_overlap(holdout, calibration)
    plan = plan_witness_schedule(
        target_probability=target_probability, tolerated_misses=tolerated_misses,
        max_batches_per_cover=max_batches_per_cover,
    )
    assessment = _check_witness_schedule_size(
        len(holdout), plan["minimum_validation_seeds"], cap=max_batches_per_cover,
        tolerated_misses=tolerated_misses, confidence_level=WITNESS_BATCH_CONFIDENCE_PER_COVER,
    )
    plan["proposed_schedule"] = {
        "status": assessment.pop("status"),
        "evaluation_seeds": _split_cover_seeds(calibration),
        "validation_seed_schedule": _split_cover_seeds(holdout),
        "validation_batches_per_cover": len(holdout) // 2,
        **assessment,
    }
    return plan


def summarize_witness_batches(
    batches: Sequence[dict[str, Any]], *, planned_batches: int, target_probability: float,
    confidence_level: float = WITNESS_BATCH_CONFIDENCE_PER_COVER,
) -> dict[str, Any]:
    """Assess independent batch outcomes only after the prespecified run completes."""
    _bounded_int(planned_batches, 1, MAX_WITNESS_BATCHES_PER_COVER, "planned_batches")
    _validate_budget(target_probability, 1)
    if (
        isinstance(confidence_level, bool) or not isinstance(confidence_level, (int, float))
        or not 0 < confidence_level < 1
    ):
        raise ValueError("confidence_level must be strictly between zero and one")
    if len(batches) > planned_batches:
        raise ValueError("completed and attempted witness batches exceed the seed plan")
    simulations = [batch["simulation"] for batch in batches if batch["status"] == "COMPLETED"]
    completed = len(simulations)
    hits = sum(simulation["verified_shots"] > 0 for simulation in simulations)
    report = {
        "status": "INCOMPLETE",
        "planned_batches": planned_batches,
        "completed_batches": completed,
        "successful_batches": hits,
        "observed_success_fraction": hits / completed if completed else None,
        "lower_success_probability": None,
        "completed_shots": sum(simulation["completed_shots"] for simulation in simulations),
    }
    if completed == planned_batches:
        lower = _witness_lower_bound(hits, planned_batches, confidence_level=confidence_level)
        report["lower_success_probability"] = lower
        report["status"] = "TARGET_SUPPORTED" if lower >= target_probability else "UNRESOLVED"
    return report

