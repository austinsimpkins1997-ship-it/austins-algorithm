"""Describe measurement support in standard, uniform-start Grover search."""

from __future__ import annotations


def ideal_grover_outcomes(qubits: int, solution_count: int, iterations: int) -> dict[str, str | int]:
    """Classify the mathematical distribution beyond floating-point rounding.

    This describes validated inputs to a uniform-start circuit with the
    standard phase oracle and diffuser. It does not classify a histogram.
    """
    total = 1 << qubits
    # When sin²((2r+1)θ) is 0 or 1, θ is a rational multiple of π.
    # The rational cosine theorem restricts cos(2θ) = 1 - 2M/N to
    # -1, -1/2, 0, 1/2, 1. An odd multiplier cannot zero or saturate
    # the M/N = 1/2 case. For 1/4 and 3/4 it must be divisible by 3.
    special_round = iterations % 3 == 1
    if solution_count == total or (4 * solution_count == total and special_round):
        validity, possible = "GUARANTEED", solution_count
    elif solution_count == 0 or (4 * solution_count == 3 * total and special_round):
        validity, possible = "IMPOSSIBLE", total - solution_count
    else:
        validity, possible = "PROBABILISTIC", total
    return {
        "ideal_validity": validity,
        "ideal_outcome": "SINGLE" if possible == 1 else "MULTIPLE",
        "ideal_possible_outcomes": possible,
    }
