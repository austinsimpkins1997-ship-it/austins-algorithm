"""Dependency-free, ideal classical statevector simulation for Grover search.

This module is deliberately separate from circuit construction. It enumerates
the supplied predicate on a classical CPU and must never be described as QPU
execution or a quantum speedup.
"""

from __future__ import annotations

import bisect
import math
import random
import time
from collections.abc import Callable, Iterable
from typing import Any


MIN_QUBITS = 1
MAX_SIMULATION_QUBITS = 16
MAX_ITERATIONS = 256
MAX_SHOTS = 100_000
ALGORITHM_NAME = "Austin's Algorithm (ChronoRealm Grover-based constraint-search prototype)"


class AustinAlgorithmError(ValueError):
    """Raised when an input is malformed or exceeds the portable demo limits."""


def _bounded_int(value: Any, lower: int, upper: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise AustinAlgorithmError(f"{label} must be an integer from {lower} to {upper}.")
    return value


def validate_marked_states(qubits: int, marked_states: Iterable[str]) -> tuple[str, ...]:
    """Validate MSB-first bitstrings for the portable simulator."""
    _bounded_int(qubits, MIN_QUBITS, MAX_SIMULATION_QUBITS, "qubits")
    if isinstance(marked_states, (str, bytes)):
        raise AustinAlgorithmError("marked_states must be a collection of bitstrings.")
    try:
        states = tuple(marked_states)
    except TypeError as exc:
        raise AustinAlgorithmError("marked_states must be an iterable of bitstrings.") from exc
    if len(states) > (1 << qubits):
        raise AustinAlgorithmError("marked_states cannot exceed the search-space size.")
    for state in states:
        if not isinstance(state, str) or len(state) != qubits or any(bit not in "01" for bit in state):
            raise AustinAlgorithmError(f"Each marked state must be exactly {qubits} binary digits.")
    if len(set(states)) != len(states):
        raise AustinAlgorithmError("marked_states must not contain duplicates.")
    return tuple(sorted(states))


def recommended_iterations(qubits: int, solution_count: int) -> int:
    """Return the nearest first-peak iteration count when M is known.

    The target is the nearest integer to pi/(4*theta) - 1/2, where
    sin(theta)^2 = M / 2**qubits. For non-boundary values this equals
    floor(pi/(4*theta)); adding iterations blindly can overshoot the peak.
    """
    _bounded_int(qubits, MIN_QUBITS, MAX_SIMULATION_QUBITS, "qubits")
    total = 1 << qubits
    _bounded_int(solution_count, 0, total, "solution_count")
    if solution_count == 0 or solution_count * 2 >= total:
        return 0
    theta = math.asin(math.sqrt(solution_count / total))
    return max(0, math.floor(math.pi / (4 * theta) + 1e-12))


def theoretical_success_probability(qubits: int, solution_count: int, iterations: int) -> float:
    """Ideal Grover success probability for known N, M, and iteration count."""
    _bounded_int(qubits, MIN_QUBITS, MAX_SIMULATION_QUBITS, "qubits")
    total = 1 << qubits
    _bounded_int(solution_count, 0, total, "solution_count")
    _bounded_int(iterations, 0, MAX_ITERATIONS, "iterations")
    if solution_count == 0:
        return 0.0
    if solution_count == total:
        return 1.0
    theta = math.asin(math.sqrt(solution_count / total))
    return math.sin((2 * iterations + 1) * theta) ** 2


def austin_predicate(bits: str) -> bool:
    """ChronoRealm's example predicate: q0=1 and q1 != q2.

    Display strings are MSB-first as q2 q1 q0; therefore q0 is ``bits[-1]``.
    """
    return len(bits) == 3 and bits[-1] == "1" and bits[-2] != bits[-3]


def run_austin_search(
    qubits: int,
    predicate: Callable[[str], bool],
    *,
    iterations: int | None = None,
    shots: int = 2048,
    seed: int = 20261109,
    predicate_name: str | None = None,
) -> dict[str, Any]:
    """Enumerate a Boolean predicate and simulate ideal Grover state evolution.

    This classical reference evaluates every input first. A real quantum
    implementation needs an efficient reversible oracle, not this callable.
    """
    _bounded_int(qubits, MIN_QUBITS, MAX_SIMULATION_QUBITS, "qubits")
    if not callable(predicate):
        raise AustinAlgorithmError("predicate must be a callable that returns bool.")
    _bounded_int(shots, 1, MAX_SHOTS, "shots")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AustinAlgorithmError("seed must be an integer.")

    started = time.perf_counter()
    total = 1 << qubits
    marked_indices: list[int] = []
    marked_examples: list[str] = []
    for index in range(total):
        bitstring = format(index, f"0{qubits}b")
        result = predicate(bitstring)
        if not isinstance(result, bool):
            raise AustinAlgorithmError("predicate must return bool for every input bitstring.")
        if result:
            marked_indices.append(index)
            if len(marked_examples) < 64:
                marked_examples.append(bitstring)

    marked_count = len(marked_indices)
    suggested = recommended_iterations(qubits, marked_count)
    actual_iterations = suggested if iterations is None else _bounded_int(
        iterations, 0, MAX_ITERATIONS, "iterations"
    )

    amplitude = 1.0 / math.sqrt(total)
    state = [complex(amplitude, 0.0) for _ in range(total)]
    for _ in range(actual_iterations):
        for index in marked_indices:
            state[index] = -state[index]
        mean = sum(state) / total
        state = [2 * mean - value for value in state]

    probabilities = [abs(value) ** 2 for value in state]
    normalization = sum(probabilities)
    success_probability = sum(probabilities[index] for index in marked_indices)
    theoretical_probability = theoretical_success_probability(qubits, marked_count, actual_iterations)
    if abs(normalization - 1.0) > 1e-9:
        raise RuntimeError("Statevector normalization check failed.")

    cumulative: list[float] = []
    running = 0.0
    for probability in probabilities:
        running += probability
        cumulative.append(running)
    cumulative[-1] = 1.0

    rng = random.Random(seed)
    counts: dict[str, int] = {}
    marked_lookup = set(marked_indices)
    successful_shots = 0
    for _ in range(shots):
        index = min(bisect.bisect_left(cumulative, rng.random()), total - 1)
        bitstring = format(index, f"0{qubits}b")
        counts[bitstring] = counts.get(bitstring, 0) + 1
        successful_shots += index in marked_lookup

    return {
        "schema": "ChronoRealmAustinAlgorithmPCResult/1",
        "algorithm": ALGORITHM_NAME,
        "status": "CLASSICAL_IDEAL_STATEVECTOR_SIMULATION",
        "predicate": predicate_name or getattr(predicate, "__name__", "user_predicate"),
        "qubits": qubits,
        "basis_order": "MSB-first display bitstrings; basis index is the displayed binary integer",
        "search_space_size": total,
        "solution_count_known_by_classical_enumeration": marked_count,
        "solution_examples": marked_examples,
        "solution_examples_truncated": marked_count > len(marked_examples),
        "recommended_iterations_given_known_solution_count": suggested,
        "iterations": actual_iterations,
        "theoretical_success_probability": theoretical_probability,
        "statevector_success_probability": success_probability,
        "normalization_sum": normalization,
        "shots": shots,
        "seed": seed,
        "sample_counts": dict(sorted(counts.items())),
        "observed_success_fraction": successful_shots / shots,
        "runtime_seconds": time.perf_counter() - started,
        "resource_estimate": {
            "basis_states": total,
            "raw_complex128_statevector_bytes_lower_bound": 16 * total,
            "python_object_overhead_included": False,
            "statevector_memory_growth": "O(2^qubits)",
            "simulation_time_growth": "O((iterations + 1) * 2^qubits) plus predicate cost",
        },
        "execution": {
            "quantum_hardware_used": False,
            "gpu_used": False,
            "external_network_used": False,
            "model_invoked": False,
            "persisted": False,
        },
        "limits": [
            "This is an ideal classical statevector simulation, not quantum hardware or a quantum speedup.",
            "The predicate is enumerated classically over the full search space to construct the simulator oracle.",
            "A hardware implementation needs an efficient reversible circuit for the real predicate.",
            "The 16-qubit cap is for this portable simulator only, not a quantum-device limit.",
            "Raw complex128 bytes are a lower bound; Python objects and temporary vectors use more memory.",
            "Statevector amplitudes are not tomography and do not recover an unknown physical state.",
        ],
    }
