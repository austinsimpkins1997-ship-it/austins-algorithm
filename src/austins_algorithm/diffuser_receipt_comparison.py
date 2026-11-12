"""Compare two verified offline diffuser runs by circuit variant and seed."""

from __future__ import annotations

from math import fsum, isclose, isfinite
from pathlib import Path
from typing import Any

from .constraint_diffuser_comparison import (
    DIFFUSER_PLAN_PROBABILITY_ROUNDOFF, _compare_diffuser_plan_roundoff, _diffuser_circuit_identity,
    _diffuser_replay_plan,
)
from .diffuser_receipt import _VerifiedDiffuserBundle, _read_verified_diffuser_receipt
from .diffuser_replay import _decode_diffuser_replay, _diffuser_request_bytes_for_plan


_CircuitKey = tuple[tuple[int, ...], int, str]
_Variant = tuple[int, dict[str, Any], list[int]]
_COMPILATION_PHASES = ("compile_export_verify", "transpile", "openqasm_export", "openqasm_import", "openqasm_verify")
_SEED_COUNTS = (
    "shared_seed_count", "left_only_seed_count", "right_only_seed_count", "matching_completed_batches",
    "changed_completed_batches", "completed_batches_in_different_conditions", "matching_unmeasured_batches",
    "changed_batch_states",
)
_DIFFERENCE_KINDS = {
    "left_only_seed_count": "LEFT_ONLY",
    "right_only_seed_count": "RIGHT_ONLY",
    "changed_completed_batches": "COUNTS_CHANGED",
    "completed_batches_in_different_conditions": "CONDITIONS_CHANGED",
    "changed_batch_states": "STATE_CHANGED",
}


