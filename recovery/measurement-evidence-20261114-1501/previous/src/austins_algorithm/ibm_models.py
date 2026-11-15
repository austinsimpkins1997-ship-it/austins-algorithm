"""Run bounded Grover experiments against packaged IBM calibration snapshots."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as JobWaitTimeout
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from functools import cache, partial
from importlib.metadata import version
from time import perf_counter
from typing import TYPE_CHECKING, Any, TypeVar

from .compilation import (
    MEASUREMENT_TOLERANCE, CompiledProgram, _compact_wires, _transpile_initial_layout, compile_and_verify,
)
from .core import austin_predicate, theoretical_success_probability
from .device_experiment import austin_classical_baseline, summarize_austin_counts, summarize_search_counts
from .qiskit_adapter import build_austin_circuit

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.circuit import CircuitInstruction
    from qiskit.providers import BackendV2
    from qiskit.quantum_info import DensityMatrix
    from qiskit.result import Result
    from qiskit.result.models import ExperimentResult
    from qiskit.transpiler import Target
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel, QuantumError, ReadoutError
    from qiskit_ibm_runtime.fake_provider import FakeBackendV2
    from qiskit_ibm_runtime.models import BackendProperties

    _GateChannels = Sequence[tuple[str, Sequence[int], QuantumError]]
    _ReadoutChannels = list[tuple[Sequence[int], ReadoutError]]
    _StaticSnapshotChannels = tuple[_GateChannels, _ReadoutChannels]


IBM_SNAPSHOT_MODELS = ("manila", "jakarta", "torino")
MAX_IBM_SNAPSHOT_SHOTS = 4_096
MAX_IBM_SNAPSHOT_ACTIVE_QUBITS = 7
MAX_AER_STATE_MEMORY_MB = 128
MAX_MPS_BOND_DIMENSION = 8
LOCAL_JOB_WAIT_SECONDS = 30
MAX_MOD4_SWEEP_SHOTS = 1024
MOD4_SWEEP_ROUNDS = (0, 1, 2, 3)
MAX_CONSTRAINT_SNAPSHOT_SHOTS = 256
_PREPARED_DENSITY_FUSION_OPTIONS = {"fusion_enable": True, "fusion_threshold": 5, "fusion_max_qubit": 2}
_DENSITY_SAVE_LABEL = "snapshot_prepared_density"
_MAX_DENSITY_BATCH_ROUNDS = 3
_ProgramKey = TypeVar("_ProgramKey")


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
    make_simulator: Callable[[], AerSimulator],
    *,
    method: str,
    shots: int,
    seed: int,
    verifier: Callable[[str], bool],
    prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    started = perf_counter()
    compiled = compile_and_verify(circuit, backend=backend, seed=seed, prefer_native_qasm=prefer_native_qasm)
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
    simulator = make_simulator()
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


def _derive_static_snapshot_channels(source_target: Target, union: tuple[int, ...]) -> _StaticSnapshotChannels:
    from qiskit_aer.noise.device import basic_device_gate_errors, basic_device_readout_errors

    target = source_target if len(union) == source_target.num_qubits else _reduced_noise_target(source_target, union)
    return basic_device_gate_errors(target=target), basic_device_readout_errors(target=target)


def _project_static_snapshot_channels(
    channels: _StaticSnapshotChannels, covering: tuple[int, ...], requested: tuple[int, ...],
) -> _StaticSnapshotChannels:
    # Aer derives each channel from its ordered gate wires or one readout wire.
    compact = {wire: index for index, wire in enumerate(requested)}
    remap = {index: compact[wire] for index, wire in enumerate(covering) if wire in compact}
    gates, readout = channels
    return (
        tuple((operation, tuple(remap[wire] for wire in wires), error)
              for operation, wires, error in gates if all(wire in remap for wire in wires)),
        [(tuple(remap[wire] for wire in wires), error)
         for wires, error in readout if all(wire in remap for wire in wires)],
    )


class _StaticSnapshotChannelCache:
    """Keep isolated static channel templates for one prepared backend target."""

    def __init__(self, target: Target) -> None:
        self._target = target
        self._by_union: dict[tuple[int, ...], _StaticSnapshotChannels] = {}

    def for_union(self, target: Target, union: tuple[int, ...]) -> _StaticSnapshotChannels:
        if target is not self._target:
            raise ValueError("static noise channel cache belongs to a different backend target")
        if union not in self._by_union:
            covering = self._covering_union(union)
            if covering is None:
                gate_errors, readout_errors = _derive_static_snapshot_channels(target, union)
                gates = tuple((operation, tuple(wires), error) for operation, wires, error in gate_errors)
                self._by_union[union] = gates, readout_errors
            else:
                self._by_union[union] = _project_static_snapshot_channels(self._by_union[covering], covering, union)
        gates, readout_errors = self._by_union[union]
        # The static Aer path reads gate channels during execution. Keep their
        # descriptors fixed and give each layout independent readout buffers.
        return gates, deepcopy(readout_errors)

    def _covering_union(self, union: tuple[int, ...]) -> tuple[int, ...] | None:
        requested = set(union)
        covering = min(
            (candidate for candidate in self._by_union if len(candidate) > len(union) and requested.issubset(candidate)),
            key=lambda candidate: (len(candidate), candidate), default=None,
        )
        if covering is not None and len(covering) == self._target.num_qubits:
            # Full-device derivation bypasses the compact target's global calibration check.
            for _, entries in self._target.items():
                properties = (entries or {}).get(None)
                if properties is not None and (properties.duration or properties.error):
                    return None
        return covering


def _compact_snapshot_inputs(
    backend: BackendV2, programs: Mapping[_ProgramKey, CompiledProgram], simulator_type: type[AerSimulator], *,
    static_channels: _StaticSnapshotChannelCache | None = None, prune_unused_gate_noise: bool = False,
) -> dict[_ProgramKey, tuple[QuantumCircuit, AerSimulator]]:
    """Prepare one compact circuit and its remapped noise model per verified program."""
    from qiskit.circuit import Gate, Measure
    from qiskit_aer.noise import NoiseModel

    stock_gates = {}
    if prune_unused_gate_noise:
        from qiskit.circuit.library import CXGate, CZGate, ECRGate, IGate, RZGate, SXGate, XGate

        stock_gates = {
            gate.name: gate
            for gate in (CXGate(), CZGate(), ECRGate(), IGate(), RZGate(0), SXGate(), XGate())
        }

    compact_programs = {}
    used_channels_by_active = {}
    for name, program in programs.items():
        compact, active = _compact_wires(program.circuit)
        exported = program.report["openqasm"]
        if active != exported["export_wire_to_target_qubit"] or len(active) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            raise ValueError("compact snapshot requires the verified active wires within the simulator limit")
        active = tuple(active)
        used_channels = used_channels_by_active.setdefault(active, set()) if stock_gates else None
        measured = {}
        measured_wires = set()
        for item in compact.data:
            operation = item.operation
            wires = tuple(compact.find_bit(bit).index for bit in item.qubits)
            if isinstance(operation, Measure):
                classical = compact.find_bit(item.clbits[0]).index
                wire = wires[0]
                if classical in measured or wire in measured_wires:
                    raise ValueError("compact snapshot requires unique terminal measurements")
                measured[classical] = active[wire]
                measured_wires.add(wire)
            elif (
                not isinstance(operation, Gate)
                or item.clbits
                or operation.is_parameterized()
                or not measured_wires.isdisjoint(wires)
            ):
                raise ValueError("compact snapshot supports bound static gates and terminal measurements")
            elif used_channels is not None:
                # Aer may use the label as its noise key and expand open controls into extra X gates.
                stock_gate = stock_gates.get(operation.name)
                if (
                    stock_gate is None
                    or type(operation) not in (type(stock_gate), stock_gate.base_class)
                    or operation.label is not None
                    or getattr(operation, "ctrl_state", 1) != 1
                    or getattr(operation, "condition", None) is not None
                ):
                    used_channels_by_active[active] = used_channels = None
                else:
                    used_channels.add((operation.name, wires))
        measurement_map = [measured[index] for index in sorted(measured)]
        if len(measurement_map) != compact.num_clbits or measurement_map != exported["classical_bit_to_target_qubit"]:
            raise ValueError("compact snapshot changed the verified measurement map")
        compact_programs[name] = compact, active

    # Use the same Aer device-error functions as NoiseModel.from_backend. The
    # additional delay relaxation pass is outside the verified static circuit.
    # Derive shared channels once, including when covers have different active maps.
    union = tuple(sorted({wire for _, active in compact_programs.values() for wire in active}))
    union_wire = {physical: index for index, physical in enumerate(union)}
    source_target = backend.target
    if static_channels is None:
        gate_errors, readout_errors = _derive_static_snapshot_channels(source_target, union)
    else:
        gate_errors, readout_errors = static_channels.for_union(source_target, union)

    simulators = {}
    prepared = {}
    for name, (compact, active) in compact_programs.items():
        simulator = simulators.get(active)
        if simulator is None:
            wire_map = {union_wire[physical]: index for index, physical in enumerate(active)}
            basis_gates = backend.operation_names
            noise = NoiseModel(basis_gates=basis_gates)
            mapped_gate_errors = [
                (operation, tuple(wire_map[wire] for wire in qubits), error)
                for operation, qubits, error in gate_errors
                if all(wire in wire_map for wire in qubits)
            ]
            used_channels = used_channels_by_active.get(active)
            if used_channels and any(
                (operation, qubits) in used_channels and not error.ideal()
                for operation, qubits, error in mapped_gate_errors
            ):
                # Keep other channel types and any rows that can extend the simulator basis.
                mapped_gate_errors = [
                    (operation, qubits, error)
                    for operation, qubits, error in mapped_gate_errors
                    if (operation, qubits) in used_channels
                    or operation not in stock_gates
                    or operation not in basis_gates
                    or len(qubits) != stock_gates[operation].num_qubits
                    or error.num_qubits != len(qubits)
                ]
            for operation, qubits, error in mapped_gate_errors:
                noise.add_quantum_error(error, operation, list(qubits))
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
    if not result.success or len(result.results) != 1:
        raise RuntimeError(f"{backend_name} snapshot simulation did not complete")
    return _checked_snapshot_experiment_metadata(result.results[0], backend_name, input_wires)


def _checked_snapshot_experiment_metadata(
    experiment: ExperimentResult, backend_name: str, input_wires: list[int],
) -> dict[str, Any]:
    if not experiment.success:
        raise RuntimeError(f"{backend_name} snapshot simulation did not complete")
    metadata = experiment.metadata
    if metadata.get("num_qubits") != len(input_wires) or sorted(metadata.get("active_input_qubits", [])) != input_wires:
        raise RuntimeError(f"{backend_name} snapshot simulation did not preserve the verified active wire set")
    return metadata


def _density_readout_simulator(readout: NoiseModel) -> AerSimulator:
    from qiskit_aer import AerSimulator

    return AerSimulator(
        method="density_matrix", device="CPU", noise_model=readout, enable_truncation=True,
        max_memory_mb=MAX_AER_STATE_MEMORY_MB, max_parallel_threads=1,
    )


@dataclass(frozen=True)
class _SnapshotDensityReplay:
    """Own the immutable inputs needed to rebuild one verified density replay."""

    qubits: int
    clbits: int
    global_phase: float
    matrix: bytes
    measurements: tuple[tuple[int, int], ...]
    basis_gates: tuple[str, ...]
    readout_errors: tuple[tuple[int, tuple[tuple[float, ...], ...]], ...]

    @classmethod
    def capture(cls, replay: QuantumCircuit, simulator: AerSimulator) -> _SnapshotDensityReplay:
        from numpy import asarray, complex128
        from qiskit.circuit import Measure
        from qiskit_aer.library import SetDensityMatrix

        initial, *measurements = replay.data
        if not isinstance(initial.operation, SetDensityMatrix) or any(
            not isinstance(item.operation, Measure) for item in measurements
        ):
            raise RuntimeError("density cache requires the verified state and terminal measurements")
        state = asarray(initial.operation.params[0])
        dimension = 1 << replay.num_qubits
        if (
            replay.num_qubits > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
            or state.shape != (dimension, dimension)
            or state.dtype != complex128
        ):
            raise RuntimeError("density cache requires a bounded complex128 state")
        noise = simulator.options.noise_model
        return cls(
            replay.num_qubits, replay.num_clbits, float(replay.global_phase), state.tobytes(),
            tuple((replay.find_bit(item.qubits[0]).index, replay.find_bit(item.clbits[0]).index)
                  for item in measurements),
            tuple(noise.basis_gates),
            tuple(
                (wire, tuple(tuple(row) for row in channel["probabilities"]))
                for channel in noise.to_dict()["errors"]
                for (wire,) in channel["gate_qubits"]
            ),
        )

    def materialize(self) -> tuple[QuantumCircuit, AerSimulator]:
        from numpy import complex128, frombuffer
        from qiskit import QuantumCircuit
        from qiskit_aer.noise import NoiseModel, ReadoutError

        dimension = 1 << self.qubits
        state = frombuffer(self.matrix, dtype=complex128).reshape(dimension, dimension).copy()
        replay = QuantumCircuit(self.qubits, self.clbits, global_phase=self.global_phase)
        replay.set_density_matrix(state)
        for wire, classical in self.measurements:
            replay.measure(wire, classical)
        readout = NoiseModel(basis_gates=list(self.basis_gates))
        for wire, probabilities in self.readout_errors:
            readout.add_readout_error(ReadoutError(probabilities), [wire])
        return replay, _density_readout_simulator(readout)


def _snapshot_readout_noise(simulator: AerSimulator) -> NoiseModel:
    from qiskit_aer.noise import NoiseModel, ReadoutError

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
    return readout


def _snapshot_density_body(circuit: QuantumCircuit) -> tuple[QuantumCircuit, list[CircuitInstruction]]:
    from qiskit.circuit import Measure

    body = circuit.copy_empty_like()
    measurements = []
    for item in circuit.data:
        if isinstance(item.operation, Measure):
            measurements.append(item)
        else:
            body.append(item)
    # The compact preflight ensures each measure can commute past later disjoint gates.
    # No readout channels are applied during this gate-only evolution.
    body.save_density_matrix(label=_DENSITY_SAVE_LABEL)
    return body, measurements


def _snapshot_density_replay(
    circuit: QuantumCircuit, measurements: Sequence[CircuitInstruction], result: Result, index: int,
    metadata: dict[str, Any], backend_name: str,
) -> tuple[QuantumCircuit, DensityMatrix]:
    from qiskit.quantum_info import DensityMatrix

    if metadata.get("method") != "density_matrix":
        raise RuntimeError(f"{backend_name} snapshot preparation did not use the density-matrix method")

    state = DensityMatrix(result.data(index)[_DENSITY_SAVE_LABEL])
    replay = circuit.copy_empty_like()
    # Aer validates trace, Hermiticity, positivity, and width. Keep its raw state
    # so every batch sees the same pre-readout matrix, including numerical drift.
    try:
        replay.set_density_matrix(state)
    except ValueError as exc:
        raise ValueError(f"{backend_name} prepared density is invalid for replay: {exc}") from exc
    for item in measurements:
        replay.append(item)
    return replay, state


def _observed_density_fusion(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    fusion = metadata.get("fusion")
    if not isinstance(fusion, Mapping):
        return None
    return {
        key: fusion[key]
        for key in ("enabled", "applied", "threshold", "method", "max_fused_qubits", "parallelization")
        if key in fusion
    }


def _density_preparation_timeout(cancel: Callable[[], bool], started: float) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": False}
    try:
        report["local_job_cancelled"] = cancel() is True
    except Exception as exc:
        report["local_job_cancellation_error"] = type(exc).__name__
    report["wall_seconds"] = perf_counter() - started
    return report


def _prepare_density_snapshot(
    circuit: QuantumCircuit, simulator: AerSimulator, backend_name: str, *, density_fusion: bool = False,
) -> tuple[tuple[QuantumCircuit, AerSimulator] | None, dict[str, Any]]:
    """Evolve an audited static compact circuit once and prepare its readout sampler."""
    started = perf_counter()
    readout = _snapshot_readout_noise(simulator)
    body, measurements = _snapshot_density_body(circuit)
    fusion_options = _PREPARED_DENSITY_FUSION_OPTIONS if density_fusion else {}
    job = simulator.run(body, shots=1, seed_simulator=0, **fusion_options)
    try:
        result = job.result(timeout=LOCAL_JOB_WAIT_SECONDS)
    except JobWaitTimeout:
        return None, _density_preparation_timeout(job.cancel, started)
    metadata = _checked_snapshot_metadata(result, backend_name, list(range(circuit.num_qubits)))
    replay, state = _snapshot_density_replay(circuit, measurements, result, 0, metadata, backend_name)
    trace = complex(state.trace())
    readout_simulator = _density_readout_simulator(readout)
    preparation: dict[str, Any] = {
        "status": "COMPLETED",
        "matrix_bytes": int(state.data.nbytes),
        "readout_noise_wires": readout.noise_qubits,
        "trace_real": trace.real,
        "trace_imag": trace.imag,
        "wall_seconds": perf_counter() - started,
    }
    if density_fusion:
        fusion = _observed_density_fusion(metadata)
        if fusion is not None:
            preparation["fusion"] = fusion
    return (replay, readout_simulator), preparation


def _prepare_density_snapshot_batch(
    circuits: Sequence[QuantumCircuit], simulator: AerSimulator, backend_name: str, *, density_fusion: bool = False,
) -> tuple[list[tuple[QuantumCircuit, AerSimulator]] | None, dict[str, Any]]:
    """Prepare a bounded batch on one compact simulator without publishing partial states."""
    if type(density_fusion) is not bool:
        raise TypeError("density_fusion must be a boolean")
    from qiskit import QuantumCircuit

    if not isinstance(circuits, Sequence) or not 2 <= len(circuits) <= _MAX_DENSITY_BATCH_ROUNDS:
        raise ValueError("snapshot density batch requires two or three circuits")
    if any(not isinstance(circuit, QuantumCircuit) for circuit in circuits):
        raise TypeError("snapshot density batch requires QuantumCircuit inputs")
    width = circuits[0].num_qubits
    if not 1 <= width <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS or any(circuit.num_qubits != width for circuit in circuits):
        raise ValueError("snapshot density batch requires one bounded compact circuit width")

    started = perf_counter()
    readout = _snapshot_readout_noise(simulator)
    bodies = []
    measurements = []
    for index, circuit in enumerate(circuits):
        body, terminal = _snapshot_density_body(circuit)
        body.name = f"{_DENSITY_SAVE_LABEL}_{index}"
        body.metadata = {"snapshot_density_batch_index": index}
        bodies.append(body)
        measurements.append(terminal)

    fusion_options = _PREPARED_DENSITY_FUSION_OPTIONS if density_fusion else {}
    job = simulator.run(bodies, shots=1, seed_simulator=0, max_parallel_experiments=1, **fusion_options)
    try:
        result = job.result(timeout=LOCAL_JOB_WAIT_SECONDS)
    except JobWaitTimeout:
        return None, _density_preparation_timeout(job.cancel, started)
    if not result.success or len(result.results) != len(bodies):
        raise RuntimeError(f"{backend_name} snapshot density batch did not complete")

    replays = []
    fusion_by_experiment = []
    input_wires = list(range(width))
    for index, (circuit, body, terminal, experiment) in enumerate(zip(
        circuits, bodies, measurements, result.results, strict=True,
    )):
        metadata = _checked_snapshot_experiment_metadata(experiment, backend_name, input_wires)
        header = getattr(experiment, "header", None)
        identity = header.get("metadata") if isinstance(header, Mapping) else None
        if (
            not isinstance(header, Mapping) or header.get("name") != body.name
            or not isinstance(identity, Mapping) or type(identity.get("snapshot_density_batch_index")) is not int
            or identity["snapshot_density_batch_index"] != index
        ):
            raise RuntimeError(f"{backend_name} snapshot density batch changed the experiment order")
        replay, _ = _snapshot_density_replay(circuit, terminal, result, index, metadata, backend_name)
        replays.append(replay)
        if density_fusion:
            fusion_by_experiment.append(_observed_density_fusion(metadata))
    sampler = _density_readout_simulator(readout)
    preparation: dict[str, Any] = {
        "status": "COMPLETED", "wall_seconds": perf_counter() - started,
    }
    if density_fusion:
        preparation["fusion_by_experiment"] = fusion_by_experiment
    return [(replay, sampler) for replay in replays], preparation


def _prepared_snapshot_probabilities(
    circuit: QuantumCircuit, simulator: AerSimulator, *, verifier: Callable[[str], bool],
) -> dict[str, Any]:
    """Marginalize an audited compact density and apply its local readout channels."""
    import numpy as np
    from qiskit.circuit import Measure
    from qiskit_aer.library import SetDensityMatrix

    qubits, width = circuit.num_qubits, circuit.num_clbits
    if not 1 <= width <= qubits <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS or len(circuit.data) != width + 1:
        raise ValueError("modeled probabilities require a bounded prepared density and terminal measurements")
    initial, *measurements = circuit.data
    if not isinstance(initial.operation, SetDensityMatrix) or initial.qubits != tuple(circuit.qubits):
        raise ValueError("modeled probabilities require the density in compact wire order")

    measured_wires = {}
    for item in measurements:
        if not isinstance(item.operation, Measure):
            raise ValueError("modeled probabilities require terminal measurements")
        measured_wires[circuit.find_bit(item.clbits[0]).index] = circuit.find_bit(item.qubits[0]).index
    if set(measured_wires) != set(range(width)) or len(set(measured_wires.values())) != width:
        raise ValueError("modeled probabilities require a one-to-one classical measurement map")

    dimension = 1 << qubits
    matrix = np.asarray(initial.operation.params[0])
    if matrix.shape != (dimension, dimension):
        raise ValueError("prepared density has the wrong dimensions")
    diagonal = np.diag(matrix)
    if (
        not np.isfinite(diagonal).all()
        or np.max(np.abs(diagonal.imag)) > MEASUREMENT_TOLERANCE
        or diagonal.real.min() < -MEASUREMENT_TOLERANCE
    ):
        raise ValueError("prepared density has invalid basis weights")
    weights = np.maximum(diagonal.real, 0.0)
    mass = math.fsum(weights)
    if mass <= 0.0:
        raise ValueError("prepared density has no probability mass")

    basis = np.arange(dimension)
    outcomes = np.zeros_like(basis)
    for classical, wire in measured_wires.items():
        outcomes |= ((basis >> wire) & 1) << classical
    probabilities = np.bincount(outcomes, weights=weights, minlength=1 << width) / mass

    channels = {}
    for channel in simulator.options.noise_model.to_dict()["errors"]:
        groups = channel.get("gate_qubits")
        if channel["type"] != "roerror" or not groups or any(len(group) != 1 for group in groups):
            raise ValueError("modeled probabilities require single-wire local readout channels")
        transition = np.asarray(channel["probabilities"], dtype=float)
        if (
            transition.shape != (2, 2) or not np.isfinite(transition).all()
            or np.any(transition < 0.0)
            or not np.allclose(transition.sum(axis=1), 1.0, atol=MEASUREMENT_TOLERANCE, rtol=0.0)
        ):
            raise ValueError("modeled probabilities require normalized readout transition rows")
        for (wire,) in groups:
            if not 0 <= wire < qubits or wire in channels:
                raise ValueError("modeled probabilities require unique readout wires")
            channels[wire] = transition

    for classical, wire in measured_wires.items():
        if wire in channels:
            # Aer readout rows are true outcomes. Each block pairs the two
            # values of this classical bit while holding other bits fixed.
            pairs = probabilities.reshape(-1, 2, 1 << classical)
            probabilities = (channels[wire].T @ pairs).reshape(-1)
    if not math.isclose(math.fsum(probabilities), 1.0, rel_tol=0.0, abs_tol=MEASUREMENT_TOLERANCE):
        raise ValueError("modeled output probability mass differs from one")

    distribution = {format(index, f"0{width}b"): float(probability) for index, probability in enumerate(probabilities)}
    hit_probability = math.fsum(probability for bits, probability in distribution.items() if verifier(bits))
    return {
        "outcome_probabilities": distribution,
        # A normalized all-hit distribution can round just above one.
        "verified_hit_probability": min(1.0, hit_probability),
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
    submit_started = perf_counter()
    job = simulator.run(sampling_circuit, shots=shots, seed_simulator=seed)
    submitted_at = perf_counter()
    try:
        result = job.result(timeout=LOCAL_JOB_WAIT_SECONDS)
    except JobWaitTimeout:
        timed_out_at = perf_counter()
        return {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "simulation": None,
            "local_job_cancelled": job.cancel(),
            "timing_seconds": {
                "sample_wait": perf_counter() - model_ready,
                "submit": submitted_at - submit_started,
                "result_wait": timed_out_at - submitted_at,
            },
        }
    sampled_at = perf_counter()
    metadata = _checked_snapshot_metadata(result, backend.name, input_wires)

    format_started = perf_counter()
    measured = result.get_counts(0)
    counts_ready = perf_counter()
    counts = summarize_search_counts(
        measured, requested_shots=shots, width=compiled.circuit.num_clbits, verifier=verifier,
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
        "timing_seconds": {
            "sample": sampled_at - model_ready,
            "submit": submitted_at - submit_started,
            "result_wait": sampled_at - submitted_at,
            "verify": verified_at - sampled_at,
            "count_format": counts_ready - format_started,
            "count_verify": verified_at - counts_ready,
        },
    }


def _summarize_snapshot_batches(
    batches: list[dict[str, Any]], confidence: float, *, expected_batches: int = 2,
) -> dict[str, Any] | None:
    """Summarize a complete seed schedule from the same compiled circuit."""
    if not batches or len(batches) != expected_batches or any(batch["status"] != "COMPLETED" for batch in batches):
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


def _validate_snapshot_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer from 0 to 2**32 - 1")


def _validate_snapshot_request(model: str, shots: int, seed: int) -> None:
    if model not in (*IBM_SNAPSHOT_MODELS, "all"):
        raise ValueError(f"model must be one of {', '.join(IBM_SNAPSHOT_MODELS)}, or all")
    if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= MAX_IBM_SNAPSHOT_SHOTS:
        raise ValueError(f"shots must be an integer from 1 to {MAX_IBM_SNAPSHOT_SHOTS}")
    _validate_snapshot_seed(seed)


def _snapshot_specs():
    from qiskit_ibm_runtime.fake_provider import FakeJakartaV2, FakeManilaV2, FakeTorino

    return {
        "manila": (FakeManilaV2, "density_matrix"),
        "jakarta": (FakeJakartaV2, "density_matrix"),
        "torino": (FakeTorino, "matrix_product_state"),
    }


def _snapshot_fingerprint_value(value: Any) -> dict[str, Any]:
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, complex):
        return {"complex": [value.real, value.imag]}
    raise ValueError(f"unsupported snapshot fingerprint value {type(value).__name__}")


def _snapshot_payload(backend: FakeBackendV2, *, properties: BackendProperties | None = None) -> bytes:
    """Return the exact canonical configuration and properties used for snapshot identity."""
    if properties is None:
        properties = backend.properties()
    payload = {
        "configuration": backend.configuration().to_dict(),
        "properties": None if properties is None else properties.to_dict(),
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_snapshot_fingerprint_value,
    ).encode("utf-8")


def _snapshot_identity(backend: FakeBackendV2) -> dict[str, Any]:
    properties = backend.properties()
    return {
        "backend_name": backend.name,
        "backend_version": backend.backend_version,
        "last_update_date": None if properties is None else properties.last_update_date.isoformat(),
        "configuration_properties_sha256": hashlib.sha256(_snapshot_payload(backend, properties=properties)).hexdigest(),
    }


def compile_ibm_snapshot(
    circuit: QuantumCircuit, *, model: str, seed: int = 7, prefer_native_qasm: bool = False,
) -> CompiledProgram:
    """Compile and verify a portable export against a bundled IBM snapshot."""
    if model not in IBM_SNAPSHOT_MODELS:
        raise ValueError(f"model must be one of {', '.join(IBM_SNAPSHOT_MODELS)}")
    _validate_snapshot_seed(seed)

    from qiskit import QuantumCircuit

    if not isinstance(circuit, QuantumCircuit):
        raise TypeError("circuit must be a Qiskit QuantumCircuit")
    factory, _ = _snapshot_specs()[model]
    backend = factory()
    if circuit.num_qubits > backend.num_qubits:
        raise ValueError(
            f"IBM snapshot {model} has {backend.num_qubits} qubits for a {circuit.num_qubits}-wire circuit"
        )
    program = compile_and_verify(circuit, backend=backend, seed=seed, prefer_native_qasm=prefer_native_qasm)
    return replace(program, report={
        **program.report,
        "snapshot": {"model": model, **_snapshot_identity(backend)},
        "versions": {name: version(name) for name in ("qiskit", "qiskit-ibm-runtime")},
    })


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
    model: str, shots: int = 64, seed: int = 7, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Compile and sample one audited declarative search circuit on an offline snapshot.

    The importer preference applies to the verified OpenQASM round trip.
    """
    _validate_snapshot_request(model, shots, seed)
    if model == "all":
        raise ValueError("constraint snapshot requires one named model")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"constraint snapshot shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS}")
    if not callable(verifier):
        raise TypeError("verifier must be callable")
    if not isinstance(prefer_native_qasm, bool):
        raise TypeError("prefer_native_qasm must be a boolean")

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
        **report,
        **_model_run(
            circuit, backend, partial(_snapshot_simulator, backend, AerSimulator, method),
            method=method, shots=shots, seed=seed, verifier=verifier,
            prefer_native_qasm=prefer_native_qasm,
        ),
    }


