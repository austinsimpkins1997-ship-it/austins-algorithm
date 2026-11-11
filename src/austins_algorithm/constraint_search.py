"""Solve a declarative constraint with an audited oracle and an independent verifier."""

from __future__ import annotations

from copy import deepcopy
from time import perf_counter
from typing import Any

from .classical_constraint import CLASSICAL_METHODS, classical_first_match
from .compilation import MAX_EXACT_QUBITS, compile_and_verify, line_target
from .constraint_oracle import synthesize_constraint_oracle
from .constraints import BooleanConstraint
from .core import _bounded_int, recommended_iterations
from .grover_search import (
    DIFFUSER_METHODS, IDEAL_SAMPLERS, MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS,
    _FixedRoundProbabilities, _LastAttempt, _supports_prepared_sampling,
    audit_phase_oracle, build_grover_search_circuit, run_grover_search,
)


class PreparedConstraintLineSearch:
    """Reuse one verified synthetic-line compilation across known-count ideal searches.

    The formula, oracle synthesis, diffuser, line target, compiler seed and
    importer preference are fixed at preparation. Each run uses its own sampler
    seed and budget and checks candidate bits against the original formula.
    Eligible probability sampling prepares the logical measurement once per plan.
    """

    def __init__(
        self, constraint: BooleanConstraint, *, synthesis: str = "auto", diffuser: str = "standard",
        line_width: int | None = None, compiler_seed: int = 7, prefer_native_qasm: bool = False,
    ) -> None:
        if not isinstance(constraint, BooleanConstraint):
            raise TypeError("constraint must be a BooleanConstraint")
        if diffuser not in DIFFUSER_METHODS:
            raise ValueError("diffuser must be standard or clean-ancilla")
        if line_width is not None:
            _bounded_int(line_width, 1, MAX_EXACT_QUBITS, "line_width")
        _bounded_int(compiler_seed, 0, 2**32 - 1, "compiler_seed")

        constraint = BooleanConstraint(constraint.qubits, constraint.clauses)
        oracle, _ = synthesize_constraint_oracle(constraint, synthesis=synthesis)
        audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
        marked_count = audit["marked_count"]
        if marked_count == 0:
            raise ValueError("the oracle audit proved the formula empty, so there is no search to prepare")

        rounds = recommended_iterations(constraint.qubits, marked_count)
        circuit = build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds, diffuser=diffuser)
        width = circuit.num_qubits if line_width is None else line_width
        if width < circuit.num_qubits:
            raise ValueError(f"synthetic line width {width} is smaller than the {circuit.num_qubits}-wire circuit")
        program = compile_and_verify(
            circuit, target=line_target(width, seed=compiler_seed), seed=compiler_seed,
            prefer_native_qasm=prefer_native_qasm,
        )
        self._constraint = constraint
        self._constraint_sha256 = constraint.digest()
        self._oracle = oracle
        self._marked_count = marked_count
        self._diffuser = diffuser
        self._fixed_probabilities = None
        if _supports_prepared_sampling(oracle, constraint.qubits, use_probabilities=True):
            self._fixed_probabilities = _FixedRoundProbabilities(oracle, circuit, constraint.qubits, rounds, diffuser)
        self._openqasm = program.openqasm
        self._compilation_report = program.report

    @property
    def openqasm(self) -> str:
        """The verified portable artifact for the prepared search."""
        return self._openqasm

    @property
    def compilation_report(self) -> dict[str, Any]:
        """Return a detached report with the verification and physical wire mapping."""
        return deepcopy(self._compilation_report)

    def run(
        self, *, seed: int = 7, max_shots: int = 64, max_oracle_queries: int = 256,
        ideal_sampler: str = "prepared_probabilities",
    ) -> dict[str, Any]:
        """Run one bounded ideal search using this plan's verified inputs."""
        report = run_grover_search(
            self._oracle, self._constraint.qubits, self._constraint.matches,
            marked_count=self._marked_count, diffuser=self._diffuser, max_shots=max_shots,
            max_oracle_queries=max_oracle_queries, seed=seed, ideal_sampler=ideal_sampler,
            _fixed_probabilities=self._fixed_probabilities,
        )
        return {
            **report,
            "constraint_sha256": self._constraint_sha256,
            "prepared_compilation_sha256": self._compilation_report["openqasm"]["sha256"],
        }


