"""Find the least unsigned witness for a bounded Boolean constraint."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from .constraints import BooleanConstraint


CLASSICAL_METHODS = ("scan", "propagation")


def _scan(constraint: BooleanConstraint) -> dict[str, Any]:
    assignment_count = 1 << constraint.qubits
    bit_format = f"0{constraint.qubits}b"
    for value in range(assignment_count):
        bits = format(value, bit_format)
        if constraint.matches(bits):
            return {"candidate": bits, "assignments_checked": value + 1}
    return {"candidate": None, "assignments_checked": assignment_count}


def _clause_masks(constraint: BooleanConstraint) -> tuple[tuple[int, int], ...]:
    """Represent positive and negative literals as separate variable masks."""
    masks = []
    for clause in constraint.clauses:
        positive = negative = 0
        for literal in clause:
            bit = 1 << (abs(literal) - 1)
            if literal > 0:
                positive |= bit
            else:
                negative |= bit
        if not positive & negative:
            # Overlap makes a clause always true. An empty clause keeps both masks zero.
            masks.append((positive, negative))
    return tuple(dict.fromkeys(masks))


def _unit_closure(
    clauses: tuple[tuple[int, int], ...], assigned: int, ones: int,
) -> tuple[int, int, int | None]:
    """Return updated masks and unresolved bits. The third value is None on conflict."""
    while True:
        zeros = assigned ^ ones
        remaining = forced_zeros = forced_ones = 0
        for positive, negative in clauses:
            if positive & ones or negative & zeros:
                continue
            unassigned = (positive | negative) & ~assigned
            if not unassigned:
                return assigned, ones, None
            remaining |= unassigned
            if unassigned.bit_count() == 1:
                forced_ones |= unassigned & positive
                forced_zeros |= unassigned & negative
        if forced_zeros & forced_ones:
            return assigned, ones, None
        implications = forced_zeros | forced_ones
        if not implications:
            return assigned, ones, remaining
        assigned |= implications
        ones |= forced_ones


@dataclass(frozen=True)
class UnitReduction:
    """An equivalent CNF and the root-forced literals it retains."""

    constraint: BooleanConstraint
    forced_literals: tuple[int, ...]
    contradiction: bool


def _signed_literals(positive: int, negative: int, width: int) -> tuple[int, ...]:
    literals = []
    for index in range(width):
        bit = 1 << index
        if positive & bit:
            literals.append(index + 1)
        elif negative & bit:
            literals.append(-(index + 1))
    return tuple(literals)


def reduce_constraint_units(constraint: BooleanConstraint) -> UnitReduction:
    """Substitute root-forced literals while preserving their original unit clauses."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    clauses = _clause_masks(constraint)
    assigned, ones, remaining = _unit_closure(clauses, 0, 0)
    if remaining is None:
        return UnitReduction(BooleanConstraint(constraint.qubits, ((),)), (), True)

    zeros = assigned ^ ones
    forced = _signed_literals(ones, zeros, constraint.qubits)
    residual = dict.fromkeys(
        _signed_literals(positive & ~assigned, negative & ~assigned, constraint.qubits)
        for positive, negative in clauses
        if not (positive & ones or negative & zeros)
    )
    # Retaining each forced bit as a unit rejects inputs outside the simplified subspace.
    units = tuple((literal,) for literal in forced)
    reduced = BooleanConstraint(constraint.qubits, units + tuple(residual))
    return UnitReduction(reduced, forced, False)


def _propagate(constraint: BooleanConstraint) -> dict[str, Any]:
    clauses = _clause_masks(constraint)
    nodes = forced = conflicts = 0

    def search(assigned: int, ones: int) -> int | None:
        nonlocal nodes, forced, conflicts
        nodes += 1
        before = assigned
        assigned, ones, remaining = _unit_closure(clauses, assigned, ones)
        forced += (assigned ^ before).bit_count()
        if remaining is None:
            conflicts += 1
            return None
        if not remaining:
            return ones

        # The highest unresolved bit defines the next unsigned prefix.
        # Bits absent from the residual clauses can stay zero.
        branch = 1 << (remaining.bit_length() - 1)
        first = search(assigned | branch, ones)
        if first is not None:
            return first
        return search(assigned | branch, ones | branch)

    result = search(0, 0)
    candidate = None if result is None else format(result, f"0{constraint.qubits}b")
    if candidate is not None and not constraint.matches(candidate):
        raise RuntimeError("unit propagation returned a witness that failed clause verification")
    return {
        "candidate": candidate,
        "nodes_visited": nodes,
        "forced_assignments": forced,
        "conflicts": conflicts,
    }


def classical_first_match(constraint: BooleanConstraint, *, method: str = "scan") -> dict[str, Any]:
    """Return the smallest matching bitstring or None if the constraint is unsatisfiable.

    The scan checks complete assignments in unsigned order. Propagation counts
    visited search nodes (including the root), applied forced assignments and
    pruned contradictory nodes. These counters measure different kinds of work.
    """
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if method not in CLASSICAL_METHODS:
        raise ValueError("classical method must be scan or propagation")
    started = perf_counter()
    if method == "scan":
        result = _scan(constraint)
        label = "classical_clause_scan_first_match"
    else:
        result = _propagate(constraint)
        label = "classical_unit_propagation_first_match"
    return {"method": label, **result, "wall_seconds": perf_counter() - started}

