"""Compare independent saved diffuser benchmarks after verifying each bundle."""

from __future__ import annotations

from math import isfinite
from pathlib import Path
from typing import Any

from .diffuser_benchmark import _matched_request_fields
from .diffuser_benchmark_verification import _VerifiedBenchmarkBundle, _read_verified_benchmark


def _read_side(directory: str | Path, label: str) -> _VerifiedBenchmarkBundle:
    try:
        return _read_verified_benchmark(directory)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} benchmark {directory}: {exc}") from exc


def _overview(bundle: _VerifiedBenchmarkBundle) -> dict[str, Any]:
    verification, summary, _ = bundle
    return {
        **{key: value for key, value in verification.items() if key != "timing_summary"},
        "verified_pairs": verification["verified_trials"] // 2,
        "timeout_seconds": summary["timeout_seconds"],
        "environment": summary["environment"],
    }


def _timing_group(bundle: _VerifiedBenchmarkBundle, key: str) -> dict[str, Any] | None:
    timing = bundle[0]["timing_summary"]
    return timing[key] if timing is not None else None


def _median_deltas(baseline: dict[str, Any] | None, candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if baseline is None or candidate is None:
        return None
    differences = {key: candidate[key]["median"] - stats["median"] for key, stats in baseline.items()}
    return {key: value if isfinite(value) else None for key, value in differences.items()}


def _arm_record(bundle: _VerifiedBenchmarkBundle, side: str) -> dict[str, Any]:
    _, summary, _ = bundle
    verified = [row for row in summary["trials"] if row["side"] == side and row["status"] == "VERIFIED"]
    entry = summary["inputs"][side]
    return {
        "verified_trials": len(verified),
        "prefer_native_qasm": entry["prefer_native_qasm"],
        "actual_importers_per_trial": verified[0]["actual_importers"] if verified else None,
        "request_sha256": entry["request_sha256"],
        "content_sha256": entry["content_sha256"],
        "request_circuits_sha256": entry["expected"]["circuits_sha256"],
        "timing_seconds": _timing_group(bundle, side),
    }


def _workload_fields(document: dict[str, Any]) -> dict[str, Any]:
    fields = _matched_request_fields(document)
    expected = fields.pop("expected")
    return {**fields, **{key: value for key, value in expected.items() if key != "versions"}}


def _compare_arm(
    baseline: _VerifiedBenchmarkBundle, candidate: _VerifiedBenchmarkBundle, side: str,
) -> dict[str, Any]:
    baseline_arm, candidate_arm = _arm_record(baseline, side), _arm_record(candidate, side)
    before, after = _workload_fields(baseline[2][side]), _workload_fields(candidate[2][side])
    baseline_importers = baseline_arm["actual_importers_per_trial"]
    candidate_importers = candidate_arm["actual_importers_per_trial"]
    importer_changes = []
    if baseline_arm["prefer_native_qasm"] != candidate_arm["prefer_native_qasm"]:
        importer_changes.append("prefer_native_qasm")
    if baseline_importers is not None and candidate_importers is not None and baseline_importers != candidate_importers:
        importer_changes.append("actual_importers_per_trial")
    return {
        "baseline": baseline_arm,
        "candidate": candidate_arm,
        "workload_changes": [key for key, value in before.items() if value != after[key]],
        "importer_changes": importer_changes,
        "request_circuit_digest_matches": (
            baseline_arm["request_circuits_sha256"] == candidate_arm["request_circuits_sha256"]
        ),
        "request_content_matches": baseline_arm["content_sha256"] == candidate_arm["content_sha256"],
        "median_delta_seconds": _median_deltas(baseline_arm["timing_seconds"], candidate_arm["timing_seconds"]),
    }


def _environment_fields(environment: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: environment[key] for key in ("platform", "processor", "logical_cpu_count")},
        **{f"software_versions.{name}": version for name, version in sorted(environment["software_versions"].items())},
    }


def compare_diffuser_benchmarks(baseline: str | Path, candidate: str | Path) -> dict[str, Any]:
    """Compare declared arms and paired-effect summaries across verified offline bundles."""
    baseline_bundle = _read_side(baseline, "baseline")
    candidate_bundle = _read_side(candidate, "candidate")
    baseline_overview, candidate_overview = _overview(baseline_bundle), _overview(candidate_bundle)
    before = _environment_fields(baseline_overview["environment"])
    after = _environment_fields(candidate_overview["environment"])
    environment_changes = [
        {"field": key, "baseline": value, "candidate": after[key]}
        for key, value in before.items() if value != after[key]
    ]
    protocol_changes = []
    if baseline_overview["timeout_seconds"] != candidate_overview["timeout_seconds"]:
        protocol_changes.append("timeout_seconds")
    arms = {side: _compare_arm(baseline_bundle, candidate_bundle, side) for side in ("left", "right")}
    baseline_effect = _timing_group(baseline_bundle, "right_minus_left")
    candidate_effect = _timing_group(candidate_bundle, "right_minus_left")
    completed = baseline_overview["benchmark_status"] == candidate_overview["benchmark_status"] == "BENCHMARK_COMPLETED"
    compiled_match = None
    context = "INCOMPLETE"
    if completed:
        compiled_match = baseline_overview["compiled_output_sha256"] == candidate_overview["compiled_output_sha256"]
        changed = environment_changes or protocol_changes or not compiled_match or any(
            arm["workload_changes"] or arm["importer_changes"] or not arm["request_circuit_digest_matches"]
            for arm in arms.values()
        )
        context = "RECORDED_CONDITIONS_DIFFER" if changed else "MATCHING_RECORDED_CONDITIONS"
    return {
        "schema_version": 1,
        "status": "BENCHMARKS_COMPARED" if completed else "BENCHMARKS_INCOMPLETE",
        "timing_context": context,
        "baseline": baseline_overview,
        "candidate": candidate_overview,
        "summary_bytes_match": baseline_overview["summary_sha256"] == candidate_overview["summary_sha256"],
        "environment_changes": environment_changes,
        "protocol_changes": protocol_changes,
        "compiled_output_matches": compiled_match,
        "arms": arms,
        "paired_effect": {
            "baseline": baseline_effect,
            "candidate": candidate_effect,
            "median_delta_seconds": _median_deltas(baseline_effect, candidate_effect),
        },
    }

