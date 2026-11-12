"""Benchmark two matched diffuser requests with fresh, check-only workers."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from math import fsum, isclose, isfinite
import os
from pathlib import Path
import platform
from stat import S_ISREG
from statistics import median
import subprocess
import sys
from time import perf_counter
from typing import Any

from .compilation import _NATIVE_QASM_IMPORTER
from .constraint_diffuser_comparison import (
    DIFFUSER_VARIANT_COUNT,
    _diffuser_replay_circuit_digest,
    _diffuser_replay_digest,
    _diffuser_replay_plan_evidence,
)
from .constraint_replay import MAX_REPLAY_BYTES, _parse_request, _read_request
from .core import _bounded_int
from .diffuser_receipt_comparison import _COMPILATION_PHASES, _elapsed_seconds, _timing_circuit_identity
from .diffuser_replay import _decode_diffuser_replay, _run_diffuser_replay_bytes
from .replay_receipt import _LIMITS, _json_bytes, _software_versions
from .replay_regression import MAX_REGRESSION_TIMEOUT_SECONDS


MAX_BENCHMARK_PAIRS = 8
_WORKER_ENTRY = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from austins_algorithm.diffuser_benchmark import _worker_main; _worker_main(*sys.argv[1:])"
)


def _worker_main(staged: str, expected_sha256: str) -> None:
    request = sys.stdin.buffer.read(MAX_REPLAY_BYTES + 1)
    if sha256(request).hexdigest() != expected_sha256:
        raise ValueError("staged diffuser benchmark request differs from preflight")
    report = _run_diffuser_replay_bytes(request, path=Path(staged), check_only=True)
    sys.stdout.buffer.write(_json_bytes(report))


def _launch_trial(
    staged: Path, trial_directory: Path, timeout: int, *, expected_sha256: str,
) -> tuple[int, float]:
    command = [
        sys.executable, "-I", "-B", "-c", _WORKER_ENTRY,
        str(Path(__file__).resolve().parents[1]), str(staged), expected_sha256,
    ]
    with (
        staged.open("rb") as stdin,
        (trial_directory / "report.json").open("xb") as stdout,
        (trial_directory / "stderr.log").open("xb") as stderr,
    ):
        start = perf_counter()
        process = subprocess.run(command, stdin=stdin, stdout=stdout, stderr=stderr, timeout=timeout)
        return process.returncode, perf_counter() - start


def _fingerprint_trial_file(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not S_ISREG(info.st_mode):
        raise ValueError(f"expected a regular benchmark artifact named {path.name}")
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _wall_time(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("benchmark wall time must be a finite nonnegative number")
    try:
        elapsed = float(value)
    except OverflowError as exc:
        raise ValueError("benchmark wall time must be a finite nonnegative number") from exc
    if not isfinite(elapsed) or elapsed < 0:
        raise ValueError("benchmark wall time must be a finite nonnegative number")
    return elapsed


def _verify_trial_report(
    path: Path, document: dict[str, Any], process_wall: float,
) -> tuple[dict[str, float], dict[str, int], list[dict[str, Any]], dict[str, Any]]:
    process_wall = _wall_time(process_wall)
    limit = _LIMITS["report.json"]
    if not S_ISREG(path.lstat().st_mode):
        raise ValueError("expected a regular benchmark report file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("diffuser benchmark report exceeds its size limit")
    report = _parse_request(raw, label="diffuser benchmark report")
    expected = document["expected"]
    try:
        plan = report["modeled_witness_plan"]
        replay = report["witness_replay"]
        if (
            report["status"] != "DIFFUSER_REPLAY_VERIFIED"
            or replay["status"] != "DIFFUSER_REPLAY_VERIFIED"
            or replay["content_sha256"] != document["content_sha256"]
            or report["result_kind"] != "MODELED_PROBABILITIES_WITH_WITNESS_PLAN"
            or plan["local_sampling_performed"] is not False
            or any(type(report["limits"][key]) is not int or report["limits"][key] != 0
                   for key in ("shots", "max_sampling_batches"))
            or report["provider_job_submitted"] is not False
            or report["quantum_hardware_used"] is not False
        ):
            raise ValueError("worker did not verify the pinned check-only request")
        cells = report["cells"]
        if (
            report["witness_replay_evidence"] != expected
            or _diffuser_replay_circuit_digest(cells) != expected["circuits_sha256"]
            or any(expected[key] != value for key, value in _diffuser_replay_plan_evidence(report, plan).items())
        ):
            raise ValueError("worker circuit or plan evidence differs from the request")
        timing = report["timing_seconds"]
        seconds = _elapsed_seconds(timing)
        worker_wall = _wall_time(report["wall_seconds"])
        if (
            seconds is None or type(timing["sampling"]) not in (int, float) or timing["sampling"] != 0
            or not seconds["compile_export_verify"] <= worker_wall <= process_wall
        ):
            raise ValueError("worker reported invalid aggregate compiler timing")

        preference = _NATIVE_QASM_IMPORTER if document["sampling"].get("prefer_native_qasm", False) else None
        circuits = []
        timings = []
        importers: Counter[str] = Counter()
        for cell in cells:
            for diffuser, record in cell["variants"].items():
                openqasm = record["compilation"]["openqasm"]
                importer = openqasm["importer"]
                if not isinstance(importer, str) or not importer or openqasm.get("preferred_importer") != preference:
                    raise ValueError("worker reported invalid OpenQASM importer evidence")
                importers[importer] += 1
                elapsed = _elapsed_seconds(record["compilation_timing_seconds"])
                if elapsed is None:
                    raise ValueError("worker reported invalid per-circuit compiler timing")
                timings.append(elapsed)
                circuits.append({
                    "placement": cell["placement"], "rounds": cell["rounds"], "diffuser": diffuser,
                    **_timing_circuit_identity(record),
                })
        if len(circuits) != DIFFUSER_VARIANT_COUNT or any(
            not isclose(seconds[phase], fsum(row[phase] for row in timings), rel_tol=1e-12, abs_tol=1e-9)
            for phase in _COMPILATION_PHASES
        ):
            raise ValueError("worker compiler totals differ from the per-circuit timings")
    except (AttributeError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid diffuser benchmark report structure or timing") from exc
    fingerprint = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    return {"process_wall": process_wall, **seconds}, dict(importers), circuits, fingerprint


def _matched_request_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "formula": document["formula"],
        "sampling": {key: value for key, value in document["sampling"].items() if key != "prefer_native_qasm"},
        "witness": document["witness"],
        "expected": {key: value for key, value in document["expected"].items() if key != "circuits_sha256"},
    }


def _benchmark_schedule(pairs: int) -> list[tuple[int, str]]:
    return [
        (pair, side)
        for pair in range(1, pairs + 1)
        for side in (("left", "right") if pair % 2 else ("right", "left"))
    ]


def _timing_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    left, right = ([row["seconds"] for row in trials if row["side"] == side] for side in ("left", "right"))
    deltas = [{key: b[key] - value for key, value in a.items()} for a, b in zip(left, right, strict=True)]
    summary = {}
    for label, series in (("left", left), ("right", right), ("right_minus_left", deltas)):
        stats = {}
        for metric in series[0]:
            values = [row[metric] for row in series]
            stats[metric] = {"min": min(values), "median": median(values), "max": max(values)}
        summary[label] = stats
    return summary


def run_diffuser_benchmark(
    left: str | Path, right: str | Path, *, output_directory: str | Path, pairs: int = 4, timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run alternating matched check-only pairs and retain the verified local timing evidence."""
    _bounded_int(pairs, 1, MAX_BENCHMARK_PAIRS, "pairs")
    _bounded_int(timeout_seconds, 1, MAX_REGRESSION_TIMEOUT_SECONDS, "timeout_seconds")
    sources = {"left": Path(left).resolve(), "right": Path(right).resolve()}
    requests = {side: _read_request(source) for side, source in sources.items()}
    documents = {side: _decode_diffuser_replay(request)[0] for side, request in requests.items()}
    first, second = (_matched_request_fields(documents[side]) for side in ("left", "right"))
    changed = [key for key, value in first.items() if value != second[key]]
    if changed:
        raise ValueError(f"diffuser benchmark requests differ in {', '.join(changed)}")

    output = Path(output_directory).resolve()
    output.mkdir()
    staged = {}
    inputs = {}
    for side, request in requests.items():
        staged[side] = output / f"{side}-request.json"
        with staged[side].open("xb") as stream:
            stream.write(request)
        inputs[side] = {
            "source": str(sources[side]), "staged_file": staged[side].name,
            "request_sha256": sha256(request).hexdigest(),
            "content_sha256": documents[side]["content_sha256"],
            "prefer_native_qasm": documents[side]["sampling"].get("prefer_native_qasm", False),
            "expected": documents[side]["expected"],
        }

    schedule = _benchmark_schedule(pairs)
    environment = {
        "platform": platform.platform(), "processor": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(), "software_versions": _software_versions(),
    }
    trials = []
    reference = None
    for index, (pair, side) in enumerate(schedule, start=1):
        directory = output / f"{index:02d}-{side}"
        row = {"pair": pair, "side": side, "artifacts_directory": directory.name}
        trials.append(row)
        report_path = directory / "report.json"
        report_fingerprint = None
        try:
            directory.mkdir()
            exit_code, wall = _launch_trial(staged[side], directory, timeout_seconds,
                                            expected_sha256=inputs[side]["request_sha256"])
            row["worker_exit_code"] = exit_code
            if exit_code:
                raise ValueError("check-only worker exited nonzero")
            seconds, importers, circuits, report_fingerprint = _verify_trial_report(report_path, documents[side], wall)
            if reference is not None and reference != circuits:
                raise ValueError("compiled outputs differ beyond importer evidence")
            reference = circuits
            row.update(status="VERIFIED", seconds=seconds, actual_importers=importers)
        except subprocess.TimeoutExpired:
            row.update(status="TIMED_OUT", error="check-only worker timed out")
        except (OSError, ValueError) as exc:
            row.update(status="FAILED", error=str(exc))
        row["artifacts"] = {
            "report.json": report_fingerprint or _fingerprint_trial_file(report_path),
            "stderr.log": _fingerprint_trial_file(directory / "stderr.log"),
        }
        if row["status"] != "VERIFIED":
            break

    complete = len(trials) == len(schedule) and trials[-1]["status"] == "VERIFIED"
    summary = {
        "schema_version": 2,
        "status": "BENCHMARK_COMPLETED" if complete else "BENCHMARK_FAILED",
        "output_directory": str(output), "requested_pairs": pairs, "timeout_seconds": timeout_seconds,
        "verified_trials": sum(row["status"] == "VERIFIED" for row in trials),
        "environment": environment, "inputs": inputs,
        "compiled_output_sha256": _diffuser_replay_digest(reference) if complete else None,
        "local_sampling_performed": False, "provider_job_submitted": False, "quantum_hardware_used": False,
        "trials": trials, "timing_summary": _timing_summary(trials) if complete else None,
    }
    with (output / "summary.json").open("xb") as stream:
        stream.write(_json_bytes(summary))
    return summary

