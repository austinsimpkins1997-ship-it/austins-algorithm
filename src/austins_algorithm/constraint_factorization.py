"""Analyze independent bounded CNF components without a global truth table."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from math import prod

from .constraints import BooleanConstraint


MAX_COMPONENT_WIDTH = 4
Cube = tuple[tuple[int, bool], ...]


@dataclass(frozen=True)
class ComponentFactorization:
    """An independent set of constrained variables and its local cube cover.

    Variable and cube indices are zero-based qubit indices. Clauses retain
    their signed one-based literal encoding. A cube fixes the listed bits
    and leaves omitted bits free. None in cubes denotes a component above the
    cap. The optional complement_cubes covers the failing local assignments.
    """

    variables: tuple[int, ...]
    clauses: tuple[tuple[int, ...], ...]
    cubes: tuple[Cube, ...] | None
    marked_count: int | None
    assignments_checked: int
    complement_cubes: tuple[Cube, ...] | None = None


@dataclass(frozen=True)
class ConstraintFactorization:
    """Local component analysis and the resulting exact count when known."""

    qubits: int
    components: tuple[ComponentFactorization, ...]
    unconstrained_variables: tuple[int, ...]
    marked_count: int | None
    status: str
    reason: str | None = None

    @property
    def eligible(self) -> bool:
        """Whether the plan supplies an exact factorization or a constant."""
        return self.status != "COMPONENT_WIDTH_LIMIT"

    @property
    def assignments_checked(self) -> int:
        return sum(component.assignments_checked for component in self.components)


def _normalized_clauses(constraint: BooleanConstraint) -> tuple[tuple[int, ...], ...]:
    clauses = set()
    for clause in constraint.clauses:
        literals = set(clause)
        if any(-literal in literals for literal in literals):
            continue
        clauses.add(tuple(sorted(literals, key=abs)))
    return tuple(sorted(clauses))


def _connected_variables(clauses: tuple[tuple[int, ...], ...]) -> tuple[tuple[int, ...], ...]:
    neighbors: dict[int, set[int]] = {}
    for clause in clauses:
        variables = {abs(literal) - 1 for literal in clause}
        for variable in variables:
            neighbors.setdefault(variable, set()).update(variables)

    remaining = set(neighbors)
    components = []
    while remaining:
        members: set[int] = set()
        frontier = [min(remaining)]
        while frontier:
            variable = frontier.pop()
            if variable in members:
                continue
            members.add(variable)
            frontier.extend(neighbors[variable] - members)
        remaining.difference_update(members)
        components.append(tuple(sorted(members)))
    return tuple(components)


def _mergeable_pair(cubes: set[Cube]) -> tuple[Cube, Cube, int] | None:
    for cube in sorted(cubes):
        for index, (variable, value) in enumerate(cube):
            partner = cube[:index] + ((variable, not value),) + cube[index + 1 :]
            if partner in cubes:
                return cube, partner, index
    return None


def _disjoint_cubes(minterms: list[Cube]) -> tuple[Cube, ...]:
    # Start with disjoint minterms. Each merge replaces two complementary
    # cubes with precisely their union, which preserves both cover and disjointness.
    cubes = set(minterms)
    pair = _mergeable_pair(cubes)
    while pair is not None:
        cube, partner, index = pair
        cubes.remove(cube)
        cubes.remove(partner)
        cubes.add(cube[:index] + cube[index + 1 :])
        pair = _mergeable_pair(cubes)
    return tuple(sorted(cubes))


def _analyze_component(
    variables: tuple[int, ...], clauses: tuple[tuple[int, ...], ...], *, include_complement: bool,
) -> ComponentFactorization:
    if len(variables) > MAX_COMPONENT_WIDTH:
        return ComponentFactorization(variables, clauses, None, None, 0)

    minterms = []
    failing = []
    for values in product((False, True), repeat=len(variables)):
        minterm = tuple(zip(variables, values))
        assignment = dict(minterm)
        if all(
            any(assignment[abs(literal) - 1] == (literal > 0) for literal in clause)
            for clause in clauses
        ):
            minterms.append(minterm)
        elif include_complement:
            failing.append(minterm)
    return ComponentFactorization(
        variables, clauses, _disjoint_cubes(minterms), len(minterms), 1 << len(variables),
        _disjoint_cubes(failing) if include_complement else None,
    )


def analyze_constraint_factorization(
    constraint: BooleanConstraint, *, include_complement: bool = False,
) -> ConstraintFactorization:
    """Derive disjoint local cube covers and count their satisfying product.

    Only constrained connected components at or below the width cap are
    enumerated. Deterministic cube merging preserves the satisfying set.
    The resulting cover can have more cubes than a globally minimal cover.
    When requested, the same assignments also yield a disjoint failing cover.
    """
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    normalized = _normalized_clauses(constraint)
    nonempty = tuple(clause for clause in normalized if clause)
    components = tuple(
        _analyze_component(
            variables, tuple(clause for clause in nonempty if abs(clause[0]) - 1 in variables),
            include_complement=include_complement,
        )
        for variables in _connected_variables(nonempty)
    )
    constrained = {variable for component in components for variable in component.variables}
    unconstrained = tuple(variable for variable in range(constraint.qubits) if variable not in constrained)
    common = (constraint.qubits, components, unconstrained)

    if () in normalized:
        return ConstraintFactorization(*common, 0, "PROVEN_EMPTY", "EMPTY_CLAUSE")
    units = {clause[0] for clause in nonempty if len(clause) == 1}
    if any(-literal in units for literal in units):
        return ConstraintFactorization(*common, 0, "PROVEN_EMPTY", "CONTRADICTORY_UNITS")
    if any(component.marked_count == 0 for component in components):
        return ConstraintFactorization(*common, 0, "PROVEN_EMPTY", "UNSATISFIABLE_COMPONENT")
    if any(component.marked_count is None for component in components):
        return ConstraintFactorization(*common, None, "COMPONENT_WIDTH_LIMIT")
    marked_count = prod(component.marked_count for component in components) * (1 << len(unconstrained))
    status = "ALWAYS_TRUE" if not components else "ELIGIBLE"
    return ConstraintFactorization(*common, marked_count, status)
