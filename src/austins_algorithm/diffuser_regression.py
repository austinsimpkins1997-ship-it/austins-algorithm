"""Run one verified diffuser baseline under an explicit local workload limit."""

from __future__ import annotations

import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from .constraint_diffuser_comparison import DIFFUSER_PLAN_PROBABILITY_ROUNDOFF, DIFFUSER_VARIANT_COUNT
from .constraint_replay import MAX_REPLAY_BYTES
from .core import _bounded_int
from .diffuser_receipt import _VerifiedDiffuserBundle, _read_verified_diffuser_receipt
from .diffuser_receipt_comparison import _compare_verified_diffuser_bundles
from .diffuser_replay import _run_diffuser_replay_bytes
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS
from .replay_receipt import _json_bytes
from .replay_regression import MAX_REGRESSION_TIMEOUT_SECONDS, _read_manifest, _run_worker
from .witness_budget import MAX_WITNESS_BATCHES_PER_COVER


MAX_DIFFUSER_REGRESSION_SHOTS = (
    DIFFUSER_VARIANT_COUNT * MAX_WITNESS_BATCHES_PER_COVER * MAX_CONSTRAINT_SNAPSHOT_SHOTS
)
_WORKER_ENTRY = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from austins_algorithm.diffuser_regression import _worker_main; _worker_main(sys.argv[1], sys.argv[2])"
)
_ROUNDOFF_WORKER_ENTRY = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from austins_algorithm.diffuser_regression import _worker_main; _worker_main(*sys.argv[1:])"
)


def _worker_main(
    directory: str, expected_sha256: str, baseline_directory: str | None = None, expected_report_sha256: str | None = None,
) -> None:
    raw = sys.stdin.buffer.read(MAX_REPLAY_BYTES + 1)
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("staged diffuser request differs from the verified baseline request")
    options = {}
    if baseline_directory is not None or expected_report_sha256 is not None:
        if baseline_directory is None or expected_report_sha256 is None:
            raise ValueError("diffuser roundoff worker requires the verified baseline directory and report SHA-256")
        receipt, report, baseline_request = _read_verified_diffuser_receipt(Path(baseline_directory))
        if receipt["artifacts"]["report.json"]["sha256"] != expected_report_sha256 or baseline_request != raw:
            raise ValueError("diffuser roundoff worker baseline differs from the preflight report or request")
        options["_baseline_report"] = report
    _run_diffuser_replay_bytes(raw, path=Path(directory).parent / "request.json", receipt_directory=directory, **options)


def _preflight_diffuser_regression(
    baseline_path: Path, limit: int, summary: dict[str, Any],
) -> _VerifiedDiffuserBundle | None:
    try:
        baseline = _read_verified_diffuser_receipt(baseline_path)
    except (OSError, ValueError) as exc:
        summary.update(status="REGRESSION_PREFLIGHT_FAILED", reason="INVALID_BASELINE", error=str(exc))
        return None

    receipt, _, _ = baseline
    summary["baseline_artifacts"] = receipt["artifacts"]
    summary["planned_workload"] = {
        key: value for key, value in receipt["workload"].items() if key.startswith("planned_")
    }
    if summary["planned_workload"]["planned_shots"] > limit:
        summary.update(status="REGRESSION_PREFLIGHT_FAILED", reason="SHOT_LIMIT_EXCEEDED")
        return None
    return baseline


def _run_verified_diffuser_regression(
    baseline: _VerifiedDiffuserBundle, output: Path, timeout: int, summary: dict[str, Any], *,
    probability_roundoff: bool = False,
) -> None:
    receipt, _, request = baseline
    staged = output / "request.json"
    replay_path = output / "receipt"
    worker_options: dict[str, Any] = {"worker_entry": _WORKER_ENTRY}
    if probability_roundoff:
        worker_options = {
            "worker_entry": _ROUNDOFF_WORKER_ENTRY,
            "worker_arguments": (receipt["receipt_directory"], receipt["artifacts"]["report.json"]["sha256"]),
        }
    try:
        with staged.open("xb") as stream:
            stream.write(request)
        code = _run_worker(
            staged, replay_path, output / "stderr.log", timeout,
            expected_sha256=receipt["artifacts"]["request.json"]["sha256"], **worker_options,
        )
    except subprocess.TimeoutExpired:
        summary.update(status="REGRESSION_TIMED_OUT", reason="WORKER_TIMEOUT")
        return
    except OSError as exc:
        summary.update(status="REGRESSION_REPLAY_FAILED", reason="WORKER_IO_ERROR", error=str(exc))
        return

    summary["worker_exit_code"] = code
    if code:
        summary.update(status="REGRESSION_REPLAY_FAILED", reason="WORKER_EXITED_NONZERO")
        return
    try:
        replay = _read_verified_diffuser_receipt(replay_path)
        if not probability_roundoff and replay[2] != request:
            raise ValueError("diffuser replay receipt did not record the preflight request bytes")
        compare_options = {"probability_roundoff": True} if probability_roundoff else {}
        comparison = _compare_verified_diffuser_bundles(baseline, replay, **compare_options)
    except (OSError, ValueError) as exc:
        summary.update(status="REGRESSION_VERIFICATION_FAILED", reason="RECEIPT_CHECK_FAILED", error=str(exc))
        return

    summary["comparison"] = comparison
    if comparison["status"] == "DIFFUSER_RECEIPTS_MATCH":
        summary["status"] = "REGRESSION_MATCHED"
    else:
        summary.update(status="REGRESSION_DIFFERED", reason="RECORDED_RUN_DIFFERS")


