"""Optional Cirq circuit for the project-defined three-qubit example."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any


def classical_solutions() -> tuple[str, ...]:
    """Return the q2q1q0 display strings satisfying q0=1 and q1!=q2."""
    return tuple(
        format(value, "03b")
        for value in range(8)
        if value & 1 and ((value >> 1) & 1) != ((value >> 2) & 1)
    )


def build_austin_circuit(*, iterations: int = 1):
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")
    import cirq

    q = cirq.LineQubit.range(3)
    circuit = cirq.Circuit()
    circuit.append([cirq.H(q[index]) for index in range(3)])
    for _ in range(iterations):
        circuit.append([cirq.CZ(q[0], q[1]), cirq.CZ(q[0], q[2])])
        circuit.append([cirq.H(q[index]) for index in range(3)])
        circuit.append([cirq.X(q[index]) for index in range(3)])
        circuit.append([cirq.H(q[2]), cirq.TOFFOLI(q[0], q[1], q[2]), cirq.H(q[2])])
        circuit.append([cirq.X(q[index]) for index in range(3)])
        circuit.append([cirq.H(q[index]) for index in range(3)])
    circuit.append(cirq.measure(q[2], q[1], q[0], key="answer"))
    return circuit


def run_local(*, iterations: int = 1, shots: int = 2048, seed: int = 20261109) -> dict[str, Any]:
    if isinstance(shots, bool) or not isinstance(shots, int) or shots < 1:
        raise ValueError("shots must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    import cirq

    circuit = build_austin_circuit(iterations=iterations)
    result = cirq.Simulator(seed=seed).run(circuit, repetitions=shots)
    counts = Counter("".join(str(int(bit)) for bit in row) for row in result.measurements["answer"])
    theta = math.asin(math.sqrt(len(classical_solutions()) / 8))
    return {
        "status": "LOCAL_IDEAL_CIRQ_SIMULATION",
        "predicate": "q0 == 1 and q1 != q2",
        "bit_order": "output strings are q2 q1 q0",
        "iterations": iterations,
        "theoretical_success_probability": math.sin((2 * iterations + 1) * theta) ** 2,
        "shots": shots,
        "seed": seed,
        "counts": dict(sorted(counts.items())),
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
    }
