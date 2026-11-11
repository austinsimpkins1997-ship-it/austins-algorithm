"""Synthesize a reversible phase oracle directly from a bounded CNF."""

from __future__ import annotations

from math import pi
from typing import TYPE_CHECKING, Any

from .classical_constraint import reduce_constraint_units
from .constraint_factorization import ConstraintFactorization, Cube, analyze_constraint_factorization
from .constraints import BooleanConstraint
from .grover_search import MAX_SEARCH_WIDTH

if TYPE_CHECKING:
    from qiskit import QuantumCircuit


SYNTHESIS_METHODS = (
    "auto", "clause-flags", "factorized", "unit-reduced", "unit-factorized", "unit-factorized-complement",
)


def _toggle_clause_flag(circuit: QuantumCircuit, clause: tuple[int, ...], flag: int) -> None:
    """XOR the disjunction of a non-unit clause into its flag."""
    controls = [abs(literal) - 1 for literal in clause]
    positive_wires = [literal - 1 for literal in clause if literal > 0]
    # OR is false only when every literal is false. Arrange that condition
    # as all controls being one, then invert its result on the flag.
    circuit.x(flag)
    if positive_wires:
        circuit.x(positive_wires)
    circuit.mcx(controls, flag)
    if positive_wires:
        circuit.x(positive_wires)


def build_constraint_phase_oracle(constraint: BooleanConstraint) -> QuantumCircuit:
    """Mark satisfying CNF assignments and return all work wires to zero.

    Literal +k tests qubit k-1 for one, and literal -k tests it for zero.
    Input variables occupy the lowest-numbered wires. Each distinct,
    non-tautological clause with multiple literals gets one flag above the
    input register. Unit clauses control the conjunction directly.
    """
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if constraint.qubits > MAX_SEARCH_WIDTH:
        raise ValueError(f"CNF exceeds the {MAX_SEARCH_WIDTH}-wire search limit")

    from qiskit import QuantumCircuit

    clauses: set[tuple[int, ...]] = set()
    for clause in constraint.clauses:
        literals = set(clause)
        if not literals:
            return QuantumCircuit(constraint.qubits, name="cnf_phase_oracle")
        if any(-literal in literals for literal in literals):
            continue
        clauses.add(tuple(sorted(literals, key=abs)))

    units = {clause[0] for clause in clauses if len(clause) == 1}
    if any(-literal in units for literal in units):
        return QuantumCircuit(constraint.qubits, name="cnf_phase_oracle")
    if not clauses:
        return QuantumCircuit(constraint.qubits, name="cnf_phase_oracle", global_phase=pi)

    non_units = sorted(clause for clause in clauses if len(clause) > 1)
    width = constraint.qubits + len(non_units)
    if width > MAX_SEARCH_WIDTH:
        raise ValueError(
            f"CNF requires {width} wires ({constraint.qubits} variables and "
            f"{len(non_units)} clause flags), exceeding the {MAX_SEARCH_WIDTH}-wire search limit"
        )
    oracle = QuantumCircuit(width, name="cnf_phase_oracle")
    flagged_clauses = list(enumerate(non_units, start=constraint.qubits))
    for flag, clause in flagged_clauses:
        _toggle_clause_flag(oracle, clause, flag)

    negative_unit_wires = sorted(-literal - 1 for literal in units if literal < 0)
    if negative_unit_wires:
        oracle.x(negative_unit_wires)
    controls = sorted(abs(literal) - 1 for literal in units) + [flag for flag, _ in flagged_clauses]
    if len(controls) == 1:
        oracle.z(controls[0])
    elif len(controls) == 2:
        oracle.cz(*controls)
    else:
        oracle.h(controls[-1])
        oracle.mcx(controls[:-1], controls[-1])
        oracle.h(controls[-1])
    if negative_unit_wires:
        oracle.x(negative_unit_wires)

    for flag, clause in reversed(flagged_clauses):
        _toggle_clause_flag(oracle, clause, flag)
    return oracle


def _cube_operation(
    circuit: QuantumCircuit, cube: Cube, *, flag: int | None = None,
) -> None:
    controls = [wire for wire, _ in cube]
    open_wires = [wire for wire, value in cube if not value]
    if open_wires:
        circuit.x(open_wires)
    if flag is not None:
        if controls:
            circuit.mcx(controls, flag)
        else:
            circuit.x(flag)
    elif not controls:
        circuit.global_phase += pi
    elif len(controls) == 1:
        circuit.z(controls[0])
    elif len(controls) == 2:
        circuit.cz(*controls)
    else:
        circuit.h(controls[-1])
        circuit.mcx(controls[:-1], controls[-1])
        circuit.h(controls[-1])
    if open_wires:
        circuit.x(open_wires)


def _phase_arities(terms: tuple[Cube, ...]) -> list[int]:
    """List phase term widths in descending order, omitting global phases."""
    return sorted((len(term) for term in terms if term), reverse=True)


