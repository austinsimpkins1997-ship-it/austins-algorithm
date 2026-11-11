"""Matched fixed-layout diffuser comparisons on offline IBM snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from importlib.metadata import version
from math import fsum
from time import perf_counter
from typing import Any

from .compilation import MEASUREMENT_TOLERANCE
from .constraint_oracle import synthesize_constraint_oracle
from .constraint_snapshot_sweep import _compile_fixed_cell, _layout_pair
from .constraints import BooleanConstraint
from .grover_search import audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_CONSTRAINT_SNAPSHOT_SHOTS,
    _local_limits,
    _sample_snapshot,
    _snapshot_hit_rate_difference,
    _snapshot_simulator,
    _snapshot_specs,
    _summarize_snapshot_batches,
    _validate_snapshot_request,
)


COMPARISON_ROUNDS = (1, 2)
COMPARED_DIFFUSERS = ("standard", "clean-ancilla")
DEFAULT_EVALUATION_SEEDS = (101, 103, 107, 109)
FAMILY_CONFIDENCE = 0.95


def run_constraint_diffuser_comparison(
    constraint: BooleanConstraint, *, model: str, placements: Sequence[Sequence[int]],
    synthesis: str = "auto", shots: int = 64, evaluation_seeds: Sequence[int] = DEFAULT_EVALUATION_SEEDS,
    transpiler_seed: int = 7,
) -> dict[str, Any]:
    """Compare both diffusers at the same two placements, one and two rounds each."""
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    _validate_snapshot_request(model, shots, transpiler_seed)
    if model == "all":
        raise ValueError("diffuser comparison requires one named model")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"diffuser comparison shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS} per batch")
    if not isinstance(evaluation_seeds, Sequence) or len(evaluation_seeds) != 4:
        raise ValueError("diffuser comparison requires four evaluation seeds")
    for seed in evaluation_seeds:
        _validate_snapshot_request(model, shots, seed)
    if len(set(evaluation_seeds)) != 4:
        raise ValueError("evaluation seeds must be distinct")
    seeds = {method: list(evaluation_seeds[index::2]) for index, method in enumerate(COMPARED_DIFFUSERS)}
    layouts = _layout_pair(placements)

    started = perf_counter()
    oracle, synthesis_report = synthesize_constraint_oracle(constraint, synthesis=synthesis)
    if any(len(layout) != oracle.num_qubits for layout in layouts):
        raise ValueError(f"each placement requires {oracle.num_qubits} indices for the selected oracle")
    audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    num_variants = len(layouts) * len(COMPARISON_ROUNDS) * len(COMPARED_DIFFUSERS)
    confidence = 1 - (1 - FAMILY_CONFIDENCE) / num_variants
    report: dict[str, Any] = {
        "status": "PROVEN_EMPTY" if audit["marked_count"] == 0 else "PARTIAL",
        "experiment": "LOCAL_IBM_SNAPSHOT_CONSTRAINT_DIFFUSER_COMPARISON",
        "execution_mode": "SIMULATOR",
        "requested_model": model,
        "constraint_sha256": constraint.digest(),
        "oracle": {**synthesis_report, "qubits": oracle.num_qubits, "work_qubits": oracle.num_qubits - constraint.qubits},
        "oracle_audit": audit,
        "round_candidates": list(COMPARISON_ROUNDS),
        "placements": [list(layout) for layout in layouts],
        "shots_per_batch": shots,
        "evaluation_seeds": seeds,
        "transpiler_seed": transpiler_seed,
        "family_confidence_per_snapshot": FAMILY_CONFIDENCE,
        "confidence_per_variant": confidence,
        "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson; subtract opposite endpoints",
        "limits": {**_local_limits(), "shots": MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_sampling_batches": 2 * num_variants},
        "cells": [],
        "local_cancellation_unconfirmed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    try:
        if audit["marked_count"] == 0:
            return report

        from qiskit_aer import AerSimulator

        factory, simulator_method = _snapshot_specs()[model]
        backend = factory()
        properties = backend.properties()
        report.update({
            "backend": backend.name,
            "backend_version": backend.backend_version,
            "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
            "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
        })
        if oracle.num_qubits > backend.num_qubits:
            report.update(status="INSUFFICIENT_TARGET_WIDTH", target_qubits=backend.num_qubits)
            return report
        if any(index >= backend.num_qubits for layout in layouts for index in layout):
            raise ValueError(f"placement indices must be below the {backend.num_qubits}-qubit target width")

        circuits = {
            (rounds, method): build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds, diffuser=method)
            for rounds in COMPARISON_ROUNDS for method in COMPARED_DIFFUSERS
        }
        programs = {}
        for placement, layout in enumerate(layouts, start=1):
            for rounds in COMPARISON_ROUNDS:
                variants = {}
                for method in COMPARED_DIFFUSERS:
                    program, record = _compile_fixed_cell(
                        circuits[rounds, method], backend=backend, layout=layout, seed=transpiler_seed,
                        constraint=constraint, marked_count=audit["marked_count"], rounds=rounds,
                    )
                    programs[placement, rounds, method] = program
                    variants[method] = {**record, "batches": [], "summary": None}
                standard, clean = (variants[method] for method in COMPARED_DIFFUSERS)
                a, b = (variant["compilation"]["verification"]["roundtrip_probabilities"]
                        for variant in (standard, clean))
                tvd = fsum(abs(a.get(bits, 0) - b.get(bits, 0)) for bits in a.keys() | b.keys()) / 2
                if tvd > MEASUREMENT_TOLERANCE:
                    raise RuntimeError(f"{backend.name} placement {placement} round {rounds} diffuser distributions differ")
                report["cells"].append({
                    "placement": placement, "rounds": rounds, "ideal_distribution_total_variation_distance": tvd,
                    "two_qubit_gate_savings": (
                        standard["compilation"]["compiled"]["two_qubit_gates"]
                        - clean["compilation"]["compiled"]["two_qubit_gates"]
                    ),
                    "variants": variants, "difference": None,
                })
        cells = report["cells"]
        if any(not variant["fits_active_qubit_limit"] for cell in cells for variant in cell["variants"].values()):
            report["status"] = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
            return report

        simulator = _snapshot_simulator(backend, AerSimulator, simulator_method)
        for cell in cells:
            for method, variant in cell["variants"].items():
                program = programs[cell["placement"], cell["rounds"], method]
                for seed in seeds[method]:
                    sample = _sample_snapshot(
                        program, backend, simulator, method=simulator_method, shots=shots, seed=seed,
                        verifier=constraint.matches,
                    )
                    variant["batches"].append({"seed": seed, **sample})
                    if sample.get("local_job_cancelled") is False:
                        report["local_cancellation_unconfirmed"] = True
                        return report
                variant["summary"] = _summarize_snapshot_batches(variant["batches"], confidence)
            standard, clean = (cell["variants"][method]["summary"] for method in COMPARED_DIFFUSERS)
            cell["difference"] = _snapshot_hit_rate_difference(standard, clean)
        if all(cell["difference"] is not None for cell in cells):
            report["status"] = "COMPLETED"
        return report
    finally:
        report["wall_seconds"] = perf_counter() - started

