"""Persist immutable fusion attempts bound to a complete pinned reference chain."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
import json
from math import isqrt
from pathlib import Path
from threading import Lock
from typing import Any

from .constraints import _unique_object
from .core import _bounded_int
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .snapshot_calibration_checkpoint_store import _MAX_CHECKPOINT_BYTES, _finite_float, _regular_entry
from .snapshot_calibration_compilation_bundle import _publish_bundle
from .snapshot_calibration_fusion import _CAPTURE_CHANGES, _compare_capture_pair, _unchanged
from .snapshot_calibration_incremental_store import (
    _read_generation_document, _store_directories, _verify_existing,
)
from .snapshot_calibration_planning import _profile
from .snapshot_calibration_study_captures import _STATIC_ROUND_FIELDS, _TIMEOUT_FIELDS, _read_capture
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
from .unknown_count_snapshot_profile import _object, _sha256


_GENERATION_FIELDS = {
    "schema_version", "reference_generation_sha256", "previous_generation_sha256", "ordinal", "layout",
    "provider_job_submitted", "quantum_hardware_used",
}


@dataclass(frozen=True)
class _VerifiedFusionGeneration:
    sha256: str
    document: dict[str, Any]


@dataclass(frozen=True)
class _FusionReference:
    sha256: str
    request: dict[str, Any]
    captures: tuple[dict[str, Any], ...]
    generation_sha256s: tuple[str, ...]


@dataclass
class _CalibrationFusionContext:
    """Validated state for one invocation and one explicitly selected branch."""

    _root: Path
    _reference: _FusionReference
    history: tuple[_VerifiedFusionGeneration, ...]
    _reference_seal: str
    _history_seals: tuple[tuple[str, str], ...]
    _lock: Any = field(default_factory=Lock, repr=False)


def _fusion_generation_terminal(document: dict[str, Any]) -> bool:
    """Use the study's failure and literal cancellation confirmation boundary."""
    if "failure" in document:
        return True
    report = document["capture"]["report"]
    return report["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT" and report["local_job_cancelled"] is not True


def _read_complete_reference(directory: str | Path, expected_sha256: str) -> _FusionReference:
    tip = _sha256(expected_sha256, "fusion checkpoint reference generation SHA-256")
    generations, _ = _store_directories(directory)
    current = tip
    seen = set()
    documents = []
    pins = []
    request = None
    ordinal = None
    while current is not None:
        if current in seen or len(documents) == MAX_LAYOUT_PROFILES:
            raise ValueError("fusion checkpoint reference lineage is cyclic or exceeds its limit")
        seen.add(current)
        document = _read_generation_document(generations / f"{current}.json", current)
        if request is None:
            request, ordinal = document["request"], document["ordinal"]
            if ordinal != len(request["initial_layouts"]):
                raise ValueError("fusion checkpoint requires a complete reference generation chain")
        if document["ordinal"] != ordinal or _json_bytes(document["request"]) != _json_bytes(request):
            raise ValueError("fusion checkpoint reference lineage has conflicting request or ordinal evidence")
        documents.append(document)
        pins.append(current)
        ordinal -= 1
        current = document["previous_generation_sha256"]
    if ordinal != 0:
        raise ValueError("fusion checkpoint reference generation chain is incomplete")
    return _FusionReference(
        tip, request, tuple(document["capture"] for document in reversed(documents)), tuple(reversed(pins)),
    )


def _read_fusion_document(path: Path, expected_sha256: str) -> dict[str, Any]:
    _regular_entry(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_CHECKPOINT_BYTES + 1)
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"fusion checkpoint generation exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("fusion checkpoint generation does not match the expected SHA-256 digest")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=partial(_unique_object, label="fusion checkpoint generation"),
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid fusion checkpoint generation JSON") from exc
    _validate_generation_fields(document)
    return document


def _validate_generation_fields(document: Any) -> None:
    if not isinstance(document, dict) or ("capture" in document) == ("failure" in document):
        raise ValueError("fusion checkpoint requires exactly one capture or failure outcome")
    outcome = "capture" if "capture" in document else "failure"
    if document.keys() != _GENERATION_FIELDS | {outcome}:
        raise ValueError("invalid fusion checkpoint generation fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported fusion checkpoint generation schema version")
    if document["provider_job_submitted"] is not False or document["quantum_hardware_used"] is not False:
        raise ValueError("fusion checkpoint must describe local simulation evidence")
    _sha256(document["reference_generation_sha256"], "fusion checkpoint reference generation SHA-256")
    ordinal = document["ordinal"]
    if type(ordinal) is not int or not 1 <= ordinal <= MAX_LAYOUT_PROFILES:
        raise ValueError("fusion checkpoint generation has an invalid ordinal")
    if ordinal == 1:
        if document["previous_generation_sha256"] is not None:
            raise ValueError("fusion checkpoint genesis cannot have a predecessor")
    else:
        _sha256(document["previous_generation_sha256"], "fusion checkpoint predecessor SHA-256")
    if outcome == "failure":
        failure = _object(document["failure"], "fusion checkpoint failure")
        if (
            failure.keys() != {"error_type", "message"} or not isinstance(failure["error_type"], str)
            or not failure["error_type"].isidentifier() or not isinstance(failure["message"], str)
        ):
            raise ValueError("fusion checkpoint failure has invalid error evidence")


def _validate_reference_outcome(document: dict[str, Any], reference: _FusionReference) -> None:
    if document["reference_generation_sha256"] != reference.sha256:
        raise ValueError("fusion checkpoint generation refers to a different reference tip")
    index = document["ordinal"] - 1
    layouts = reference.request["initial_layouts"]
    if index >= len(layouts) or _json_bytes(document["layout"]) != _json_bytes(layouts[index]):
        raise ValueError("fusion checkpoint generation differs from the canonical layout prefix")
    if "failure" in document:
        return
    sealed = reference.captures[index]
    original = sealed["report"]
    request = reference.request
    required = request.get("required_rounds")
    if (
        not isinstance(required, list) or not required or required != list(range(len(required)))
        or any(type(index) is not int for index in required)
    ):
        raise ValueError("fusion checkpoint reference has an invalid round request")
    inputs = _object(request.get("inputs"), "fusion checkpoint reference inputs")
    if not {"search_qubits", "constraint_sha256", "seed_transpiler"} <= inputs.keys():
        raise ValueError("fusion checkpoint reference lacks the recorded compiler inputs")
    width = _bounded_int(inputs["search_qubits"], 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "fusion checkpoint search width")
    _bounded_int(inputs["seed_transpiler"], 0, 2**32 - 1, "fusion checkpoint transpiler seed")
    _sha256(inputs["constraint_sha256"], "fusion checkpoint constraint SHA-256")
    if len(required) > isqrt((1 << width) - 1) + 1:
        raise ValueError("fusion checkpoint reference round request exceeds its bound")
    audit = _object(original.get("audit"), "fusion checkpoint reference audit")
    marked = audit.get("marked_states")
    if (
        not isinstance(marked, list) or any(not isinstance(bits, str) or len(bits) != width
                                          or set(bits) - {"0", "1"} for bits in marked)
        or marked != sorted(set(marked)) or type(audit.get("states_checked")) is not int
        or audit["states_checked"] != 1 << width or type(audit.get("marked_count")) is not int
        or audit["marked_count"] != len(marked)
    ):
        raise ValueError("fusion checkpoint reference audit is inconsistent")
    report, _ = _read_capture(
        document["capture"], tuple(layouts[index]), inputs, len(required), audit, fused=True,
    )
    if report["status"] == "COMPLETED":
        outcomes = [format(state, f"0{width}b") for state in range(1 << width)]
        witnesses = set(marked)
        args = width, inputs["constraint_sha256"], outcomes, witnesses
        _compare_capture_pair(original, report, _profile(original, *args), _profile(report, *args))
    else:
        if not _unchanged(original, report, _CAPTURE_CHANGES | _TIMEOUT_FIELDS | {"status"}):
            raise ValueError("fusion checkpoint timed-out capture differs from its reference scope")
        for key, row in report["rounds"].items():
            expected = {field: original["rounds"][key][field] for field in _STATIC_ROUND_FIELDS}
            if _json_bytes(row) != _json_bytes(expected):
                raise ValueError("fusion checkpoint timed-out capture differs from its reference compilation")


def _fusion_directory(directory: str | Path, *, create: bool = False) -> Path:
    _store_directories(directory)
    path = Path(directory) / "fusion-generations"
    if create:
        try:
            path.mkdir()
        except FileExistsError:
            pass
    _regular_entry(path, directory=True)
    return path


def _read_fusion_chain(
    directory: str | Path, tip: str, reference: _FusionReference | None = None,
) -> tuple[_VerifiedFusionGeneration, ...]:
    generations = _fusion_directory(directory)
    current = tip
    seen = set()
    checked = []
    ordinal = None
    while current is not None:
        if current in seen or len(checked) == MAX_LAYOUT_PROFILES:
            raise ValueError("fusion checkpoint lineage is cyclic or exceeds its limit")
        seen.add(current)
        document = _read_fusion_document(generations / f"{current}.json", current)
        if reference is None:
            reference = _read_complete_reference(directory, document["reference_generation_sha256"])
        if ordinal is not None and document["ordinal"] != ordinal:
            raise ValueError("fusion checkpoint lineage has conflicting ordinal evidence")
        _validate_reference_outcome(document, reference)
        checked.append(_VerifiedFusionGeneration(current, document))
        ordinal = document["ordinal"] - 1
        current = document["previous_generation_sha256"]
    checked.reverse()
    if ordinal != 0:
        raise ValueError("fusion checkpoint lineage is incomplete")
    if any(_fusion_generation_terminal(generation.document) for generation in checked[:-1]):
        raise ValueError("fusion checkpoint lineage continues after a terminal outcome")
    return tuple(checked)


def _read_calibration_fusion_generations(
    directory: str | Path, expected_tip_sha256: str,
) -> tuple[_VerifiedFusionGeneration, ...]:
    """Read only the explicit fusion tip and its pinned, canonical predecessors."""
    tip = _sha256(expected_tip_sha256, "expected fusion checkpoint tip SHA-256")
    return _read_fusion_chain(directory, tip)


def _reference_state(root: Path, reference: _FusionReference) -> str:
    return sha256(_json_bytes([
        str(root), reference.sha256, reference.request, reference.captures, reference.generation_sha256s,
    ])).hexdigest()


def _generation_state(generation: _VerifiedFusionGeneration) -> tuple[str, str]:
    return generation.sha256, sha256(_json_bytes(generation.document)).hexdigest()


def _check_context_state(context: _CalibrationFusionContext) -> None:
    try:
        intact = (
            _reference_state(context._root, context._reference) == context._reference_seal
            and tuple(_generation_state(row) for row in context.history) == context._history_seals
        )
    except (AttributeError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("fusion checkpoint context has mutated validated state") from exc
    if not intact:
        raise ValueError("fusion checkpoint context has mutated validated state")


def _open_calibration_fusion_context(
    directory: str | Path, reference_tip: str, fusion_tip: str | None = None,
) -> _CalibrationFusionContext:
    """Validate explicit history once; never discover or select another disk tip."""
    reference = _read_complete_reference(directory, reference_tip)
    history = () if fusion_tip is None else _read_fusion_chain(
        directory, _sha256(fusion_tip, "expected fusion checkpoint tip SHA-256"), reference,
    )
    root = Path(directory).resolve()
    return _CalibrationFusionContext(
        root, reference, history, _reference_state(root, reference),
        tuple(_generation_state(row) for row in history),
    )


def _recheck_context_ancestors(context: _CalibrationFusionContext) -> None:
    """Rehash pinned regular manifests after the callback, without reparsing them."""
    generations, _ = _store_directories(context._root)
    paths = [(generations / f"{pin}.json", pin) for pin in context._reference.generation_sha256s]
    if context.history:
        fusion = _fusion_directory(context._root)
        paths.extend((fusion / f"{row.sha256}.json", row.sha256) for row in context.history)
    for path, expected in paths:
        _regular_entry(path)
        size = 0
        digest = sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(min(1024 * 1024, _MAX_CHECKPOINT_BYTES + 1 - size)):
                size += len(chunk)
                if size > _MAX_CHECKPOINT_BYTES:
                    raise ValueError(f"fusion checkpoint ancestor exceeds {_MAX_CHECKPOINT_BYTES} bytes")
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("fusion checkpoint ancestor does not match its expected SHA-256 digest")


def _publish_calibration_fusion_generation(
    directory: str | Path, *, reference_generation_sha256: str, previous_generation_sha256: str | None,
    ordinal: int, layout: Sequence[int], capture: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    verify_generation: Callable[[_VerifiedFusionGeneration], None] | None = None,
    _context: _CalibrationFusionContext | None = None,
) -> dict[str, Any]:
    """Validate an attempted layout and publish its immutable marker last."""
    if (capture is None) == (failure is None):
        raise ValueError("fusion checkpoint requires exactly one capture or failure outcome")
    document = {
        "schema_version": 1, "reference_generation_sha256": reference_generation_sha256,
        "previous_generation_sha256": previous_generation_sha256, "ordinal": ordinal, "layout": list(layout),
        **({"capture": capture} if capture is not None else {"failure": failure}),
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    _validate_generation_fields(document)
    if _context is None:
        return _publish_fusion_generation(directory, document, verify_generation, None)
    if type(_context) is not _CalibrationFusionContext:
        raise ValueError("invalid fusion checkpoint context")
    if not _context._lock.acquire(blocking=False):
        raise ValueError("fusion checkpoint context already has an append in progress")
    try:
        return _publish_fusion_generation(directory, document, verify_generation, _context)
    finally:
        _context._lock.release()


def _publish_fusion_generation(
    directory: str | Path, document: dict[str, Any],
    verify_generation: Callable[[_VerifiedFusionGeneration], None] | None,
    context: _CalibrationFusionContext | None,
) -> dict[str, Any]:
    previous_pin = document["previous_generation_sha256"]
    ordinal = document["ordinal"]
    if context is None:
        reference = _read_complete_reference(directory, document["reference_generation_sha256"])
        previous = () if previous_pin is None else _read_fusion_chain(directory, previous_pin, reference)
    else:
        _store_directories(directory)
        _check_context_state(context)
        if (Path(directory).resolve() != context._root
                or document["reference_generation_sha256"] != context._reference.sha256):
            raise ValueError("fusion checkpoint context differs from the requested root or reference tip")
        reference, previous = context._reference, context.history
        if previous_pin != (previous[-1].sha256 if previous else None):
            raise ValueError("fusion checkpoint context differs from the requested predecessor")
    if len(previous) != ordinal - 1 or (previous and _fusion_generation_terminal(previous[-1].document)):
        raise ValueError("fusion checkpoint cannot extend this predecessor ordinal or terminal outcome")
    _validate_reference_outcome(document, reference)
    try:
        raw = (json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid fusion checkpoint generation JSON") from exc
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"fusion checkpoint generation exceeds {_MAX_CHECKPOINT_BYTES} bytes")
    digest = sha256(raw).hexdigest()
    if verify_generation is not None:
        checked = _VerifiedFusionGeneration(digest, json.loads(raw))
        verify_generation(checked)
        try:
            unchanged = (json.dumps(checked.document, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("fusion checkpoint callback mutated the staged generation") from exc
        if checked.sha256 != digest or unchanged != raw:
            raise ValueError("fusion checkpoint callback mutated the staged generation")
    if context is not None:
        _check_context_state(context)
        _recheck_context_ancestors(context)
    marker = _fusion_directory(directory, create=True) / f"{digest}.json"
    try:
        _publish_bundle(marker, raw)
    except FileExistsError:
        _verify_existing(marker, {"bytes": len(raw), "sha256": digest})
    if context is not None:
        # The callback never owns the record retained for subsequent appends.
        generation = _VerifiedFusionGeneration(digest, json.loads(raw))
        context.history = (*previous, generation)
        context._history_seals = (*context._history_seals, _generation_state(generation))
    return {"generation_sha256": digest, "generation_bytes": len(raw)}