def _read_side(path: str | Path) -> _VerifiedDiffuserBundle:
    try:
        return _read_verified_diffuser_receipt(Path(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _index_variants(report: dict[str, Any]) -> dict[_CircuitKey, _Variant]:
    seeds = report["modeled_witness_validation"]["seed_schedule"]
    return {
        (tuple(report["placements"][cell["placement"] - 1]), cell["rounds"], diffuser):
            (cell["placement"], variant, seeds[diffuser])
        for cell in report["cells"] for diffuser, variant in cell["variants"].items()
    }


def _variant_summary(variant: _Variant | None) -> dict[str, Any] | None:
    if variant is None:
        return None
    placement, record, _ = variant
    validation = record["modeled_witness_validation"]
    return {
        "placement": placement, "shots_per_batch": record["modeled_witness_budget"]["shots_required"],
        **{key: validation[key] for key in (
            "status", "planned_batches", "planned_shots", "completed_batches", "completed_shots",
            "completed_nominal_two_qubit_gate_executions", "successful_batches",
        )},
    }


def _batch_records(variant: _Variant | None) -> dict[int, tuple[dict[str, Any], dict[str, int] | None]]:
    if variant is None:
        return {}
    _, record, seeds = variant
    row = record["modeled_witness_validation"]
    skipped = set((row.get("futility_stop") or {}).get("skipped_seeds", ()))
    attempts = {batch["seed"]: batch for batch in row["batches"]}
    records = {}
    for seed in seeds:
        batch = attempts.get(seed)
        if batch is None:
            status = "FUTILITY_SKIPPED" if seed in skipped else "UNATTEMPTED"
            records[seed] = ({"status": status}, None)
        elif batch["status"] == "COMPLETED":
            simulation = batch["simulation"]
            records[seed] = ({
                "status": "COMPLETED", "completed_shots": simulation["completed_shots"],
                "verified_shots": simulation["verified_shots"],
            }, simulation["counts"])
        else:
            records[seed] = ({
                "status": batch["status"], "local_job_cancelled": batch["local_job_cancelled"],
            }, None)
    return records


def _compare_batches(left: _Variant | None, right: _Variant | None, *, matching_conditions: bool) -> dict[str, Any]:
    left_records, right_records = _batch_records(left), _batch_records(right)
    summary = dict.fromkeys(_SEED_COUNTS, 0)
    summary["shared_seed_count"] = len(left_records.keys() & right_records.keys())
    differences = []
    for seed in sorted(left_records.keys() | right_records.keys()):
        left_state, left_counts = left_records.get(seed, ({"status": "NOT_PLANNED"}, None))
        right_state, right_counts = right_records.get(seed, ({"status": "NOT_PLANNED"}, None))
        item = {"seed": seed, "left": left_state, "right": right_state}
        if seed not in right_records:
            kind = "left_only_seed_count"
        elif seed not in left_records:
            kind = "right_only_seed_count"
        elif left_counts is not None and right_counts is not None:
            if not matching_conditions:
                kind = "completed_batches_in_different_conditions"
            elif left_counts == right_counts:
                summary["matching_completed_batches"] += 1
                continue
            else:
                kind = "changed_completed_batches"
                item["count_deltas"] = {
                    bits: right_counts.get(bits, 0) - left_counts.get(bits, 0)
                    for bits in sorted(left_counts.keys() | right_counts.keys())
                    if left_counts.get(bits, 0) != right_counts.get(bits, 0)
                }
        elif left_state == right_state:
            summary["matching_unmeasured_batches"] += 1
            continue
        else:
            kind = "changed_batch_states"
        summary[kind] += 1
        differences.append({"kind": _DIFFERENCE_KINDS[kind], **item})
    return {**summary, "differences": differences}


def _elapsed_seconds(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    values = [raw.get(phase) for phase in _COMPILATION_PHASES]
    if any(type(value) not in (int, float) for value in values):
        return None
    try:
        elapsed = [float(value) for value in values]
    except OverflowError:
        return None
    if any(not isfinite(value) or value < 0 for value in elapsed):
        return None
    total, *phases = elapsed
    subtotals = sum(phases)
    if subtotals > total and not isclose(subtotals, total, rel_tol=1e-12, abs_tol=1e-12):
        return None
    return dict(zip(_COMPILATION_PHASES, elapsed, strict=True))


def _timing_side(variant: _Variant | None) -> dict[str, Any]:
    if variant is None:
        return {"availability": "VARIANT_NOT_PRESENT", "importer": None, "preferred_importer": None, "seconds": None}
    record = variant[1]
    openqasm = record["compilation"]["openqasm"]
    if not isinstance(openqasm, dict):
        openqasm = {}
    seconds = _elapsed_seconds(record.get("compilation_timing_seconds"))
    availability = "NOT_RECORDED"
    if "compilation_timing_seconds" in record:
        availability = "RECORDED" if seconds is not None else "INVALID"
    importer, preferred = openqasm.get("importer"), openqasm.get("preferred_importer")
    return {
        "availability": availability,
        "importer": importer if isinstance(importer, str) and importer else None,
        "preferred_importer": preferred if isinstance(preferred, str) and preferred else None,
        "seconds": seconds,
    }


def _timing_circuit_identity(record: dict[str, Any]) -> dict[str, Any]:
    identity = _diffuser_circuit_identity(record)
    openqasm = identity["openqasm"]
    if isinstance(openqasm, dict):
        identity["openqasm"] = {key: value for key, value in openqasm.items()
                                if key not in ("importer", "preferred_importer")}
    return identity


def _timing_delta(left: dict[str, float] | None, right: dict[str, float] | None) -> dict[str, float] | None:
    if left is None or right is None:
        return None
    return {phase: right[phase] - left[phase] for phase in _COMPILATION_PHASES}


def _compare_compilation_timing(
    left: _Variant | None, right: _Variant | None, context_changes: list[str],
) -> dict[str, Any]:
    left_side, right_side = _timing_side(left), _timing_side(right)
    changes = None
    if left is not None and right is not None:
        changes = list(context_changes)
        if _timing_circuit_identity(left[1]) != _timing_circuit_identity(right[1]):
            changes.append("compiled_circuit")
        if left[1]["compilation"].get("optimization_level") != right[1]["compilation"].get("optimization_level"):
            changes.append("optimization_level")
        changes.extend(field for field in ("importer", "preferred_importer") if left_side[field] != right_side[field])
        if left_side["importer"] is None or right_side["importer"] is None:
            changes.append("importer_unrecorded")
    return {
        "left": left_side, "right": right_side, "context_changes": changes,
        "delta_seconds": _timing_delta(left_side["seconds"], right_side["seconds"]),
    }


def _summarize_compilation_timing(variants: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [row["compilation_timing"] for row in variants
              if row["compilation_timing"]["delta_seconds"] is not None]
    matching = sum(not timing["context_changes"] for timing in paired)
    availability = "NO_PAIRED_TIMINGS"
    left_seconds = right_seconds = None
    if paired:
        try:
            totals = {
                side: {phase: fsum(timing[side]["seconds"][phase] for timing in paired)
                       for phase in _COMPILATION_PHASES}
                for side in ("left", "right")
            }
        except OverflowError:
            availability = "OUT_OF_RANGE"
        else:
            availability = "PAIRED"
            left_seconds, right_seconds = totals["left"], totals["right"]
    return {
        "availability": availability, "paired_variants": len(paired),
        "matching_recorded_conditions": matching, "different_recorded_conditions": len(paired) - matching,
        "left_seconds": left_seconds, "right_seconds": right_seconds,
        "delta_seconds": _timing_delta(left_seconds, right_seconds),
    }


def compare_diffuser_receipts(
    left: str | Path, right: str | Path, *, include_timings: bool = False,
) -> dict[str, Any]:
    """Compare verified diffuser receipts offline, with optional compiler timing diagnostics."""
    if not isinstance(include_timings, bool):
        raise ValueError("include_timings must be a boolean")
    return _compare_verified_diffuser_bundles(_read_side(left), _read_side(right), include_timings=include_timings)


def _compare_verified_diffuser_bundles(
    left: _VerifiedDiffuserBundle, right: _VerifiedDiffuserBundle, *, include_timings: bool = False,
    probability_roundoff: bool = False,
) -> dict[str, Any]:
    left_receipt, left_report, left_request = left
    right_receipt, right_report, right_request = right
    roundoff = None
    if probability_roundoff:
        observed_digest = right_report["witness_replay_evidence"]["plan_sha256"]
        document, _ = _decode_diffuser_replay(left_request)
        expected_request, _ = _diffuser_request_bytes_for_plan(left_request, document, observed_digest)
        if right_request != expected_request:
            raise ValueError("diffuser replay receipt differs from the preflight request beyond the observed plan digest")
        statistics = _compare_diffuser_plan_roundoff(
            _diffuser_replay_plan(left_report, left_report["modeled_witness_validation"]),
            _diffuser_replay_plan(right_report, right_report["modeled_witness_validation"]),
        )
        baseline_digest = document["expected"]["plan_sha256"]
        roundoff = {
            "status": "EXACT_PLAN_MATCH" if baseline_digest == observed_digest else "MODELED_PROBABILITY_ROUNDOFF_MATCHED",
            "absolute_tolerance": DIFFUSER_PLAN_PROBABILITY_ROUNDOFF,
            "baseline_plan_sha256": baseline_digest, "observed_plan_sha256": observed_digest, **statistics,
        }
    left_snapshot = left_report["witness_replay_evidence"]["snapshot"]
    right_snapshot = right_report["witness_replay_evidence"]["snapshot"]
    context_changes = [
        field for field, matches in (
            ("formula", left_report["constraint_sha256"] == right_report["constraint_sha256"]),
            ("snapshot", left_snapshot == right_snapshot),
            ("software_versions", left_receipt["software_versions"] == right_receipt["software_versions"]),
        ) if not matches
    ]
    timing_context = list(context_changes)
    if include_timings and left_report["transpiler_seed"] != right_report["transpiler_seed"]:
        timing_context.append("transpiler_seed")
    left_variants, right_variants = _index_variants(left_report), _index_variants(right_report)
    variants = []
    totals = dict.fromkeys(_SEED_COUNTS, 0)
    for key in sorted(left_variants.keys() | right_variants.keys()):
        left_variant, right_variant = left_variants.get(key), right_variants.get(key)
        left_summary, right_summary = _variant_summary(left_variant), _variant_summary(right_variant)
        changes = list(context_changes)
        if left_variant is not None and right_variant is not None:
            left_record, right_record = left_variant[1], right_variant[1]
            for field, matches in (
                ("compiled_circuit", _diffuser_circuit_identity(left_record) == _diffuser_circuit_identity(right_record)),
                ("modeled_output", probability_roundoff or left_record["modeled_output"] == right_record["modeled_output"]),
                ("shots_per_batch", left_record["modeled_witness_budget"]["shots_required"] ==
                 right_record["modeled_witness_budget"]["shots_required"]),
            ):
                if not matches:
                    changes.append(field)
        batches = _compare_batches(left_variant, right_variant, matching_conditions=not changes)
        for field in totals:
            totals[field] += batches[field]
        layout, rounds, diffuser = key
        row = {
            "physical_placement": list(layout), "rounds": rounds, "diffuser": diffuser,
            "left": left_summary, "right": right_summary, "context_changes": changes, "batches": batches,
        }
        if include_timings:
            row["compilation_timing"] = _compare_compilation_timing(left_variant, right_variant, timing_context)
        variants.append(row)

    left_workload, right_workload = left_receipt["workload"], right_receipt["workload"]
    same_request = left_receipt["content_sha256"] == right_receipt["content_sha256"]
    matches = (same_request or probability_roundoff) and left_workload == right_workload and all(
        not row["context_changes"] and not row["batches"]["differences"]
        and (not probability_roundoff or row["left"] == row["right"]) for row in variants
    )
    report = {
        "status": "DIFFUSER_RECEIPTS_MATCH" if matches else "DIFFUSER_RECEIPTS_DIFFER",
        "schema_version": 1,
        "left": {**left_receipt, "snapshot": left_snapshot},
        "right": {**right_receipt, "snapshot": right_snapshot},
        "request_content_matches": same_request,
        "exact_artifact_bytes_match": {
            name: left_receipt["artifacts"][name] == right_receipt["artifacts"][name]
            for name in ("request.json", "report.json")
        },
        "context_changes": context_changes,
        "workload_delta": {
            key: right_workload[key] - value for key, value in left_workload.items() if type(value) is int
        },
        "alignment": {
            "shared_variants": len(left_variants.keys() & right_variants.keys()),
            "left_only_variants": len(left_variants.keys() - right_variants.keys()),
            "right_only_variants": len(right_variants.keys() - left_variants.keys()),
            **totals,
        },
        "variants": variants,
    }
    if include_timings:
        report["compilation_timing"] = _summarize_compilation_timing(variants)
    if roundoff is not None:
        report["probability_roundoff"] = roundoff
    return report