def _case_summary(baseline: Path, limit: int, timeout: int) -> dict[str, Any]:
    return {
        "baseline": str(baseline), "max_sampling_shots": limit, "timeout_seconds": timeout,
        "status": "READY", "reason": None, "baseline_artifacts": None, "planned_workload": None,
        "worker_exit_code": None, "comparison": None,
    }


def _write_summary(output: Path, summary: dict[str, Any]) -> None:
    with (output / "summary.json").open("xb") as stream:
        stream.write(_json_bytes(summary))


def run_diffuser_regression(
    baseline: str | Path, *, output_directory: str | Path, max_sampling_shots: int, timeout_seconds: int,
    probability_roundoff: bool = False,
) -> dict[str, Any]:
    """Verify and pin the baseline, run one checked worker, and save the observed comparison."""
    if not isinstance(probability_roundoff, bool):
        raise ValueError("probability_roundoff must be a boolean")
    limit = _bounded_int(max_sampling_shots, 1, MAX_DIFFUSER_REGRESSION_SHOTS, "max_sampling_shots")
    timeout = _bounded_int(timeout_seconds, 1, MAX_REGRESSION_TIMEOUT_SECONDS, "timeout_seconds")
    baseline_path = Path(baseline).resolve()
    output = Path(output_directory).resolve()
    output.mkdir()
    summary = {"schema_version": 1, "output_directory": str(output), **_case_summary(baseline_path, limit, timeout)}
    if probability_roundoff:
        summary["probability_roundoff"] = {"absolute_tolerance": DIFFUSER_PLAN_PROBABILITY_ROUNDOFF}
    verified = _preflight_diffuser_regression(baseline_path, limit, summary)
    if verified is not None:
        options = {"probability_roundoff": True} if probability_roundoff else {}
        _run_verified_diffuser_regression(verified, output, timeout, summary, **options)
    _write_summary(output, summary)
    return summary


def run_diffuser_regression_suite(
    manifest: str | Path, *, output_directory: str | Path, probability_roundoff: bool = False,
) -> dict[str, Any]:
    """Preflight every case and the combined plan, then execute the pinned requests in manifest order."""
    if not isinstance(probability_roundoff, bool):
        raise ValueError("probability_roundoff must be a boolean")
    raw, cases, total_limit = _read_manifest(Path(manifest).resolve(), max_case_shots=MAX_DIFFUSER_REGRESSION_SHOTS)
    output = Path(output_directory).resolve()
    output.mkdir()
    with (output / "manifest.json").open("xb") as stream:
        stream.write(raw)

    rows = []
    verified = {}
    for case in cases:
        row = {"id": case.id, **_case_summary(case.baseline, case.max_sampling_shots, case.timeout_seconds)}
        rows.append(row)
        baseline = _preflight_diffuser_regression(case.baseline, case.max_sampling_shots, row)
        if baseline is not None:
            verified[case.id] = baseline

    planned = [row["planned_workload"]["planned_shots"] for row in rows if row["planned_workload"] is not None]
    planned_total = sum(planned) if len(planned) == len(cases) else None
    reason = None
    if len(verified) != len(cases):
        reason = "CASE_PREFLIGHT_FAILED"
    elif planned_total is not None and planned_total > total_limit:
        reason = "TOTAL_SHOT_LIMIT_EXCEEDED"
    if reason is not None:
        status = "REGRESSION_PREFLIGHT_FAILED"
        for row in rows:
            if row["status"] == "READY":
                row.update(status="NOT_RUN_PREFLIGHT_FAILED", reason=reason)
    else:
        options = {"probability_roundoff": True} if probability_roundoff else {}
        for index, (case, row) in enumerate(zip(cases, rows, strict=True), start=1):
            directory = output / f"{index:02d}"
            try:
                directory.mkdir()
            except OSError as exc:
                row.update(status="REGRESSION_REPLAY_FAILED", reason="CASE_DIRECTORY_ERROR", error=str(exc))
                continue
            row["artifacts_directory"] = directory.name
            _run_verified_diffuser_regression(verified.pop(case.id), directory, case.timeout_seconds, row, **options)
        status = "REGRESSION_MATCHED" if all(row["status"] == "REGRESSION_MATCHED" for row in rows) else "REGRESSION_FAILED"

    observed = [row["comparison"]["right"]["workload"] for row in rows if row["comparison"] is not None]
    summary = {
        "schema_version": 1, "status": status, "reason": reason, "output_directory": str(output),
        "max_total_sampling_shots": total_limit, "planned_max_sampling_shots": planned_total,
        "max_worker_seconds": sum(case.timeout_seconds for case in cases), "verified_replays": len(observed),
        "verified_completed_sampling_shots": sum(workload["completed_shots"] for workload in observed),
        "cases": rows,
    }
    if probability_roundoff:
        summary["probability_roundoff"] = {"absolute_tolerance": DIFFUSER_PLAN_PROBABILITY_ROUNDOFF}
    _write_summary(output, summary)
    return summary
