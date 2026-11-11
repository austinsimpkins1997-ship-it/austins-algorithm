"""Compare fixed Grover rounds and placements on one offline IBM snapshot."""

from __future__ import annotations

from collections.abc import Sequence
from importlib.metadata import version
from math import fsum, isclose
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .compilation import MEASUREMENT_TOLERANCE, CompiledProgram, compile_and_verify
from .constraint_oracle import synthesize_constraint_oracle
from .constraints import BooleanConstraint
from .core import theoretical_success_probability
from .grover_search import DIFFUSER_METHODS, MAX_SEARCH_WIDTH, audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_CONSTRAINT_SNAPSHOT_SHOTS,
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
    _local_limits,
    _sample_snapshot,
    _snapshot_simulator,
    _snapshot_specs,
    _validate_snapshot_request,
)


if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit_ibm_runtime.fake_provider import FakeBackendV2


ROUND_CANDIDATES = (0, 1, 2)


def _layout_pair(placements: Sequence[Sequence[int]]) -> tuple[tuple[int, ...], ...]:
    if not isinstance(placements, Sequence) or len(placements) != 2:
        raise ValueError("constraint snapshot sweep requires exactly two placements")
    layouts = []
    for layout in placements:
        if not isinstance(layout, Sequence) or not 1 <= len(layout) <= MAX_SEARCH_WIDTH:
            raise ValueError(f"each placement requires 1 to {MAX_SEARCH_WIDTH} physical qubit indices")
        if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in layout):
            raise ValueError("placement indices must be nonnegative integers")
        if len(set(layout)) != len(layout):
            raise ValueError("indices within a placement must be distinct")
        layouts.append(tuple(layout))
    if layouts[0] == layouts[1]:
        raise ValueError("placements must differ")
    return tuple(layouts)


def _compile_fixed_cell(
    circuit: QuantumCircuit, *, backend: FakeBackendV2, layout: Sequence[int], seed: int,
    constraint: BooleanConstraint, marked_count: int, rounds: int,
) -> tuple[CompiledProgram, dict[str, Any]]:
    program = compile_and_verify(circuit, backend=backend, seed=seed, initial_layout=layout)
    probabilities = program.report["verification"]["roundtrip_probabilities"]
    observed = fsum(p for bits, p in probabilities.items() if constraint.matches(bits))
    expected = theoretical_success_probability(constraint.qubits, marked_count, rounds)
    if not isclose(observed, expected, rel_tol=0, abs_tol=MEASUREMENT_TOLERANCE):
        raise RuntimeError(f"{backend.name} layout {list(layout)} round {rounds} disagrees with the ideal success model")
    active = program.report["openqasm"]["export_wire_to_target_qubit"]
    return program, {
        "active_target_qubits": active,
        "fits_active_qubit_limit": len(active) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
        "ideal_verified_hit_rate": expected,
        "compilation": program.report,
    }


