"""Capture the installed Manila smoke environment as diagnostic evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys


_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?"
_VERSION = r"[A-Za-z0-9][A-Za-z0-9.+!_-]{0,127}"
_RUNNER_FIELDS = {
    "environment": ("RUNNER_ENVIRONMENT", r"github-hosted|self-hosted"),
    "os": ("RUNNER_OS", r"Windows|Linux|macOS"),
    "arch": ("RUNNER_ARCH", r"X86|X64|ARM|ARM64"),
    "image_os": ("ImageOS", r"win(?:dows)?[0-9]{2,4}"),
    "image_version": ("ImageVersion", r"[0-9]{8}(?:\.[0-9]+){1,3}"),
    "trigger_commit": ("GITHUB_SHA", r"[0-9a-f]{40}"),
    "workflow_commit": ("GITHUB_WORKFLOW_SHA", r"[0-9a-f]{40}"),
    "run_id": ("GITHUB_RUN_ID", r"[0-9]{1,20}"),
    "run_attempt": ("GITHUB_RUN_ATTEMPT", r"[0-9]{1,8}"),
}


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_pins(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("smoke requirements must be UTF-8") from None
    pins = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        match = re.fullmatch(rf"({_NAME})==({_VERSION})", entry)
        if match is None:
            raise ValueError(f"smoke requirements line {line_number} must contain one exact version pin")
        name, expected = match.groups()
        normalized = _canonical_name(name)
        if normalized in pins:
            raise ValueError(f"duplicate smoke requirement on line {line_number}")
        pins[normalized] = expected
    if not pins:
        raise ValueError("smoke requirements contain no exact version pins")
    return pins


def _installed_versions() -> dict[str, str]:
    installed = {}
    for distribution in distributions():
        name, version = distribution.metadata["Name"], distribution.version
        if not re.fullmatch(_NAME, name or "") or not re.fullmatch(_VERSION, version or ""):
            raise ValueError("installed distribution has an invalid name or version")
        normalized = _canonical_name(name)
        if normalized in installed:
            raise ValueError(f"duplicate installed distribution: {normalized}")
        installed[normalized] = version
    return dict(sorted(installed.items()))


def _fingerprint(path: Path, data: bytes) -> dict:
    return {"name": path.name, "bytes": len(data), "sha256": sha256(data).hexdigest()}


def _runner_fields() -> dict[str, str | None]:
    fields = {}
    for key, (variable, pattern) in _RUNNER_FIELDS.items():
        value = os.environ.get(variable, "")
        fields[key] = value if len(value) <= 128 and re.fullmatch(pattern, value, re.IGNORECASE) else None
    return fields


def _capture(args: argparse.Namespace) -> dict:
    uv = re.match(rf"uv ({_VERSION})(?:\s|$)", args.uv_version)
    if uv is None or not re.fullmatch(_VERSION, args.powershell_version):
        raise ValueError("could not identify the uv or PowerShell version")

    requirements = args.requirements.read_bytes()
    pins = _read_pins(requirements)
    installed = _installed_versions()
    pin_results = {}
    for name, expected in sorted(pins.items()):
        observed = installed.get(name)
        status = "MATCHED" if observed == expected else "VERSION_MISMATCH"
        if observed is None:
            status = "MISSING"
        pin_results[name] = {"expected": expected, "installed": observed, "status": status}
    pins_match = all(pin["status"] == "MATCHED" for pin in pin_results.values())
    architecture = platform.machine()
    if architecture.lower() not in {"amd64", "x86_64", "x86", "i386", "i686", "arm64", "aarch64", "armv7l"}:
        architecture = None
    installed_bytes = json.dumps(installed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "status": "SMOKE_ENVIRONMENT_CAPTURED" if pins_match else "SMOKE_ENVIRONMENT_PIN_MISMATCH",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "source_distribution": _fingerprint(args.source_distribution, args.source_distribution.read_bytes()),
            "requirements": _fingerprint(args.requirements, requirements),
        },
        "tools": {"uv": uv[1], "powershell": args.powershell_version},
        "python": {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "compiler": platform.python_compiler(),
            "pointer_bits": struct.calcsize("P") * 8,
        },
        "host": {
            "os": platform.system(),
            "architecture": architecture,
            "release": platform.release(),
            "windows_build": sys.getwindowsversion().build if sys.platform == "win32" else None,
            "logical_cpu_count": os.cpu_count(),
        },
        "runner": _runner_fields(),
        "dependencies": {
            "installed": installed,
            "sha256": sha256(installed_bytes).hexdigest(),
            "pins": pin_results,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--source-distribution", type=Path, required=True)
    parser.add_argument("--uv-version", required=True)
    parser.add_argument("--powershell-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = _capture(args)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(record, output, indent=2, allow_nan=False)
            output.write("\n")
    except OSError as exc:
        parser.exit(1, f"Smoke environment evidence I/O failed ({type(exc).__name__}, errno={exc.errno}).\n")
    except ValueError as exc:
        parser.exit(1, f"Smoke environment capture failed: {exc}.\n")
    return int(record["status"] != "SMOKE_ENVIRONMENT_CAPTURED")


if __name__ == "__main__":
    raise SystemExit(main())
