"""Save and verify a fixed, powered offline diffuser holdout."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .constraint_diffuser_comparison import (
    _DiffuserExports,
    _diffuser_replay_digest,
    _diffuser_replay_plan,
    run_constraint_diffuser_comparison,
)
from .constraint_oracle import SYNTHESIS_METHODS
from .constraint_replay import MAX_REPLAY_BYTES, _parse_request, _read_request, _require_digest, _require_fields
from .constraints import BooleanConstraint, _constraint_document, _constraint_from_document
from .witness_budget import _normalize_validation_seeds


_WORKFLOW = "DIFFUSER_WITNESS_REPLAY"
_DOCUMENT_FIELDS = {"schema_version", "workflow", "formula", "sampling", "witness", "expected", "content_sha256"}
_SAMPLING_FIELDS = {"model", "placements", "synthesis", "transpiler_seed"}
_WITNESS_FIELDS = {
    "witness_probability", "witness_max_shots", "witness_seed_schedule",
    "witness_validation_power", "witness_stop_on_futility",
}
_TOTALS = {"planned_batches", "planned_shots", "planned_nominal_two_qubit_gate_executions"}
_EXPECTED_FIELDS = {"snapshot", "versions", "circuits_sha256", "plan_sha256"} | _TOTALS


def save_diffuser_replay(
    path: str | Path, constraint: BooleanConstraint, *, model: str, placements: Sequence[Sequence[int]],
    synthesis: str = "auto", transpiler_seed: int = 7, witness_probability: float | None = None,
    witness_max_shots: int | None = None, witness_seed_schedule: Sequence[int] | None = None,
    witness_validation_power: float | None = None, witness_stop_on_futility: bool = False,
    prefer_native_qasm: bool = False,
) -> dict[str, Any]:
    """Prepare a fully feasible fixed schedule and write a new request without sampling."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"replay request file already exists: {path}")
    if witness_seed_schedule is None or witness_validation_power is None:
        raise ValueError("diffuser replay requires witness_seed_schedule and witness_validation_power")
    if not isinstance(witness_stop_on_futility, bool):
        raise ValueError("witness_stop_on_futility must be a boolean")
    seeds = _normalize_validation_seeds(witness_seed_schedule)
    report = run_constraint_diffuser_comparison(
        constraint, model=model, placements=placements, synthesis=synthesis, transpiler_seed=transpiler_seed,
        simulation_method="modeled_probabilities", witness_probability=witness_probability,
        witness_max_shots=witness_max_shots, witness_seed_schedule=seeds, witness_validation_power=witness_validation_power,
        witness_plan_only=True, prefer_native_qasm=prefer_native_qasm, _capture_witness_replay=True,
    )
    expected = report.get("witness_replay_evidence", {})
    if report["status"] != "COMPLETED" or set(expected) != _EXPECTED_FIELDS:
        raise ValueError(f"diffuser replay preparation did not complete ({report['status']})")
    sampling = {
        "model": model, "placements": report["placements"], "synthesis": synthesis, "transpiler_seed": transpiler_seed,
    }
    if prefer_native_qasm:
        sampling["prefer_native_qasm"] = True
    document = {
        "schema_version": 2 if prefer_native_qasm else 1, "workflow": _WORKFLOW,
        "formula": _constraint_document(constraint), "sampling": sampling,
        "witness": {
            "witness_probability": witness_probability, "witness_max_shots": report["modeled_witness_target"]["max_shots"],
            "witness_seed_schedule": list(seeds), "witness_validation_power": witness_validation_power,
            "witness_stop_on_futility": witness_stop_on_futility,
        },
        "expected": expected,
    }
    document["content_sha256"] = _diffuser_replay_digest(document)
    encoded = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REPLAY_BYTES:
        raise ValueError(f"replay request exceeds {MAX_REPLAY_BYTES} bytes")
    with path.open("xb") as stream:
        stream.write(encoded)
    return {
        "status": "DIFFUSER_REPLAY_REQUEST_SAVED", "replay_request": str(path),
        "content_sha256": document["content_sha256"], "expected": expected,
        "timing_seconds": report["timing_seconds"], "local_sampling_performed": False, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _decode_diffuser_replay(raw: bytes) -> tuple[dict[str, Any], BooleanConstraint]:
    document = _require_fields(_parse_request(raw), _DOCUMENT_FIELDS, "diffuser request")
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version not in (1, 2) or document["workflow"] != _WORKFLOW:
        raise ValueError("unsupported diffuser replay workflow or schema_version")
    digest = document["content_sha256"]
    _require_digest(digest, "diffuser request content_sha256")
    try:
        actual_digest = _diffuser_replay_digest({key: value for key, value in document.items() if key != "content_sha256"})
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid diffuser replay request content") from exc
    if actual_digest != digest:
        raise ValueError("diffuser replay request content digest differs")

    sampling_fields = _SAMPLING_FIELDS if schema_version == 1 else _SAMPLING_FIELDS | {"prefer_native_qasm"}
    sampling = _require_fields(document["sampling"], sampling_fields, "diffuser sampling")
    if schema_version == 2 and not isinstance(sampling["prefer_native_qasm"], bool):
        raise ValueError("diffuser replay prefer_native_qasm must be a boolean")
    if sampling["synthesis"] not in SYNTHESIS_METHODS or not isinstance(sampling["placements"], list):
        raise ValueError("diffuser replay requires a synthesis method and a JSON array of placements")
    witness = _require_fields(document["witness"], _WITNESS_FIELDS, "diffuser witness")
    if not isinstance(witness["witness_seed_schedule"], list) or not isinstance(witness["witness_stop_on_futility"], bool):
        raise ValueError("diffuser replay requires a seed array and a boolean futility setting")
    if witness["witness_validation_power"] is None:
        raise ValueError("diffuser replay requires a validation power target")
    expected = _require_fields(document["expected"], _EXPECTED_FIELDS, "diffuser evidence")
    for field in ("circuits_sha256", "plan_sha256"):
        _require_digest(expected[field], field)
    if any(type(expected[field]) is not int or expected[field] < 0 for field in _TOTALS):
        raise ValueError("diffuser replay planned work totals must be nonnegative integers")
    return document, _constraint_from_document(document["formula"])


def run_diffuser_replay(
    path: str | Path, *, check_only: bool = False, receipt_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Run a checked diffuser request and optionally record the exact execution artifacts."""
    if not isinstance(check_only, bool):
        raise ValueError("check_only must be a boolean")
    if check_only and receipt_directory is not None:
        raise ValueError("a diffuser execution receipt requires a validation run")
    return _run_diffuser_replay_bytes(
        _read_request(path), path=path, check_only=check_only, receipt_directory=receipt_directory,
    )


def _run_diffuser_replay_bytes(
    raw: bytes, *, path: str | Path, check_only: bool = False, receipt_directory: str | Path | None = None,
    _baseline_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(raw) > MAX_REPLAY_BYTES:
        raise ValueError(f"replay request exceeds {MAX_REPLAY_BYTES} bytes")
    document, constraint = _decode_diffuser_replay(raw)
    if receipt_directory is None:
        if _baseline_report is not None:
            raise ValueError("a diffuser regression with roundoff evidence requires a new execution receipt")
        return _evaluate_diffuser_replay(path, document, constraint, check_only=check_only)

    from .replay_receipt import _new_receipt_directory, _write_replay_receipt

    with _new_receipt_directory(receipt_directory) as directory:
        options = {} if _baseline_report is None else {"_baseline_report": _baseline_report}
        report = _evaluate_diffuser_replay(path, document, constraint, check_only=False, **options)
        if _baseline_report is not None:
            raw, content_sha256 = _diffuser_request_bytes_for_plan(raw, document, report["witness_replay_evidence"]["plan_sha256"])
            if content_sha256 != document["content_sha256"]:
                report["witness_replay"].update(replay_request=str(directory / "request.json"), content_sha256=content_sha256)
        _write_replay_receipt(directory, request=raw, report=report, openqasm=None)
    return report


def _diffuser_request_bytes_for_plan(
    raw: bytes, document: dict[str, Any], observed_plan_sha256: str,
) -> tuple[bytes, str]:
    if observed_plan_sha256 == document["expected"]["plan_sha256"]:
        return raw, document["content_sha256"]
    _require_digest(observed_plan_sha256, "observed plan_sha256")
    updated = {key: value for key, value in document.items() if key != "content_sha256"}
    updated["expected"] = {**document["expected"], "plan_sha256": observed_plan_sha256}
    updated["content_sha256"] = _diffuser_replay_digest(updated)
    encoded = (json.dumps(updated, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REPLAY_BYTES:
        raise ValueError(f"observed diffuser replay request exceeds {MAX_REPLAY_BYTES} bytes")
    return encoded, updated["content_sha256"]


def _evaluate_diffuser_replay(
    path: str | Path, document: dict[str, Any], constraint: BooleanConstraint, *, check_only: bool,
    _openqasm_exports: _DiffuserExports | None = None,
    _baseline_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    witness = dict(document["witness"])
    if check_only:
        witness["witness_stop_on_futility"] = False
    expected = document["expected"]
    plan_options = {}
    if _baseline_report is not None:
        plan_options["_expected_witness_replay_plan"] = _diffuser_replay_plan(
            _baseline_report, _baseline_report["modeled_witness_validation"],
        )
    report = run_constraint_diffuser_comparison(
        constraint, **document["sampling"], **witness, simulation_method="modeled_probabilities",
        witness_plan_only=check_only, _expected_witness_replay=expected, _openqasm_exports=_openqasm_exports,
        **plan_options,
    )
    evidence = report.get("witness_replay_evidence")
    if _baseline_report is not None and isinstance(evidence, dict) and evidence.keys() == expected.keys():
        evidence = {**evidence, "plan_sha256": expected["plan_sha256"]}
    if evidence != expected:
        raise ValueError(f"diffuser replay preparation did not complete ({report['status']})")
    report["witness_replay"] = {
        "status": "DIFFUSER_REPLAY_VERIFIED", "replay_request": str(path),
        "content_sha256": document["content_sha256"],
    }
    if check_only:
        report["status"] = "DIFFUSER_REPLAY_VERIFIED"
    return report
