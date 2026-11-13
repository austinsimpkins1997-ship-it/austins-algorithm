"""Build and audit a reversible phase oracle for a weighted CNF threshold."""

from __future__ import annotations

from typing import Any

from .classical_constraint import _clause_masks
from .core import _bounded_int
from .optimization import _problem_parts, problem_from_document


def build_weighted_phase_oracle(
    problem: object, *, incumbent: int | None = None,
) -> tuple[Any, dict[str, int]]:
    """Mark feasible assignments that strictly improve the supplied incumbent.

    A decision tree rejects infeasible and over-budget prefixes. Fully marked
    subtrees become disjoint phase terms, leaving the search register intact.
    This bounded synthesis records tree work separately from circuit queries.
    """
    from qiskit import QuantumCircuit

    from .constraint_oracle import _cube_operation

    constraint, weights = _problem_parts(problem_from_document(problem))
    total = sum(weights)
    bound = total + 1 if incumbent is None else _bounded_int(incumbent, 0, total + 1, "Incumbent cost")
    suffix = [0] * (constraint.qubits + 1)
    for index in reversed(range(constraint.qubits)):
        suffix[index] = suffix[index + 1] + weights[index]
    clauses = _clause_masks(constraint)
    circuit = QuantumCircuit(constraint.qubits, name="weighted_threshold")
    counts = {"prefixes_checked": 0, "phase_terms": 0, "marked_assignments": 0}

    def descend(index: int, enabled: int, cost: int, cube: tuple[tuple[int, bool], ...]) -> None:
        counts["prefixes_checked"] += 1
        if cost >= bound:
            return
        assigned = (1 << index) - 1
        disabled = assigned ^ enabled
        satisfied = True
        for positive, negative in clauses:
            if positive & enabled or negative & disabled:
                continue
            if (positive | negative) & ~assigned == 0:
                return
            satisfied = False
        if satisfied and cost + suffix[index] < bound:
            _cube_operation(circuit, cube)
            counts["phase_terms"] += 1
            counts["marked_assignments"] += 1 << (constraint.qubits - index)
            return
        # At a leaf a feasible in-budget assignment always matched the case above.
        descend(index + 1, enabled, cost, cube + ((index, False),))
        descend(index + 1, enabled | (1 << index), cost + weights[index], cube + ((index, True),))

    descend(0, 0, 0, ())
    return circuit, counts