def run_constraint_snapshot_sweep(
    constraint: BooleanConstraint, *, model: str, placements: Sequence[Sequence[int]],
    synthesis: str = "auto", diffuser: str = "standard", shots: int = 64, calibration_seed: int = 7,
    validation_seed: int = 19, transpiler_seed: int = 7,
) -> dict[str, Any]:
    """Compare all six predeclared cells with equal calibration and validation budgets."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if diffuser not in DIFFUSER_METHODS:
        raise ValueError("diffuser must be standard or clean-ancilla")
    for seed in (calibration_seed, validation_seed, transpiler_seed):
        _validate_snapshot_request(model, shots, seed)
    if model == "all":
        raise ValueError("constraint snapshot sweep requires one named model")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"constraint snapshot sweep shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS} per cell per stage")
    if calibration_seed == validation_seed:
        raise ValueError("calibration and validation seeds must differ")
    layouts = _layout_pair(placements)

    started = perf_counter()
    oracle, synthesis_report = synthesize_constraint_oracle(constraint, synthesis=synthesis)
    if any(len(layout) != oracle.num_qubits for layout in layouts):
        raise ValueError(f"each placement requires {oracle.num_qubits} indices for the selected oracle")
    built = perf_counter()
    audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    audited = perf_counter()
    timing = {"oracle_build": built - started, "oracle_audit": audited - built}
    report: dict[str, Any] = {
        "status": "PROVEN_EMPTY" if audit["marked_count"] == 0 else "PARTIAL_CALIBRATION",
        "experiment": "LOCAL_IBM_SNAPSHOT_CONSTRAINT_SWEEP",
        "execution_mode": "SIMULATOR",
        "requested_model": model,
        "diffuser": diffuser,
        "constraint_sha256": constraint.digest(),
        "basis_order": " ".join(f"q{i}" for i in reversed(range(constraint.qubits))),
        "oracle": {**synthesis_report, "qubits": oracle.num_qubits, "work_qubits": oracle.num_qubits - constraint.qubits},
        "oracle_audit": audit,
        "round_candidates": list(ROUND_CANDIDATES),
        "placements": [list(layout) for layout in layouts],
        "shots_per_cell_per_stage": shots,
        "calibration_seed": calibration_seed,
        "validation_seed": validation_seed,
        "transpiler_seed": transpiler_seed,
        "selection_policy": "most calibration hits, then fewest two-qubit gates, shallowest depth, fewest rounds, earliest placement",
        "limits": {**_local_limits(), "shots": MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_sampling_batches": 12},
        "candidates": [],
        "selected_candidate": None,
        "validation_selected_minus_same_placement_zero_hit_rate": None,
        "local_cancellation_unconfirmed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "timing_seconds": timing,
    }
    try:
        if audit["marked_count"] == 0:
            return report

        from qiskit_aer import AerSimulator

        factory, method = _snapshot_specs()[model]
        backend = factory()
        properties = backend.properties()
        report.update({
            "backend": backend.name,
            "backend_version": backend.backend_version,
            "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
            "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime")},
        })
        if oracle.num_qubits > backend.num_qubits:
            report.update(status="INSUFFICIENT_TARGET_WIDTH", target_qubits=backend.num_qubits)
            return report
        if any(index >= backend.num_qubits for layout in layouts for index in layout):
            raise ValueError(f"placement indices must be below the {backend.num_qubits}-qubit target width")

        compiling = perf_counter()
        circuits = {
            rounds: build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds, diffuser=diffuser)
            for rounds in ROUND_CANDIDATES
        }
        programs = []
        for placement, layout in enumerate(layouts, start=1):
            for rounds, circuit in circuits.items():
                program, record = _compile_fixed_cell(
                    circuit, backend=backend, layout=layout, seed=transpiler_seed,
                    constraint=constraint, marked_count=audit["marked_count"], rounds=rounds,
                )
                programs.append(program)
                report["candidates"].append({
                    "placement": placement,
                    "rounds": rounds,
                    "logical_oracle_calls_per_shot": rounds,
                    **record,
                    "calibration": None,
                    "validation": None,
                })
        timing["compile_export_verify"] = perf_counter() - compiling
        rows = report["candidates"]
        if not all(row["fits_active_qubit_limit"] for row in rows):
            report["status"] = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
            return report

        preparing = perf_counter()
        simulator = _snapshot_simulator(backend, AerSimulator, method)
        timing["noise_model_setup"] = perf_counter() - preparing
        for stage, seed in (("calibration", calibration_seed), ("validation", validation_seed)):
            report["status"] = f"PARTIAL_{stage.upper()}"
            for row, program in zip(rows, programs, strict=True):
                sample = _sample_snapshot(
                    program, backend, simulator, method=method, shots=shots, seed=seed, verifier=constraint.matches,
                )
                row[stage] = {"seed": seed, **sample}
                if sample.get("local_job_cancelled") is False:
                    report["local_cancellation_unconfirmed"] = True
                    return report
            if any(row[stage]["status"] != "COMPLETED" for row in rows):
                return report
            if stage == "calibration":
                selected = min(rows, key=lambda row: (
                    -row["calibration"]["simulation"]["verified_shots"],
                    row["compilation"]["compiled"]["two_qubit_gates"],
                    row["compilation"]["compiled"]["depth"],
                    row["rounds"],
                    row["placement"],
                ))
                report["selected_candidate"] = {"placement": selected["placement"], "rounds": selected["rounds"]}

        zero = next(row for row in rows if row["placement"] == selected["placement"] and row["rounds"] == 0)
        report["validation_selected_minus_same_placement_zero_hit_rate"] = (
            selected["validation"]["simulation"]["verified_hit_rate"] - zero["validation"]["simulation"]["verified_hit_rate"]
        )
        report["status"] = "COMPLETED"
        return report
    finally:
        timing["wall"] = perf_counter() - started

