"""Conservative bounded budgets for at least one verified simulator witness."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .core import _bounded_int
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS


DEFAULT_WITNESS_VALIDATION_SEEDS = (233, 239)
MAX_WITNESS_BATCHES_PER_COVER = 128
WITNESS_BATCH_CONFIDENCE_PER_COVER = 0.975


def _validate_budget(target_probability: float, max_shots: int) -> None:
    if (
        isinstance(target_probability, bool) or not isinstance(target_probability, (int, float))
        or not 0 < target_probability < 1
    ):
        raise ValueError("target_probability must be strictly between zero and one")
    _bounded_int(max_shots, 1, MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_shots")


@dataclass(frozen=True)
class WitnessBudgetRequest:
    """A probability target, single-batch cap, and prespecified fresh seed pairs."""

    target_probability: float
    max_shots: int = MAX_CONSTRAINT_SNAPSHOT_SHOTS
    validation_seeds: Sequence[int] = DEFAULT_WITNESS_VALIDATION_SEEDS

    def __post_init__(self) -> None:
        _validate_budget(self.target_probability, self.max_shots)
        if (
            not isinstance(self.validation_seeds, Sequence)
            or not 2 <= len(self.validation_seeds) <= 2 * MAX_WITNESS_BATCHES_PER_COVER
            or len(self.validation_seeds) % 2
        ):
            raise ValueError(
                f"witness validation requires an even number of seeds from 2 to {2 * MAX_WITNESS_BATCHES_PER_COVER}"
            )
        seeds = tuple(self.validation_seeds)
        for seed in seeds:
            _bounded_int(seed, 0, 2**32 - 1, "validation seed")
        if len(set(seeds)) != len(seeds):
            raise ValueError("witness validation seeds must be distinct")
        object.__setattr__(self, "validation_seeds", seeds)


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


def summarize_witness_batches(
    batches: Sequence[dict[str, Any]], *, planned_batches: int, target_probability: float,
) -> dict[str, Any]:
    """Assess independent batch outcomes only after the prespecified run completes."""
    _bounded_int(planned_batches, 1, MAX_WITNESS_BATCHES_PER_COVER, "planned_batches")
    _validate_budget(target_probability, 1)
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
        from scipy.stats import binomtest

        interval = binomtest(hits, completed, alternative="greater").proportion_ci(
            confidence_level=WITNESS_BATCH_CONFIDENCE_PER_COVER, method="exact",
        )
        lower = float(interval.low)
        report["lower_success_probability"] = lower
        report["status"] = "TARGET_SUPPORTED" if lower >= target_probability else "UNRESOLVED"
    return report

