"""Save and replay bounded offline phase-cover requests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from re import fullmatch
from typing import Any

from .constraint_cover_comparison import (
    COVER_SNAPSHOT_METHODS,
    PhaseCoverComparison,
    _compile_snapshot_covers,
    _phase_cover_replay_exports,
    _prepare_snapshot_request,
    _snapshot_sampling_limit,
    run_constraint_cover_snapshot,
)
from .constraints import BooleanConstraint, _constraint_document, _constraint_from_document, _unique_object
from .core import _bounded_int
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS, _snapshot_identity
from .witness_budget import WitnessBudgetRequest, _normalize_evaluation_seeds, check_witness_schedule


MAX_REPLAY_BYTES = 64 * 1024
_DOCUMENT_FIELDS = {"schema_version", "formula", "formula_sha256", "snapshot", "sampling", "witness", "exports"}
_SNAPSHOT_FIELDS = {"backend_name", "backend_version", "last_update_date", "configuration_properties_sha256"}
_SAMPLING_FIELDS = {
    "model", "initial_layout", "rounds", "seed", "max_active_qubits", "shots", "evaluation_seeds",
    "simulation_method", "prefer_native_qasm", "density_fusion",
}
_WITNESS_FIELDS = {"target_probability", "max_shots", "validation_seeds", "stop_on_futility", "tolerated_misses"}
_EXPORT_WIRE_FIELDS = {
    "initial_logical_to_target_qubit", "export_wire_to_target_qubit", "classical_bit_to_target_qubit",
}


def save_constraint_replay(
    path: str | Path, constraint: BooleanConstraint, *, model: str, initial_layout: Sequence[int],
    rounds: int, seed: int, max_active_qubits: int | None, shots: int, evaluation_seeds: Sequence[int],
    simulation_method: str | None, prefer_native_qasm: bool, density_fusion: bool,
    witness_budget: WitnessBudgetRequest, tolerated_misses: int = 0,
) -> dict[str, Any]:
    """Check and compile an offline request, then write it to a new file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"replay request file already exists: {path}")
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if not isinstance(witness_budget, WitnessBudgetRequest):
        raise TypeError("witness_budget must be a WitnessBudgetRequest")
    if not isinstance(initial_layout, Sequence) or isinstance(initial_layout, (str, bytes, bytearray)):
        raise ValueError("replay requires a shared initial_layout")
    initial_layout = tuple(initial_layout)
    evaluation_seeds = _normalize_evaluation_seeds(evaluation_seeds)
    constraint = BooleanConstraint(constraint.qubits, constraint.clauses)
    _prepare_snapshot_request(
        constraint, model=model, seed=seed, shots=shots, evaluation_seeds=evaluation_seeds,
        witness_budget=witness_budget, simulation_method=simulation_method, density_fusion=density_fusion,
    )
    schedule_check = check_witness_schedule(
        target_probability=witness_budget.target_probability, tolerated_misses=tolerated_misses,
        validation_seeds=witness_budget.validation_seeds, evaluation_seeds=evaluation_seeds,
    )
    compilation = _compile_snapshot_covers(
        constraint, model=model, rounds=rounds, seed=seed, initial_layout=initial_layout,
        max_active_qubits=max_active_qubits, prefer_native_qasm=prefer_native_qasm,
    )
    document = {
        "schema_version": 1,
        "formula": _constraint_document(constraint),
        "formula_sha256": constraint.digest(),
        "snapshot": _snapshot_identity(compilation.backend),
        "sampling": {
            "model": model, "initial_layout": list(initial_layout), "rounds": rounds, "seed": seed,
            "max_active_qubits": max_active_qubits, "shots": shots, "evaluation_seeds": list(evaluation_seeds),
            "simulation_method": compilation.default_method if simulation_method is None else simulation_method,
            "prefer_native_qasm": prefer_native_qasm, "density_fusion": density_fusion,
        },
        "witness": {
            "target_probability": witness_budget.target_probability, "max_shots": witness_budget.max_shots,
            "validation_seeds": list(witness_budget.validation_seeds),
            "stop_on_futility": witness_budget.stop_on_futility, "tolerated_misses": tolerated_misses,
        },
        "exports": _phase_cover_replay_exports(compilation.comparison),
    }
    encoded = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REPLAY_BYTES:
        raise ValueError(f"replay request exceeds {MAX_REPLAY_BYTES} bytes")
    with path.open("xb") as stream:
        stream.write(encoded)
    return {
        "status": "REPLAY_REQUEST_SAVED",
        "replay_request": str(path),
        "formula_sha256": document["formula_sha256"],
        "snapshot": document["snapshot"],
        "exports": document["exports"],
        "schedule_check": schedule_check,
    }


def _reject_constant(token: str) -> None:
    raise ValueError(f"invalid replay request JSON number {token}")


def _read_request(path: str | Path) -> bytes:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_REPLAY_BYTES + 1)
    if len(raw) > MAX_REPLAY_BYTES:
        raise ValueError(f"replay request exceeds {MAX_REPLAY_BYTES} bytes")
    return raw


def _parse_request(raw: bytes, *, label: str = "replay request") -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=lambda pairs: _unique_object(pairs, label),
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc


def _require_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"replay {label} requires exactly {', '.join(sorted(fields))}")
    return value


def _require_digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"replay {label} must be a lowercase SHA-256 digest")


