"""Run bounded Grover experiments against packaged IBM calibration snapshots."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from concurrent.futures import TimeoutError as JobWaitTimeout
from importlib.metadata import version
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .compilation import MEASUREMENT_TOLERANCE, CompiledProgram, _compact_wires, compile_and_verify
from .core import austin_predicate, theoretical_success_probability
from .device_experiment import austin_classical_baseline, summarize_austin_counts, summarize_search_counts
from .qiskit_adapter import build_austin_circuit

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2
    from qiskit.result import Result
    from qiskit.transpiler import Target
    from qiskit_aer import AerSimulator
    from qiskit_ibm_runtime.fake_provider import FakeBackendV2


IBM_SNAPSHOT_MODELS = ("manila", "jakarta", "torino")
MAX_IBM_SNAPSHOT_SHOTS = 4_096
MAX_IBM_SNAPSHOT_ACTIVE_QUBITS = 7
MAX_AER_STATE_MEMORY_MB = 128
MAX_MPS_BOND_DIMENSION = 8
LOCAL_JOB_WAIT_SECONDS = 30
MAX_MOD4_SWEEP_SHOTS = 1024
MOD4_SWEEP_ROUNDS = (0, 1, 2, 3)
MAX_CONSTRAINT_SNAPSHOT_SHOTS = 256


def _wilson_interval(hits: int, shots: int) -> list[float]:
    """Two-sided 95% binomial interval for shot sampling only."""
    z = 1.959963984540054
    p = hits / shots
    denominator = 1 + z * z / shots
    center = (p + z * z / (2 * shots)) / denominator
    radius = z * math.sqrt(p * (1 - p) / shots + z * z / (4 * shots * shots)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _model_run(
    circuit: QuantumCircuit,
    backend: FakeBackendV2,
    simulator_type: type[AerSimulator],
    *,
    method: str,
    shots: int,
    seed: int,
    verifier: Callable[[str], bool],
) -> dict[str, Any]:
    started = perf_counter()
    compiled = compile_and_verify(circuit, backend=backend, seed=seed)
    compiled_at = perf_counter()
    active_wires = compiled.report["openqasm"]["export_wire_to_target_qubit"]
    properties = backend.properties()
    snapshot_date = None if properties is None else properties.last_update_date.isoformat()
    timings = {"compile_export_verify": compiled_at - started}
    report: dict[str, Any] = {
        "status": "COMPILED",
        "backend": backend.name,
        "backend_class": type(backend).__name__,
        "backend_version": backend.backend_version,
        "backend_interface_version": backend.version,
        "snapshot": {
            "source": "qiskit-ibm-runtime bundled fake backend properties",
            "last_update_date": snapshot_date,
        },
        "compilation": compiled.report,
        "active_target_qubits": active_wires,
        "simulation": None,
        "timing_seconds": timings,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }

    if len(active_wires) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        report["status"] = "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
        timings["wall"] = perf_counter() - started
        return report

    model_started = perf_counter()
    simulator = _snapshot_simulator(backend, simulator_type, method)
    timings["noise_model_setup"] = perf_counter() - model_started
    sample = _sample_snapshot(compiled, backend, simulator, method=method, shots=shots, seed=seed, verifier=verifier)
    timings.update(sample.pop("timing_seconds"))
    timings["wall"] = perf_counter() - started
    report.update(sample)
    return report


def _snapshot_simulator(
    backend: FakeBackendV2, simulator_type: type[AerSimulator], method: str,
) -> AerSimulator:
    options = {
        "enable_truncation": True,
        "max_memory_mb": MAX_AER_STATE_MEMORY_MB,
        "max_parallel_threads": 1,
    }
    if method == "matrix_product_state":
        options.update(
            matrix_product_state_max_bond_dimension=MAX_MPS_BOND_DIMENSION,
            matrix_product_state_truncation_threshold=0.0,
        )
    simulator = simulator_type.from_backend(backend, method=method, device="CPU", **options)
    noise_model = simulator.options.noise_model
    if noise_model is None or noise_model.is_ideal():
        raise RuntimeError(f"{backend.name} did not produce a calibrated Aer noise model")
    return simulator


def _reduced_noise_target(source: Target, active: tuple[int, ...]) -> Target:
    """Project calibrated properties onto compact indices for static noise derivation."""
    from qiskit.transpiler import Target

    wire_map = {wire: index for index, wire in enumerate(active)}
    qubit_properties = source.qubit_properties
    target = Target(
        num_qubits=len(active), dt=source.dt,
        qubit_properties=None if qubit_properties is None else [qubit_properties[wire] for wire in active],
    )
    for name, entries in source.items():
        retained = {}
        for qubits, properties in (entries or {}).items():
            if properties is None:
                continue
            if qubits is None:
                if properties.duration or properties.error:
                    raise ValueError("compact snapshot requires calibrated operations on explicit wire tuples")
                continue
            if all(qubit in wire_map for qubit in qubits):
                retained[tuple(wire_map[qubit] for qubit in qubits)] = properties
        if retained:
            target.add_instruction(source.operation_from_name(name), retained, name=name)
    return target


def _compact_snapshot_inputs(
    backend: BackendV2, programs: Mapping[str, CompiledProgram], simulator_type: type[AerSimulator],
) -> dict[str, tuple[QuantumCircuit, AerSimulator]]:
    """Prepare one compact circuit and its remapped noise model per verified cover."""
    from qiskit.circuit import Gate, Measure
    from qiskit_aer.noise import NoiseModel
    from qiskit_aer.noise.device import basic_device_gate_errors, basic_device_readout_errors

    compact_programs = {}
    for name, program in programs.items():
        compact, active = _compact_wires(program.circuit)
        exported = program.report["openqasm"]
        if active != exported["export_wire_to_target_qubit"] or len(active) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            raise ValueError("compact snapshot requires the verified active wires within the simulator limit")
        measured = {}
        measured_wires = set()
        for item in compact.data:
            wires = {compact.find_bit(bit).index for bit in item.qubits}
            if isinstance(item.operation, Measure):
                classical = compact.find_bit(item.clbits[0]).index
                wire = next(iter(wires))
                if classical in measured or wire in measured_wires:
                    raise ValueError("compact snapshot requires unique terminal measurements")
                measured[classical] = active[wire]
                measured_wires.add(wire)
            elif (
                not isinstance(item.operation, Gate)
                or item.clbits
                or item.operation.is_parameterized()
                or not measured_wires.isdisjoint(wires)
            ):
                raise ValueError("compact snapshot supports bound static gates and terminal measurements")
        measurement_map = [measured[index] for index in sorted(measured)]
        if len(measurement_map) != compact.num_clbits or measurement_map != exported["classical_bit_to_target_qubit"]:
            raise ValueError("compact snapshot changed the verified measurement map")
        compact_programs[name] = compact, tuple(active)

    # Use the same Aer device-error functions as NoiseModel.from_backend. The
    # additional delay relaxation pass is outside the verified static circuit.
    # Derive shared channels once, including when covers have different active maps.
    union = tuple(sorted({wire for _, active in compact_programs.values() for wire in active}))
    union_wire = {physical: index for index, physical in enumerate(union)}
    source_target = backend.target
    target = source_target if len(union) == source_target.num_qubits else _reduced_noise_target(source_target, union)
    gate_errors = basic_device_gate_errors(target=target)
    readout_errors = basic_device_readout_errors(target=target)

    simulators = {}
    prepared = {}
    for name, (compact, active) in compact_programs.items():
        simulator = simulators.get(active)
        if simulator is None:
            wire_map = {union_wire[physical]: index for index, physical in enumerate(active)}
            noise = NoiseModel(basis_gates=backend.operation_names)
            for operation, qubits, error in gate_errors:
                if all(wire in wire_map for wire in qubits):
                    noise.add_quantum_error(error, operation, [wire_map[wire] for wire in qubits])
            for qubits, error in readout_errors:
                if len(qubits) != 1:
                    raise ValueError("compact snapshot supports single-wire device readout channels")
                if qubits[0] in wire_map:
                    noise.add_readout_error(error, [wire_map[qubits[0]]])
            simulator = simulator_type(
                method="density_matrix", device="CPU", noise_model=noise, enable_truncation=True,
                max_memory_mb=MAX_AER_STATE_MEMORY_MB, max_parallel_threads=1,
            )
            simulators[active] = simulator
        prepared[name] = compact, simulator
    return prepared


def _checked_snapshot_metadata(result: Result, backend_name: str, input_wires: list[int]) -> dict[str, Any]:
    if not result.success or len(result.results) != 1 or not result.results[0].success:
        raise RuntimeError(f"{backend_name} snapshot simulation did not complete")
    metadata = result.results[0].metadata
    if metadata.get("num_qubits") != len(input_wires) or sorted(metadata.get("active_input_qubits", [])) != input_wires:
        raise RuntimeError(f"{backend_name} snapshot simulation did not preserve the verified active wire set")
    return metadata


def _prepare_density_snapshot(
    circuit: QuantumCircuit, simulator: AerSimulator, backend_name: str,
) -> tuple[tuple[QuantumCircuit, AerSimulator] | None, dict[str, Any]]:
    """Evolve an audited static compact circuit once and prepare its readout sampler."""
    from qiskit.circuit import Measure
    from qiskit.quantum_info import DensityMatrix
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel, ReadoutError

    started = perf_counter()
    source_noise = simulator.options.noise_model
    readout = NoiseModel(basis_gates=source_noise.basis_gates)
    for channel in source_noise.to_dict()["errors"]:
        if channel["type"] == "qerror" and "measure" not in channel["operations"]:
            continue
        wires = channel.get("gate_qubits")
        if channel["type"] != "roerror" or not wires or any(len(group) != 1 for group in wires):
            raise ValueError("prepared snapshot requires static gate noise and single-wire local readout")
        for group in wires:
            readout.add_readout_error(ReadoutError(channel["probabilities"]), group)

    body = circuit.copy_empty_like()
    measurements = []
    for item in circuit.data:
        if isinstance(item.operation, Measure):
            measurements.append(item)
        else:
            body.append(item)
    # The compact preflight ensures each measure can commute past later disjoint gates.
    # No readout channels are applied during this gate-only evolution.
    label = "snapshot_prepared_density"
    body.save_density_matrix(label=label)
    job = simulator.run(body, shots=1, seed_simulator=0)
    try:
        result = job.result(timeout=LOCAL_JOB_WAIT_SECONDS)
    except JobWaitTimeout:
        return None, {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": job.cancel(),
            "wall_seconds": perf_counter() - started,
        }
    metadata = _checked_snapshot_metadata(result, backend_name, list(range(circuit.num_qubits)))
    if metadata.get("method") != "density_matrix":
        raise RuntimeError(f"{backend_name} snapshot preparation did not use the density-matrix method")

    state = DensityMatrix(result.data(0)[label])
    replay = circuit.copy_empty_like()
    # Aer validates trace, Hermiticity, positivity, and width. Keep its raw state
    # so every batch sees the same pre-readout matrix, including numerical drift.
    try:
        replay.set_density_matrix(state)
    except ValueError as exc:
        raise ValueError(f"{backend_name} prepared density is invalid for replay: {exc}") from exc
    for item in measurements:
        replay.append(item)
    trace = complex(state.trace())
    readout_simulator = AerSimulator(
        method="density_matrix", device="CPU", noise_model=readout, enable_truncation=True,
        max_memory_mb=MAX_AER_STATE_MEMORY_MB, max_parallel_threads=1,
    )
    return (replay, readout_simulator), {
        "status": "COMPLETED",
        "matrix_bytes": int(state.data.nbytes),
        "readout_noise_wires": readout.noise_qubits,
        "trace_real": trace.real,
        "trace_imag": trace.imag,
        "wall_seconds": perf_counter() - started,
    }


def _sample_snapshot(
    compiled: CompiledProgram, backend: FakeBackendV2, simulator: AerSimulator, *,
    method: str, shots: int, seed: int, verifier: Callable[[str], bool],
    sampling_circuit: QuantumCircuit | None = None,
) -> dict[str, Any]:
    active_wires = compiled.report["openqasm"]["export_wire_to_target_qubit"]
    if len(active_wires) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        return {"status": "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT", "simulation": None, "timing_seconds": {}}
    model_ready = perf_counter()

    # Physical circuits keep source indices. Compact inputs carry their own remapped channels.
    if sampling_circuit is None:
        sampling_circuit = compiled.circuit
        input_wires = active_wires
    else:
        input_wires = list(range(len(active_wires)))
    job = simulator.run(sampling_circuit, shots=shots, seed_simulator=seed)
    try:
        result = job.result(timeout=LOCAL_JOB_WAIT_SECONDS)
    except JobWaitTimeout:
        return {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "simulation": None,
            "local_job_cancelled": job.cancel(),
            "timing_seconds": {"sample_wait": perf_counter() - model_ready},
        }
    sampled_at = perf_counter()
    metadata = _checked_snapshot_metadata(result, backend.name, input_wires)

    counts = summarize_search_counts(
        result.get_counts(0), requested_shots=shots, width=compiled.circuit.num_clbits, verifier=verifier
    )
    if counts["completed_shots"] != shots:
        raise RuntimeError(f"{backend.name} snapshot simulation returned an incomplete shot count")
    verified_at = perf_counter()
    simulation = {
        "execution_mode": "SIMULATOR",
        "method": method,
        "noise_instructions": sorted(simulator.options.noise_model.noise_instructions),
        "simulated_qubits": metadata["num_qubits"],
        "aer_experiment_seconds": metadata.get("time_taken"),
        "verified_hit_rate_wilson_95": _wilson_interval(counts["verified_shots"], shots),
        **counts,
    }
    if method == "matrix_product_state":
        simulation["max_bond_dimension"] = MAX_MPS_BOND_DIMENSION
        simulation["truncation_threshold"] = 0.0
    return {
        "status": "COMPLETED",
        "simulation": simulation,
        "timing_seconds": {"sample": sampled_at - model_ready, "verify": verified_at - sampled_at},
    }


def _summarize_snapshot_batches(batches: list[dict[str, Any]], confidence: float) -> dict[str, Any] | None:
    """Summarize two complete seed batches from the same compiled circuit."""
    if len(batches) != 2 or any(batch["status"] != "COMPLETED" for batch in batches):
        return None

    from scipy.stats import binomtest

    hits = sum(batch["simulation"]["verified_shots"] for batch in batches)
    shots = sum(batch["simulation"]["completed_shots"] for batch in batches)
    interval = binomtest(hits, shots).proportion_ci(confidence_level=confidence, method="exact")
    return {
        "verified_shots": hits,
        "completed_shots": shots,
        "verified_hit_rate": hits / shots,
        "interval": [float(interval.low), float(interval.high)],
    }


def _snapshot_hit_rate_difference(
    reference: dict[str, Any] | None, candidate: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Subtract opposing bounds from the two prespecified rate intervals."""
    if reference is None or candidate is None:
        return None
    lower = candidate["interval"][0] - reference["interval"][1]
    upper = candidate["interval"][1] - reference["interval"][0]
    status = "UNRESOLVED"
    if lower > 0:
        status = "IMPROVED"
    elif upper < 0:
        status = "DEGRADED"
    return {
        "estimate": candidate["verified_hit_rate"] - reference["verified_hit_rate"],
        "interval": [lower, upper],
        "status": status,
    }


