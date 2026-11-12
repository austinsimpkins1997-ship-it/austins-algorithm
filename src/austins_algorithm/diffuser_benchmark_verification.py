"""Check a retained diffuser benchmark without launching a worker."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from re import fullmatch
from typing import Any

from .constraint_diffuser_comparison import _diffuser_replay_digest
from .constraint_replay import _parse_request, _read_request, _require_digest, _require_fields
from .core import _bounded_int
from .diffuser_benchmark import (
    MAX_BENCHMARK_PAIRS,
    _benchmark_schedule,
    _fingerprint_trial_file,
    _matched_request_fields,
    _timing_summary,
    _verify_trial_report,
    _wall_time,
)
from .diffuser_replay import _decode_diffuser_replay
from .replay_receipt import _LIMITS, _SOFTWARE_PACKAGES, _regular_file
from .replay_regression import MAX_REGRESSION_TIMEOUT_SECONDS


_SUMMARY_FIELDS = {
    "schema_version", "status", "output_directory", "requested_pairs", "timeout_seconds",
    "verified_trials", "environment", "inputs", "compiled_output_sha256", "local_sampling_performed",
    "provider_job_submitted", "quantum_hardware_used", "trials", "timing_summary",
}
_INPUT_FIELDS = {"source", "staged_file", "request_sha256", "content_sha256", "prefer_native_qasm", "expected"}
_TRIAL_FIELDS = {"pair", "side", "artifacts_directory", "status"}
_TRIAL_ARTIFACTS = {"report.json", "stderr.log"}
_NO_SAMPLING_FLAGS = ("local_sampling_performed", "provider_job_submitted", "quantum_hardware_used")


def _canonical_child(directory: Path, name: str) -> Path:
    child = directory / name
    if child.is_symlink() or child.resolve() != child:
        raise ValueError(f"benchmark artifact is an alias or outside its canonical path: {name}")
    return child


def _read_summary(directory: Path) -> tuple[dict[str, Any], str]:
    path = _canonical_child(directory, "summary.json")
    _regular_file(path)
    limit = _LIMITS["report.json"]
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("diffuser benchmark summary exceeds its size limit")
    summary = _require_fields(_parse_request(raw, label="diffuser benchmark summary"), _SUMMARY_FIELDS,
                              "diffuser benchmark summary")
    return summary, sha256(raw).hexdigest()


def _read_inputs(directory: Path, inputs: Any) -> dict[str, dict[str, Any]]:
    _require_fields(inputs, {"left", "right"}, "diffuser benchmark inputs")
    documents = {}
    for side in ("left", "right"):
        entry = _require_fields(inputs[side], _INPUT_FIELDS, f"diffuser benchmark {side} input")
        name = f"{side}-request.json"
        if entry["staged_file"] != name or not isinstance(entry["source"], str) or not entry["source"]:
            raise ValueError(f"invalid diffuser benchmark {side} input metadata")
        path = _canonical_child(directory, name)
        _regular_file(path)
        request = _read_request(path)
        if entry["request_sha256"] != sha256(request).hexdigest():
            raise ValueError(f"diffuser benchmark {side} request SHA-256 mismatch")
        document, _ = _decode_diffuser_replay(request)
        if (
            entry["content_sha256"] != document["content_sha256"]
            or entry["prefer_native_qasm"] is not document["sampling"].get("prefer_native_qasm", False)
            or _diffuser_replay_digest(entry["expected"]) != _diffuser_replay_digest(document["expected"])
        ):
            raise ValueError(f"diffuser benchmark {side} request metadata differs from the staged request")
        documents[side] = document
    left, right = (_matched_request_fields(documents[side]) for side in ("left", "right"))
    if left != right:
        raise ValueError("diffuser benchmark staged requests do not describe matched work")
    return documents


def _check_environment(environment: Any, expected_versions: Any = None) -> None:
    _require_fields(environment, {"platform", "processor", "logical_cpu_count", "software_versions"},
                    "diffuser benchmark environment")
    cpu_count = environment["logical_cpu_count"]
    processor = environment["processor"]
    if (
        not isinstance(environment["platform"], str)
        or (processor is not None and (not isinstance(processor, str) or not processor))
        or (cpu_count is not None and (type(cpu_count) is not int or cpu_count < 1))
    ):
        raise ValueError("invalid recorded diffuser benchmark environment")
    versions = _require_fields(environment["software_versions"], {"python", "austins_algorithm", *_SOFTWARE_PACKAGES},
                               "diffuser benchmark software versions")
    if (
        any(value is not None and (not isinstance(value, str) or not value) for value in versions.values())
        or versions["python"] is None or versions["austins_algorithm"] is None
    ):
        raise ValueError("invalid recorded diffuser benchmark software versions")
    if expected_versions is not None and (
        not isinstance(expected_versions, dict)
        or any(name not in versions or versions[name] != version for name, version in expected_versions.items())
    ):
        raise ValueError("recorded diffuser benchmark software versions differ from the staged request")


def _check_trial_artifacts(
    row: dict[str, Any], paths: dict[str, Path], version: int, *, report_fingerprint: dict[str, Any] | None,
) -> None:
    required = row["status"] in ("VERIFIED", "TIMED_OUT") or "worker_exit_code" in row
    for path in paths.values():
        if required or path.exists():
            _regular_file(path)
    if version == 1:
        return

    artifacts = _require_fields(row["artifacts"], _TRIAL_ARTIFACTS, "diffuser benchmark trial artifacts")
    for name, path in paths.items():
        expected = artifacts[name]
        if expected is not None:
            _require_fields(expected, {"bytes", "sha256"}, f"diffuser benchmark {name} fingerprint")
            if type(expected["bytes"]) is not int or expected["bytes"] < 0:
                raise ValueError(f"invalid diffuser benchmark {name} byte count")
            _require_digest(expected["sha256"], f"diffuser benchmark {name} SHA-256")
        actual = report_fingerprint if name == "report.json" and report_fingerprint is not None else (
            _fingerprint_trial_file(path)
        )
        if actual != expected:
            raise ValueError(f"diffuser benchmark artifact fingerprint mismatch: {row['artifacts_directory']}/{name}")


def _verify_trial(
    directory: Path, row: Any, pair: int, side: str, *, verified: bool, version: int,
    document: dict[str, Any],
) -> list[dict[str, Any]] | None:
    if not isinstance(row, dict):
        raise ValueError("invalid diffuser benchmark trial row")
    fields = set(_TRIAL_FIELDS)
    if version == 2:
        fields.add("artifacts")
    status = row.get("status")
    if verified:
        if status != "VERIFIED":
            raise ValueError("diffuser benchmark trial is outside the verified schedule prefix")
        fields.update(("worker_exit_code", "seconds", "actual_importers"))
    else:
        if status not in ("FAILED", "TIMED_OUT"):
            raise ValueError("diffuser benchmark must end at its first failed or timed-out trial")
        fields.add("error")
        if status == "FAILED" and "worker_exit_code" in row:
            fields.add("worker_exit_code")
    _require_fields(row, fields, "diffuser benchmark trial")
    if type(row["pair"]) is not int or row["pair"] != pair or row["side"] != side:
        raise ValueError("diffuser benchmark trial order differs from the alternating schedule")
    if "worker_exit_code" in row and type(row["worker_exit_code"]) is not int:
        raise ValueError("invalid diffuser benchmark worker exit code")
    if not verified and not isinstance(row["error"], str):
        raise ValueError("invalid recorded diffuser benchmark failure")

    if directory.exists() and not directory.is_dir():
        raise ValueError(f"diffuser benchmark trial path is not a directory: {directory.name}")
    paths = {name: _canonical_child(directory, name) for name in _TRIAL_ARTIFACTS}
    report_fingerprint = None
    circuits = None
    if verified:
        if not isinstance(row["seconds"], dict) or row["worker_exit_code"] != 0:
            raise ValueError("invalid diffuser benchmark verified trial timing or exit code")
        wall = _wall_time(row["seconds"].get("process_wall"))
        _regular_file(paths["report.json"])
        seconds, importers, circuits, report_fingerprint = _verify_trial_report(paths["report.json"], document, wall)
        if (
            _diffuser_replay_digest(row["seconds"]) != _diffuser_replay_digest(seconds)
            or _diffuser_replay_digest(row["actual_importers"]) != _diffuser_replay_digest(importers)
        ):
            raise ValueError(f"diffuser benchmark verified trial summary differs from its report: {directory.name}")

    _check_trial_artifacts(row, paths, version, report_fingerprint=report_fingerprint)
    return circuits


_VerifiedBenchmarkBundle = tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]


def _read_verified_benchmark(directory: str | Path) -> _VerifiedBenchmarkBundle:
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("diffuser benchmark evidence must be a directory")
    summary, summary_sha256 = _read_summary(root)
    version = summary["schema_version"]
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported diffuser benchmark summary schema_version")
    status = summary["status"]
    if status not in ("BENCHMARK_COMPLETED", "BENCHMARK_FAILED"):
        raise ValueError("unsupported diffuser benchmark status")
    if (
        any(summary[flag] is not False for flag in _NO_SAMPLING_FLAGS)
        or not isinstance(summary["output_directory"], str) or not summary["output_directory"]
    ):
        raise ValueError("invalid diffuser benchmark summary provenance")
    pairs = _bounded_int(summary["requested_pairs"], 1, MAX_BENCHMARK_PAIRS, "requested_pairs")
    _bounded_int(summary["timeout_seconds"], 1, MAX_REGRESSION_TIMEOUT_SECONDS, "timeout_seconds")
    schedule = _benchmark_schedule(pairs)
    completed = status == "BENCHMARK_COMPLETED"
    trials = summary["trials"]
    if not isinstance(trials, list) or not 1 <= len(trials) <= len(schedule):
        raise ValueError("invalid diffuser benchmark trial count")
    if completed and len(trials) != len(schedule):
        raise ValueError("completed diffuser benchmark is missing scheduled trials")
    verified_count = len(trials) if completed else len(trials) - 1
    if type(summary["verified_trials"]) is not int or summary["verified_trials"] != verified_count:
        raise ValueError("diffuser benchmark verified trial count differs from its schedule")

    documents = _read_inputs(root, summary["inputs"])
    _check_environment(summary["environment"], documents["left"]["expected"]["versions"] if verified_count else None)
    reference = None
    expected_directories = set()
    for index, (row, (pair, side)) in enumerate(zip(trials, schedule)):
        name = f"{index + 1:02d}-{side}"
        expected_directories.add(name)
        if not isinstance(row, dict) or row.get("artifacts_directory") != name:
            raise ValueError("diffuser benchmark trial artifacts_directory differs from the alternating schedule")
        trial_directory = _canonical_child(root, name)
        circuits = _verify_trial(trial_directory, row, pair, side, verified=index < verified_count,
                                 version=version, document=documents[side])
        if circuits is not None:
            if reference is not None and reference != circuits:
                raise ValueError("diffuser benchmark compiled outputs differ beyond importer evidence")
            reference = circuits
    if any(
        fullmatch(r"[0-9]{2}-(left|right)", path.name) and path.name not in expected_directories
        for path in root.iterdir()
    ):
        raise ValueError("diffuser benchmark has unlisted trial artifacts")

    timing_summary = _timing_summary(trials) if completed else None
    compiled_output_sha256 = _diffuser_replay_digest(reference) if completed else None
    if summary["compiled_output_sha256"] != compiled_output_sha256:
        raise ValueError("diffuser benchmark compiled output digest differs from the verified trials")
    if _diffuser_replay_digest(summary["timing_summary"]) != _diffuser_replay_digest(timing_summary):
        raise ValueError("diffuser benchmark timing summary differs from the verified paired statistics")
    verification = {
        "status": "BENCHMARK_EVIDENCE_VERIFIED",
        "benchmark_status": status,
        "benchmark_schema_version": version,
        "benchmark_directory": str(root),
        "artifact_integrity": "CHECKSUMS_VERIFIED" if version == 2 else "TRIAL_CHECKSUMS_UNRECORDED",
        "requested_pairs": pairs,
        "verified_trials": verified_count,
        "terminal_trial_status": None if completed else trials[-1]["status"],
        "compiled_output_sha256": compiled_output_sha256,
        "timing_summary": timing_summary,
        "summary_sha256": summary_sha256,
    }
    return verification, summary, documents


def verify_diffuser_benchmark(directory: str | Path) -> dict[str, Any]:
    """Validate saved benchmark evidence and recompute complete paired statistics."""
    return _read_verified_benchmark(directory)[0]
