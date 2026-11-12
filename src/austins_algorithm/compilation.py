"""Compile and verify the ideal measurement behavior of a small static circuit."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.circuit import Gate
    from qiskit.providers import BackendV2
    from qiskit.quantum_info import Statevector
    from qiskit.transpiler import Target


MAX_EXACT_QUBITS = 16
MAX_CIRCUIT_INSTRUCTIONS = 10_000
MAX_EXACT_WORK = 64_000_000
MEASUREMENT_TOLERANCE = 1e-10
_NATIVE_QASM_IMPORTER = "qiskit.qasm3.loads_experimental"
_DEFAULT_OPTIMIZATION_LEVEL = 1
_MAX_STOCK_EVOLUTION_QUBITS = 8
_MIN_STOCK_EVOLUTION_GATES = 64


@dataclass(frozen=True)
class CompiledProgram:
    """A target circuit, portable OpenQASM, verification report, and elapsed timings."""

    circuit: QuantumCircuit
    openqasm: str
    report: dict[str, Any]
    timing_seconds: dict[str, float] = field(default_factory=dict, compare=False, repr=False)


def line_target(n_qubits: int = 3, *, seed: int = 7) -> Target:
    """Return a synthetic bidirectional line with CX, RZ, SX, and X gates."""
    if isinstance(n_qubits, bool) or not isinstance(n_qubits, int) or not 1 <= n_qubits <= MAX_EXACT_QUBITS:
        raise ValueError(f"line target must have from 1 to {MAX_EXACT_QUBITS} qubits")
    from qiskit.providers.fake_provider import GenericBackendV2
    from qiskit.transpiler import CouplingMap

    basis_gates = ["rz", "sx", "x"]
    if n_qubits > 1:
        basis_gates.append("cx")
    target = GenericBackendV2(
        n_qubits,
        basis_gates=basis_gates,
        coupling_map=CouplingMap.from_line(n_qubits),
        seed=seed,
        noise_info=False,
    ).target
    target.description = f"Synthetic bidirectional line ({n_qubits} qubits)"
    return target


def _initial_qubit_mapping(compiled: QuantumCircuit) -> list[int] | None:
    layout = compiled.layout
    return None if layout is None else layout.initial_index_layout(filter_ancillas=True)


def _transpile_initial_layout(circuit: QuantumCircuit, *, backend: BackendV2, seed: int) -> list[int] | None:
    """Read the automatic starting layout from the normal seeded transpilation."""
    from qiskit import transpile

    compiled = transpile(circuit, backend=backend, optimization_level=_DEFAULT_OPTIMIZATION_LEVEL, seed_transpiler=seed)
    return _initial_qubit_mapping(compiled)


def _check_exact_work(qubits: int, instructions: int) -> None:
    if not 1 <= qubits <= MAX_EXACT_QUBITS:
        raise ValueError(f"exact verification requires from 1 to {MAX_EXACT_QUBITS} active qubits")
    if (1 << qubits) * instructions > MAX_EXACT_WORK:
        raise ValueError("circuit exceeds the exact statevector verification work limit")


def _compact_wires(circuit: QuantumCircuit) -> tuple[QuantumCircuit, list[int]]:
    """Declare active wires explicitly while retaining the compiled wire map."""
    from qiskit import QuantumCircuit

    if len(circuit.data) > MAX_CIRCUIT_INSTRUCTIONS:
        raise ValueError(f"circuit exceeds {MAX_CIRCUIT_INSTRUCTIONS} instructions")
    instructions = [item for item in circuit.data if item.operation.name != "barrier"]
    active = sorted({circuit.find_bit(bit).index for item in instructions for bit in item.qubits})
    _check_exact_work(len(active), len(instructions))

    wires = {physical: compact for compact, physical in enumerate(active)}
    portable = QuantumCircuit(len(active), circuit.num_clbits, global_phase=circuit.global_phase)
    for item in instructions:
        portable.append(
            item.operation,
            [wires[circuit.find_bit(bit).index] for bit in item.qubits],
            [circuit.find_bit(bit).index for bit in item.clbits],
        )
    return portable, active


def _stock_statevector(
    width: int, phase: float, gates: list[tuple[Gate, tuple[int, ...]]],
) -> Statevector | None:
    """Evolve a small stock-gate circuit using the actual gate matrices."""
    if width > _MAX_STOCK_EVOLUTION_QUBITS or len(gates) < _MIN_STOCK_EVOLUTION_GATES:
        return None

    from numpy import argsort, dot, exp, zeros
    from qiskit.circuit.library import CXGate, CZGate, RZGate, SXGate, XGate
    from qiskit.quantum_info import Statevector

    from .grover_search import _uses_stock_matrix

    stock_types = {RZGate, *(type(gate()) for gate in (CXGate, CZGate, SXGate, XGate))}
    if not all(
        _uses_stock_matrix(gate, stock_types) and "broadcast_arguments" not in vars(gate)
        for gate, _ in gates
    ):
        return None

    shape = (2,) * width
    state = zeros(1 << width, dtype=complex)
    state[0] = 1
    if phase:
        state = state * exp(1j * float(phase))
    plans: dict[tuple[int, ...], tuple[list[int], list[int], int]] = {}
    for gate, operands in gates:
        plan = plans.get(operands)
        if plan is None:
            # Qiskit's last operand is the most significant local matrix bit.
            local = [width - 1 - qubit for qubit in reversed(operands)]
            axes = local + [axis for axis in range(width) if axis not in local]
            inverse = argsort(axes).tolist()
            plan = plans[operands] = (axes, inverse, 1 << len(operands))
        axes, inverse, dimension = plan
        ordered = state.reshape(shape).transpose(axes)
        evolved = dot(gate.to_matrix(), ordered.reshape(dimension, -1))
        state = evolved.reshape(ordered.shape).transpose(inverse).reshape(-1)
    return Statevector(state)


def _distribution(circuit: QuantumCircuit) -> tuple[dict[str, float], list[int]]:
    from qiskit import QuantumCircuit
    from qiskit.circuit import Gate
    from qiskit.quantum_info import Statevector

    quantum_indices = {bit: index for index, bit in enumerate(circuit.qubits)}
    gates = []
    measured: dict[int, int] = {}
    used_qubits: set[int] = set()
    for item in circuit.data:
        operation = item.operation
        qubits = tuple(quantum_indices[bit] for bit in item.qubits)
        if operation.name == "measure":
            classical = circuit.find_bit(item.clbits[0]).index
            qubit = qubits[0]
            if classical in measured or qubit in used_qubits:
                raise ValueError("each output bit and measured qubit must be measured once")
            measured[classical] = qubit
            used_qubits.add(qubit)
        elif operation.name == "barrier":
            continue
        elif isinstance(operation, Gate) and not item.clbits and used_qubits.isdisjoint(qubits):
            # Gates on other qubits commute with an earlier terminal measurement.
            gates.append((operation, qubits))
        else:
            raise ValueError(f"exact verification cannot handle instruction {operation.name!r} here")

    if not measured or len(measured) != circuit.num_clbits:
        raise ValueError("each classical output bit requires a terminal measurement")
    ordered_qubits = [measured[index] for index in range(circuit.num_clbits)]
    phase = circuit.global_phase
    state = _stock_statevector(circuit.num_qubits, phase, gates)
    if state is None:
        body = QuantumCircuit(circuit.num_qubits, global_phase=phase)
        for gate, qubits in gates:
            body.append(gate, qubits)
        state = Statevector.from_instruction(body)
    probabilities = {
        str(bits): float(probability)
        for bits, probability in state.probabilities_dict(qargs=ordered_qubits).items()
    }
    if (
        any(not 0.0 <= value <= 1.0 + MEASUREMENT_TOLERANCE for value in probabilities.values())
        or not math.isclose(math.fsum(probabilities.values()), 1.0, abs_tol=MEASUREMENT_TOLERANCE, rel_tol=0.0)
    ):
        raise RuntimeError("exact verification requires finite, normalized measurement probabilities")
    return probabilities, ordered_qubits


def _import_openqasm(text: str, *, prefer_native: bool = False) -> tuple[QuantumCircuit, str]:
    from importlib.util import find_spec

    from qiskit import qasm3

    standard_available = find_spec("qiskit_qasm3_import") is not None
    native_loads = getattr(qasm3, "loads_experimental", None)
    if native_loads is not None and (prefer_native or not standard_available):
        try:
            return native_loads(text), _NATIVE_QASM_IMPORTER
        except qasm3.QASM3ImporterError:
            if not standard_available:
                raise
    if standard_available:
        return qasm3.loads(text), "qiskit.qasm3.loads"
    raise ImportError("OpenQASM verification requires the optional qiskit-qasm3-import package")


def _distance(first: dict[str, float], second: dict[str, float]) -> float:
    return math.fsum(abs(first.get(key, 0.0) - second.get(key, 0.0)) for key in first.keys() | second.keys()) / 2


def _roundtrip_distribution(
    circuit: QuantumCircuit, *, qubits: int, clbits: int, measured: Sequence[int],
) -> dict[str, float]:
    from qiskit import QuantumCircuit

    if not isinstance(circuit, QuantumCircuit):
        raise RuntimeError("OpenQASM importer did not return a Qiskit circuit")
    if circuit.num_qubits != qubits or circuit.num_clbits != clbits:
        raise RuntimeError("OpenQASM round trip changed the declared wires or measurement mapping")
    if len(circuit.data) > MAX_CIRCUIT_INSTRUCTIONS:
        raise ValueError(f"OpenQASM round trip exceeds {MAX_CIRCUIT_INSTRUCTIONS} instructions")
    _check_exact_work(circuit.num_qubits, len(circuit.data))
    distribution, observed_map = _distribution(circuit)
    if list(measured) != observed_map:
        raise RuntimeError("OpenQASM round trip changed the declared wires or measurement mapping")
    return distribution


def _metrics(circuit: QuantumCircuit) -> dict[str, Any]:
    from qiskit.circuit import Gate

    return {
        "qubits": circuit.num_qubits,
        "depth": circuit.depth(),
        "two_qubit_gates": sum(isinstance(item.operation, Gate) and len(item.qubits) == 2 for item in circuit.data),
        "operations": dict(circuit.count_ops()),
    }


def _check_compiled_target(
    compiled: QuantumCircuit, target: Target, requested_layout: list[int] | None,
) -> list[int] | None:
    initial_mapping = _initial_qubit_mapping(compiled)
    if requested_layout is not None and initial_mapping != requested_layout:
        raise RuntimeError("compiled initial layout differs from the requested layout")
    for item in compiled.data:
        op = item.operation
        if op.name != "barrier" and not target.instruction_supported(
            operation_name=op.name,
            qargs=tuple(compiled.find_bit(bit).index for bit in item.qubits),
            parameters=op.params,
        ):
            raise RuntimeError(f"compiled instruction is unsupported by the selected target: {op.name}")
    return initial_mapping


def _verification_report(
    original: dict[str, float], compiled: dict[str, float], roundtrip: dict[str, float],
) -> dict[str, Any]:
    compilation_distance = _distance(original, compiled)
    export_distance = _distance(compiled, roundtrip)
    roundtrip_distance = _distance(original, roundtrip)
    if max(compilation_distance, export_distance, roundtrip_distance) > MEASUREMENT_TOLERANCE:
        raise RuntimeError("compilation or OpenQASM round trip changed measurement probabilities")
    return {
        "bit_order": "Highest classical bit index first",
        "tolerance": MEASUREMENT_TOLERANCE,
        "original_probabilities": original,
        "compiled_probabilities": compiled,
        "roundtrip_probabilities": roundtrip,
        "compilation_total_variation_distance": compilation_distance,
        "export_total_variation_distance": export_distance,
        "roundtrip_total_variation_distance": roundtrip_distance,
    }


def _compilation_report(
    circuit: QuantumCircuit, compiled: QuantumCircuit, target: Target, *,
    backend: BackendV2 | None, seed: int, optimization_level: int, initial_mapping: list[int] | None,
    text: str, importer: str, active: list[int], measured: list[int], verification: dict[str, Any],
) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {
        "status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION",
        "target": {
            "kind": "TARGET" if backend is None else "BACKEND_V2",
            "name": (target.description or "Qiskit Target") if backend is None else backend.name,
            "qubits": target.num_qubits,
            "operations": sorted(target.operation_names),
        },
        "seed_transpiler": seed,
        "optimization_level": optimization_level,
        "initial_logical_to_target_qubit": initial_mapping,
        "input": _metrics(circuit),
        "compiled": _metrics(compiled),
        "openqasm": {
            "representation": "PORTABLE_COMPILED_WIRES",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "importer": importer,
            "export_wire_to_target_qubit": active,
            "classical_bit_to_target_qubit": [active[wire] for wire in measured],
        },
        "verification": verification,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }


def compile_and_verify(
    circuit: QuantumCircuit,
    *,
    target: Target | None = None,
    backend: BackendV2 | None = None,
    optimization_level: int = _DEFAULT_OPTIMIZATION_LEVEL,
    seed: int = 7,
    initial_layout: Sequence[int] | None = None,
    prefer_native_qasm: bool = False,
) -> CompiledProgram:
    """Compare classical bit probabilities before compilation and after export.

    Verification uses an ideal zero-input statevector for a bounded static
    circuit with terminal measurements. The portable export declares active
    compiled wires in ascending physical order and preserves their mapping in
    the report. The returned circuit retains Qiskit's target layout.
    Pass one distinct target index per input qubit to fix the initial layout.
    The report records the transpiler's initial layout before routing.
    The native importer can be requested for the same verified round trip.
    Elapsed timings are available separately from the verification report.
    """
    started = perf_counter()
    from qiskit import QuantumCircuit, qasm3, transpile
    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target

    if not isinstance(circuit, QuantumCircuit):
        raise TypeError("circuit must be a Qiskit QuantumCircuit")
    if (target is None) == (backend is None):
        raise ValueError("supply exactly one Qiskit target or backend")
    if target is not None and not isinstance(target, Target):
        raise TypeError("target must be a Qiskit Target")
    if backend is not None and not isinstance(backend, BackendV2):
        raise TypeError("backend must be a Qiskit BackendV2")
    if isinstance(optimization_level, bool) or not isinstance(optimization_level, int) or optimization_level not in range(4):
        raise ValueError("optimization_level must be an integer from 0 to 3")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if not isinstance(prefer_native_qasm, bool):
        raise TypeError("prefer_native_qasm must be a boolean")
    if circuit.num_parameters:
        raise ValueError("circuit parameters must be bound before verification")
    active_target = target if target is not None else backend.target
    requested_layout = None
    if initial_layout is not None:
        if not isinstance(initial_layout, Sequence) or isinstance(initial_layout, (str, bytes, bytearray)):
            raise ValueError("initial_layout must be a sequence of target qubit indices")
        if len(initial_layout) != circuit.num_qubits:
            raise ValueError("initial_layout requires one target qubit index per circuit qubit")
        requested_layout = list(initial_layout)
        if active_target.num_qubits is None:
            raise ValueError("initial_layout requires a target with a declared qubit count")
        if any(
            isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < active_target.num_qubits
            for index in requested_layout
        ):
            raise ValueError("initial_layout contains an index outside the target qubit range")
        if len(set(requested_layout)) != len(requested_layout):
            raise ValueError("initial_layout indices must be distinct")

    original_portable, _ = _compact_wires(circuit)
    original, _ = _distribution(original_portable)
    target_kwargs = {"target": target} if target is not None else {"backend": backend}
    if requested_layout is not None:
        target_kwargs["initial_layout"] = requested_layout
    transpile_started = perf_counter()
    compiled = transpile(circuit, optimization_level=optimization_level, seed_transpiler=seed, **target_kwargs)
    transpile_seconds = perf_counter() - transpile_started
    initial_mapping = _check_compiled_target(compiled, active_target, requested_layout)
    portable, active = _compact_wires(compiled)
    compiled_distribution, measured = _distribution(portable)
    export_started = perf_counter()
    text = qasm3.dumps(portable, disable_constants=True)
    import_started = perf_counter()
    reloaded, importer = _import_openqasm(text, prefer_native=prefer_native_qasm)
    verify_started = perf_counter()
    roundtrip_distribution = _roundtrip_distribution(
        reloaded, qubits=portable.num_qubits, clbits=portable.num_clbits, measured=measured,
    )

    verification = _verification_report(original, compiled_distribution, roundtrip_distribution)
    verify_seconds = perf_counter() - verify_started

    report = _compilation_report(
        circuit, compiled, active_target, backend=backend, seed=seed, optimization_level=optimization_level,
        initial_mapping=initial_mapping, text=text, importer=importer, active=active, measured=measured,
        verification=verification,
    )
    if prefer_native_qasm:
        report["openqasm"]["preferred_importer"] = _NATIVE_QASM_IMPORTER
    timing_seconds = {
        "compile_export_verify": perf_counter() - started,
        "transpile": transpile_seconds,
        "openqasm_export": import_started - export_started,
        "openqasm_import": verify_started - import_started,
        "openqasm_verify": verify_seconds,
    }
    return CompiledProgram(compiled, text, report, timing_seconds)
