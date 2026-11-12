"""Read and compare two saved Manila smoke environment reports offline."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .constraints import _unique_object


_MAX_REPORT_BYTES = 1024 * 1024
_VERSION = r"[A-Za-z0-9][A-Za-z0-9.+!_-]{0,127}"
_NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_DIGEST = r"[0-9a-f]{64}"
_DESCRIPTION = r"[A-Za-z0-9 ._()+,\[\]-]{0,128}"
_ARCHITECTURES = {"amd64", "x86_64", "x86", "i386", "i686", "arm64", "aarch64", "armv7l"}
_RUNNER_EXECUTION = {
    "environment": r"github-hosted|self-hosted",
    "os": r"Windows|Linux|macOS",
    "arch": r"X86|X64|ARM|ARM64",
    "image_os": r"win(?:dows)?[0-9]{2,4}",
    "image_version": r"[0-9]{8}(?:\.[0-9]+){1,3}",
}
_RUNNER_IDENTITY = {
    "trigger_commit": r"[0-9a-f]{40}",
    "workflow_commit": r"[0-9a-f]{40}",
    "run_id": r"[0-9]{1,20}",
    "run_attempt": r"[0-9]{1,8}",
}
_REPORT_FIELDS = {"schema_version", "status", "observed_at_utc", "inputs", "tools", "python", "host", "runner", "dependencies"}


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid {label} fields")
    return value


def _matches(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and len(value) <= 128 and re.fullmatch(pattern, value) is not None


def _fingerprints(inputs: Any) -> None:
    inputs = _object(inputs, {"source_distribution", "requirements"}, "inputs")
    for key, value in inputs.items():
        stamp = _object(value, {"name", "bytes", "sha256"}, f"{key} fingerprint")
        name, size = stamp["name"], stamp["bytes"]
        if (
            not isinstance(name, str) or not 1 <= len(name) <= 255 or not name.isprintable()
            or name in {".", ".."} or any(char in name for char in "/\\:")
            or type(size) is not int or size < 0 or not _matches(stamp["sha256"], _DIGEST)
        ):
            raise ValueError(f"invalid {key} fingerprint")


def _runtime_fields(report: dict[str, Any]) -> None:
    tools = _object(report["tools"], {"uv", "powershell"}, "tools")
    if not all(_matches(value, _VERSION) for value in tools.values()):
        raise ValueError("invalid tool versions")
    python = _object(report["python"], {"implementation", "version", "cache_tag", "compiler", "pointer_bits"}, "Python")
    if (
        not _matches(python["implementation"], _VERSION) or not _matches(python["version"], _VERSION)
        or (python["cache_tag"] is not None and not _matches(python["cache_tag"], _VERSION))
        or not _matches(python["compiler"], _DESCRIPTION)
        or type(python["pointer_bits"]) is not int or python["pointer_bits"] not in (32, 64)
    ):
        raise ValueError("invalid Python metadata")
    host = _object(report["host"], {"os", "architecture", "release", "windows_build", "logical_cpu_count"}, "host")
    architecture = host["architecture"]
    if (
        not _matches(host["os"], _DESCRIPTION) or not _matches(host["release"], _DESCRIPTION)
        or (architecture is not None and (not isinstance(architecture, str) or architecture.lower() not in _ARCHITECTURES))
        or any(host[key] is not None and (type(host[key]) is not int or host[key] < 1)
               for key in ("windows_build", "logical_cpu_count"))
    ):
        raise ValueError("invalid host metadata")
    rules = {**_RUNNER_EXECUTION, **_RUNNER_IDENTITY}
    runner = _object(report["runner"], set(rules), "runner")
    if any(value is not None and not (
        isinstance(value, str) and len(value) <= 128 and re.fullmatch(rules[key], value, re.IGNORECASE)
    ) for key, value in runner.items()):
        raise ValueError("invalid runner metadata")


def _dependency_fields(report: dict[str, Any]) -> None:
    deps = _object(report["dependencies"], {"installed", "sha256", "pins"}, "dependencies")
    installed = deps["installed"]
    if not isinstance(installed, dict) or any(
        not _matches(name, _NAME) or not _matches(version, _VERSION) for name, version in installed.items()
    ):
        raise ValueError("invalid installed distribution inventory")
    encoded = json.dumps(installed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if deps["sha256"] != sha256(encoded).hexdigest():
        raise ValueError("installed distribution SHA-256 mismatch")
    pins = deps["pins"]
    if not isinstance(pins, dict) or not pins:
        raise ValueError("invalid pin inventory")
    all_match = True
    for name, value in pins.items():
        result = _object(value, {"expected", "installed", "status"}, "pin")
        expected, observed = result["expected"], installed.get(name)
        if not _matches(name, _NAME) or not _matches(expected, _VERSION):
            raise ValueError("invalid dependency pin")
        status = "MATCHED" if observed == expected else "VERSION_MISMATCH"
        if observed is None:
            status = "MISSING"
        if result["installed"] != observed or result["status"] != status:
            raise ValueError(f"inconsistent pin evidence for {name}")
        all_match = all_match and status == "MATCHED"
    status = "SMOKE_ENVIRONMENT_CAPTURED" if all_match else "SMOKE_ENVIRONMENT_PIN_MISMATCH"
    if report["status"] != status:
        raise ValueError("capture status disagrees with pin outcomes")


def _read_report(path: str | Path, side: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        file = Path(path)
        if not file.is_file():
            raise ValueError(f"{side} smoke environment: expected a regular report file")
        with file.open("rb") as stream:
            raw = stream.read(_MAX_REPORT_BYTES + 1)
    except OSError:
        raise ValueError(f"{side} smoke environment: could not read report") from None
    return _decode_report(raw, side)


def _decode_report(raw: bytes, side: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        if len(raw) > _MAX_REPORT_BYTES:
            raise ValueError("report exceeds its size limit")
        try:
            document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ValueError("invalid report JSON") from None
        report = _object(document, _REPORT_FIELDS, "report")
        if type(report["schema_version"]) is not int or report["schema_version"] != 1:
            raise ValueError("unsupported schema_version")
        observed_at = report["observed_at_utc"]
        if not _matches(observed_at, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)"):
            raise ValueError("invalid UTC observation time")
        try:
            datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid UTC observation time") from None
        _fingerprints(report["inputs"])
        _runtime_fields(report)
        _dependency_fields(report)
    except ValueError as exc:
        raise ValueError(f"{side} smoke environment: {exc}") from None
    info = {
        "status": report["status"],
        "observed_at_utc": observed_at,
        "bytes": len(raw),
        "sha256": sha256(raw).hexdigest(),
        "dependency_sha256": report["dependencies"]["sha256"],
        "distribution_count": len(report["dependencies"]["installed"]),
    }
    return report, info


def _changes(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {key: {"left": left[key], "right": right[key]} for key in sorted(left.keys() & right.keys())
            if left[key] != right[key]}


def _map_changes(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": {key: right[key] for key in sorted(right.keys() - left.keys())},
        "removed": {key: left[key] for key in sorted(left.keys() - right.keys())},
        "changed": _changes(left, right),
    }


def _unavailable(left: dict[str, Any], right: dict[str, Any]) -> dict[str, str]:
    fields = {}
    for group in ("python", "host", "runner"):
        for key in sorted(left[group]):
            before, after = left[group][key] in (None, ""), right[group][key] in (None, "")
            if before and after:
                fields[f"{group}.{key}"] = "BOTH_UNAVAILABLE"
            elif before or after:
                fields[f"{group}.{key}"] = "LEFT_UNAVAILABLE" if before else "RIGHT_UNAVAILABLE"
    return fields


def compare_smoke_environments(left: str | Path, right: str | Path) -> dict[str, Any]:
    """Compare two internally consistent reports without opening their recorded artifacts."""
    before, left_info = _read_report(left, "left")
    after, right_info = _read_report(right, "right")
    return _compare_reports(before, left_info, after, right_info)


def _compare_reports(
    before: dict[str, Any], left_info: dict[str, Any], after: dict[str, Any], right_info: dict[str, Any],
) -> dict[str, Any]:
    deps = _map_changes(before["dependencies"]["installed"], after["dependencies"]["installed"])
    pins = _map_changes(before["dependencies"]["pins"], after["dependencies"]["pins"])
    inputs = _changes(before["inputs"], after["inputs"])
    for change in inputs.values():
        change["same_content_fingerprint"] = all(change["left"][key] == change["right"][key] for key in ("bytes", "sha256"))
    runner = _changes(before["runner"], after["runner"])
    environment = {group: _changes(before[group], after[group]) for group in ("tools", "python", "host")}
    environment["runner"] = {key: value for key, value in runner.items() if key in _RUNNER_EXECUTION}
    run_identity = {key: value for key, value in runner.items() if key in _RUNNER_IDENTITY}
    if left_info["observed_at_utc"] != right_info["observed_at_utc"]:
        run_identity["observed_at_utc"] = {"left": left_info["observed_at_utc"], "right": right_info["observed_at_utc"]}
    return {
        "schema_version": 1,
        "status": "SMOKE_ENVIRONMENTS_COMPARED",
        "left": left_info,
        "right": right_info,
        "summary": {
            "distribution_changes": sum(map(len, deps.values())),
            "pin_changes": sum(map(len, pins.values())),
            "recorded_environment_changes": sum(map(len, environment.values())),
            "input_changes": len(inputs),
            "run_identity_changes": len(run_identity),
        },
        "dependencies": deps,
        "pins": pins,
        "input_changes": inputs,
        "environment_changes": environment,
        "run_identity_changes": run_identity,
        "unavailable_fields": _unavailable(before, after),
    }
