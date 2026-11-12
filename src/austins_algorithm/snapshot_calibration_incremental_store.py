"""Persist immutable layout checkpoints addressed by their exact SHA-256 bytes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any

from .constraints import _unique_object
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .replay_receipt import _new_receipt_directory, _verify_artifact
from .snapshot_calibration_checkpoint_store import (
    _MAX_CHECKPOINT_BYTES, _VerifiedCheckpointBundle, _encode_checkpoint, _finite_float, _regular_entry,
    _verified_checkpoint_bundle,
)
from .snapshot_calibration_comparison import MAX_CALIBRATION_BACKEND_QUBITS
from .snapshot_calibration_compilation_bundle import _MAX_BUNDLE_BYTES, _publish_bundle
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
from .unknown_count_snapshot_profile import _digest, _object, _sha256, _wires

if TYPE_CHECKING:
    from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache


_GENERATION_FIELDS = {
    "schema_version", "ordinal", "previous_generation_sha256", "request", "layout", "capture",
    "provider_job_submitted", "quantum_hardware_used",
}


@dataclass(frozen=True)
class _VerifiedIncrementalGeneration:
    sha256: str
    document: dict[str, Any]
    bundle: _VerifiedCheckpointBundle


def _initialize_incremental_calibration_store(directory: str | Path) -> Path:
    """Reserve a new store without reusing a preexisting directory."""
    root = Path(directory)
    with _new_receipt_directory(root):
        (root / "generations").mkdir()
        (root / "bundles").mkdir()
    return root


def _store_directories(directory: str | Path) -> tuple[Path, Path]:
    root = Path(directory)
    _regular_entry(root, directory=True)
    generations, bundles = root / "generations", root / "bundles"
    _regular_entry(generations, directory=True)
    _regular_entry(bundles, directory=True)
    return generations, bundles


def _validate_generation(document: Any, *, includes_bundle: bool) -> None:
    fields = _GENERATION_FIELDS | ({"compilation_bundle"} if includes_bundle else set())
    if not isinstance(document, dict) or document.keys() != fields:
        raise ValueError("invalid incremental checkpoint generation fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported incremental checkpoint generation schema version")
    if document["provider_job_submitted"] is not False or document["quantum_hardware_used"] is not False:
        raise ValueError("incremental checkpoint must describe local reference capture")

    request = _object(document["request"], "incremental checkpoint request")
    layouts = request.get("initial_layouts")
    if not isinstance(layouts, list) or not 1 <= len(layouts) <= MAX_LAYOUT_PROFILES:
        raise ValueError("incremental checkpoint request has an invalid layout count")
    ordered = [
        tuple(_wires(layout, MAX_CALIBRATION_BACKEND_QUBITS, label="incremental checkpoint requested layout"))
        for layout in layouts
    ]
    if ordered != sorted(set(ordered)) or len({len(layout) for layout in ordered}) != 1 or (
        len(ordered[0]) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
    ):
        raise ValueError("incremental checkpoint request must record distinct canonical ordered layouts")
    ordinal = document["ordinal"]
    if type(ordinal) is not int or not 1 <= ordinal <= len(ordered):
        raise ValueError("incremental checkpoint generation has an invalid ordinal")
    previous = document["previous_generation_sha256"]
    if ordinal == 1:
        if previous is not None:
            raise ValueError("incremental checkpoint genesis cannot have a predecessor")
    else:
        _sha256(previous, "incremental checkpoint predecessor SHA-256")
    layout = _wires(document["layout"], MAX_CALIBRATION_BACKEND_QUBITS, label="incremental checkpoint layout")
    if tuple(layout) != ordered[ordinal - 1]:
        raise ValueError("incremental checkpoint generation differs from the requested layout prefix")

    capture = _object(document["capture"], "incremental checkpoint capture envelope")
    if capture.keys() != {"report_sha256", "report"}:
        raise ValueError("invalid incremental checkpoint capture fields")
    report = _object(capture["report"], "incremental checkpoint capture")
    if _sha256(capture["report_sha256"], "incremental checkpoint capture SHA-256") != _digest(report):
        raise ValueError("incremental checkpoint capture does not match its digest")
    if (
        report.get("status") != "COMPLETED" or "density_fusion" in report
        or _json_bytes(report.get("initial_layout")) != _json_bytes(layout)
        or report.get("provider_job_submitted") is not False or report.get("quantum_hardware_used") is not False
    ):
        raise ValueError("incremental checkpoint requires a completed reference capture for its layout")


def _read_generation_document(path: Path, expected_sha256: str) -> dict[str, Any]:
    _regular_entry(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_CHECKPOINT_BYTES + 1)
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"incremental checkpoint generation exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("incremental checkpoint generation does not match the expected SHA-256 digest")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=partial(_unique_object, label="incremental checkpoint generation"),
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid incremental checkpoint generation JSON") from exc
    _validate_generation(document, includes_bundle=True)
    return document


def _generation_bundle(bundles: Path, document: dict[str, Any]) -> _VerifiedCheckpointBundle:
    fingerprint = _object(document["compilation_bundle"], "incremental checkpoint compilation bundle")
    digest = _sha256(fingerprint.get("sha256"), "incremental checkpoint compilation bundle SHA-256")
    return _verified_checkpoint_bundle(bundles / f"{digest}.json", fingerprint)


def _read_incremental_calibration_generation(
    directory: str | Path, expected_generation_sha256: str,
) -> _VerifiedIncrementalGeneration:
    """Verify one known generation and its bundle without rediscovering its ancestors."""
    digest = _sha256(expected_generation_sha256, "expected incremental checkpoint generation SHA-256")
    generations, bundles = _store_directories(directory)
    document = _read_generation_document(generations / f"{digest}.json", digest)
    return _VerifiedIncrementalGeneration(digest, document, _generation_bundle(bundles, document))


def _read_incremental_calibration_generations(
    directory: str | Path, expected_tip_sha256: str,
) -> tuple[_VerifiedIncrementalGeneration, ...]:
    """Verify the explicit trusted lineage and return it in genesis-to-tip order."""
    current = _sha256(expected_tip_sha256, "expected incremental checkpoint tip SHA-256")
    generations, bundles = _store_directories(directory)
    documents = []
    seen = set()
    request_bytes = None
    next_ordinal = None
    while current is not None:
        if current in seen or len(documents) == MAX_LAYOUT_PROFILES:
            raise ValueError("incremental checkpoint lineage is cyclic or exceeds its limit")
        seen.add(current)
        document = _read_generation_document(generations / f"{current}.json", current)
        ordinal = document["ordinal"]
        scope = _json_bytes(document["request"])
        if request_bytes is not None and (scope != request_bytes or ordinal != next_ordinal):
            raise ValueError("incremental checkpoint lineage differs from its immutable request or ordinal")
        request_bytes, next_ordinal = scope, ordinal - 1
        documents.append((current, document))
        current = document["previous_generation_sha256"]

    checked = []
    bundle_pins = set()
    entry_keys = set()
    for digest, document in reversed(documents):
        bundle = _generation_bundle(bundles, document)
        if bundle.sha256 in bundle_pins or entry_keys.intersection(bundle.entries):
            raise ValueError("incremental checkpoint lineage contains duplicate bundle or round keys")
        if checked and _json_bytes(checked[0].bundle.software_versions) != _json_bytes(bundle.software_versions):
            raise ValueError("incremental checkpoint lineage has conflicting bundle software versions")
        bundle_pins.add(bundle.sha256)
        entry_keys.update(bundle.entries)
        checked.append(_VerifiedIncrementalGeneration(digest, document, bundle))
    return tuple(checked)


def _verify_existing(path: Path, fingerprint: dict[str, Any]) -> None:
    _regular_entry(path)
    _verify_artifact(path, fingerprint, capture=False)


def _publish_incremental_calibration_generation(
    directory: str | Path, *, request: dict[str, Any], layout: Sequence[int], capture: dict[str, Any],
    cache: SnapshotCalibrationCompilationCache, ordinal: int, previous_generation_sha256: str | None,
    expected_keys: frozenset[str],
    verify_generation: Callable[[_VerifiedIncrementalGeneration], None] | None = None,
) -> dict[str, Any]:
    """Publish one verified layout bundle, then its immutable generation marker."""
    document = {
        "schema_version": 1, "ordinal": ordinal, "previous_generation_sha256": previous_generation_sha256,
        "request": request, "layout": list(layout), "capture": capture,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    _validate_generation(document, includes_bundle=False)
    _encode_checkpoint(document, {"bytes": _MAX_BUNDLE_BYTES, "sha256": "0" * 64})
    root = Path(directory)
    if previous_generation_sha256 is None and not root.exists() and not root.is_symlink():
        _initialize_incremental_calibration_store(root)
    generations, bundles = _store_directories(root)
    prior_bundle_pins = set()
    prior_entry_keys = set()
    prior_versions = None
    if previous_generation_sha256 is not None:
        lineage = _read_incremental_calibration_generations(root, previous_generation_sha256)
        if len(lineage) != ordinal - 1 or _json_bytes(lineage[-1].document["request"]) != _json_bytes(request):
            raise ValueError("incremental checkpoint predecessor differs from the requested scope or ordinal")
        prior_bundle_pins = {generation.bundle.sha256 for generation in lineage}
        prior_entry_keys = {key for generation in lineage for key in generation.bundle.entries}
        prior_versions = lineage[-1].bundle.software_versions
        del lineage

    staging = Path(mkdtemp(dir=bundles, prefix=".checkpoint-bundle-"))
    staged = staging / "compilation-bundle.json"
    try:
        pin = cache.save_bundle(staged)
        fingerprint = {"bytes": _regular_entry(staged).st_size, "sha256": pin}
        bundle = _verified_checkpoint_bundle(staged, fingerprint)
        if bundle.entries.keys() != expected_keys:
            raise ValueError("incremental checkpoint compilation bundle does not cover the exact required round keys")
        if pin in prior_bundle_pins or prior_entry_keys.intersection(bundle.entries):
            raise ValueError("incremental checkpoint would duplicate an earlier bundle or round key")
        if prior_versions is not None and _json_bytes(bundle.software_versions) != _json_bytes(prior_versions):
            raise ValueError("incremental checkpoint bundle software versions differ from the predecessor")
        destination = bundles / f"{pin}.json"
        try:
            os.link(staged, destination)
        except FileExistsError:
            _verify_existing(destination, fingerprint)
    finally:
        staged.unlink(missing_ok=True)
        staging.rmdir()

    raw = _encode_checkpoint(document, fingerprint)
    digest = sha256(raw).hexdigest()
    if verify_generation is not None:
        verify_generation(_VerifiedIncrementalGeneration(digest, json.loads(raw), bundle))
    marker = generations / f"{digest}.json"
    try:
        _publish_bundle(marker, raw)
    except FileExistsError:
        _verify_existing(marker, {"bytes": len(raw), "sha256": digest})
    return {"generation_sha256": digest, "generation_bytes": len(raw), "compilation_bundle": fingerprint}
