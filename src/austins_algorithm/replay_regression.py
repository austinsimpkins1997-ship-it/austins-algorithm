"""Run a named suite of offline replay receipts within explicit local limits."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from re import fullmatch
from typing import Any

from .constraint_replay import MAX_REPLAY_BYTES, _replay_sampling_shot_bound, _run_constraint_replay_bytes
from .core import _bounded_int
from .ibm_models import MAX_CONSTRAINT_SNAPSHOT_SHOTS
from .replay_receipt_comparison import _compare_verified_bundles, _load_object, _read_comparison_bundle
from .witness_budget import DEFAULT_COVER_EVALUATION_SEEDS, MAX_WITNESS_BATCHES_PER_COVER


MAX_REGRESSION_CASES = 8
MAX_REGRESSION_TIMEOUT_SECONDS = 300
MAX_REGRESSION_CASE_SHOTS = MAX_CONSTRAINT_SNAPSHOT_SHOTS * (
    len(DEFAULT_COVER_EVALUATION_SEEDS) + 2 * MAX_WITNESS_BATCHES_PER_COVER
)
MAX_REGRESSION_MANIFEST_BYTES = 32 * 1024
_WORKER_ENTRY = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from austins_algorithm.replay_regression import _worker_main; _worker_main(sys.argv[1], sys.argv[2])"
)


@dataclass(frozen=True)
class _Case:
    id: str
    baseline: Path
    max_sampling_shots: int
    timeout_seconds: int


def _read_manifest(
    path: Path, *, max_case_shots: int = MAX_REGRESSION_CASE_SHOTS,
) -> tuple[bytes, list[_Case], int]:
    if not path.is_file():
        raise ValueError(f"expected a regular regression manifest: {path}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REGRESSION_MANIFEST_BYTES + 1)
    if len(raw) > MAX_REGRESSION_MANIFEST_BYTES:
        raise ValueError("regression manifest exceeds its size limit")
    manifest = _load_object(raw, "regression manifest")
    if set(manifest) != {"schema_version", "max_total_sampling_shots", "cases"}:
        raise ValueError("regression manifest requires schema_version, max_total_sampling_shots and cases")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported regression manifest schema_version")
    limit = _bounded_int(
        manifest["max_total_sampling_shots"], 1, MAX_REGRESSION_CASES * max_case_shots,
        "max_total_sampling_shots",
    )
    entries = manifest["cases"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_REGRESSION_CASES:
        raise ValueError(f"regression manifest requires 1 to {MAX_REGRESSION_CASES} cases")
    ids = set()
    cases = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "baseline", "max_sampling_shots", "timeout_seconds"}:
            raise ValueError("each regression case requires id, baseline, max_sampling_shots and timeout_seconds")
        identifier, baseline = entry["id"], entry["baseline"]
        if not isinstance(identifier, str) or fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", identifier) is None:
            raise ValueError("regression case id requires 1 to 48 ASCII letters, digits, underscores or hyphens")
        if identifier.casefold() in ids:
            raise ValueError(f"duplicate regression case id: {identifier}")
        ids.add(identifier.casefold())
        if not isinstance(baseline, str) or not baseline.strip() or "\0" in baseline:
            raise ValueError(f"regression case {identifier} requires a baseline directory")
        cases.append(_Case(
            identifier, path.parent / baseline,
            _bounded_int(entry["max_sampling_shots"], 1, max_case_shots, "max_sampling_shots"),
            _bounded_int(entry["timeout_seconds"], 1, MAX_REGRESSION_TIMEOUT_SECONDS, "timeout_seconds"),
        ))
    return raw, cases, limit


def _execution_record(raw: bytes, label: str) -> dict[str, Any]:
    report = _load_object(raw, label)
    snapshot, witness = report.get("snapshot"), report.get("witness_budget")
    if (
        report.get("experiment") != "CONSTRAINT_PHASE_COVER_COMPARISON"
        or report.get("execution_mode") not in ("SIMULATOR", "COMPILE_ONLY")
        or report.get("provider_job_submitted") is not False
        or report.get("quantum_hardware_used") is not False
        or not isinstance(snapshot, dict) or not isinstance(witness, dict)
    ):
        raise ValueError(f"{label} must record an offline constraint replay")
    if any(not isinstance(record.get("status"), str) for record in (report, snapshot, witness)):
        raise ValueError(f"{label} must record compilation, snapshot and witness statuses")
    return {
        "execution_mode": report["execution_mode"],
        "compilation_status": report["status"],
        "snapshot_status": snapshot["status"],
        "witness_status": witness["status"],
    }


def _worker_main(directory: str, expected_sha256: str) -> None:
    raw = sys.stdin.buffer.read(MAX_REPLAY_BYTES + 1)
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("staged request differs from the verified baseline request")
    _run_constraint_replay_bytes(raw, receipt_directory=directory)


def _run_worker(
    request: Path, directory: Path, log: Path, timeout_seconds: int, *, expected_sha256: str,
    worker_entry: str | None = None, worker_arguments: Sequence[str] = (),
) -> int:
    command = [
        sys.executable, "-B", "-c", _WORKER_ENTRY if worker_entry is None else worker_entry,
        str(Path(__file__).resolve().parents[1]),
        str(directory), expected_sha256, *worker_arguments,
    ]
    # File-backed stdin lets the process timeout apply even if startup never reads the request.
    with request.open("rb") as stdin, log.open("xb") as stderr:
        return subprocess.run(
            command, stdin=stdin, stdout=subprocess.DEVNULL, stderr=stderr, timeout=timeout_seconds,
        ).returncode


def _run_case(
    case: _Case, request: bytes, baseline_receipt: dict[str, Any],
    directory: Path, row: dict[str, Any], *, probability_roundoff: bool = False,
) -> None:
    staged = directory / "request.json"
    receipt = directory / "receipt"
    log = directory / "stderr.log"
    try:
        directory.mkdir()
        row["artifacts_directory"] = directory.name
        with staged.open("xb") as stream:
            stream.write(request)
        code = _run_worker(
            staged, receipt, log, case.timeout_seconds,
            expected_sha256=baseline_receipt["artifacts"]["request.json"]["sha256"],
        )
    except subprocess.TimeoutExpired:
        row["status"] = "TIMED_OUT"
        return
    except OSError as exc:
        row.update(status="REPLAY_FAILED", error=str(exc))
        return
    row["worker_exit_code"] = code
    if code:
        row["status"] = "REPLAY_FAILED"
        return
    try:
        baseline = _read_comparison_bundle(case.baseline)
        if baseline[0] != baseline_receipt:
            raise ValueError("baseline receipt changed after preflight")
        replay = _read_comparison_bundle(receipt)
        if replay[1]["request.json"] != request:
            raise ValueError("replay receipt did not record the preflight request bytes")
        row["replay_execution"] = _execution_record(replay[1]["report.json"], "replay report")
        row["comparison"] = _compare_verified_bundles(
            case.baseline, receipt, baseline, replay, probability_roundoff=probability_roundoff,
        )
    except ValueError as exc:
        row.update(status="COMPARISON_FAILED", error=str(exc))
        return
    row["status"] = "MATCHED" if row["comparison"]["status"] == "REPLAY_RECEIPTS_MATCH" else "DIFFERED"


def run_replay_regression(
    manifest: str | Path, *, output_directory: str | Path, probability_roundoff: bool = False,
) -> dict[str, Any]:
    """Preflight all budgets, run each case once, and save a summary of the observed outcomes."""
    if type(probability_roundoff) is not bool:
        raise TypeError("probability_roundoff must be a boolean")
    manifest = Path(manifest).resolve()
    raw, cases, total_limit = _read_manifest(manifest)
    output = Path(output_directory).resolve()
    output.mkdir()
    with (output / "manifest.json").open("xb") as stream:
        stream.write(raw)

    rows = []
    verified = {}
    planned = []
    for case in cases:
        row = {
            "id": case.id, "baseline": str(case.baseline), "status": "READY",
            "max_sampling_shots": case.max_sampling_shots, "timeout_seconds": case.timeout_seconds,
            "planned_max_sampling_shots": None,
        }
        rows.append(row)
        try:
            receipt, payloads = _read_comparison_bundle(case.baseline)
            request = payloads["request.json"]
            bound = _replay_sampling_shot_bound(request)
            row["baseline_execution"] = _execution_record(payloads["report.json"], "baseline report")
        except (ValueError, TypeError) as exc:
            row.update(status="INVALID_BASELINE", error=str(exc))
            continue
        row["baseline_artifacts"] = receipt["artifacts"]
        row["planned_max_sampling_shots"] = bound
        planned.append(bound)
        if bound > case.max_sampling_shots:
            row["status"] = "CASE_SHOT_LIMIT_EXCEEDED"
        else:
            verified[case.id] = (request, receipt)

    planned_total = sum(planned) if len(planned) == len(cases) else None
    preflight_error = None
    if len(verified) != len(cases):
        preflight_error = "CASE_PREFLIGHT_FAILED"
    elif planned_total is not None and planned_total > total_limit:
        preflight_error = "TOTAL_SHOT_LIMIT_EXCEEDED"
    if preflight_error is not None:
        status = "REGRESSION_PREFLIGHT_FAILED"
        for row in rows:
            if row["status"] == "READY":
                row["status"] = "NOT_RUN_PREFLIGHT_FAILED"
    else:
        for index, (case, row) in enumerate(zip(cases, rows), start=1):
            request, receipt = verified[case.id]
            _run_case(case, request, receipt, output / f"{index:02d}", row, probability_roundoff=probability_roundoff)
        status = "REGRESSION_MATCHED" if all(row["status"] == "MATCHED" for row in rows) else "REGRESSION_FAILED"

    summary = {
        "schema_version": 1, "status": status, "preflight_error": preflight_error, "output_directory": str(output),
        "max_total_sampling_shots": total_limit, "planned_max_sampling_shots": planned_total,
        "max_worker_seconds": sum(case.timeout_seconds for case in cases),
        "cases": rows,
    }
    if probability_roundoff:
        summary["probability_roundoff"] = True
    with (output / "summary.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return summary

