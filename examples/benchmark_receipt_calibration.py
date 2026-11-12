"""Profile receipt-backed dated calibration studies and their local density preparation."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
from collections import defaultdict
from collections.abc import Set
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
from re import fullmatch
from statistics import median
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from austins_algorithm.constraints import BooleanConstraint
    from austins_algorithm.snapshot_calibration_comparison import SnapshotCalibrationFiles


_MAX_PAIRS = 8
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_BASELINE_BYTES = 32 * 1024 * 1024
_PHASES = ("receipt_replay_verify", "compilation", "density", "other")
_COMPILER_STAGES = ("compile_export_verify", "transpile", "openqasm_export", "openqasm_import", "openqasm_verify")
_ARMS = ("separate", "shared")
_DENSITY_ARMS = ("serial", "batched")


@dataclass(frozen=True)
class _Scenario:
    constraint: BooleanConstraint
    archives: tuple[SnapshotCalibrationFiles, SnapshotCalibrationFiles]
    study: dict[str, Any]
    receipt: dict[str, Any]
    identity: dict[str, Any]
    baseline: bytes | None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _record(value: Any, fields: set[str], label: str, *, optional: Set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not fields <= value.keys() <= fields | optional:
        raise ValueError(f"invalid {label} fields")
    return value


def _path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} requires a file path")
    return root / value


def _pin(value: Any, label: str) -> str:
    if not isinstance(value, str) or fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} requires a lowercase SHA-256 pin")
    return value


def _file_pin(root: Path, value: Any, label: str) -> tuple[Path, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} requires [path, SHA-256]")
    return _path(root, value[0], label), _pin(value[1], label)


def _read_json(path: Path, limit: int, label: str, *, expected_sha256: str | None = None) -> tuple[Any, str]:
    from austins_algorithm.constraints import _unique_object

    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not 1 <= len(raw) <= limit:
        raise ValueError(f"{label} is outside its byte limit")
    digest = sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError(f"{label} does not match its SHA-256 pin")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=lambda pairs: _unique_object(pairs, label))
        _canonical(document)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc
    return document, digest


def _load_scenario(path: Path) -> _Scenario:
    from austins_algorithm.constraints import load_constraint
    from austins_algorithm.snapshot_calibration_comparison import SnapshotCalibrationFiles

    raw, manifest_sha256 = _read_json(path, _MAX_MANIFEST_BYTES, "scenario manifest")
    manifest = _record(raw, {"schema_version", "constraint", "earlier", "later", "receipt", "expected_receipt_sha256",
                             "output_bundle", "expected_output_sha256", "sources", "study"},
                       "scenario manifest", optional={"baseline_study_report"})
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported scenario manifest schema version")
    root = path.absolute().parent
    constraint = load_constraint(_path(root, manifest["constraint"], "constraint"))

    def archive(label: str) -> SnapshotCalibrationFiles:
        row = _record(manifest[label], {"configuration", "properties", "configuration_sha256", "properties_sha256"},
                      f"{label} archive")
        return SnapshotCalibrationFiles(
            _path(root, row["configuration"], f"{label} configuration"),
            _path(root, row["properties"], f"{label} properties"),
            _pin(row["configuration_sha256"], f"{label} configuration"),
            _pin(row["properties_sha256"], f"{label} properties"),
        )

    archives = archive("earlier"), archive("later")
    study = _record(manifest["study"], {"initial_layouts", "target_probability", "shot_caps", "query_caps",
                                       "seed_transpiler", "validation_trials"}, "study options",
                    optional={"max_rounds_per_shot", "prefer_native_qasm"})
    raw_sources = manifest["sources"]
    if not isinstance(raw_sources, list) or not 2 <= len(raw_sources) <= 8:
        raise ValueError("scenario requires 2 to 8 source bundles")
    sources = [_file_pin(root, item, "source bundle") for item in raw_sources]
    receipt = {
        "receipt": _path(root, manifest["receipt"], "receipt"),
        "expected_receipt_sha256": _pin(manifest["expected_receipt_sha256"], "receipt"),
        "output_bundle": _path(root, manifest["output_bundle"], "output bundle"),
        "expected_output_sha256": _pin(manifest["expected_output_sha256"], "output bundle"),
        "sources": sources,
    }
    baseline = baseline_sha256 = None
    if "baseline_study_report" in manifest:
        baseline_path, baseline_sha256 = _file_pin(root, manifest["baseline_study_report"], "baseline study report")
        report, _ = _read_json(baseline_path, _MAX_BASELINE_BYTES, "baseline study report",
                               expected_sha256=baseline_sha256)
        baseline = _canonical(report)
    identity = {
        "scenario_manifest_sha256": manifest_sha256, "constraint_sha256": constraint.digest(),
        "archive_sha256s": {label: {"configuration": item.configuration_sha256, "properties": item.properties_sha256}
                            for label, item in zip(("earlier", "later"), archives, strict=True)},
        "receipt_sha256": receipt["expected_receipt_sha256"], "output_bundle_sha256": receipt["expected_output_sha256"],
        "source_bundle_sha256s": sorted(pin for _, pin in sources), "baseline_study_report_sha256": baseline_sha256,
    }
    return _Scenario(constraint, archives, study, receipt, identity, baseline)


def _offline(event: str, _arguments: tuple[Any, ...]) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
        raise RuntimeError(f"offline benchmark blocked {event}")


def _measure(scenario: _Scenario, arm: str) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    from qiskit import qpy
    from qiskit_aer import AerSimulator
    from qiskit_aer.jobs import AerJob
    from qiskit_aer.noise import NoiseModel

    from austins_algorithm import snapshot_calibration_comparison as comparison
    from austins_algorithm import snapshot_calibration_compilation_cache as cache
    from austins_algorithm import snapshot_calibration_overlap as overlap
    from austins_algorithm import snapshot_calibration_overlap_receipt as receipts
    from austins_algorithm import snapshot_calibration_study as study

    instruments = (
        (comparison, "_read_archive", "archive_reads"), (comparison, "_offline_backend", "target_builds"),
        (comparison, "audit_phase_oracle", "oracle_audits"), (comparison, "compile_and_verify", "compiler_calls"),
        (comparison, "_program_calibration", "target_checks"),
        (comparison, "_compact_snapshot_inputs", "noise_setups"),
        (comparison, "_prepare_density_snapshot", "density_single_preparations"),
        (comparison, "_prepare_density_snapshot_batch", "density_batch_preparations"),
        (comparison, "_prepared_snapshot_probabilities", "probability_evaluations"),
        (AerSimulator, "run", "aer_run_calls"), (AerJob, "result", "aer_result_waits"),
        (NoiseModel, "to_dict", "noise_model_serializations"),
        (study, "_prepare_study", "request_preparations"),
        (study, "_run_prepared_study", "study_captures"),
        (study, "plan_snapshot_calibration_budgets", "planning_and_seeded_replay"),
        (study, "build_grover_search_circuit", "logical_circuits"),
        (comparison, "build_grover_search_circuit", "logical_circuits"),
        (study, "_calibration_round_key", "compilation_keys"),
        (comparison, "_calibration_round_key", "compilation_keys"),
        (receipts, "_read_verified_overlap_receipt", "receipt_verifications"),
        (receipts, "_read_bundle_document", "receipt_source_bundle_reads"),
        (receipts, "_verify_artifact", "receipt_output_pin_checks"),
        (cache, "_read_bundle", "extra_output_reads"),
        (cache, "_restore_compiled_round", "bundle_restores"),
        (overlap, "_restore_compiled_round", "overlap_restores"),
        (qpy, "load", "qpy_decodes"),
    )
    calls = dict.fromkeys((name for _, _, name in instruments), 0)
    elapsed: defaultdict[str, int] = defaultdict(int)
    compiler_stages = dict.fromkeys(_COMPILER_STAGES, 0.0)
    density_batch_sizes = []
    capture_start = None
    shared = arm == "shared" or arm in _DENSITY_ARMS

    def instrument(function, label):
        @wraps(function)
        def measured(*args, **kwargs):
            nonlocal capture_start
            started = perf_counter_ns()
            if shared and label == "study_captures":
                capture_start = started
            if label in {"compiler_calls", "density_single_preparations", "density_batch_preparations"} and capture_start is None:
                raise RuntimeError(f"{arm} ran {label} before reaching study capture")
            if label == "density_batch_preparations":
                density_batch_sizes.append(len(args[0]))
            try:
                result = function(*args, **kwargs)
            finally:
                elapsed[label] += perf_counter_ns() - started
                calls[label] += 1
            if label == "compiler_calls":
                for stage in _COMPILER_STAGES:
                    compiler_stages[stage] += result.timing_seconds[stage]
            return result
        return measured

    args = (scenario.constraint, *scenario.archives)
    with ExitStack() as stack:
        for module, name, label in instruments:
            stack.enter_context(patch.object(module, name, instrument(getattr(module, name), label)))
        start = perf_counter_ns()
        if shared:
            density_options = {"batch_density": True} if arm == "batched" else {}
            combined = study.run_snapshot_calibration_study_with_overlap_receipt(
                *args, **scenario.receipt, **scenario.study, **density_options,
            )
            replay, report = combined["replay"], combined["study"]
        elif arm == "separate":
            replay = study.replay_snapshot_calibration_overlap_receipt(*args, **scenario.receipt, **scenario.study)
            capture_start = perf_counter_ns()
            loaded = cache.SnapshotCalibrationCompilationCache.load_bundle(
                scenario.receipt["output_bundle"], expected_sha256=scenario.receipt["expected_output_sha256"],
            )
            report = study.run_snapshot_calibration_study(*args, **scenario.study, compilation_cache=loaded)
        else:
            replay = None
            capture_start = start
            report = study.run_snapshot_calibration_study(*args, **scenario.study)
        wall = perf_counter_ns() - start

    if capture_start is None or calls["study_captures"] != 1:
        raise RuntimeError(f"{arm} did not reach one study capture")
    if report["status"] != "CALIBRATION_STUDY_COMPLETED":
        raise RuntimeError(f"{arm} returned {report['status']}; stopping matched timing")
    if report["provider_job_submitted"] or report["quantum_hardware_used"]:
        raise RuntimeError(f"{arm} returned unexpected provider or hardware evidence")
    cells = len(report["coverage"]["completed_layouts"]) * len(report["required_rounds"])
    calls["density_simulations"] = calls["density_single_preparations"] + sum(density_batch_sizes)
    density_jobs = calls["density_single_preparations"] + calls["density_batch_preparations"]
    if calls["density_simulations"] != 2 * cells:
        raise RuntimeError(f"{arm} did not simulate both snapshots for every completed round")
    if calls["aer_run_calls"] != density_jobs or calls["aer_result_waits"] != density_jobs:
        raise RuntimeError(f"{arm} did not submit and await one Aer job per density preparation")
    if arm != "batched" and density_batch_sizes:
        raise RuntimeError(f"{arm} unexpectedly used batched density preparation")
    if arm == "fresh" and calls["compiler_calls"] != cells:
        raise RuntimeError("fresh baseline did not compile every requested round")

    replay_ns = capture_start - start
    elapsed["density_simulations"] = elapsed["density_single_preparations"] + elapsed["density_batch_preparations"]
    compile_ns, density_ns = elapsed["compiler_calls"], elapsed["density_simulations"]
    other_ns = wall - replay_ns - compile_ns - density_ns
    if other_ns < 0:
        raise RuntimeError(f"{arm} timing phases overlap")
    phases = dict(zip(_PHASES, (replay_ns, compile_ns, density_ns, other_ns), strict=True))
    measurement = {
        "arm": arm, "wall_seconds": wall / 1e9, "phase_seconds": {key: value / 1e9 for key, value in phases.items()},
        "calls": calls, "operation_inclusive_seconds": {key: elapsed[key] / 1e9 for key in calls},
        "compiler_inclusive_seconds": compiler_stages, "density_batch_sizes": density_batch_sizes,
    }
    return measurement, replay, report


def _verify_match(arm: str, replay: dict[str, Any] | None, report: dict[str, Any],
                  expected_replay: bytes, expected_study: bytes) -> None:
    if arm != "fresh" and _canonical(replay) != expected_replay:
        raise RuntimeError(f"{arm} replay report differs from the matched reference")
    if _canonical(report) != expected_study:
        raise RuntimeError(f"{arm} study report differs from the matched reference")


def _without_batch_markers(report: dict[str, Any]) -> dict[str, Any]:
    """Check sealed batch provenance and recompute only the digests it changes."""
    normalized = deepcopy(report)
    if normalized["inputs"].pop("batch_density", None) is not True:
        raise RuntimeError("batched study did not report its density policy")
    for key, capture in normalized["captures"].items():
        capture_report = capture["report"]
        layout = normalized["planning"]["layouts"][key]
        recorded = capture["report_sha256"]
        if recorded != sha256(_canonical(capture_report)).hexdigest() or layout["source_capture_sha256"] != recorded:
            raise RuntimeError(f"batched capture digest does not bind the report for layout {key}")
        if capture_report.pop("batch_density", None) is not True:
            raise RuntimeError(f"batched capture did not report its density policy for layout {key}")
        capture["report_sha256"] = layout["source_capture_sha256"] = sha256(_canonical(capture_report)).hexdigest()
    return normalized


def _benchmark(scenario: _Scenario, pairs: int, *, fresh_baseline: bool,
               compare_density_batching: bool = False) -> dict[str, Any]:
    from austins_algorithm.snapshot_calibration_compilation_bundle import _bundle_software_versions

    arms = _DENSITY_ARMS if compare_density_batching else _ARMS
    first, replay, report = _measure(scenario, arms[0])
    expected_replay, expected_study = _canonical(replay), _canonical(report)
    request = {"inputs": report["inputs"], "required_rounds": report["required_rounds"],
               "layouts": report["coverage"]["requested_layouts"]}
    if scenario.baseline is not None and scenario.baseline != expected_study:
        raise RuntimeError("study report differs from the pinned baseline")
    second, replay, report = _measure(scenario, arms[1])
    _verify_match(arms[1], replay, _without_batch_markers(report) if compare_density_batching else report,
                  expected_replay, expected_study)
    expected_second_study = _canonical(report) if compare_density_batching else expected_study

    measured = []
    for pair in range(pairs):
        for arm in arms if pair % 2 == 0 else reversed(arms):
            row, replay, report = _measure(scenario, arm)
            expected = expected_study if arm == arms[0] else expected_second_study
            _verify_match(arm, replay, report, expected_replay, expected)
            measured.append({"pair": pair + 1, **row})
    medians = {}
    for arm in arms:
        rows = [row for row in measured if row["arm"] == arm]
        medians[arm] = {
            "wall_seconds": median(row["wall_seconds"] for row in rows),
            "phase_seconds": {stage: median(row["phase_seconds"][stage] for row in rows) for stage in _PHASES},
            "operation_inclusive_seconds": {
                stage: median(row["operation_inclusive_seconds"][stage] for row in rows) for stage in rows[0]["calls"]
            },
            "compiler_inclusive_seconds": {
                stage: median(row["compiler_inclusive_seconds"][stage] for row in rows) for stage in _COMPILER_STAGES
            },
        }

    fresh = None
    if fresh_baseline:
        fresh, replay, report = _measure(scenario, "fresh")
        _verify_match("fresh", replay, report, expected_replay, expected_study)
    summary = {
        "schema_version": 1, "status": "RECEIPT_CALIBRATION_BENCHMARK_COMPLETED", "inputs": scenario.identity,
        "study_request": request,
        "software_versions": _bundle_software_versions(), "qiskit_parallel": os.environ["QISKIT_PARALLEL"],
        "logical_cpu_count": os.cpu_count(),
        "thread_environment": {name: os.environ.get(name) for name in ("OMP_NUM_THREADS", "RAYON_NUM_THREADS")},
        "timing_scope": {
            "sampling": "one excluded warmup pair, then alternating serial pairs in one process",
            "wall": "instrumented API elapsed time; excludes startup, scenario loading, output and parity checks",
            "receipt_replay_verify": "entry through replay completion for separate, or through prepared-study entry for receipt reuse",
            "compilation": "compile_and_verify calls, including their transpilation and export verification",
            "density": "whole single or batched density preparation calls, including Aer result waits and readout setup",
            "other": "wall remainder, including study setup, bundled restore, noise setup and budget planning",
            "inclusive": "per-operation times can overlap; Aer noise serialization can run inside result waits",
            "medians": "independent medians per stage, which need not sum to the median wall time",
            "fresh_baseline": "optional uncached compilation after the pairs in the same warmed process",
        },
        "pairs": pairs, "replay_report_sha256": sha256(expected_replay).hexdigest(),
        "report_digest_encoding": "CANONICAL_JSON_UTF8",
        "baseline_report_matched": True if scenario.baseline is not None else None,
        "warmup": [first, second], "measurements": measured, "medians": medians, "fresh_baseline": fresh,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if compare_density_batching:
        serial_rows = [row for row in measured if row["arm"] == "serial"]
        batched_rows = [row for row in measured if row["arm"] == "batched"]
        summary.update(
            status="RECEIPT_CALIBRATION_DENSITY_BATCHING_BENCHMARK_COMPLETED",
            serial_study_report_sha256=sha256(expected_study).hexdigest(),
            batched_study_report_sha256=sha256(expected_second_study).hexdigest(),
            scientific_report_matched=True,
            batched_group_observed=any(row["density_batch_sizes"] for row in batched_rows),
            paired_median_savings_seconds={
                "wall": median(serial["wall_seconds"] - batched["wall_seconds"]
                               for serial, batched in zip(serial_rows, batched_rows, strict=True)),
                **{phase: median(serial["phase_seconds"][phase] - batched["phase_seconds"][phase]
                                 for serial, batched in zip(serial_rows, batched_rows, strict=True))
                   for phase in _PHASES},
            },
        )
    else:
        summary["study_report_sha256"] = sha256(expected_study).hexdigest()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="JSON manifest with pinned receipt, archive and bundle paths")
    parser.add_argument("--pairs", type=int, default=5, help=f"number of measured pairs, 1 to {_MAX_PAIRS}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fresh-baseline", action="store_true", help="also profile one full uncached compilation")
    mode.add_argument("--compare-density-batching", action="store_true",
                      help="profile matched serial and batched density preparation with the same pinned receipt")
    args = parser.parse_args()
    if not 1 <= args.pairs <= _MAX_PAIRS:
        parser.error(f"--pairs must be from 1 to {_MAX_PAIRS}")
    os.environ["QISKIT_PARALLEL"] = "FALSE"
    sys.addaudithook(_offline)
    try:
        summary = _benchmark(_load_scenario(args.scenario), args.pairs, fresh_baseline=args.fresh_baseline,
                             compare_density_batching=args.compare_density_batching)
        print(json.dumps(summary, indent=2, allow_nan=False))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Receipt calibration benchmark failed: {exc}\n")


if __name__ == "__main__":
    main()
