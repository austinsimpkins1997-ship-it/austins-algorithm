"""Bounded exact binomial intervals for offline placement verification."""

from __future__ import annotations

import math

from .core import _bounded_int


def _lower_binomial_bound(hits: int, shots: int, alpha: float) -> float:
    """Invert P(X >= hits) for the lower endpoint."""
    if hits == 0:
        return 0.0
    if hits == shots:
        return alpha ** (1 / shots)

    # The lower endpoint is below the observed fraction. In this bracket the
    # binomial masses in the upper tail decrease from the first term onward.
    log_coefficient = math.log(math.comb(shots, hits))
    low, high = 0.0, hits / shots
    for _ in range(52):
        probability = (low + high) / 2
        term = math.exp(
            log_coefficient + hits * math.log(probability)
            + (shots - hits) * math.log1p(-probability)
        )
        tail = term
        odds = probability / (1 - probability)
        for successes in range(hits, shots):
            term *= (shots - successes) / (successes + 1) * odds
            if term == 0.0:
                break
            tail += term
        if tail < alpha:
            low = probability
        else:
            high = probability
    return (low + high) / 2


def _placement_exact_interval(hits: int, shots: int, confidence: float) -> tuple[float, float]:
    """Compute a two-sided Clopperâ€“Pearson interval for a bounded count."""
    _bounded_int(shots, 1, 2048, "shots")
    _bounded_int(hits, 0, shots, "hits")
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not 0 < confidence < 1:
        raise ValueError("confidence must be strictly between zero and one")

    alpha = (1 - confidence) / 2
    lower = _lower_binomial_bound(hits, shots, alpha)
    upper = 1 - _lower_binomial_bound(shots - hits, shots, alpha)
    return lower, upper
