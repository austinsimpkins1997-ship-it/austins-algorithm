"""Index and verify the published file evidence of a Manila smoke run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .constraints import _unique_object
from .replay_regression import MAX_REGRESSION_CASES


_BUNDLES = {
    "environment": ("manila-smoke-environment", "manila-smoke-environment"),
    "phase-cover": ("manila-smoke-results", "manila-smoke-evidence"),
    "diffuser": ("manila-diffuser-smoke-results", "manila-diffuser-smoke-evidence"),
    "diffuser-native": ("manila-diffuser-native-smoke-results", "manila-diffuser-native-smoke-evidence"),
    "placement": ("manila-placement-smoke-results", "manila-placement-smoke-evidence"),
}
_STEPS = ("checkout", "setup_uv", "venv", "build", "install", "capture", "regression", "placement")
_OUTCOMES = ("success", "failure", "cancelled", "skipped")
_RUN_FIELDS = {
    "run_id": ("GITHUB_RUN_ID", r"[0-9]{1,20}"),
    "run_attempt": ("GITHUB_RUN_ATTEMPT", r"[0-9]{1,8}"),
    "trigger_commit": ("GITHUB_SHA", r"[0-9a-fA-F]{40}"),
    "workflow_commit": ("GITHUB_WORKFLOW_SHA", r"[0-9a-fA-F]{40}"),
}
_ENVIRONMENT_STATUS = {"SMOKE_ENVIRONMENT_CAPTURED", "SMOKE_ENVIRONMENT_PIN_MISMATCH"}
_REGRESSION_STATUS = {"REGRESSION_PREFLIGHT_FAILED", "REGRESSION_MATCHED", "REGRESSION_FAILED"}
_PLACEMENT_STATUS = {
    "verify": {"PLACEMENT_RECEIPT_VERIFIED"},
    "check-only": {"PLACEMENT_REPLAY_IDENTITIES_VERIFIED"},
    "replay": {
        "PLACEMENT_REPLAY_MATCHED", "PLACEMENT_REPLAY_INCOMPLETE", "PLACEMENT_REPLAY_DIFFERED",
        "PLACEMENT_REPLAY_SOFTWARE_CHANGED",
    },
}
_MAX_OBSERVATION_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 32 * 1024 * 1024
_GAP_REASONS = ("UNSAFE_ENTRY", "IO_ERROR", "SIZE_LIMIT", "CHANGED_DURING_READ")
_OBSERVATIONS = ("OBSERVED", "NOT_RECORDED", "EMPTY", "INVALID", "UNAVAILABLE")


def _phase_specs(bundle: str) -> dict[str, tuple[str, str, set[str]]]:
    if bundle == "environment":
        return {"capture": ("capture.exit.txt", "environment.json", _ENVIRONMENT_STATUS)}
    if bundle == "placement":
        return {phase: (f"{phase}.exit.txt", f"{phase}.stdout.json", statuses)
                for phase, statuses in _PLACEMENT_STATUS.items()}
    return {"replay": ("replay.exit.txt", "summary.json", _REGRESSION_STATUS)}


def _evidence_paths(bundle: str) -> tuple[str, ...]:
    if bundle == "environment":
        return ("capture.exit.txt", "capture.stderr.txt", "environment.json")
    if bundle == "placement":
        return tuple(f"{phase}.{suffix}" for phase in _PLACEMENT_STATUS
                     for suffix in ("exit.txt", "stderr.txt", "stdout.json"))
    receipt = ["receipt/receipt.json", "receipt/request.json", "receipt/report.json"]
    if bundle == "phase-cover":
        receipt.append("receipt/selected.qasm")
    case_files = ("request.json", "stderr.log", *receipt)
    cases = (f"{case:02d}/{file}" for case in range(1, MAX_REGRESSION_CASES + 1) for file in case_files)
    return ("manifest.json", "summary.json", "replay.exit.txt", *cases)


def _metadata_value(variable: str, pattern: str) -> str | None:
    value = os.environ.get(variable, "")
    return value if len(value) <= 64 and re.fullmatch(pattern, value) else None


def _outcome(variable: str) -> str | None:
    value = os.environ.get(variable)
    return value if value in _OUTCOMES else None


def _directory(path: str | Path) -> Path:
    try:
        root = Path(path).resolve(strict=True)
        if root.is_dir():
            return root
    except OSError:
        pass
    raise ValueError("expected an accessible evidence root directory")


def _entry_stat(path: Path, *, directory: bool) -> os.stat_result:
    entry = path.lstat()
    reparse = getattr(entry, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    valid = stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISREG(entry.st_mode)
    if reparse or not valid:
        raise ValueError("UNSAFE_ENTRY")
    return entry


def _fingerprint(root: Path, relative: str, *, capture: bool) -> tuple[dict[str, Any], bytes | None]:
    *parents, name = relative.split("/")
    path = root
    for parent in parents:
        path /= parent
        _entry_stat(path, directory=True)
    path /= name
    entry = _entry_stat(path, directory=False)
    if entry.st_size > _MAX_FILE_BYTES:
        raise ValueError("SIZE_LIMIT")
    buffer = bytearray() if capture and entry.st_size <= _MAX_OBSERVATION_BYTES else None
    digest, size = sha256(), 0
    with path.open("rb") as stream:
        current = os.fstat(stream.fileno())
        if not os.path.samestat(entry, current):
            raise ValueError("CHANGED_DURING_READ")
        while block := stream.read(64 * 1024):
            size += len(block)
            if size > entry.st_size:
                raise ValueError("CHANGED_DURING_READ")
            digest.update(block)
            if buffer is not None:
                buffer.extend(block)
        if size != entry.st_size or os.fstat(stream.fileno()).st_mtime_ns != entry.st_mtime_ns:
            raise ValueError("CHANGED_DURING_READ")
    return {"bytes": size, "sha256": digest.hexdigest()}, None if buffer is None else bytes(buffer)


def _phase_value(raw: bytes, statuses: set[str] | None) -> int | str:
    text = raw.decode("utf-8-sig").strip()
    if statuses is None:
        if re.fullmatch(r"-?[0-9]{1,10}", text):
            return int(text)
    else:
        document = json.loads(text, object_pairs_hook=_unique_object)
        value = document.get("status") if isinstance(document, dict) else None
        if isinstance(value, str) and value in statuses:
            return value
    raise ValueError("invalid phase observation")


def _observation(
    relative: str, files: dict[str, Any], payloads: dict[str, bytes | None], gaps: dict[str, str],
    statuses: set[str] | None = None,
) -> tuple[int | str | None, str]:
    if "." in gaps or relative in gaps or (relative in files and payloads[relative] is None):
        return None, "UNAVAILABLE"
    if relative not in files:
        return None, "NOT_RECORDED"
    raw = payloads[relative]
    if not raw.strip():
        return None, "EMPTY"
    try:
        return _phase_value(raw, statuses), "OBSERVED"
    except (ValueError, RecursionError):
        return None, "INVALID"


def _inspect(root: Path, name: str, *, runner_layout: bool) -> tuple[dict[str, Any], bytes | None]:
    directory = root / _BUNDLES[name][0 if runner_layout else 1]
    specs = _phase_specs(name)
    observation_paths = {path for exit_file, status_file, _ in specs.values() for path in (exit_file, status_file)}
    files, payloads, gaps = {}, {}, {}
    state = "OBSERVED"
    try:
        _entry_stat(directory, directory=True)
    except FileNotFoundError:
        state = "MISSING"
    except (OSError, ValueError) as exc:
        state = "UNAVAILABLE"
        gaps["."] = str(exc) if isinstance(exc, ValueError) else "IO_ERROR"
    if state == "OBSERVED":
        for relative in _evidence_paths(name):
            try:
                files[relative], payloads[relative] = _fingerprint(directory, relative, capture=relative in observation_paths)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                gaps[relative] = str(exc) if isinstance(exc, ValueError) else "IO_ERROR"
    phases = {}
    for phase, (exit_file, status_file, statuses) in specs.items():
        code, exit_observation = _observation(exit_file, files, payloads, gaps)
        reported, status_observation = _observation(status_file, files, payloads, gaps, statuses)
        phases[phase] = {
            "exit_code": code, "exit_observation": exit_observation,
            "reported_status": reported, "status_observation": status_observation,
        }
    evidence = {"directory_observation": state, "files": files, "gaps": gaps, "phases": phases}
    return evidence, payloads.get("environment.json")


def write_smoke_run_index(run_directory: str | Path, index_file: str | Path) -> dict[str, Any]:
    """Write an exclusive index of the evidence that is available in a runner directory."""
    root = _directory(run_directory)
    bundles = {}
    for name, (_, artifact) in _BUNDLES.items():
        label = name.upper().replace("-", "_")
        evidence, _ = _inspect(root, name, runner_layout=True)
        bundles[name] = {
            "artifact_name": artifact,
            "upload": {
                "outcome": _outcome(f"MANILA_UPLOAD_{label}"),
                "artifact_id": _metadata_value(f"MANILA_ARTIFACT_{label}", r"[0-9]{1,20}"),
            },
            **evidence,
        }
    result = {
        "schema_version": 1,
        "status": "SMOKE_RUN_INDEX_CREATED",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "run": {name: _metadata_value(variable, pattern) for name, (variable, pattern) in _RUN_FIELDS.items()},
        "steps": {name: _outcome(f"MANILA_STEP_{name.upper()}") for name in _STEPS},
        "bundles": bundles,
    }
    with Path(index_file).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")
    return result


def _fields(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _optional_text(value: Any, pattern: str) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= 64 and re.fullmatch(pattern, value) is not None)


def _validate_bundle(name: str, value: Any) -> None:
    if not _fields(value, {"artifact_name", "upload", "directory_observation", "files", "gaps", "phases"}):
        raise ValueError(f"invalid {name} index entry")
    upload = value["upload"]
    if (
        value["artifact_name"] != _BUNDLES[name][1]
        or not _fields(upload, {"outcome", "artifact_id"})
        or upload["outcome"] not in (*_OUTCOMES, None)
        or not _optional_text(upload["artifact_id"], r"[0-9]{1,20}")
    ):
        raise ValueError(f"invalid {name} upload observation")
    allowed = set(_evidence_paths(name))
    files, gaps = value["files"], value["gaps"]
    if not isinstance(files, dict) or not isinstance(gaps, dict) or set(files) & set(gaps):
        raise ValueError(f"invalid {name} evidence inventory")
    if any(path not in allowed | {"."} or reason not in _GAP_REASONS for path, reason in gaps.items()):
        raise ValueError(f"invalid {name} evidence gap")
    for path, entry in files.items():
        if (
            path not in allowed or not _fields(entry, {"bytes", "sha256"})
            or type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= _MAX_FILE_BYTES
            or entry["sha256"] is None or not _optional_text(entry["sha256"], r"[0-9a-f]{64}")
        ):
            raise ValueError(f"invalid {name} file fingerprint")
    directory = value["directory_observation"]
    if (
        directory not in ("OBSERVED", "MISSING", "UNAVAILABLE")
        or (directory == "MISSING" and (files or gaps))
        or (directory == "UNAVAILABLE" and (files or set(gaps) != {"."}))
        or (directory == "OBSERVED" and "." in gaps)
    ):
        raise ValueError(f"invalid {name} directory observation")
    specs = _phase_specs(name)
    if not _fields(value["phases"], set(specs)):
        raise ValueError(f"invalid {name} phase inventory")
    for phase, (_, _, statuses) in specs.items():
        row = value["phases"][phase]
        if not _fields(row, {"exit_code", "exit_observation", "reported_status", "status_observation"}):
            raise ValueError(f"invalid {name} phase observation")
        code, status = row["exit_code"], row["reported_status"]
        valid_code = type(code) is int and -9_999_999_999 <= code <= 9_999_999_999
        valid_status = isinstance(status, str) and status in statuses
        if (
            row["exit_observation"] not in _OBSERVATIONS or row["status_observation"] not in _OBSERVATIONS
            or (not valid_code if row["exit_observation"] == "OBSERVED" else code is not None)
            or (not valid_status if row["status_observation"] == "OBSERVED" else status is not None)
        ):
            raise ValueError(f"invalid {name} phase value")


def _read_index(index_file: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        file = Path(index_file)
        _entry_stat(file, directory=False)
        with file.open("rb") as stream:
            raw = stream.read(_MAX_OBSERVATION_BYTES + 1)
    except (OSError, ValueError):
        raise ValueError("could not read smoke run index") from None
    if len(raw) > _MAX_OBSERVATION_BYTES:
        raise ValueError("smoke run index exceeds its size limit")
    try:
        record = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise ValueError("invalid smoke run index JSON") from None
    if (
        not _fields(record, {"schema_version", "status", "observed_at_utc", "run", "steps", "bundles"})
        or type(record["schema_version"]) is not int or record["schema_version"] != 1
        or record["status"] != "SMOKE_RUN_INDEX_CREATED"
    ):
        raise ValueError("unsupported smoke run index")
    if (
        not _optional_text(record["observed_at_utc"], r"[0-9T:.+-]{20,40}")
        or record["observed_at_utc"] is None
        or not _fields(record["steps"], set(_STEPS))
        or any(value not in (*_OUTCOMES, None) for value in record["steps"].values())
        or not _fields(record["run"], set(_RUN_FIELDS))
        or not all(_optional_text(record["run"][name], pattern) for name, (_, pattern) in _RUN_FIELDS.items())
        or not _fields(record["bundles"], set(_BUNDLES))
    ):
        raise ValueError("invalid smoke run index metadata")
    try:
        timestamp = datetime.fromisoformat(record["observed_at_utc"])
        if timestamp.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError
    except ValueError:
        raise ValueError("invalid smoke run observation time") from None
    for name, value in record["bundles"].items():
        _validate_bundle(name, value)
    return record, {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}


def _phase_match(recorded: dict[str, Any], observed: dict[str, Any]) -> bool | None:
    unknown = False
    for phase, before in recorded.items():
        after = observed[phase]
        for kind, value_key in (("exit", "exit_code"), ("status", "reported_status")):
            evidence_key = f"{kind}_observation"
            left, right = before[evidence_key], after[evidence_key]
            if "UNAVAILABLE" in (left, right):
                unknown = True
            elif (left, before[value_key]) != (right, after[value_key]):
                return False
    return None if unknown else True


def _verify_bundle(recorded: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    before, after = recorded["files"], observed["files"]
    gaps = observed["gaps"]
    omitted = observed["directory_observation"] == "MISSING" and bool(before)
    unreadable = "." in gaps
    missing = sorted(before.keys() - after.keys() - gaps.keys()) if not (omitted or unreadable) else []
    recorded_gaps = recorded["gaps"]
    extra = sorted(after.keys() - before.keys() - recorded_gaps.keys()) if "." not in recorded_gaps else []
    changed = sorted(path for path in before.keys() & after.keys() if before[path] != after[path])
    count = len(before.keys() & after.keys()) - len(changed)
    incomplete = bool(omitted or gaps or recorded_gaps)
    phase_match = None if omitted else _phase_match(recorded["phases"], observed["phases"])
    if missing or extra or changed or phase_match is False:
        status = "MISMATCH"
    elif omitted:
        status = "NOT_PROVIDED"
    elif incomplete:
        status = "PARTIAL"
    elif count:
        status = "VERIFIED"
    else:
        status = "NO_EVIDENCE_RECORDED"
    return {
        "status": status, "verified_files": count, "missing_files": missing, "changed_files": changed,
        "unexpected_files": extra, "phase_observations_match": phase_match, "observed_gaps": gaps,
    }


@dataclass(frozen=True)
class _VerifiedSmokeRunIndex:
    record: dict[str, Any]
    fingerprint: dict[str, Any]
    verification: dict[str, Any]
    observations: dict[str, Any]
    environment_bytes: bytes | None


def _read_verified_smoke_run_index(
    index_file: str | Path, artifact_directory: str | Path, *, runner_layout: bool,
) -> _VerifiedSmokeRunIndex:
    record, fingerprint = _read_index(index_file)
    root = _directory(artifact_directory)
    bundles, observations = {}, {}
    environment_bytes = None
    for name, entry in record["bundles"].items():
        observed, environment = _inspect(root, name, runner_layout=runner_layout)
        observations[name] = observed
        bundles[name] = _verify_bundle(entry, observed)
        if environment is not None and entry["files"].get("environment.json") == observed["files"]["environment.json"]:
            environment_bytes = environment
    count = sum(entry["verified_files"] for entry in bundles.values())
    statuses = {entry["status"] for entry in bundles.values()}
    status = "SMOKE_RUN_INDEX_VERIFIED"
    if "MISMATCH" in statuses:
        status = "SMOKE_RUN_INDEX_MISMATCH"
    elif not count or statuses & {"NOT_PROVIDED", "PARTIAL"}:
        status = "SMOKE_RUN_INDEX_PARTIALLY_VERIFIED"
    verification = {"status": status, "verified_files": count, "bundles": bundles}
    return _VerifiedSmokeRunIndex(record, fingerprint, verification, observations, environment_bytes)


def verify_smoke_run_index(
    index_file: str | Path, artifact_directory: str | Path, *, runner_layout: bool = False,
) -> dict[str, Any]:
    """Check indexed fingerprints and phase observations against supplied evidence files."""
    return _read_verified_smoke_run_index(index_file, artifact_directory, runner_layout=runner_layout).verification
