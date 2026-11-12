"""Write and verify exact-byte evidence from an offline replay."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from re import fullmatch
from typing import Any

from . import __version__
from .constraints import _unique_object


_RECEIPT_FILE = "receipt.json"
_LIMITS = {"request.json": 64 * 1024, "report.json": 16 * 1024**2, "selected.qasm": 16 * 1024**2}
_SOFTWARE_PACKAGES = ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "qiskit-qasm3-import", "numpy", "scipy")
_MAX_RECEIPT_BYTES = 16 * 1024


def _software_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"python": platform.python_version(), "austins_algorithm": __version__}
    for name in _SOFTWARE_PACKAGES:
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")


@contextmanager
def _new_receipt_directory(path: str | Path) -> Iterator[Path]:
    directory = Path(path)
    directory.mkdir()
    try:
        yield directory
    except BaseException:
        # Retain any evidence written before a publication failure.
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def _write_replay_receipt(
    directory: Path, *, request: bytes, report: dict[str, Any], openqasm: str | None,
) -> None:
    payloads = {
        "request.json": request,
        "report.json": _json_bytes(report),
        "selected.qasm": None if openqasm is None else openqasm.encode("utf-8"),
    }
    artifacts = {}
    for name, data in payloads.items():
        if data is not None and not 1 <= len(data) <= _LIMITS[name]:
            raise ValueError(f"receipt artifact {name} is outside its size limit")
        artifacts[name] = None if data is None else {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    receipt = _json_bytes({"schema_version": 1, "software_versions": _software_versions(), "artifacts": artifacts})
    if len(receipt) > _MAX_RECEIPT_BYTES:
        raise ValueError("replay receipt exceeds its size limit")
    for name, data in payloads.items():
        if data is not None:
            with (directory / name).open("xb") as stream:
                stream.write(data)
    # A receipt can be used only after every listed artifact has been written.
    with (directory / _RECEIPT_FILE).open("xb") as stream:
        stream.write(receipt)


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular receipt file named {path.name}")


def _read_receipt(path: Path) -> dict[str, Any]:
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_RECEIPT_BYTES + 1)
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("replay receipt exceeds its size limit")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_object(pairs, "receipt"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid replay receipt JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "software_versions", "artifacts"}:
        raise ValueError("invalid replay receipt fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported replay receipt schema_version")
    versions = document["software_versions"]
    if (
        not isinstance(versions, dict)
        or set(versions) != {"python", "austins_algorithm", *_SOFTWARE_PACKAGES}
        or any(value is not None and (not isinstance(value, str) or not value) for value in versions.values())
        or any(versions[name] is None for name in ("python", "austins_algorithm"))
    ):
        raise ValueError("invalid recorded software versions")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(_LIMITS):
        raise ValueError("invalid replay receipt artifact list")
    for name, expected in artifacts.items():
        if expected is None and name == "selected.qasm":
            continue
        if not isinstance(expected, dict) or set(expected) != {"bytes", "sha256"}:
            raise ValueError(f"invalid receipt entry for {name}")
        size, digest = expected["bytes"], expected["sha256"]
        if (
            type(size) is not int or not 1 <= size <= _LIMITS[name]
            or not isinstance(digest, str) or fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError(f"invalid receipt fingerprint for {name}")
    return document


def _verify_artifact(path: Path, expected: dict[str, Any], *, capture: bool) -> bytes | None:
    _regular_file(path)
    remaining = expected["bytes"]
    digest = hashlib.sha256()
    buffer = bytearray() if capture else None
    with path.open("rb") as stream:
        while remaining:
            block = stream.read(min(remaining, 64 * 1024))
            if not block:
                raise ValueError(f"receipt size mismatch for {path.name}")
            remaining -= len(block)
            digest.update(block)
            if buffer is not None:
                buffer.extend(block)
        if stream.read(1):
            raise ValueError(f"receipt size mismatch for {path.name}")
    if digest.hexdigest() != expected["sha256"]:
        raise ValueError(f"receipt SHA-256 mismatch for {path.name}")
    return None if buffer is None else bytes(buffer)


def _read_verified_receipt(
    directory: Path, *, capture_json: bool = False,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    document = _read_receipt(directory / _RECEIPT_FILE)
    payloads = {}
    for name, expected in document["artifacts"].items():
        path = directory / name
        if expected is None:
            if path.exists() or path.is_symlink():
                raise ValueError(f"receipt did not record {name}")
        else:
            payload = _verify_artifact(path, expected, capture=capture_json and name != "selected.qasm")
            if payload is not None:
                payloads[name] = payload
    return document, payloads


def verify_replay_receipt(directory: str | Path) -> dict[str, Any]:
    """Verify the local artifact bytes against a receipt without executing the request."""
    directory = Path(directory)
    document, _ = _read_verified_receipt(directory)
    return {"status": "REPLAY_RECEIPT_VERIFIED", "receipt_directory": str(directory), **document}