def _factored_oracle(
    plan: ConstraintFactorization, *, phase_cover_policy: str = "direct",
) -> tuple[QuantumCircuit, dict[str, Any] | None]:
    from qiskit import QuantumCircuit

    if plan.status == "PROVEN_EMPTY":
        return QuantumCircuit(plan.qubits, name="factored_cnf_phase_oracle"), None
    if plan.status == "ALWAYS_TRUE":
        return QuantumCircuit(plan.qubits, name="factored_cnf_phase_oracle", global_phase=pi), None
    if any(component.cubes is None for component in plan.components):
        raise ValueError("factorized synthesis requires bounded cubes for each component")
    fixed = tuple(
        condition for component in plan.components if len(component.cubes) == 1
        for condition in component.cubes[0]
    )
    multiple = [component for component in plan.components if len(component.cubes) > 1]
    work = max(0, len(multiple) - 1)
    width = plan.qubits + work
    if width > MAX_SEARCH_WIDTH:
        raise ValueError(f"factorized oracle requires {width} wires, exceeding the {MAX_SEARCH_WIDTH}-wire limit")
    oracle = QuantumCircuit(width, name="factored_cnf_phase_oracle")
    if not multiple:
        _cube_operation(oracle, fixed)
        return oracle, None

    flagged = list(enumerate(multiple[:-1], start=plan.qubits))
    for wire, component in flagged:
        for cube in component.cubes:
            _cube_operation(oracle, cube, flag=wire)
    prefix = fixed + tuple((wire, True) for wire, _ in flagged)
    terminal = multiple[-1]
    terms = tuple(cube + prefix for cube in terminal.cubes)
    phase_cover = None
    if phase_cover_policy != "direct":
        if terminal.complement_cubes is None:
            raise ValueError("complement synthesis requires the terminal failing cover")
        # Prefix AND terminal equals prefix XOR (prefix AND NOT terminal).
        # An empty prefix contributes the exact global phase.
        alternate = (prefix, *(cube + prefix for cube in terminal.complement_cubes))
        direct_arities = _phase_arities(terms)
        complement_arities = _phase_arities(alternate)
        use_complement = phase_cover_policy == "complement" or complement_arities < direct_arities
        if use_complement:
            terms = alternate
        phase_cover = {
            "selected": "complement" if use_complement else "direct",
            "terminal_component_variables": list(terminal.variables),
            "direct_arities": direct_arities,
            "complement_arities": complement_arities,
        }
    for term in terms:
        _cube_operation(oracle, term)
    for wire, component in reversed(flagged):
        for cube in reversed(component.cubes):
            _cube_operation(oracle, cube, flag=wire)
    return oracle, phase_cover


def synthesize_constraint_oracle(
    constraint: BooleanConstraint, *, synthesis: str = "auto",
) -> tuple[QuantumCircuit, dict[str, Any]]:
    """Build the requested phase oracle, optionally reducing units before synthesis."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if synthesis not in SYNTHESIS_METHODS:
        raise ValueError(f"synthesis must be one of {', '.join(SYNTHESIS_METHODS)}")
    report: dict[str, Any] = {"synthesis_requested": synthesis, "synthesis_used": "clause-flags"}
    if synthesis == "clause-flags":
        return build_constraint_phase_oracle(constraint), report
    consider_complement = synthesis == "unit-factorized-complement"
    if consider_complement or synthesis in {"unit-reduced", "unit-factorized"}:
        reduction = reduce_constraint_units(constraint)
        retained = len(reduction.constraint.clauses)
        report["unit_reduction"] = {
            "forced_literals": list(reduction.forced_literals),
            "contradiction": reduction.contradiction,
            "input_clause_count": len(constraint.clauses),
            "retained_clause_count": retained,
            "residual_clause_count": None if reduction.contradiction else retained - len(reduction.forced_literals),
        }
        constraint = reduction.constraint
        if synthesis == "unit-reduced":
            report["synthesis_used"] = synthesis
            return build_constraint_phase_oracle(constraint), report

    require_factorization = consider_complement or synthesis in {"factorized", "unit-factorized"}
    plan = analyze_constraint_factorization(constraint, include_complement=consider_complement)
    report["factorization"] = {
        "status": plan.status,
        "local_assignments_checked": plan.assignments_checked,
        "component_variables": [list(component.variables) for component in plan.components],
        "cubes_per_component": [
            None if component.cubes is None else len(component.cubes) for component in plan.components
        ],
    }
    if not plan.eligible:
        if require_factorization:
            raise ValueError(f"{synthesis} synthesis unavailable: {plan.status}")
        return build_constraint_phase_oracle(constraint), report

    if require_factorization or len(plan.components) > 1:
        policy = "heuristic" if consider_complement else "direct"
        factored, phase_cover = _factored_oracle(plan, phase_cover_policy=policy)
        if require_factorization:
            report["synthesis_used"] = synthesis
            if consider_complement:
                report["phase_cover"] = phase_cover or {"selected": "not-applicable"}
            return factored, report
        reference_width = constraint.qubits + sum(
            len(clause) > 1 for component in plan.components for clause in component.clauses
        )
        if factored.num_qubits < reference_width:
            report["synthesis_used"] = "factorized"
            return factored, report
    return build_constraint_phase_oracle(constraint), report
