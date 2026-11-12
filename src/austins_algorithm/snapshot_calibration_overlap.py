"""Verify competing pinned QPY rows against one dated calibration request."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Mapping
from hashlib import sha256
from numbers import Integral
from typing import TYPE_CHECKING, Any

from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .snapshot_calibration_compilation_bundle import _restore_compiled_round
from .unknown_count_snapshot_cache import _json_bytes

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2

    from .compilation import CompiledProgram


def _physical_signature(
    program: CompiledProgram, earlier_backend: BackendV2, later_backend: BackendV2,
) -> dict[str, Any]:
    from numpy import asarray, isfinite
    from qiskit.circuit import Gate
    from qiskit.exceptions import QiskitError

    from .snapshot_calibration_comparison import _program_calibration

    circuit = program.circuit
    export = program.report["openqasm"]
    if len(export["export_wire_to_target_qubit"]) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        raise ValueError("restored compilation exceeds the snapshot active wire limit")
    layout = circuit.layout
    if layout is None:
        raise ValueError("restored compilation has no routing layout")
    operations = []
    for item in circuit.data:
        operation = item.operation
        kind = operation.base_class
        record = {
            "class": [kind.__module__, kind.__qualname__],
            "name": operation.name, "label": operation.label,
            "params": [str(int(value)) if isinstance(value, Integral) else float(value).hex()
                       for value in operation.params],
            "control_state": getattr(operation, "ctrl_state", None),
            "qubits": [circuit.find_bit(bit).index for bit in item.qubits],
            "clbits": [circuit.find_bit(bit).index for bit in item.clbits],
        }
        if isinstance(operation, Gate):
            try:
                matrix = asarray(operation.to_matrix(), dtype="<c16")
            except (AttributeError, NotImplementedError, QiskitError, TypeError, ValueError) as exc:
                raise ValueError(f"could not verify the exact matrix of restored gate {operation.name}") from exc
            size = 1 << operation.num_qubits
            if matrix.shape != (size, size) or not isfinite(matrix).all():
                raise ValueError(f"invalid exact matrix for restored gate {operation.name}")
            record["matrix"] = {"shape": [size, size], "sha256": sha256(matrix.tobytes(order="C")).hexdigest()}
        operations.append(record)
    return {
        "dimensions": [circuit.num_qubits, circuit.num_clbits],
        "global_phase_hex": float(circuit.global_phase).hex(),
        "registers": {
            name: [[register.name, [circuit.find_bit(bit).index for bit in register]] for register in registers]
            for name, registers in (("quantum", circuit.qregs), ("classical", circuit.cregs))
        },
        "operations": operations,
        "initial_layout": layout.initial_index_layout(filter_ancillas=True),
        "initial_layout_with_ancillas": layout.initial_index_layout(filter_ancillas=False),
        "final_layout": layout.final_index_layout(filter_ancillas=True),
        "final_layout_with_ancillas": layout.final_index_layout(filter_ancillas=False),
        "routing_permutation": layout.routing_permutation(),
        "active_wires": export["export_wire_to_target_qubit"],
        "measurement_map": export["classical_bit_to_target_qubit"],
        "compilation_report_sha256": sha256(_json_bytes(program.report)).hexdigest(),
        "earlier_target_calibration": _program_calibration(program, earlier_backend),
        "later_target_calibration": _program_calibration(program, later_backend),
    }


def verify_snapshot_calibration_overlap(
    key: str, sources: Mapping[str, dict[str, Any]], logical: QuantumCircuit, *,
    earlier_backend: BackendV2, later_backend: BackendV2, layout: tuple[int, ...], seed: int,
    prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Restore and compare each pinned row, preserving the selected source identity."""
    evidence, _ = _verify_snapshot_calibration_overlap(
        key, sources, logical, earlier_backend=earlier_backend, later_backend=later_backend, layout=layout, seed=seed,
        prefer_native_qasm=prefer_native_qasm,
    )
    return evidence


def _verify_snapshot_calibration_overlap(
    key: str, sources: Mapping[str, dict[str, Any]], logical: QuantumCircuit, *,
    earlier_backend: BackendV2, later_backend: BackendV2, layout: tuple[int, ...], seed: int,
    prefer_native_qasm: bool = False,
) -> tuple[dict[str, Any], CompiledProgram | None]:
    """Return the evidence and the selected restored program on exact equivalence."""
    from qiskit.exceptions import QiskitError

    if len(sources) < 2:
        raise ValueError("compilation overlap requires at least two pinned source rows")
    evidence_by_source = {}
    signatures = {}
    ordered_sources = sorted(sources.items())
    first_pin = ordered_sources[0][0]
    selected_program = None
    for pin, row in ordered_sources:
        evidence: dict[str, Any] = {
            "status": "RESTORE_REJECTED", "qpy_sha256": None, "physical_signature_sha256": None,
            "stored_qpy_decoded": False, "error": None,
        }
        try:
            evidence["qpy_sha256"] = sha256(b64decode(row["qpy"], validate=True)).hexdigest()
            evidence["stored_qpy_decoded"] = None
            program = _restore_compiled_round(
                row, logical, later_backend, layout, seed, prefer_native_qasm=prefer_native_qasm,
            )
            evidence["stored_qpy_decoded"] = True
            signature = _physical_signature(program, earlier_backend, later_backend)
            evidence["physical_signature_sha256"] = sha256(_json_bytes(signature)).hexdigest()
            signatures[pin] = signature
            evidence["status"] = "RESTORED"
            if pin == first_pin:
                selected_program = program
        except (AttributeError, LookupError, OverflowError, QiskitError, RuntimeError, TypeError, ValueError) as exc:
            evidence["error"] = str(exc) or type(exc).__name__
        finally:
            program = None
        evidence_by_source[pin] = evidence

    reference = next(iter(signatures.values()), {})
    differing = sorted(field for field, value in reference.items()
                       if any(signature[field] != value for signature in signatures.values()))
    if len(signatures) != len(sources):
        status = "RESTORE_REJECTED"
    elif differing:
        status = "PHYSICAL_CIRCUITS_DIFFER"
    else:
        status = "EQUIVALENT_PHYSICAL_CIRCUITS"
    selected_pin = first_pin if status == "EQUIVALENT_PHYSICAL_CIRCUITS" else None
    decode_evidence = {item["stored_qpy_decoded"] for item in evidence_by_source.values()}
    evidence = {
        "status": status, "key": key, "source_sha256s": list(evidence_by_source),
        "selected_source_sha256": selected_pin,
        "differing_fields": differing, "sources": evidence_by_source,
        "stored_qpy_decoded": True if True in decode_evidence else None if None in decode_evidence else False,
    }
    return evidence, selected_program if selected_pin is not None else None
