"""Explicit bindings to local models. No generated or provider code executes here."""

from .codec import digest
from .controller import domain, routes


def capabilities():
    model = domain()
    return {
        "schema": "future-reader.capabilities/1",
        "controller": {"backend": "python-sqlite", "mode": "SIMULATION",
                       "model": model["id"], "model_digest": digest(model),
                       "operations": ["PINPOINT", "TRAVEL", "REVERSE", "CERTIFY"],
                       "external_effects": False},
        "austin": {"backend": "austins-algorithm", "mode": "LOCAL_CPU",
                   "scope": "exclusive choice of at most eight enumerated eligible paths",
                   "optional": True, "compile": "optional local Qiskit", "hardware": "UNAVAILABLE"},
        "bib": {"backend": "ChronoRealmEnvelope/1", "mode": "CALLER_SUPPLIED_REVIEW",
                "producer": "UNAVAILABLE", "operations": []},
        "live": {"status": "UNAVAILABLE", "reason": "No verified live backend binding",
                 "fallback": "Use the simulator or inspect a portable evidence record."},
    }


def austin_problem(*, now=0, check_available=True):
    options = [row for row in routes("S", "G", now, check_available) if row["eligible"]]
    if len(options) > 8:
        raise ValueError("The Austin binding supports at most eight eligible paths")
    names = ["-".join(row["path"]) for row in options]
    if not options:
        return None, options
    clauses = [list(range(1, len(options) + 1))]
    clauses += [[-i, -j] for i in range(1, len(options) + 1) for j in range(i + 1, len(options) + 1)]
    return {"name": "Future Reader eligible route choice", "clauses": clauses,
            "variables": [{"name": label, "cost": row["cost"]} for label, row in zip(names, options)]}, options


def austin(*, now=0, check_available=True, compile_qiskit=False):
    problem, eligible = austin_problem(now=now, check_available=check_available)
    base = {"schema": "future-reader.austin-binding/1", "mode": "LOCAL_CPU",
            "scope": {"now": now, "check_available": check_available}, "problem": problem}
    if problem is None:
        return {**base, "status": "NO_ELIGIBLE_PATH", "fallback": "Inspect the remaining opportunity or recovery path."}
    try:
        from austins_algorithm.optimization import exact_minimum
    except ModuleNotFoundError as error:
        if error.name != "austins_algorithm":
            raise
        return {**base, "status": "UNAVAILABLE", "reason": "Austin's Algorithm is absent",
                "fallback": "Use the local controller's direct route enumeration."}
    exact = exact_minimum(problem)
    decoded = [eligible[tuple(reversed(bits)).index("1")]["path"] for bits in exact["ties"]]
    cost = min(row["cost"] for row in eligible)
    expected = sorted(row["path"] for row in eligible if row["cost"] == cost)
    if exact["cost"] != cost or sorted(decoded) != expected:
        raise RuntimeError("Austin encoding disagrees with direct route reference")
    result = {**base, "status": "LOCAL_EQUIVALENCE_VERIFIED", "exact": exact,
              "paths": decoded, "direct_cost": cost, "hardware_used": False}
    if not compile_qiskit:
        return result
    try:
        from qiskit import transpile
        from qiskit.qasm3 import dumps, loads
        from qiskit.quantum_info import Statevector
        from austins_algorithm.constraints import BooleanConstraint
        from austins_algorithm.grover_search import audit_phase_oracle, build_grover_search_circuit
        from austins_algorithm.weighted_oracle import build_weighted_phase_oracle
    except ModuleNotFoundError as error:
        if error.name != "qiskit":
            raise
        return {**result, "quantum": {"status": "UNAVAILABLE", "reason": "Qiskit is absent"}}
    width = len(eligible)
    bound = cost + 1
    constraint = BooleanConstraint(width, problem["clauses"])
    weights = [row["cost"] for row in problem["variables"]]

    def marked(bits):
        return constraint.matches(bits) and sum(w for w, b in zip(weights, reversed(bits)) if b == "1") < bound

    oracle, synthesis = build_weighted_phase_oracle(problem, incumbent=bound)
    audit_phase_oracle(oracle, width, marked)
    circuit = build_grover_search_circuit(oracle, width, iterations=1)
    compiled = transpile(circuit, basis_gates=["cx", "rz", "sx", "x"], seed_transpiler=7)
    qasm = dumps(compiled)
    try:
        reloaded = loads(qasm)
    except Exception as error:
        if error.__class__.__name__ != "MissingOptionalLibraryError":
            raise
        return {**result, "quantum": {"status": "COMPILED", "reason": "OpenQASM importer is absent",
                                     "qasm": qasm, "hardware_used": False}}

    def probabilities(candidate):
        return Statevector.from_instruction(candidate.remove_final_measurements(inplace=False)).probabilities()

    delta = sum(abs(float(a) - float(b)) for a, b in zip(probabilities(circuit), probabilities(reloaded))) / 2
    if delta > 1e-10:
        raise RuntimeError("The QASM round trip changed the ideal distribution")
    two_qubit = sum(len(op.qubits) == 2 for op in compiled.data)
    return {**result, "quantum": {"status": "SIMULATOR_VERIFIED", "basis": ["cx", "rz", "sx", "x"],
                                 "depth": compiled.depth(), "two_qubit_gates": two_qubit,
                                 "total_variation": delta, "synthesis": synthesis,
                                 "qasm": qasm, "hardware_used": False}}
