"""Verify recorded diffuser replay evidence without executing a simulation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .compilation import _NATIVE_QASM_IMPORTER
from .constraint_diffuser_comparison import (
    COMPARED_DIFFUSERS,
    COMPARISON_ROUNDS,
    DIFFUSER_CONFIDENCE_PER_VARIANT,
    FAMILY_CONFIDENCE,
    _diffuser_replay_circuit_digest,
    _diffuser_replay_plan_evidence,
    _split_diffuser_seeds,
)
from .constraint_replay import _parse_request
from .constraint_snapshot_sweep import _layout_pair
from .constraints import BooleanConstraint
from .core import _bounded_int
from .device_experiment import summarize_search_counts
from .diffuser_replay import _decode_diffuser_replay
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS
from .replay_receipt import _read_verified_receipt
from .witness_budget import _normalize_validation_seeds, _validate_budget, _validate_minimum_validation_power


def _expect(record: dict[str, Any], label: str, **fields: Any) -> None:
    for key, expected in fields.items():
        value = record[key]
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"diffuser receipt {label} disagrees on {key}")


def _verify_variant(
    row: dict[str, Any], *, seeds: list[int], shots: int, gates: int, required: int,
    constraint: BooleanConstraint, target: float, stop_on_futility: bool, cancelled: bool,
) -> tuple[dict[str, int], bool]:
    n = len(seeds)
    batches = row["batches"]
    if not isinstance(batches, list) or len(batches) > n:
        raise ValueError("diffuser receipt batch list exceeds or differs from its plan")
    completed = successful = timed_out = 0
    futility = row.get("futility_stop")
    for index, batch in enumerate(batches, start=1):
        if cancelled:
            raise ValueError("diffuser receipt records work after an unconfirmed cancellation")
        _expect(batch, "batch", seed=seeds[index - 1])
        if batch["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT":
            if batch["simulation"] is not None or type(batch["local_job_cancelled"]) is not bool:
                raise ValueError("invalid diffuser receipt timeout")
            timed_out += 1
            cancelled = not batch["local_job_cancelled"]
            continue
        if batch["status"] != "COMPLETED" or "local_job_cancelled" in batch:
            raise ValueError("invalid diffuser receipt completed batch")
        simulation = batch["simulation"]
        counts = summarize_search_counts(
            simulation["counts"], requested_shots=shots, width=constraint.qubits, verifier=constraint.matches,
        )
        _expect(simulation, "measurement", execution_mode="SIMULATOR", method="density_matrix", **counts)
        if counts["completed_shots"] != shots:
            raise ValueError("diffuser receipt completed batch falls short of the shot budget")
        completed += 1
        successful += counts["verified_shots"] > 0
        if stop_on_futility and timed_out == 0 and index < n and n - (completed - successful) < required:
            if index != len(batches) or futility is None:
                raise ValueError("diffuser receipt continued beyond its futility boundary")

    skipped = 0
    if futility is not None:
        skipped = n - len(batches)
        best = n - (completed - successful)
        if not stop_on_futility or not skipped or not completed or timed_out or best != required - 1 or cancelled:
            raise ValueError("invalid diffuser receipt futility stop")
        _expect(
            futility, "futility stop", skipped_seeds=seeds[len(batches):],
            observed_failed_batches=completed - successful, best_case_successful_batches=best,
        )
        bound = futility["best_case_lower_success_probability"]
        if type(bound) not in (int, float) or not 0 <= bound < target:
            raise ValueError("invalid diffuser receipt futility bound")
    elif len(batches) != n and not cancelled:
        raise ValueError("diffuser receipt ends a seed schedule without a recorded stop")

    lower = row["lower_success_probability"]
    if completed == n:
        supported = successful >= required
        if type(lower) not in (int, float) or not 0 <= lower <= 1 or (lower >= target) != supported:
            raise ValueError("diffuser receipt lower bound disagrees with its support decision")
        status = "TARGET_SUPPORTED" if supported else "UNRESOLVED"
    else:
        if lower is not None:
            raise ValueError("diffuser receipt reports a lower bound for an incomplete schedule")
        status = "STOPPED_FOR_FUTILITY" if skipped else "INCOMPLETE"
    work = {
        "completed_batches": completed, "completed_shots": completed * shots,
        "completed_nominal_two_qubit_gate_executions": completed * shots * gates,
    }
    _expect(
        row, "variant", status=status, planned_batches=n, planned_shots=n * shots,
        successful_batches=successful, observed_success_fraction=successful / completed if completed else None, **work,
    )
    return {
        **work, "attempted_batches": len(batches), "timed_out_batches": timed_out,
        "skipped_batches_for_futility": skipped, "skipped_shots_for_futility": skipped * shots,
    }, cancelled


def _verify_report(document: dict[str, Any], constraint: BooleanConstraint, report: dict[str, Any]) -> dict[str, Any]:
    sampling, witness, expected = (document[key] for key in ("sampling", "witness", "expected"))
    snapshot = expected["snapshot"]
    _layout_pair(sampling["placements"])
    target, cap, power = (witness[key] for key in ("witness_probability", "witness_max_shots", "witness_validation_power"))
    _validate_budget(target, cap)
    _validate_minimum_validation_power(power)
    _expect(
        report, "report", experiment="LOCAL_IBM_SNAPSHOT_CONSTRAINT_DIFFUSER_COMPARISON",
        execution_mode="SIMULATOR", snapshot_method="modeled_probabilities",
        result_kind="MODELED_PROBABILITIES_WITH_WITNESS_VALIDATION", constraint_sha256=constraint.digest(),
        requested_model=sampling["model"], placements=sampling["placements"], transpiler_seed=sampling["transpiler_seed"],
        backend=snapshot["backend_name"], backend_version=snapshot["backend_version"],
        snapshot_last_update_date=snapshot["last_update_date"], versions=expected["versions"],
        provider_job_submitted=False, quantum_hardware_used=False, witness_replay_evidence=expected,
    )
    _expect(report["oracle"], "oracle", synthesis_requested=sampling["synthesis"])
    _expect(
        report["witness_replay"], "request binding", status="DIFFUSER_REPLAY_VERIFIED",
        content_sha256=document["content_sha256"],
    )
    _expect(
        report["modeled_witness_target"], "target",
        target_probability=target, max_shots=cap, minimum_validation_power=power,
    )
    seeds = _split_diffuser_seeds(_normalize_validation_seeds(witness["witness_seed_schedule"]))
    n = len(seeds[COMPARED_DIFFUSERS[0]])
    schedule = report["modeled_witness_validation"]
    required = _bounded_int(schedule["required_successful_batches"], 1, n, "required successful batches")
    stop = witness["witness_stop_on_futility"]
    if schedule.get("stop_on_futility", False) is not stop:
        raise ValueError("diffuser receipt futility setting differs from the request")
    _expect(
        schedule, "schedule", batches_per_variant=n, maximum_failed_batches=n - required, seed_schedule=seeds,
        batch_family_confidence=FAMILY_CONFIDENCE, confidence_per_variant=DIFFUSER_CONFIDENCE_PER_VARIANT,
        planned_batches=expected["planned_batches"], planned_shots=expected["planned_shots"],
    )

    cells = report["cells"]
    cell_order = [(placement, rounds) for placement in (1, 2) for rounds in COMPARISON_ROUNDS]
    if [(cell["placement"], cell["rounds"]) for cell in cells] != cell_order or any(
        tuple(cell["variants"]) != COMPARED_DIFFUSERS for cell in cells
    ):
        raise ValueError("diffuser receipt has an invalid circuit order")
    if _diffuser_replay_circuit_digest(cells) != expected["circuits_sha256"]:
        raise ValueError("diffuser receipt recorded circuit digest differs")
    plan = _diffuser_replay_plan_evidence(report, schedule)
    if any(expected[key] != value for key, value in plan.items()):
        raise ValueError("diffuser receipt recorded plan digest or totals differ")

    totals = dict.fromkeys((
        "completed_batches", "completed_shots", "completed_nominal_two_qubit_gate_executions",
        "attempted_batches", "timed_out_batches", "skipped_batches_for_futility", "skipped_shots_for_futility",
    ), 0)
    cancelled = False
    prefer_native = sampling.get("prefer_native_qasm", False)
    for cell in cells:
        for diffuser, variant in cell["variants"].items():
            openqasm = variant["compilation"]["openqasm"]
            if (
                ("preferred_importer" in openqasm) != prefer_native
                or (prefer_native and openqasm["preferred_importer"] != _NATIVE_QASM_IMPORTER)
            ):
                raise ValueError("diffuser receipt OpenQASM importer preference differs from the request")
            budget = variant["modeled_witness_budget"]
            shots = _bounded_int(budget["shots_required"], 1, cap, "witness shots per batch")
            gates = variant["compilation"]["compiled"]["two_qubit_gates"]
            if type(gates) is not int or gates < 0 or budget["nominal_two_qubit_gate_executions"] != shots * gates:
                raise ValueError("invalid diffuser receipt gate budget")
            work, cancelled = _verify_variant(
                variant["modeled_witness_validation"], seeds=seeds[diffuser], shots=shots, gates=gates,
                required=required, constraint=constraint, target=target, stop_on_futility=stop, cancelled=cancelled,
            )
            for key, value in work.items():
                totals[key] += value

    planned = expected["planned_batches"]
    completed = totals["completed_batches"]
    skipped = totals["skipped_batches_for_futility"]
    if completed + skipped == planned:
        status = validation_status = "STOPPED_FOR_FUTILITY" if skipped else "COMPLETED"
    else:
        status, validation_status = "PARTIAL", "PARTIAL_VALIDATION"
    _expect(report, "execution", status=status, local_cancellation_unconfirmed=cancelled)
    _expect(schedule, "validation", status=validation_status, **{k: v for k, v in totals.items() if k.startswith("completed_")})
    if stop:
        _expect(schedule, "validation", skipped_batches_for_futility=skipped,
                skipped_shots_for_futility=totals["skipped_shots_for_futility"])
    return {
        "execution_status": status, "validation_status": validation_status, **totals,
        "unattempted_batches": planned - totals["attempted_batches"] - skipped,
        "planned_batches": planned, "planned_shots": expected["planned_shots"],
        "planned_nominal_two_qubit_gate_executions": expected["planned_nominal_two_qubit_gate_executions"],
        "local_cancellation_unconfirmed": cancelled,
    }


_VerifiedDiffuserBundle = tuple[dict[str, Any], dict[str, Any], bytes]


def _read_verified_diffuser_receipt(directory: Path) -> _VerifiedDiffuserBundle:
    receipt, payloads = _read_verified_receipt(directory, capture_json=True)
    if receipt["artifacts"]["selected.qasm"] is not None:
        raise ValueError("diffuser execution receipt must describe the full set of circuits")
    document, constraint = _decode_diffuser_replay(payloads["request.json"])
    report = _parse_request(payloads["report.json"], label="diffuser receipt report")
    try:
        for name, version in document["expected"]["versions"].items():
            if receipt["software_versions"][name] != version:
                raise ValueError("diffuser receipt recorded software versions differ from the request")
        workload = _verify_report(document, constraint, report)
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError("invalid diffuser receipt report structure") from exc
    return {
        "status": "DIFFUSER_RECEIPT_VERIFIED", "receipt_directory": str(directory),
        "content_sha256": document["content_sha256"], "workload": workload, **receipt,
    }, report, payloads["request.json"]


def verify_diffuser_receipt(directory: str | Path) -> dict[str, Any]:
    """Verify receipt bytes, request bindings and recorded workload without an SDK."""
    verified, _, _ = _read_verified_diffuser_receipt(Path(directory))
    return verified