def _validate_snapshot_request(model: str, shots: int, seed: int) -> None:
    if model not in (*IBM_SNAPSHOT_MODELS, "all"):
        raise ValueError(f"model must be one of {', '.join(IBM_SNAPSHOT_MODELS)}, or all")
    if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= MAX_IBM_SNAPSHOT_SHOTS:
        raise ValueError(f"shots must be an integer from 1 to {MAX_IBM_SNAPSHOT_SHOTS}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer from 0 to 2**32 - 1")


def _snapshot_specs():
    from qiskit_ibm_runtime.fake_provider import FakeJakartaV2, FakeManilaV2, FakeTorino

    return {
        "manila": (FakeManilaV2, "density_matrix"),
        "jakarta": (FakeJakartaV2, "density_matrix"),
        "torino": (FakeTorino, "matrix_product_state"),
    }


def _local_limits() -> dict[str, int]:
    return {
        "active_qubits": MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
        "aer_state_memory_mb": MAX_AER_STATE_MEMORY_MB,
        "aer_threads": 1,
        "local_result_wait_seconds": LOCAL_JOB_WAIT_SECONDS,
        "shots": MAX_IBM_SNAPSHOT_SHOTS,
    }


def run_constraint_snapshot(
    circuit: QuantumCircuit, verifier: Callable[[str], bool], *,
    model: str, shots: int = 64, seed: int = 7,
) -> dict[str, Any]:
    """Compile and sample one audited declarative search circuit on an offline snapshot."""
    _validate_snapshot_request(model, shots, seed)
    if model == "all":
        raise ValueError("constraint snapshot requires one named model")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"constraint snapshot shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS}")
    if not callable(verifier):
        raise TypeError("verifier must be callable")

    from qiskit_aer import AerSimulator

    factory, method = _snapshot_specs()[model]
    backend = factory()
    report: dict[str, Any] = {
        "experiment": "LOCAL_IBM_SNAPSHOT_DECLARATIVE_CONSTRAINT",
        "requested_model": model,
        "requested_shots": shots,
        "seed": seed,
        "execution_mode": "SIMULATOR",
        "limits": {**_local_limits(), "shots": MAX_CONSTRAINT_SNAPSHOT_SHOTS},
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime")},
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    if circuit.num_qubits > backend.num_qubits:
        return {
            **report, "status": "INSUFFICIENT_TARGET_WIDTH", "circuit_qubits": circuit.num_qubits,
            "target_qubits": backend.num_qubits, "simulation": None,
        }
    return {
        **report, **_model_run(circuit, backend, AerSimulator, method=method, shots=shots, seed=seed, verifier=verifier),
    }


def run_ibm_snapshot_comparison(
    *, model: str = "all", shots: int = 1024, seed: int = 7
) -> dict[str, Any]:
    """Compare local ideal and snapshot-noisy Austin runs with the direct solver."""
    _validate_snapshot_request(model, shots, seed)
    started = perf_counter()
    from qiskit.primitives import StatevectorSampler
    from qiskit_aer import AerSimulator
    specs = _snapshot_specs()
    circuit = build_austin_circuit()
    baseline = austin_classical_baseline()
    ideal_started = perf_counter()
    ideal_counts = StatevectorSampler(seed=seed).run([circuit], shots=shots).result()[0].data.answer.get_counts()
    ideal_sampled = perf_counter()
    ideal = summarize_austin_counts(ideal_counts, requested_shots=shots)
    if ideal["completed_shots"] != shots:
        raise RuntimeError("ideal simulator returned an incomplete shot count")
    ideal_verified = perf_counter()
    ideal.update(
        execution_mode="SIMULATOR",
        method="qiskit_statevector_sampler",
        sample_seconds=ideal_sampled - ideal_started,
        verify_seconds=ideal_verified - ideal_sampled,
        wall_seconds=ideal_verified - ideal_started,
    )

    models = {}
    selected = IBM_SNAPSHOT_MODELS if model == "all" else (model,)
    for name in selected:
        factory, method = specs[name]
        load_started = perf_counter()
        backend = factory()
        load_seconds = perf_counter() - load_started
        models[name] = _model_run(
            circuit, backend, AerSimulator, method=method, shots=shots, seed=seed, verifier=austin_predicate
        )
        timings = models[name]["timing_seconds"]
        timings["load_backend"] = load_seconds
        timings["wall"] += load_seconds

    return {
        "status": "COMPLETED" if all(value["status"] == "COMPLETED" for value in models.values()) else "PARTIAL",
        "execution_mode": "SIMULATOR",
        "experiment": "LOCAL_IBM_CALIBRATION_SNAPSHOT_COMPARISON",
        "circuit": "AUSTIN_3Q_TWO_CZ_CONSTRAINT_SEARCH",
        "basis_order": "q2 q1 q0",
        "requested_model": model,
        "requested_shots": shots,
        "seed": seed,
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime")},
        "limits": _local_limits(),
        "classical": baseline,
        "ideal": ideal,
        "models": models,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "wall_seconds": perf_counter() - started,
    }


def run_mod4_snapshot_comparison(
    *, model: str = "all", shots: int = 1024, seed: int = 7
) -> dict[str, Any]:
    """Compare two verified implementations under the same historical backend model."""
    _validate_snapshot_request(model, shots, seed)
    started = perf_counter()
    from qiskit_aer import AerSimulator

    from .grover_search import (
        audit_phase_oracle,
        build_grover_search_circuit,
        build_mod4_range_direct_oracle,
        build_mod4_range_oracle,
        mod4_range_predicate,
    )

    specs = _snapshot_specs()
    variants = {"work-qubit": build_mod4_range_oracle(), "direct": build_mod4_range_direct_oracle()}
    audits = {name: audit_phase_oracle(oracle, 4, mod4_range_predicate) for name, oracle in variants.items()}
    if audits["work-qubit"] != audits["direct"]:
        raise RuntimeError("modulo-4 oracle variants did not pass the same exhaustive predicate audit")
    circuits = {name: build_grover_search_circuit(oracle, 4, iterations=2) for name, oracle in variants.items()}
    models = {}
    for name in IBM_SNAPSHOT_MODELS if model == "all" else (model,):
        factory, method = specs[name]
        load_started = perf_counter()
        backend = factory()
        load_seconds = perf_counter() - load_started
        results = {
            variant: _model_run(
                circuit, backend, AerSimulator, method=method, shots=shots, seed=seed,
                verifier=mod4_range_predicate,
            )
            for variant, circuit in circuits.items()
        }
        reference, direct = results["work-qubit"], results["direct"]
        p, q = (
            result["compilation"]["verification"]["roundtrip_probabilities"]
            for result in (reference, direct)
        )
        ideal_distance = math.fsum(abs(p.get(bits, 0) - q.get(bits, 0)) for bits in p.keys() | q.keys()) / 2
        if ideal_distance > MEASUREMENT_TOLERANCE:
            raise RuntimeError(f"{name} oracle variants did not preserve the same ideal output distribution")
        completed = all(result["status"] == "COMPLETED" for result in results.values())
        hit_rate_change = (
            direct["simulation"]["verified_hit_rate"] - reference["simulation"]["verified_hit_rate"]
            if completed else None
        )
        models[name] = {
            "status": "COMPLETED" if completed else "PARTIAL",
            "variants": results,
            "ideal_roundtrip_total_variation_distance": ideal_distance,
            "direct_minus_work_qubit_hit_rate": hit_rate_change,
            "load_backend_seconds": load_seconds,
        }
    return {
        "status": "COMPLETED" if all(result["status"] == "COMPLETED" for result in models.values()) else "PARTIAL",
        "execution_mode": "SIMULATOR",
        "experiment": "LOCAL_IBM_SNAPSHOT_MOD4_ORACLE_COMPARISON",
        "basis_order": "q3 q2 q1 q0",
        "grover_rounds": 2,
        "requested_model": model,
        "requested_shots_per_variant": shots,
        "seed_transpiler": seed,
        "seed_simulator": seed,
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime")},
        "limits": _local_limits(),
        "oracle_audits": audits,
        "models": models,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "wall_seconds": perf_counter() - started,
    }


def _mod4_round_sweep_model(
    circuits: dict[int, QuantumCircuit], backend: FakeBackendV2, simulator_type: type[AerSimulator], *,
    method: str, shots: int, calibration_seed: int, validation_seed: int, transpiler_seed: int,
    ideal_rounds: int, ideal_rates: dict[int, float], verifier: Callable[[str], bool],
) -> dict[str, Any]:
    started = perf_counter()
    anchor = compile_and_verify(circuits[ideal_rounds], backend=backend, seed=transpiler_seed)
    layout = anchor.report["initial_logical_to_target_qubit"]
    if layout is None:
        raise RuntimeError(f"{backend.name} did not report the initial layout for the ideal baseline")
    programs = {
        rounds: compile_and_verify(circuit, backend=backend, seed=transpiler_seed, initial_layout=layout)
        for rounds, circuit in circuits.items()
    }
    for rounds, program in programs.items():
        distribution = program.report["verification"]["roundtrip_probabilities"]
        ideal_rate = math.fsum(p for bits, p in distribution.items() if verifier(bits))
        if not math.isclose(ideal_rate, ideal_rates[rounds], rel_tol=0, abs_tol=MEASUREMENT_TOLERANCE):
            raise RuntimeError(f"{backend.name} compiled round {rounds} disagrees with the ideal success model")
    compiled_at = perf_counter()
    simulator = _snapshot_simulator(backend, simulator_type, method)
    simulator_at = perf_counter()
    properties = backend.properties()
    report: dict[str, Any] = {
        "status": "PARTIAL_CALIBRATION",
        "backend": backend.name,
        "backend_version": backend.backend_version,
        "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
        "initial_logical_to_target_qubit": layout,
        "compilations": {rounds: program.report for rounds, program in programs.items()},
        "calibration": {},
        "selected_rounds": None,
        "validation": {},
        "validation_selected_minus_ideal_hit_rate": None,
        "local_cancellation_unconfirmed": False,
        "timing_seconds": {
            "compile_export_verify": compiled_at - started,
            "noise_model_setup": simulator_at - compiled_at,
        },
    }
    try:
        for rounds, program in programs.items():
            sample = _sample_snapshot(
                program, backend, simulator, method=method, shots=shots, seed=calibration_seed, verifier=verifier
            )
            report["calibration"][rounds] = sample
            if sample.get("local_job_cancelled") is False:
                report["local_cancellation_unconfirmed"] = True
                return report
        if any(sample["status"] != "COMPLETED" for sample in report["calibration"].values()):
            return report

        # Fixed before validation: maximize verified hits, breaking ties by fewer rounds.
        selected = max(programs, key=lambda rounds: (
            report["calibration"][rounds]["simulation"]["verified_shots"], -rounds,
        ))
        report["selected_rounds"] = selected
        report["status"] = "PARTIAL_VALIDATION"
        for rounds in dict.fromkeys((selected, ideal_rounds)):
            sample = _sample_snapshot(
                programs[rounds], backend, simulator, method=method, shots=shots, seed=validation_seed, verifier=verifier
            )
            report["validation"][rounds] = sample
            if sample.get("local_job_cancelled") is False:
                report["local_cancellation_unconfirmed"] = True
                return report
        if any(sample["status"] != "COMPLETED" for sample in report["validation"].values()):
            return report
        selected_rate = report["validation"][selected]["simulation"]["verified_hit_rate"]
        baseline_rate = report["validation"][ideal_rounds]["simulation"]["verified_hit_rate"]
        report["validation_selected_minus_ideal_hit_rate"] = selected_rate - baseline_rate
        report["status"] = "COMPLETED"
        return report
    finally:
        report["timing_seconds"]["wall"] = perf_counter() - started


def run_mod4_round_sweep(
    *, model: str = "all", shots: int = 1024, calibration_seed: int = 7,
    validation_seed: int = 19, transpiler_seed: int = 7,
) -> dict[str, Any]:
    """Select a round count on calibration shots and compare with the ideal choice on fresh shots."""
    for seed in (calibration_seed, validation_seed, transpiler_seed):
        _validate_snapshot_request(model, shots, seed)
    if shots > MAX_MOD4_SWEEP_SHOTS:
        raise ValueError(f"round sweep shots must be at most {MAX_MOD4_SWEEP_SHOTS} per circuit per stage")
    if calibration_seed == validation_seed:
        raise ValueError("calibration and validation seeds must differ")

    from qiskit_aer import AerSimulator

    from .grover_search import (
        audit_phase_oracle,
        build_grover_search_circuit,
        build_mod4_range_direct_oracle,
        mod4_range_predicate,
    )

    started = perf_counter()
    oracle = build_mod4_range_direct_oracle()
    audit = audit_phase_oracle(oracle, 4, mod4_range_predicate)
    ideal_rates = {
        rounds: theoretical_success_probability(4, audit["marked_count"], rounds)
        for rounds in MOD4_SWEEP_ROUNDS
    }
    ideal_rounds = max(MOD4_SWEEP_ROUNDS, key=lambda rounds: (ideal_rates[rounds], -rounds))
    circuits = {
        rounds: build_grover_search_circuit(oracle, 4, iterations=rounds) for rounds in MOD4_SWEEP_ROUNDS
    }
    specs = _snapshot_specs()
    selected_models = IBM_SNAPSHOT_MODELS if model == "all" else (model,)
    models = {}
    for name in selected_models:
        factory, method = specs[name]
        result = _mod4_round_sweep_model(
            circuits, factory(), AerSimulator, method=method, shots=shots,
            calibration_seed=calibration_seed, validation_seed=validation_seed, transpiler_seed=transpiler_seed,
            ideal_rounds=ideal_rounds, ideal_rates=ideal_rates, verifier=mod4_range_predicate,
        )
        models[name] = result
        if result["local_cancellation_unconfirmed"]:
            break
    return {
        "status": "COMPLETED" if all(result["status"] == "COMPLETED" for result in models.values()) else "PARTIAL",
        "execution_mode": "SIMULATOR",
        "experiment": "LOCAL_IBM_SNAPSHOT_MOD4_ROUND_SWEEP",
        "oracle_variant": "direct",
        "requested_model": model,
        "models_not_run": [name for name in selected_models if name not in models],
        "basis_order": "q3 q2 q1 q0",
        "round_candidates": list(MOD4_SWEEP_ROUNDS),
        "ideal_rounds": ideal_rounds,
        "ideal_hit_rates": ideal_rates,
        "shots_per_circuit_per_stage": shots,
        "calibration_seed": calibration_seed,
        "validation_seed": validation_seed,
        "transpiler_seed": transpiler_seed,
        "selection_policy": "highest calibration verified-shot count, then fewer rounds",
        "layout_policy": "freeze the ideal baseline's initial data placement across candidates",
        "limits": {**_local_limits(), "shots": MAX_MOD4_SWEEP_SHOTS, "max_samples_per_model": 6},
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime")},
        "oracle_audit": audit,
        "models": models,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "wall_seconds": perf_counter() - started,
    }
