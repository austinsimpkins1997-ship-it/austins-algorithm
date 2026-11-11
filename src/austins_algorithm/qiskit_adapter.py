"""Optional Qiskit circuits for Grover search and ChronoRealm's 3-qubit example.

Import Qiskit lazily so the base package stays dependency-free. Local simulation
and target compilation are explicit; this module never authenticates or submits
a provider job.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .core import austin_predicate


MAX_LOCAL_SIMULATION_QUBITS = 16
MAX_LOCAL_SHOTS = 100_000


def _validate_circuit_inputs(n_qubits: int, marked_states: Iterable[str]) -> tuple[str, ...]:
    if isinstance(n_qubits, bool) or not isinstance(n_qubits, int) or n_qubits < 1:
        raise ValueError("n_qubits must be a positive integer")
    if isinstance(marked_states, (str, bytes)):
        raise ValueError("marked_states must be a collection of bitstrings, not one string")
    try:
        states = tuple(marked_states)
    except TypeError as exc:
        raise ValueError("marked_states must be an iterable of bitstrings") from exc
    for state in states:
        if not isinstance(state, str) or len(state) != n_qubits or any(bit not in "01" for bit in state):
            raise ValueError(f"Each marked state must be exactly {n_qubits} binary digits")
    if len(set(states)) != len(states):
        raise ValueError("marked_states must not contain duplicates")
    return tuple(sorted(states))


def first_peak_iterations(n_qubits: int, marked_count: int) -> int:
    """Estimate the nearest first success peak when the solution count is known."""
    if isinstance(n_qubits, bool) or not isinstance(n_qubits, int) or n_qubits < 1:
        raise ValueError("n_qubits must be a positive integer")
    total = 1 << n_qubits
    if isinstance(marked_count, bool) or not isinstance(marked_count, int) or not 0 <= marked_count <= total:
        raise ValueError(f"marked_count must be an integer from 0 to {total}")
    if marked_count == 0 or marked_count * 2 >= total:
        return 0
    theta = math.asin(math.sqrt(marked_count / total))
    return max(0, math.floor(math.pi / (4 * theta) + 1e-12))


def theoretical_success_probability(n_qubits: int, marked_count: int, iterations: int) -> float:
    if isinstance(n_qubits, bool) or not isinstance(n_qubits, int) or n_qubits < 1:
        raise ValueError("n_qubits must be a positive integer")
    if isinstance(marked_count, bool) or not isinstance(marked_count, int):
        raise ValueError("marked_count must be an integer")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")
    total = 1 << n_qubits
    if marked_count < 0 or marked_count > total:
        raise ValueError("marked_count is outside the search space")
    if marked_count == 0:
        return 0.0
    if marked_count == total:
        return 1.0
    theta = math.asin(math.sqrt(marked_count / total))
    return math.sin((2 * iterations + 1) * theta) ** 2


def build_phase_oracle(n_qubits: int, marked_states: Iterable[str]):
    """Build an exact phase-flip oracle for explicit MSB-first bitstrings.

    A truth-table list is convenient for teaching but may make an expensive
    circuit. It does not imply an efficient domain oracle.
    """
    states = _validate_circuit_inputs(n_qubits, marked_states)
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import MCMTGate, ZGate

    oracle = QuantumCircuit(n_qubits, name="phase_oracle")
    for state in states:
        for qubit in range(n_qubits):
            if state[n_qubits - 1 - qubit] == "0":
                oracle.x(qubit)
        if n_qubits == 1:
            oracle.z(0)
        else:
            oracle.append(MCMTGate(ZGate(), n_qubits - 1, 1), list(range(n_qubits)))
        for qubit in range(n_qubits):
            if state[n_qubits - 1 - qubit] == "0":
                oracle.x(qubit)
    return oracle


def build_grover_circuit(
    n_qubits: int,
    marked_states: Iterable[str],
    *,
    iterations: int | None = None,
):
    """Build a measured Qiskit Grover circuit for a known explicit marked set."""
    states = _validate_circuit_inputs(n_qubits, marked_states)
    if iterations is None:
        iterations = first_peak_iterations(n_qubits, len(states))
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
    from qiskit.circuit.library import grover_operator

    oracle = build_phase_oracle(n_qubits, states)
    grover = grover_operator(oracle=oracle)
    q = QuantumRegister(n_qubits, "q")
    answer = ClassicalRegister(n_qubits, "answer")
    circuit = QuantumCircuit(q, answer)
    circuit.h(range(n_qubits))
    for _ in range(iterations):
        circuit.compose(grover, qubits=range(n_qubits), inplace=True)
    circuit.measure(range(n_qubits), range(n_qubits))
    return circuit


def build_austin_circuit(*, iterations: int = 1):
    """Build the project-defined 3-qubit predicate circuit with a two-CZ oracle."""
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

    q = QuantumRegister(3, "q")
    answer = ClassicalRegister(3, "answer")
    circuit = QuantumCircuit(q, answer)
    circuit.h(q)
    for _ in range(iterations):
        circuit.cz(q[0], q[1])
        circuit.cz(q[0], q[2])
        circuit.h(q)
        circuit.x(q)
        circuit.h(q[2])
        circuit.ccx(q[0], q[1], q[2])
        circuit.h(q[2])
        circuit.x(q)
        circuit.h(q)
    circuit.measure(q, answer)
    return circuit


def run_austin_circuit_local_statevector(
    *,
    iterations: int = 1,
    shots: int = 2048,
    seed: int = 20261109,
) -> dict[str, Any]:
    """Sample Austin's actual reversible Qiskit circuit on a local ideal simulator.

    Unlike :func:`run_local_statevector`, this path does not rebuild the oracle
    from a caller-supplied truth table: it runs ``build_austin_circuit`` with
    the project's two-CZ phase oracle and diffuser. The execution is still a
    classical statevector simulation, never a provider or hardware job.
    """
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 0 <= iterations <= 256:
        raise ValueError("iterations must be an integer from 0 to 256")
    if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= MAX_LOCAL_SHOTS:
        raise ValueError(f"shots must be an integer from 1 to {MAX_LOCAL_SHOTS}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")

    circuit = build_austin_circuit(iterations=iterations)
    from qiskit.primitives import StatevectorSampler

    counts = StatevectorSampler(seed=seed).run([circuit], shots=shots).result()[0].data.answer.get_counts()
    if sum(counts.values()) != shots:
        raise RuntimeError("sampler returned an unexpected shot total")
    if any(len(state) != 3 or any(bit not in "01" for bit in state) for state in counts):
        raise RuntimeError("sampler returned a bitstring outside the three-qubit basis")
    marked_states = ("011", "101")
    verified_candidates = sorted(state for state in counts if austin_predicate(state))
    verified_shots = sum(counts[state] for state in verified_candidates)
    return {
        "status": "LOCAL_IDEAL_STATEVECTOR_SIMULATION",
        "execution_mode": "SIMULATOR",
        "circuit": "AUSTIN_3Q_TWO_CZ_CONSTRAINT_SEARCH",
        "n_qubits": 3,
        "basis_order": "q2 q1 q0",
        "marked_states": list(marked_states),
        "marked_count": len(marked_states),
        "iterations": iterations,
        "theoretical_success_probability": theoretical_success_probability(3, len(marked_states), iterations),
        "shots": shots,
        "seed": seed,
        "counts": dict(sorted(counts.items())),
        "verified_candidates": verified_candidates,
        "verified_shots": verified_shots,
        "observed_success_fraction": verified_shots / shots,
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
    }


def run_local_statevector(
    n_qubits: int,
    marked_states: Iterable[str],
    *,
    iterations: int | None = None,
    shots: int = 2048,
    seed: int = 20261109,
) -> dict[str, Any]:
    """Run Qiskit's local ideal StatevectorSampler; this is not hardware evidence."""
    states = _validate_circuit_inputs(n_qubits, marked_states)
    if n_qubits > MAX_LOCAL_SIMULATION_QUBITS:
        raise ValueError(f"local simulation is capped at {MAX_LOCAL_SIMULATION_QUBITS} qubits")
    if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= MAX_LOCAL_SHOTS:
        raise ValueError(f"shots must be an integer from 1 to {MAX_LOCAL_SHOTS}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if iterations is None:
        iterations = first_peak_iterations(n_qubits, len(states))
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 0 <= iterations <= 256:
        raise ValueError("local simulation iterations must be an integer from 0 to 256")
    circuit = build_grover_circuit(n_qubits, states, iterations=iterations)
    from qiskit.primitives import StatevectorSampler

    counts = StatevectorSampler(seed=seed).run([circuit], shots=shots).result()[0].data.answer.get_counts()
    if sum(counts.values()) != shots:
        raise RuntimeError("sampler returned an unexpected shot total")
    marked = set(states)
    successful = sum(count for state, count in counts.items() if state in marked)
    return {
        "status": "LOCAL_IDEAL_STATEVECTOR_SIMULATION",
        "n_qubits": n_qubits,
        "marked_states": list(states),
        "marked_count": len(states),
        "iterations": iterations,
        "theoretical_success_probability": theoretical_success_probability(n_qubits, len(states), iterations),
        "shots": shots,
        "seed": seed,
        "counts": dict(sorted(counts.items())),
        "observed_success_fraction": successful / shots,
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
    }


def transpile_for_backend(circuit: Any, backend: Any, *, optimization_level: int = 1, seed: int = 20261109):
    """Compile locally for a supplied Qiskit target; this never submits a job."""
    if backend is None:
        raise ValueError("a configured backend or target is required")
    if isinstance(optimization_level, bool) or not isinstance(optimization_level, int) or optimization_level not in range(4):
        raise ValueError("optimization_level must be an integer from 0 to 3")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    from qiskit import transpile

    return transpile(circuit, backend=backend, optimization_level=optimization_level, seed_transpiler=seed)