def run_constraint_search(
    constraint: BooleanConstraint, *, strategy: str = "unknown", max_shots: int = 64,
    max_oracle_queries: int = 256, seed: int = 7, synthesis: str = "auto", diffuser: str = "standard",
    classical_method: str = "scan", ideal_sampler: str = "statevector_sampler", _last_attempt: _LastAttempt | None = None,
) -> dict[str, Any]:
    """Audit the small phase oracle, then run a bounded ideal simulator search."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if strategy not in {"known", "unknown"}:
        raise ValueError("strategy must be known or unknown")
    if diffuser not in DIFFUSER_METHODS:
        raise ValueError("diffuser must be standard or clean-ancilla")
    if ideal_sampler not in IDEAL_SAMPLERS:
        raise ValueError(f"ideal_sampler must be one of {', '.join(IDEAL_SAMPLERS)}")
    if classical_method not in CLASSICAL_METHODS:
        raise ValueError("classical method must be scan or propagation")
    _bounded_int(max_shots, 1, MAX_SEARCH_SHOTS, "max_shots")
    _bounded_int(max_oracle_queries, 0, MAX_ORACLE_QUERIES, "max_oracle_queries")
    _bounded_int(seed, 0, 2**32 - 1, "seed")

    started = perf_counter()
    oracle, synthesis_report = synthesize_constraint_oracle(constraint, synthesis=synthesis)
    built = perf_counter()
    audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    audited = perf_counter()
    baseline = classical_first_match(constraint, method=classical_method)
    if baseline["candidate"] != next(iter(audit["marked_states"]), None):
        raise RuntimeError("classical first witness disagrees with the exhaustive oracle audit")

    search = None
    sample_seconds = None
    if audit["marked_count"]:
        search_started = perf_counter()
        search = run_grover_search(
            oracle, constraint.qubits, constraint.matches,
            marked_count=audit["marked_count"] if strategy == "known" else None,
            max_shots=max_shots, max_oracle_queries=max_oracle_queries, seed=seed, diffuser=diffuser,
            ideal_sampler=ideal_sampler, _last_attempt=_last_attempt,
        )
        sample_seconds = perf_counter() - search_started
    report = {
        "status": "PROVEN_EMPTY" if search is None else search["status"],
        "problem": "DECLARATIVE_BOOLEAN_CONSTRAINT",
        "constraint_sha256": constraint.digest(),
        "constraint_schema_version": 1,
        "search_qubits": constraint.qubits,
        "clause_count": len(constraint.clauses),
        "basis_order": " ".join(f"q{i}" for i in reversed(range(constraint.qubits))),
        "oracle": {
            **synthesis_report,
            "qubits": oracle.num_qubits,
            "work_qubits": oracle.num_qubits - constraint.qubits,
            "depth": oracle.depth(),
            "operations": dict(oracle.count_ops()),
            "build_seconds": built - started,
        },
        "exhaustive_oracle_audit": {**audit, "wall_seconds": audited - built},
        "classical_first_match": baseline,
        "requested_search": {
            "strategy": strategy, "max_shots": max_shots, "max_oracle_queries": max_oracle_queries,
            "seed": seed, "diffuser": diffuser,
        },
        "search": search,
        "simulator_search_seconds": sample_seconds,
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
        "wall_seconds": perf_counter() - started,
    }
    if ideal_sampler != "statevector_sampler":
        report["requested_search"]["ideal_sampler"] = ideal_sampler
    return report
