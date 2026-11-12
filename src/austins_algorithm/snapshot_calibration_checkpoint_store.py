"""Publish and verify pinned checkpoints after dated reference capture."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import json
import math
from os import stat_result
from pathlib import Path
import stat
from typing import TYPE_CHECKING, Any

from .constraints import _unique_object
from .replay_receipt import _new_receipt_directory, _verify_artifact
from .snapshot_calibration_compilation_bundle import _MAX_BUNDLE_BYTES, _publish_bundle, _read_bundle_document
from .unknown_count_snapshot_profile import _sha256

if TYPE_CHECKING:
    from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache


_CHECKPOINT_FILE = "checkpoint.json"
_BUNDLE_FILE = "compilation-bundle.json"
_MAX_CHECKPOINT_BYTES = _MAX_BUNDLE_BYTES
_CHECKPOINT_FIELDS = {
    "schema_version", "phase", "request", "reference_captures", "provider_job_submitted", "quantum_hardware_used",
}


@dataclass(frozen=True)
class _VerifiedCheckpointBundle:
    sha256: str
    bytes: int
    software_versions: dict[str, str | None]
    entries: dict[str, dict[str, Any]]


def _regular_entry(path: Path, *, directory: bool = False) -> stat_result:
    kind = "directory" if directory else "file"
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"expected a regular checkpoint {kind} named {path.name}") from exc
    matches_kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not matches_kind or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"expected a regular checkpoint {kind} named {path.name}")
    return metadata


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("checkpoint contains a nonfinite number")
    return value


def _validate_checkpoint_document(document: Any, *, includes_bundle: bool) -> None:
    fields = _CHECKPOINT_FIELDS | ({"compilation_bundle"} if includes_bundle else set())
    if not isinstance(document, dict) or document.keys() != fields:
        raise ValueError("invalid checkpoint manifest fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported checkpoint schema version")
    if (
        document["phase"] != "REFERENCE_CAPTURE_COMPLETED"
        or document["provider_job_submitted"] is not False or document["quantum_hardware_used"] is not False
        or not isinstance(document["request"], dict) or not isinstance(document["reference_captures"], dict)
    ):
        raise ValueError("checkpoint must record completed local reference capture")


def _encode_checkpoint(document: dict[str, Any], bundle: dict[str, Any]) -> bytes:
    try:
        raw = (json.dumps({**document, "compilation_bundle": bundle}, separators=(",", ":"), allow_nan=False)
               + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid checkpoint manifest JSON") from exc
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"checkpoint manifest exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    return raw


def _verified_checkpoint_bundle(path: Path, fingerprint: Any) -> _VerifiedCheckpointBundle:
    if not isinstance(fingerprint, dict) or fingerprint.keys() != {"sha256", "bytes"}:
        raise ValueError("invalid checkpoint compilation bundle fingerprint")
    digest = _sha256(fingerprint["sha256"], "checkpoint compilation bundle SHA-256")
    size = fingerprint["bytes"]
    if type(size) is not int or not 1 <= size <= _MAX_BUNDLE_BYTES:
        raise ValueError("checkpoint compilation bundle size is outside its limit")
    _regular_entry(path)
    # The bundle reader verifies the same pin again while parsing the envelope.
    # Its returned entries can be carried forward without reopening for SDK use.
    _verify_artifact(path, fingerprint, capture=False)
    document = _read_bundle_document(path, digest, require_current_software=False)
    return _VerifiedCheckpointBundle(digest, size, document["software_versions"], document["entries"])


def _publish_calibration_checkpoint(
    directory: str | Path, document: dict[str, Any], cache: SnapshotCalibrationCompilationCache, *,
    expected_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Publish the completed bundle and then the pinned manifest without overwriting either."""
    destination = Path(directory)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"checkpoint directory already exists: {destination}")
    _validate_checkpoint_document(document, includes_bundle=False)
    # Preflight with the longest possible valid bundle fingerprint.
    _encode_checkpoint(document, {"bytes": _MAX_BUNDLE_BYTES, "sha256": "0" * 64})
    with _new_receipt_directory(destination):
        path = destination / _BUNDLE_FILE
        digest = cache.save_bundle(path)
        fingerprint = {"bytes": _regular_entry(path).st_size, "sha256": digest}
        verified = _verified_checkpoint_bundle(path, fingerprint)
        if expected_keys is not None and verified.entries.keys() != expected_keys:
            raise ValueError("checkpoint compilation bundle does not cover the exact required round keys")
        raw = _encode_checkpoint(document, fingerprint)
        pin = _publish_bundle(destination / _CHECKPOINT_FILE, raw)
    return {"checkpoint_sha256": pin, "checkpoint_bytes": len(raw), "compilation_bundle": fingerprint}


def _read_calibration_checkpoint(
    directory: str | Path, expected_sha256: str,
) -> tuple[dict[str, Any], _VerifiedCheckpointBundle]:
    """Verify the caller's raw manifest pin and the fixed bundle before SDK work."""
    _sha256(expected_sha256, "expected checkpoint SHA-256")
    root = Path(directory)
    _regular_entry(root, directory=True)
    path = root / _CHECKPOINT_FILE
    _regular_entry(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_CHECKPOINT_BYTES + 1)
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"checkpoint manifest exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("checkpoint manifest does not match the expected SHA-256 digest")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=partial(_unique_object, label="checkpoint manifest"),
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid checkpoint manifest JSON") from exc
    _validate_checkpoint_document(document, includes_bundle=True)
    return document, _verified_checkpoint_bundle(root / _BUNDLE_FILE, document["compilation_bundle"])
