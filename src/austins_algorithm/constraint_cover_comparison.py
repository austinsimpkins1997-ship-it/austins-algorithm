"""Audit and compare the two exact terminal phase covers on one target."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from math import fsum
from time import perf_counter
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .classical_constraint import classical_first_match, reduce_constraint_units
from .compilation import MAX_EXACT_QUBITS, MEASUREMENT_TOLERANCE, CompiledProgram, compile_and_verify
from .constraint_factorization import analyze_constraint_factorization
from .constraint_oracle import _factored_oracle
from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_CONSTRAINT_SNAPSHOT_SHOTS,
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
    _compact_snapshot_inputs,
    _local_limits,
    _prepare_density_snapshot,
    _sample_snapshot,
    _snapshot_hit_rate_difference,
    _snapshot_simulator,
    _snapshot_specs,
    _summarize_snapshot_batches,
    _validate_snapshot_request,
)

from .witness_budget import (
    WITNESS_BATCH_CONFIDENCE_PER_COVER,
    WitnessBudgetRequest,
    plan_witness_budget,
    summarize_witness_batches,
)

if TYPE_CHECKING:
    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target


DEFAULT_COVER_EVALUATION_SEEDS = (167, 173, 179, 181)
COVER_CONFIDENCE_PER_ARM = 0.975
COVER_SNAPSHOT_METHODS = ("density_matrix", "matrix_product_state", "compact_density_matrix", "prepared_density_matrix")


@dataclass(frozen=True)
class PhaseCoverComparison:
    """Measured resource report and the exact compiled programs it describes."""

    report: dict[str, Any]
    selected: CompiledProgram | None
    programs: Mapping[str, CompiledProgram] = field(default_factory=dict)


def compare_constraint_phase_covers(
    constraint: BooleanConstraint, *, target: Target | None = None, backend: BackendV2 | None = None,
    rounds: int = 2, seed: int = 7, initial_layout: Sequence[int] | None = None,
    max_active_qubits: int | None = None,
    prefer_native_qasm: bool = False,
) -> PhaseCoverComparison:
    """Compile both covers with the standard diffuser and rank verified candidates.

    Each phase oracle is audited against the original formula. The selected
    program is the already verified compilation, including its portable export.
    """
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if (target is None) == (backend is None):
        raise ValueError("supply exactly one Qiskit target or backend")
    _bounded_int(rounds, 1, MAX_ITERATIONS, "rounds")
    _bounded_int(seed, 0, 2**32 - 1, "seed")
    if max_active_qubits is not None:
        _bounded_int(max_active_qubits, 1, MAX_EXACT_QUBITS, "max_active_qubits")

    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target

    if target is not None and not isinstance(target, Target):
        raise TypeError("target must be a Qiskit Target")
    if backend is not None and not isinstance(backend, BackendV2):
        raise TypeError("backend must be a Qiskit BackendV2")
    active_target = target if target is not None else backend.target

    reduction = reduce_constraint_units(constraint)
    plan = analyze_constraint_factorization(reduction.constraint, include_complement=True)
    if not plan.eligible:
        raise ValueError(f"phase cover comparison unavailable: {plan.status}")
    direct, _ = _factored_oracle(plan)
    if active_target.num_qubits is not None and direct.num_qubits > active_target.num_qubits:
        raise ValueError(f"target width {active_target.num_qubits} is smaller than the {direct.num_qubits}-wire circuit")
    alternate, phase_cover = _factored_oracle(plan, phase_cover_policy="complement")
    oracles = {"direct": direct}
    if phase_cover is not None:
        oracles["complement"] = alternate

    # Audit both oracles before compiling either candidate.
    audits = {name: audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
              for name, oracle in oracles.items()}
    if "complement" in audits and audits["direct"] != audits["complement"]:
        raise RuntimeError("phase cover audits disagree on the original formula")
    programs = {}
    candidates = {}
    for name, oracle in oracles.items():
        circuit = build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds)
        program = compile_and_verify(
            circuit, target=target, backend=backend, seed=seed, initial_layout=initial_layout,
            prefer_native_qasm=prefer_native_qasm,
        )
        active = len(program.report["openqasm"]["export_wire_to_target_qubit"])
        programs[name] = program
        candidates[name] = {
            "active_qubits": active,
            "eligible": max_active_qubits is None or active <= max_active_qubits,
            "oracle_audit": audits[name],
            "compilation": program.report,
        }

    distance = None
    if phase_cover is not None:
        a, b = (program.report["verification"]["roundtrip_probabilities"] for program in programs.values())
        distance = fsum(abs(a.get(bits, 0) - b.get(bits, 0)) for bits in a.keys() | b.keys()) / 2
        if distance > MEASUREMENT_TOLERANCE:
            raise RuntimeError("compiled phase covers disagree on the ideal measurement distribution")

    eligible = [name for name, row in candidates.items() if row["eligible"]]
    selected = min(eligible, key=lambda name: (
        candidates[name]["compilation"]["compiled"]["two_qubit_gates"],
        candidates[name]["compilation"]["compiled"]["depth"],
        candidates[name]["active_qubits"],
        name != "direct",
    ), default=None)
    status = "COMPLETED" if phase_cover is not None else "DIRECT_ONLY"
    if selected is None:
        status = "NO_COVER_FITS"
    report = {
        "status": status,
        "execution_mode": "COMPILE_ONLY",
        "experiment": "CONSTRAINT_PHASE_COVER_COMPARISON",
        "constraint_sha256": constraint.digest(),
        "rounds": rounds,
        "diffuser": "standard",
        "transpiler_seed": seed,
        "qiskit_version": version("qiskit"),
        "layout_policy": "automatic per candidate" if initial_layout is None else "shared initial layout",
        "max_active_qubits": max_active_qubits,
        "selection_policy": "fewest two-qubit gates, then depth, then active qubits, then direct cover",
        "forced_literals": list(reduction.forced_literals),
        "root_contradiction": reduction.contradiction,
        "factorization_status": plan.status,
        "alternate_available": phase_cover is not None,
        "phase_cover_profiles": None if phase_cover is None else {
            key: phase_cover[key] for key in ("terminal_component_variables", "direct_arities", "complement_arities")
        },
        "candidates": candidates,
        "cross_cover_total_variation_distance": distance,
        "selected_cover": selected,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    return PhaseCoverComparison(report, programs.get(selected), MappingProxyType(programs))


def _run_witness_validation(
    result: PhaseCoverComparison, request: WitnessBudgetRequest,
    samplers: Mapping[str, Callable[..., dict[str, Any]]],
) -> None:
    witness = result.report["witness_budget"]
    timings = witness["timing_seconds"]
    started = perf_counter()
    snapshot_candidates = result.report["snapshot"]["candidates"]
    for name, candidate in snapshot_candidates.items():
        budget = plan_witness_budget(
            candidate["summary"]["interval"][0],
            target_probability=request.target_probability, max_shots=request.max_shots,
        )
        required = budget["shots_required"]
        gates = result.programs[name].report["compiled"]["two_qubit_gates"]
        witness["candidates"][name] = {
            "budget": budget,
            "nominal_two_qubit_gate_executions": None if required is None else required * gates,
            "validation": None,
            "validation_batches": [],
            "batch_reliability": None,
            "completed_nominal_two_qubit_gate_executions": 0,
            "verified_witness": None,
        }
    timings["budget_planning"] = perf_counter() - started
    if all(row["budget"]["shots_required"] is None for row in witness["candidates"].values()):
        witness["status"] = "NO_FEASIBLE_BUDGET"
        return

    started = perf_counter()
    witness["status"] = "PARTIAL_VALIDATION"
    try:
        completed = True
        for name, fresh_seeds in witness["validation_seed_schedule"].items():
            row = witness["candidates"][name]
            required = row["budget"]["shots_required"]
            if required is None:
                continue
            for fresh_seed in fresh_seeds:
                sample = samplers[name](shots=required, seed=fresh_seed)
                batch = {"seed": fresh_seed, **sample}
                row["validation_batches"].append(batch)
                if row["validation"] is None:
                    row["validation"] = batch
                if sample.get("local_job_cancelled") is False:
                    witness["local_cancellation_unconfirmed"] = True
                    return
                if sample["status"] != "COMPLETED":
                    completed = False
                elif row["verified_witness"] is None:
                    row["verified_witness"] = next(iter(sample["simulation"]["verified_candidates"]), None)
        if completed:
            witness["status"] = "COMPLETED"
    finally:
        for name, row in witness["candidates"].items():
            if row["budget"]["shots_required"] is None:
                continue
            summary = summarize_witness_batches(
                row["validation_batches"], planned_batches=witness["validation_batches_per_cover"],
                target_probability=request.target_probability,
            )
            row["batch_reliability"] = summary
            gates = result.programs[name].report["compiled"]["two_qubit_gates"]
            row["completed_nominal_two_qubit_gate_executions"] = summary["completed_shots"] * gates
        timings["holdout_validation"] = perf_counter() - started


def run_constraint_cover_snapshot(
    constraint: BooleanConstraint, *, model: str, initial_layout: Sequence[int],
    rounds: int = 2, seed: int = 7, max_active_qubits: int | None = None,
    shots: int = 64, evaluation_seeds: Sequence[int] = DEFAULT_COVER_EVALUATION_SEEDS,
    witness_budget: WitnessBudgetRequest | None = None,
    simulation_method: str | None = None,
    prefer_native_qasm: bool = False,
) -> PhaseCoverComparison:
    """Compare fixed-layout covers and optionally validate a planned witness budget."""
    started = perf_counter()
    _validate_snapshot_request(model, shots, seed)
    if model == "all":
        raise ValueError("phase cover sampling requires one named IBM snapshot")
    if simulation_method is not None and simulation_method not in COVER_SNAPSHOT_METHODS:
        raise ValueError(f"simulation_method must be one of {', '.join(COVER_SNAPSHOT_METHODS)}")
    if initial_layout is None:
        raise ValueError("phase cover sampling requires a shared initial layout")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"phase cover sampling shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS} per batch")
    if not isinstance(evaluation_seeds, Sequence) or len(evaluation_seeds) != 4:
        raise ValueError("phase cover sampling requires four evaluation seeds")
    for evaluation_seed in evaluation_seeds:
        _validate_snapshot_request(model, shots, evaluation_seed)
    if len(set(evaluation_seeds)) != 4:
        raise ValueError("evaluation seeds must be distinct")
    witness = None
    if witness_budget is not None:
        if not isinstance(witness_budget, WitnessBudgetRequest):
            raise TypeError("witness_budget must be a WitnessBudgetRequest")
        if set(witness_budget.validation_seeds) & set(evaluation_seeds):
            raise ValueError("witness validation seeds must differ from all calibration seeds")
        baseline_started = perf_counter()
        baseline = classical_first_match(constraint, method="propagation")
        validation_schedule = {
            "direct": list(witness_budget.validation_seeds[::2]),
            "complement": list(witness_budget.validation_seeds[1::2]),
        }
        witness = {
            "status": "CALIBRATION_INCOMPLETE",
            "target_probability": witness_budget.target_probability,
            "max_shots_per_validation": witness_budget.max_shots,
            "validation_seeds": {name: seeds[0] for name, seeds in validation_schedule.items()},
            "validation_seed_schedule": validation_schedule,
            "validation_batches_per_cover": len(validation_schedule["direct"]),
            "calibration_confidence_per_cover": COVER_CONFIDENCE_PER_ARM,
            "batch_confidence_per_cover": WITNESS_BATCH_CONFIDENCE_PER_COVER,
            "batch_family_confidence": 0.95,
            "classical_reference": baseline,
            "candidates": {},
            "local_cancellation_unconfirmed": False,
            "timing_seconds": {"classical_reference": perf_counter() - baseline_started},
        }

    seeds = {"direct": list(evaluation_seeds[::2]), "complement": list(evaluation_seeds[1::2])}
    backend_started = perf_counter()
    factory, default_method = _snapshot_specs()[model]
    requested_method = default_method if simulation_method is None else simulation_method
    reuse_density = requested_method == "prepared_density_matrix"
    use_compact_wires = reuse_density or requested_method == "compact_density_matrix"
    method = "density_matrix" if use_compact_wires else requested_method
    backend = factory()
    compile_started = perf_counter()
    comparison = compare_constraint_phase_covers(
        constraint, backend=backend, rounds=rounds, seed=seed, initial_layout=initial_layout,
        max_active_qubits=max_active_qubits, prefer_native_qasm=prefer_native_qasm,
    )
    if witness is not None:
        witness["timing_seconds"]["backend_setup"] = compile_started - backend_started
        witness["timing_seconds"]["compile_and_audit"] = perf_counter() - compile_started
        marks = comparison.report["candidates"]["direct"]["oracle_audit"]["marked_states"]
        if witness["classical_reference"]["candidate"] != next(iter(marks), None):
            raise RuntimeError("classical first witness disagrees with the exhaustive oracle audit")

    properties = backend.properties()
    snapshot = {
        "status": "PARTIAL",
        "model": model,
        "backend_version": backend.backend_version,
        "last_update_date": None if properties is None else properties.last_update_date.isoformat(),
        "method": method,
        "wire_representation": "compact" if use_compact_wires else "physical",
        "shots_per_batch": shots,
        "evaluation_seeds": seeds,
        "confidence_per_cover": COVER_CONFIDENCE_PER_ARM,
        "difference_family_confidence": 0.95,
        "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson; subtract opposite endpoints",
        "limits": {**_local_limits(), "shots": MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_sampling_batches": 4},
        "candidates": {
            name: {"compilation_sha256": program.report["openqasm"]["sha256"], "batches": [], "summary": None}
            for name, program in comparison.programs.items()
        },
        "complement_minus_direct": None,
        "observed_hit_leader": None,
        "local_cancellation_unconfirmed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    if reuse_density:
        snapshot["state_preparation"] = {}
    report = {**comparison.report, "snapshot": snapshot}
    if witness is not None:
        report["witness_budget"] = witness
    result = PhaseCoverComparison(report, comparison.selected, comparison.programs)
    try:
        if not comparison.report["alternate_available"]:
            snapshot["status"] = "NO_ALTERNATE_COVER"
            if witness is not None:
                witness["status"] = (
                    "PROVEN_EMPTY" if witness["classical_reference"]["candidate"] is None else snapshot["status"]
                )
            return result
        if any(
            not row["eligible"] or row["active_qubits"] > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
            for row in comparison.report["candidates"].values()
        ):
            snapshot["status"] = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
            if witness is not None:
                witness["status"] = snapshot["status"]
            return result

        simulator_started = perf_counter()
        from qiskit_aer import AerSimulator

        report["execution_mode"] = "SIMULATOR"
        snapshot["versions"] = {name: version(name) for name in ("qiskit-aer", "qiskit-ibm-runtime", "scipy")}
        if use_compact_wires:
            prepared = _compact_snapshot_inputs(backend, comparison.programs, AerSimulator)
            if witness is not None:
                witness["timing_seconds"]["noise_model_setup"] = perf_counter() - simulator_started
            if reuse_density:
                preparation_started = perf_counter()
                try:
                    for name, (circuit, simulator) in prepared.items():
                        readout_input, preparation = _prepare_density_snapshot(circuit, simulator, backend.name)
                        snapshot["state_preparation"][name] = preparation
                        if readout_input is None:
                            snapshot["status"] = "STATE_PREPARATION_INCOMPLETE"
                            snapshot["local_cancellation_unconfirmed"] = not preparation["local_job_cancelled"]
                            if witness is not None:
                                witness["status"] = snapshot["status"]
                                witness["local_cancellation_unconfirmed"] = snapshot["local_cancellation_unconfirmed"]
                            return result
                        prepared[name] = readout_input
                finally:
                    if witness is not None:
                        witness["timing_seconds"]["density_preparation"] = perf_counter() - preparation_started
            samplers = {
                name: partial(
                    _sample_snapshot, comparison.programs[name], backend, simulator, method=method,
                    verifier=constraint.matches, sampling_circuit=circuit,
                )
                for name, (circuit, simulator) in prepared.items()
            }
        else:
            simulator = _snapshot_simulator(backend, AerSimulator, method)
            if witness is not None:
                witness["timing_seconds"]["noise_model_setup"] = perf_counter() - simulator_started
            samplers = {
                name: partial(_sample_snapshot, program, backend, simulator, method=method, verifier=constraint.matches)
                for name, program in comparison.programs.items()
            }
        calibration_started = perf_counter()
        try:
            for name, sample_cover in samplers.items():
                record = snapshot["candidates"][name]
                for evaluation_seed in seeds[name]:
                    sample = sample_cover(shots=shots, seed=evaluation_seed)
                    record["batches"].append({"seed": evaluation_seed, **sample})
                    if sample.get("local_job_cancelled") is False:
                        snapshot["local_cancellation_unconfirmed"] = True
                        return result
                record["summary"] = _summarize_snapshot_batches(record["batches"], COVER_CONFIDENCE_PER_ARM)
        finally:
            if witness is not None:
                witness["timing_seconds"]["calibration"] = perf_counter() - calibration_started

        direct = snapshot["candidates"]["direct"]["summary"]
        complement = snapshot["candidates"]["complement"]["summary"]
        difference = _snapshot_hit_rate_difference(direct, complement)
        if difference is None:
            return result
        snapshot["complement_minus_direct"] = difference
        leader = "tie"
        if difference["estimate"] > 0:
            leader = "complement"
        elif difference["estimate"] < 0:
            leader = "direct"
        snapshot["observed_hit_leader"] = leader
        snapshot["status"] = "COMPLETED"
        if witness_budget is not None:
            _run_witness_validation(result, witness_budget, samplers)
        return result
    finally:
        if witness is not None:
            witness["timing_seconds"]["workflow_wall"] = perf_counter() - started