def run_ibm_snapshot_comparison(
    *, model: str = "all", shots: int = 1024, seed: int = 7, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Compare local ideal and snapshot-noisy Austin runs with the direct solver."""
    _validate_snapshot_request(model, shots, seed)
    if not isinstance(prefer_native_qasm, bool):
        raise ValueError("prefer_native_qasm must be a boolean")
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
            circuit, backend, partial(_snapshot_simulator, backend, AerSimulator, method),
            method=method, shots=shots, seed=seed, verifier=austin_predicate,
            prefer_native_qasm=prefer_native_qasm,
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
    *, model: str = "all", shots: int = 1024, seed: int = 7, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Compare two verified implementations under the same historical backend model."""
    _validate_snapshot_request(model, shots, seed)
    if not isinstance(prefer_native_qasm, bool):
        raise ValueError("prefer_native_qasm must be a boolean")
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
    selected_models = IBM_SNAPSHOT_MODELS if model == "all" else (model,)
    unconfirmed_cancellation = False
    for name in selected_models:
        factory, method = specs[name]
        load_started = perf_counter()
        backend = factory()
        load_seconds = perf_counter() - load_started
        make_simulator = cache(partial(_snapshot_simulator, backend, AerSimulator, method))
        results = {}
        for variant, circuit in circuits.items():
            result = _model_run(
                circuit, backend, make_simulator, method=method, shots=shots, seed=seed,
                verifier=mod4_range_predicate, prefer_native_qasm=prefer_native_qasm,
            )
            results[variant] = result
            if result.get("local_job_cancelled") is False:
                unconfirmed_cancellation = True
                break

        remaining_variants = [variant for variant in circuits if variant not in results]
        completed = not remaining_variants and all(result["status"] == "COMPLETED" for result in results.values())
        ideal_distance = hit_rate_change = None
        if not remaining_variants:
            reference, direct = results["work-qubit"], results["direct"]
            p, q = (
                result["compilation"]["verification"]["roundtrip_probabilities"]
                for result in (reference, direct)
            )
            ideal_distance = math.fsum(abs(p.get(bits, 0) - q.get(bits, 0)) for bits in p.keys() | q.keys()) / 2
            if ideal_distance > MEASUREMENT_TOLERANCE:
                raise RuntimeError(f"{name} oracle variants did not preserve the same ideal output distribution")
            if completed:
                hit_rate_change = direct["simulation"]["verified_hit_rate"] - reference["simulation"]["verified_hit_rate"]
        models[name] = {
            "status": "COMPLETED" if completed else "PARTIAL",
            "variants": results,
            "ideal_roundtrip_total_variation_distance": ideal_distance,
            "direct_minus_work_qubit_hit_rate": hit_rate_change,
            "load_backend_seconds": load_seconds,
        }
        if remaining_variants:
            models[name]["variants_not_run"] = remaining_variants
        if unconfirmed_cancellation:
            break
    report = {
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
    if unconfirmed_cancellation:
        report["local_cancellation_unconfirmed"] = True
        report["models_not_run"] = [name for name in selected_models if name not in models]
    return report


def _mod4_round_sweep_model(
    circuits: dict[int, QuantumCircuit], backend: FakeBackendV2, simulator_type: type[AerSimulator], *,
    method: str, shots: int, calibration_seed: int, validation_seed: int, transpiler_seed: int,
    ideal_rounds: int, ideal_rates: dict[int, float], verifier: Callable[[str], bool], prefer_native_qasm: bool,
) -> dict[str, Any]:
    started = perf_counter()
    layout = _transpile_initial_layout(circuits[ideal_rounds], backend=backend, seed=transpiler_seed)
    if layout is None:
        raise RuntimeError(f"{backend.name} did not report the initial layout for the ideal baseline")
    programs = {
        rounds: compile_and_verify(
            circuit, backend=backend, seed=transpiler_seed, initial_layout=layout,
            prefer_native_qasm=prefer_native_qasm,
        )
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
    validation_seed: int = 19, transpiler_seed: int = 7, prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Select a round count on calibration shots and compare with the ideal choice on fresh shots."""
    if not isinstance(prefer_native_qasm, bool):
        raise ValueError("prefer_native_qasm must be a boolean")
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
            prefer_native_qasm=prefer_native_qasm,
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
