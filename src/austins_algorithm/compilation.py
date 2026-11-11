"""Compile and verify the ideal measurement behavior of a small static circuit."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target


MAX_EXACT_QUBITS = 16
MAX_CIRCUIT_INSTRUCTIONS = 10_000
MAX_EXACT_WORK = 64_000_000
MEASUREMENT_TOLERANCE = 1e-10


@dataclass(frozen=True)
class CompiledProgram:
    """A target circuit, portable OpenQASM, and its JSON-compatible report."""

    circuit: QuantumCircuit
    openqasm: str
    report: dict[str, Any]


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


def _distribution(circuit: QuantumCircuit) -> tuple[dict[str, float], list[int]]:
    from qiskit import QuantumCircuit
    from qiskit.circuit import Gate
    from qiskit.quantum_info import Statevector

    body = QuantumCircuit(circuit.num_qubits, global_phase=circuit.global_phase)
    measured: dict[int, int] = {}
    used_qubits: set[int] = set()
    for item in circuit.data:
        operation = item.operation
        qubits = [circuit.find_bit(bit).index for bit in item.qubits]
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
            body.append(operation, qubits)
        else:
            raise ValueError(f"exact verification cannot handle instruction {operation.name!r} here")

    if not measured or len(measured) != circuit.num_clbits:
        raise ValueError("each classical output bit requires a terminal measurement")
    ordered_qubits = [measured[index] for index in range(circuit.num_clbits)]
    probabilities = {
        str(bits): float(probability)
        for bits, probability in Statevector.from_instruction(body).probabilities_dict(qargs=ordered_qubits).items()
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
            return native_loads(text), "qiskit.qasm3.loads_experimental"
        except qasm3.QASM3ImporterError:
            if not standard_available:
                raise
    if standard_available:
        return qasm3.loads(text), "qiskit.qasm3.loads"
    raise ImportError("OpenQASM verification requires the optional qiskit-qasm3-import package")


def _distance(first: dict[str, float], second: dict[str, float]) -> float:
    return math.fsum(abs(first.get(key, 0.0) - second.get(key, 0.0)) for key in first.keys() | second.keys()) / 2


def _metrics(circuit: QuantumCircuit) -> dict[str, Any]:
    from qiskit.circuit import Gate

    return {
        "qubits": circuit.num_qubits,
        "depth": circuit.depth(),
        "two_qubit_gates": sum(isinstance(item.operation, Gate) and len(item.qubits) == 2 for item in circuit.data),
        "operations": dict(circuit.count_ops()),
    }


def compile_and_verify(
    circuit: QuantumCircuit,
    *,
    target: Target | None = None,
    backend: BackendV2 | None = None,
    optimization_level: int = 1,
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
    """
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
    compiled = transpile(circuit, optimization_level=optimization_level, seed_transpiler=seed, **target_kwargs)
    initial_mapping = (
        compiled.layout.initial_index_layout(filter_ancillas=True) if compiled.layout is not None else None
    )
    if requested_layout is not None and initial_mapping != requested_layout:
        raise RuntimeError("compiled initial layout differs from the requested layout")
    for item in compiled.data:
        op = item.operation
        if op.name != "barrier" and not active_target.instruction_supported(
            operation_name=op.name,
            qargs=tuple(compiled.find_bit(bit).index for bit in item.qubits),
            parameters=op.params,
        ):
            raise RuntimeError(f"compiled instruction is unsupported by the selected target: {op.name}")
    portable, active = _compact_wires(compiled)
    compiled_distribution, measured = _distribution(portable)
    text = qasm3.dumps(portable, disable_constants=True)
    reloaded, importer = _import_openqasm(text, prefer_native=prefer_native_qasm)
    if not isinstance(reloaded, QuantumCircuit):
        raise RuntimeError("OpenQASM importer did not return a Qiskit circuit")
    if reloaded.num_qubits != portable.num_qubits or reloaded.num_clbits != portable.num_clbits:
        raise RuntimeError("OpenQASM round trip changed the declared wires or measurement mapping")
    if len(reloaded.data) > MAX_CIRCUIT_INSTRUCTIONS:
        raise ValueError(f"OpenQASM round trip exceeds {MAX_CIRCUIT_INSTRUCTIONS} instructions")
    _check_exact_work(reloaded.num_qubits, len(reloaded.data))
    roundtrip_distribution, roundtrip_measured = _distribution(reloaded)
    if measured != roundtrip_measured:
        raise RuntimeError("OpenQASM round trip changed the declared wires or measurement mapping")

    compilation_distance = _distance(original, compiled_distribution)
    export_distance = _distance(compiled_distribution, roundtrip_distribution)
    roundtrip_distance = _distance(original, roundtrip_distribution)
    if max(compilation_distance, export_distance, roundtrip_distance) > MEASUREMENT_TOLERANCE:
        raise RuntimeError("compilation or OpenQASM round trip changed measurement probabilities")

    encoded = text.encode("utf-8")
    report = {
        "status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION",
        "target": {
            "kind": "TARGET" if backend is None else "BACKEND_V2",
            "name": (active_target.description or "Qiskit Target") if backend is None else backend.name,
            "qubits": active_target.num_qubits,
            "operations": sorted(active_target.operation_names),
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
        "verification": {
            "bit_order": "Highest classical bit index first",
            "tolerance": MEASUREMENT_TOLERANCE,
            "original_probabilities": original,
            "compiled_probabilities": compiled_distribution,
            "roundtrip_probabilities": roundtrip_distribution,
            "compilation_total_variation_distance": compilation_distance,
            "export_total_variation_distance": export_distance,
            "roundtrip_total_variation_distance": roundtrip_distance,
        },
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    if prefer_native_qasm:
        report["openqasm"]["preferred_importer"] = "qiskit.qasm3.loads_experimental"
    return CompiledProgram(compiled, text, report)