def _decode_replay(raw: bytes) -> tuple[dict[str, Any], BooleanConstraint, WitnessBudgetRequest]:
    document = _require_fields(_parse_request(raw), _DOCUMENT_FIELDS, "request")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported replay schema_version")
    constraint = _constraint_from_document(document["formula"])
    _require_digest(document["formula_sha256"], "formula_sha256")
    if constraint.digest() != document["formula_sha256"]:
        raise ValueError("replay formula digest differs from the embedded constraint")

    snapshot = _require_fields(document["snapshot"], _SNAPSHOT_FIELDS, "snapshot")
    _require_digest(snapshot["configuration_properties_sha256"], "snapshot fingerprint")
    if not isinstance(snapshot["backend_name"], str) or not isinstance(snapshot["backend_version"], str):
        raise ValueError("replay snapshot name and version must be strings")
    if snapshot["last_update_date"] is not None and not isinstance(snapshot["last_update_date"], str):
        raise ValueError("replay snapshot last_update_date must be a string or null")

    exports = document["exports"]
    if not isinstance(exports, dict) or set(exports) not in ({"direct"}, {"direct", "complement"}):
        raise ValueError("replay exports require a direct cover and an optional complement cover")
    for export in exports.values():
        export = _require_fields(export, _EXPORT_WIRE_FIELDS | {"sha256"}, "export")
        _require_digest(export["sha256"], "export")
        for field in _EXPORT_WIRE_FIELDS:
            wires = export[field]
            if not isinstance(wires, list) or not wires or any(type(wire) is not int or wire < 0 for wire in wires):
                raise ValueError(f"replay export {field} must contain nonnegative integer wire indices")

    sampling = _require_fields(document["sampling"], _SAMPLING_FIELDS, "sampling")
    witness = _require_fields(document["witness"], _WITNESS_FIELDS, "witness")
    if any(not isinstance(values, list) for values in (
        sampling["initial_layout"], sampling["evaluation_seeds"], witness["validation_seeds"],
    )):
        raise ValueError("replay layout and seed lists must be JSON arrays")
    if sampling["simulation_method"] not in COVER_SNAPSHOT_METHODS:
        raise ValueError("replay requires a named snapshot simulation method")
    for field, value in (
        ("prefer_native_qasm", sampling["prefer_native_qasm"]),
        ("density_fusion", sampling["density_fusion"]),
        ("stop_on_futility", witness["stop_on_futility"]),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"replay {field} must be a boolean")

    request = WitnessBudgetRequest(
        target_probability=witness["target_probability"], max_shots=witness["max_shots"],
        validation_seeds=witness["validation_seeds"], stop_on_futility=witness["stop_on_futility"],
    )
    return document, constraint, request


def _replay_sampling_shot_bound(raw: bytes) -> int:
    document, _, witness = _decode_replay(raw)
    sampling = document["sampling"]
    shots = _bounded_int(sampling["shots"], 1, MAX_CONSTRAINT_SNAPSHOT_SHOTS, "replay shots")
    evaluation_seeds = _normalize_evaluation_seeds(sampling["evaluation_seeds"])
    return len(evaluation_seeds) * shots + len(witness.validation_seeds) * witness.max_shots


def _evaluate_replay(
    raw: bytes, *, check_only: bool,
) -> tuple[dict[str, Any], dict[str, Any], PhaseCoverComparison]:
    document, constraint, request = _decode_replay(raw)
    sampling = document["sampling"]
    schedule_check = check_witness_schedule(
        target_probability=request.target_probability, tolerated_misses=document["witness"]["tolerated_misses"],
        validation_seeds=request.validation_seeds, evaluation_seeds=sampling["evaluation_seeds"],
    )
    comparison = run_constraint_cover_snapshot(
        constraint, **sampling, witness_budget=request, _expected_snapshot=document["snapshot"],
        _expected_exports=document["exports"],
        _check_only=check_only,
    )
    return document, schedule_check, comparison


def run_constraint_replay(
    path: str | Path, *, receipt_directory: str | Path | None = None,
) -> PhaseCoverComparison:
    """Run the checked offline request and optionally save an exact-byte receipt."""
    return _run_constraint_replay_bytes(_read_request(path), receipt_directory=receipt_directory)


def _run_constraint_replay_bytes(raw: bytes, *, receipt_directory: str | Path | None) -> PhaseCoverComparison:
    if len(raw) > MAX_REPLAY_BYTES:
        raise ValueError(f"replay request exceeds {MAX_REPLAY_BYTES} bytes")
    if receipt_directory is None:
        _, _, result = _evaluate_replay(raw, check_only=False)
        return result

    from .replay_receipt import _new_receipt_directory, _write_replay_receipt

    with _new_receipt_directory(receipt_directory) as directory:
        _, _, result = _evaluate_replay(raw, check_only=False)
        _write_replay_receipt(
            directory, request=raw, report=result.report,
            openqasm=None if result.selected is None else result.selected.openqasm,
        )
    return result


def check_constraint_replay(path: str | Path) -> dict[str, Any]:
    """Check the current snapshot and compiler against a saved request without sampling."""
    document, schedule_check, comparison = _evaluate_replay(_read_request(path), check_only=True)
    return {
        "status": "REPLAY_VERIFIED",
        "execution_mode": comparison.report["execution_mode"],
        "local_sampling_performed": False,
        "provider_job_submitted": comparison.report["provider_job_submitted"],
        "quantum_hardware_used": comparison.report["quantum_hardware_used"],
        "replay_request": str(path),
        "schema_version": document["schema_version"],
        "formula_sha256": document["formula_sha256"],
        "snapshot": {"model": document["sampling"]["model"], **document["snapshot"]},
        "qiskit_version": comparison.report["qiskit_version"],
        "compilation_status": comparison.report["status"],
        "compiled_sampling_status": _snapshot_sampling_limit(comparison) or "COMPILED_COVERS_ELIGIBLE",
        "selected_cover": comparison.report["selected_cover"],
        "exports": _phase_cover_replay_exports(comparison),
        "schedule_check": schedule_check,
    }

