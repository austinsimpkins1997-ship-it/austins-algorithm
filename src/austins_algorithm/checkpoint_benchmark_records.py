"""Read bounded, local checkpoint benchmark records without following their labels."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
from typing import Any

from .constraints import _unique_object
from .snapshot_calibration_checkpoint_store import _regular_entry
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_profile import _sha256


_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_FILES = 8192
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_ROLES = {"baseline", "reference-start", "reference-resume", "fusion"}
_COUNTERS = {"compiles", "restores", "reference_preparations", "fusion_preparations"}
_MEMORY = {"working_set_bytes", "peak_working_set_bytes", "private_usage_bytes"}
_METRICS = {
    "schema_version", "role", "seconds", "counters", "density_preparation_limit", "memory", "memory_scope",
    "versions", "inputs", "error", "provider_job_submitted", "quantum_hardware_used",
}
_HOST = {"python", "platform", "machine", "processor", "thread_settings"}


@dataclass(frozen=True)
class _CheckpointBenchmarkStage:
    record: dict[str, Any]
    directory: Path
    result: dict[str, Any] | None
    metrics: dict[str, Any] | None
    study: dict[str, Any] | None
    study_path: Path
    fingerprints: dict[str, dict[str, Any]]
    observations: dict[str, str]
    content_binding: str = "PARSED_CONTENT_MATCHES_SUMMARY"


@dataclass(frozen=True)
class _CheckpointBenchmarkRecords:
    root: Path
    summary: dict[str, Any]
    summary_fingerprint: dict[str, Any]
    scenario: dict[str, Any]
    scenario_fingerprint: dict[str, Any]
    stages: tuple[_CheckpointBenchmarkStage, ...]
    storage: dict[str, Any]


def _regular_path(path: str | Path, *, directory: bool = False) -> Path:
    # Normalize lexical '..' without resolving a link before inspecting it.
    value = Path(os.path.abspath(path))
    for parent in reversed(value.parents):
        _regular_entry(parent, directory=True)
    _regular_entry(value, directory=directory)
    return value


def _finite_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ValueError("benchmark JSON contains a nonfinite number")
    return value


def _read_json(
    path: str | Path, expected_sha256: str | None = None, max_bytes: int = _MAX_JSON_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_JSON_BYTES:
        raise ValueError("invalid benchmark JSON byte limit")
    if expected_sha256 is not None:
        _sha256(expected_sha256, "expected benchmark file SHA-256")
    source = _regular_path(path)
    with source.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"benchmark JSON exceeds {max_bytes} bytes: {source.name}")
    fingerprint = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    if expected_sha256 is not None and fingerprint["sha256"] != expected_sha256:
        raise ValueError(f"benchmark file does not match its expected SHA-256: {source.name}")
    try:
        document = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=partial(_unique_object, label="benchmark"),
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError) as error:
        raise ValueError(f"invalid benchmark JSON: {source.name}") from error
    if not isinstance(document, dict):
        raise ValueError(f"benchmark JSON must contain an object: {source.name}")
    return document, fingerprint


def _file_fingerprint(path: Path) -> dict[str, Any]:
    _regular_path(path)
    digest, size = sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(min(1024 * 1024, _MAX_JSON_BYTES + 1 - size)):
            size += len(block)
            if size > _MAX_JSON_BYTES:
                raise ValueError(f"benchmark file exceeds {_MAX_JSON_BYTES} bytes: {path.name}")
            digest.update(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _stage_directory(root: Path, index: int, role: str, recorded: Any) -> Path:
    if type(index) is not int or not 1 <= index <= 17 or not isinstance(role, str) or role not in _ROLES:
        raise ValueError("invalid benchmark stage index or role")
    name = f"{index:02d}-{role}"
    if recorded != name:
        raise ValueError("benchmark stage directory differs from its canonical relative name")
    return _regular_path(root / name, directory=True)


def _inventory(root: Path, exclude: tuple[str, ...] = ("summary.json",)) -> dict[str, Any]:
    root = _regular_path(root, directory=True)
    queue, fingerprints, entries = [root], {}, 0
    total = 0
    while queue:
        directory = queue.pop()
        _regular_entry(directory, directory=True)
        with os.scandir(directory) as scanned:
            children = []
            for entry in scanned:
                entries += 1
                if entries > 2 * _MAX_FILES:
                    raise ValueError("benchmark directory entry count exceeds its limit")
                children.append(entry)
        for entry in sorted(children, key=lambda row: row.name):
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                _regular_entry(path, directory=True)
                queue.append(path)
                continue
            _regular_entry(path)
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            if len(fingerprints) >= _MAX_FILES:
                raise ValueError("benchmark file count exceeds its limit")
            fingerprint = _file_fingerprint(path)
            total += fingerprint["bytes"]
            if total > _MAX_TOTAL_BYTES:
                raise ValueError("benchmark total bytes exceed its limit")
            fingerprints[relative] = fingerprint
    return {"files": len(fingerprints), "bytes": total, "fingerprints": dict(sorted(fingerprints.items())),
            "excludes": list(exclude)}


def _integer(value: Any, label: str, *, maximum: int = 2**63 - 1) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"invalid benchmark {label}")


def _seconds(value: Any, label: str) -> None:
    try:
        valid = type(value) in (int, float) and isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid benchmark {label}")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid benchmark {label}")


def _fingerprint(value: Any, label: str, *, path: bool = False) -> None:
    if not isinstance(value, dict) or value.keys() != ({"path", "bytes", "sha256"} if path else {"bytes", "sha256"}):
        raise ValueError(f"invalid benchmark {label} fingerprint")
    _integer(value["bytes"], f"{label} bytes", maximum=_MAX_JSON_BYTES)
    _sha256(value["sha256"], f"benchmark {label} SHA-256")
    if path:
        _text(value["path"], f"{label} recorded path")


def _verify_metrics(metrics: dict[str, Any], role: str) -> None:
    if not isinstance(metrics, dict) or not _METRICS <= metrics.keys() <= _METRICS | _HOST | {"seconds_scope"}:
        raise ValueError("invalid benchmark metrics fields")
    if type(metrics["schema_version"]) is not int or metrics["schema_version"] != 1 or metrics["role"] != role:
        raise ValueError("benchmark metrics schema or role differs")
    if metrics["provider_job_submitted"] is not False or metrics["quantum_hardware_used"] is not False:
        raise ValueError("benchmark metrics must describe local evidence")
    _seconds(metrics["seconds"], "worker seconds")
    _text(metrics["memory_scope"], "memory scope")
    for field in _HOST - {"thread_settings"} | {"seconds_scope"}:
        if field in metrics:
            if field == "processor" and metrics[field] is None:
                continue
            _text(metrics[field], field)
    counters = metrics["counters"]
    if not isinstance(counters, dict) or counters.keys() != _COUNTERS:
        raise ValueError("invalid benchmark counters")
    for name, value in counters.items():
        _integer(value, name)
    _integer(metrics["density_preparation_limit"], "density preparation limit")
    memory = metrics["memory"]
    if not isinstance(memory, dict) or memory.keys() != {"before", "after"}:
        raise ValueError("invalid benchmark memory fields")
    for row in memory.values():
        if not isinstance(row, dict) or row.keys() != _MEMORY | {"reason"}:
            raise ValueError("invalid benchmark memory observation")
        if all(row[field] is None for field in _MEMORY):
            _text(row["reason"], "unavailable memory reason")
        else:
            if row["reason"] is not None:
                raise ValueError("available benchmark memory cannot have an unavailable reason")
            for field in _MEMORY:
                _integer(row[field], field)
            if row["peak_working_set_bytes"] < row["working_set_bytes"]:
                raise ValueError("benchmark peak working set is below its endpoint")
    versions = metrics["versions"]
    if not isinstance(versions, dict) or not versions:
        raise ValueError("benchmark software versions are missing")
    for name, value in versions.items():
        _text(name, "software name")
        if value is not None:
            _text(value, "software version")
    settings = metrics.get("thread_settings", {})
    if not isinstance(settings, dict) or any(not isinstance(key, str) or not key or (value is not None and not isinstance(value, str))
                                             for key, value in settings.items()):
        raise ValueError("invalid benchmark thread settings")
    inputs = metrics["inputs"]
    if not isinstance(inputs, dict) or inputs.keys() != {"scenario_sha256", "constraint", "earlier", "later", "source_files"}:
        raise ValueError("invalid benchmark metrics inputs")
    _sha256(inputs["scenario_sha256"], "benchmark scenario SHA-256")
    constraint = inputs["constraint"]
    if not isinstance(constraint, list) or len(constraint) != 2:
        raise ValueError("invalid benchmark constraint label")
    _text(constraint[0], "constraint path label")
    _sha256(constraint[1], "benchmark constraint SHA-256")
    for label in ("earlier", "later"):
        archive = inputs[label]
        if not isinstance(archive, dict) or archive.keys() != {"configuration", "properties", "configuration_sha256", "properties_sha256"}:
            raise ValueError("invalid benchmark archive labels")
        for field in ("configuration", "properties"):
            _text(archive[field], "archive path label")
            _sha256(archive[field + "_sha256"], "benchmark archive SHA-256")
    sources = inputs["source_files"]
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 128:
        raise ValueError("invalid benchmark source identity inventory")
    for name, fingerprint in sources.items():
        _text(name, "source identity label")
        _fingerprint(fingerprint, "source", path=True)
    if metrics["error"] is not None:
        error = metrics["error"]
        if not isinstance(error, dict) or error.keys() != {"error_type", "message"}:
            raise ValueError("invalid benchmark worker error")
        _text(error["error_type"], "worker error type")
        if not isinstance(error["message"], str):
            raise ValueError("invalid benchmark worker error message")


def _metrics_identity(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "versions": metrics["versions"],
        "source_files": {name: {key: row[key] for key in ("bytes", "sha256")}
                         for name, row in metrics["inputs"]["source_files"].items()},
        **{name: metrics.get(name) for name in _HOST},
    }


def _read_checkpoint_benchmark_records(
    directory: str | Path, *, expected_summary_sha256: str,
) -> _CheckpointBenchmarkRecords:
    root = _regular_path(directory, directory=True)
    summary, summary_pin = _read_json(root / "summary.json", expected_summary_sha256)
    scenario, scenario_pin = _read_json(root / "scenario.json", max_bytes=1024 * 1024)
    recorded = summary.get("stages")
    if not isinstance(recorded, list) or not 0 <= len(recorded) <= 17:
        raise ValueError("invalid benchmark stage count")
    stages, covered = [], {"scenario.json": scenario_pin}
    for index, record in enumerate(recorded, start=1):
        fields = {"role", "directory", "status", "returncode", "result", "metrics", "cancellation_status", "wall_seconds"}
        if not isinstance(record, dict) or not fields <= record.keys() <= fields | {"error"}:
            raise ValueError("invalid benchmark stage fields")
        if "error" in record:
            error = record["error"]
            if not isinstance(error, dict) or error.keys() != {"error_type", "message"} or not all(
                isinstance(value, str) for value in error.values()
            ):
                raise ValueError("invalid benchmark stage error")
        role = record["role"]
        stage = _stage_directory(root, index, role, record["directory"])
        if not isinstance(record["status"], str) or record["status"] not in {"COMPLETED", "FAILED", "TIMED_OUT"}:
            raise ValueError("invalid benchmark worker status")
        if record["returncode"] is not None and (type(record["returncode"]) is not int or not -(2**31) <= record["returncode"] < 2**32):
            raise ValueError("invalid benchmark worker return code")
        if record["cancellation_status"] not in (None, "UNKNOWN"):
            raise ValueError("invalid benchmark worker cancellation observation")
        _seconds(record["wall_seconds"], "parent wall seconds")
        complete = record["status"] == "COMPLETED"
        data, fingerprints, observations = {}, {}, {}
        for name in ("result.json", "metrics.json", "study.json", "stdout.log", "stderr.log"):
            path = stage / name
            if not path.exists() and not path.is_symlink():
                observations[name] = "MISSING"
                if complete and name in {"stdout.log", "stderr.log"}:
                    raise ValueError(f"completed benchmark stage is missing {name}")
                data[name] = None
                continue
            observations[name] = "OBSERVED"
            if name.endswith(".json"):
                data[name], fingerprints[name] = _read_json(path)
            else:
                fingerprints[name] = _file_fingerprint(path)
            covered[f"{stage.name}/{name}"] = fingerprints[name]
        for name in ("result", "metrics"):
            current, embedded = data[name + ".json"], record[name]
            if embedded is not None and not isinstance(embedded, dict):
                raise ValueError(f"invalid benchmark embedded {name}")
            if current is not None and _json_bytes(current) != _json_bytes(embedded):
                raise ValueError(f"benchmark {name} parsed content differs from summary")
        if data["metrics.json"] is not None:
            metrics = data["metrics.json"]
            _verify_metrics(metrics, role)
        stages.append(_CheckpointBenchmarkStage(
            record, stage, data["result.json"], data["metrics.json"], data["study.json"], stage / "study.json",
            fingerprints, observations, ("PARSED_CONTENT_MATCHES_SUMMARY" if any(
                data[name + ".json"] is not None for name in ("result", "metrics")
            ) else "NOT_OBSERVED"),
        ))
    storage = _inventory(root)
    if any(storage["fingerprints"].get(name) != fingerprint for name, fingerprint in covered.items()):
        raise ValueError("benchmark file changed during record verification")
    if _file_fingerprint(root / "summary.json") != summary_pin:
        raise ValueError("benchmark summary changed during record verification")
    return _CheckpointBenchmarkRecords(root, summary, summary_pin, scenario, scenario_pin, tuple(stages), storage)
