"""Compare verified offline replay receipts without rerunning the workflow."""

from __future__ import annotations

import json
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any

from .constraints import _unique_object
from .replay_receipt import _read_verified_receipt


_BATCH_PATHS = tuple(
    path
    for cover in ("direct", "complement")
    for path in (
        ("snapshot", "candidates", cover, "batches", None),
        ("witness_budget", "candidates", cover, "validation"),
        ("witness_budget", "candidates", cover, "validation_batches", None),
    )
)
_REPORT_TIMING_PATHS = frozenset({
    ("witness_budget", "timing_seconds"),
    ("witness_budget", "classical_reference", "wall_seconds"),
    ("snapshot", "state_preparation", "direct", "wall_seconds"),
    ("snapshot", "state_preparation", "complement", "wall_seconds"),
    *(path + ("timing_seconds",) for path in _BATCH_PATHS),
    *(path + ("simulation", "aer_experiment_seconds") for path in _BATCH_PATHS),
})
_PROBABILITY_ROUNDOFF = Decimal("1e-12")
_ROUNDOFF_TV_PATHS = frozenset({
    ("cross_cover_total_variation_distance",),
    *(("candidates", cover, "compilation", "verification", field)
      for cover in ("direct", "complement")
      for field in ("compilation_total_variation_distance", "export_total_variation_distance",
                    "roundtrip_total_variation_distance")),
})
_ROUNDOFF_PROBABILITY_PATHS = frozenset(
    ("candidates", cover, "compilation", "verification", field)
    for cover in ("direct", "complement") for field in ("compiled_probabilities", "roundtrip_probabilities")
)


def _reject_constant(token: str) -> None:
    raise ValueError(f"nonfinite JSON number {token}")


def _load_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=lambda pairs: _unique_object(pairs, label),
            parse_constant=_reject_constant, parse_float=Decimal,
        )
    except (ValueError, DecimalException, RecursionError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _difference(path: tuple[str | int, ...], kind: str) -> dict[str, str]:
    pointer = "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return {"path": pointer, "kind": kind}


def _accepted_roundoff(left: Any, right: Any, path: tuple[str | int, ...]) -> Decimal | None:
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        return None
    if path not in _ROUNDOFF_TV_PATHS and not (
        path[:-1] in _ROUNDOFF_PROBABILITY_PATHS and isinstance(path[-1], str)
        and path[-1] and set(path[-1]) <= {"0", "1"}
    ):
        return None
    if not all(value.is_finite() and 0 <= value <= Decimal("1.0000000001") for value in (left, right)):
        return None
    # Generated binary floats fit this exponent bound, including subnormals.
    # Widen Decimal precision so subtraction cannot round across the tolerance.
    exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    if exponent < -340:
        return None
    with localcontext() as context:
        context.prec = max(28, 2 - exponent)
        delta = abs(right - left)
    return delta if delta <= _PROBABILITY_ROUNDOFF else None


def _first_difference(
    left: Any, right: Any, path: tuple[str | int, ...] = (),
    ignored_paths: frozenset[tuple[str | None, ...]] = frozenset(),
    roundoff: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    if type(left) is not type(right):
        return _difference(path, "TYPE")
    if isinstance(left, dict):
        # None in an ignore pattern represents an array index, never an object key.
        pattern = tuple(part if isinstance(part, str) else None for part in path)
        for key in sorted(left.keys() | right.keys()):
            if pattern + (key,) in ignored_paths:
                continue
            child = path + (key,)
            if key not in left:
                return _difference(child, "LEFT_MISSING")
            if key not in right:
                return _difference(child, "RIGHT_MISSING")
            difference = _first_difference(left[key], right[key], child, ignored_paths, roundoff)
            if difference is not None:
                return difference
    elif isinstance(left, list):
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = _first_difference(left_item, right_item, path + (index,), ignored_paths, roundoff)
            if difference is not None:
                return difference
        if len(left) != len(right):
            return _difference(path + (min(len(left), len(right)),),
                               "LEFT_MISSING" if len(left) < len(right) else "RIGHT_MISSING")
    elif left != right:
        difference = _difference(path, "VALUE")
        if roundoff is not None:
            delta = _accepted_roundoff(left, right, path)
            if delta is not None:
                roundoff.append({"path": difference["path"], "left": str(left), "right": str(right),
                                 "absolute_difference": str(delta)})
                return None
        return difference
    return None


def _compare_objects(
    left: dict[str, Any], right: dict[str, Any],
    *, ignored_paths: frozenset[tuple[str | None, ...]] = frozenset(),
    probability_roundoff: bool = False,
) -> dict[str, Any]:
    roundoff: list[dict[str, str]] | None = [] if probability_roundoff else None
    try:
        difference = _first_difference(left, right, ignored_paths=ignored_paths, roundoff=roundoff)
    except RecursionError as exc:
        raise ValueError("receipt comparison exceeds the supported JSON nesting depth") from exc
    result: dict[str, Any] = {"matches": difference is None, "first_difference": difference}
    if roundoff is not None:
        result["probability_roundoff"] = {
            "absolute_tolerance": str(_PROBABILITY_ROUNDOFF),
            "accepted_count": len(roundoff),
            "maximum_absolute_difference": str(max(
                (Decimal(item["absolute_difference"]) for item in roundoff), default=Decimal(0),
            )),
            "accepted_differences": roundoff,
            "comparison_complete": difference is None,
        }
    return result


_VerifiedBundle = tuple[dict[str, Any], dict[str, bytes]]


def _read_comparison_bundle(directory: Path) -> _VerifiedBundle:
    try:
        return _read_verified_receipt(directory, capture_json=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{directory}: {exc}") from exc


def compare_replay_receipts(left: str | Path, right: str | Path) -> dict[str, Any]:
    """Verify two bundles and locate the first content difference in each JSON section."""
    return _compare_verified_bundles(
        left, right, _read_comparison_bundle(Path(left)), _read_comparison_bundle(Path(right)),
    )


def _compare_verified_bundles(
    left: str | Path, right: str | Path, left_bundle: _VerifiedBundle, right_bundle: _VerifiedBundle,
    *, probability_roundoff: bool = False,
) -> dict[str, Any]:
    left_receipt, left_payloads = left_bundle
    right_receipt, right_payloads = right_bundle
    left_artifacts, right_artifacts = left_receipt["artifacts"], right_receipt["artifacts"]
    request = _compare_objects(
        _load_object(left_payloads["request.json"], "left request"),
        _load_object(right_payloads["request.json"], "right request"),
    )
    request["exact_bytes_match"] = left_artifacts["request.json"] == right_artifacts["request.json"]
    report = _compare_objects(
        _load_object(left_payloads["report.json"], "left report"),
        _load_object(right_payloads["report.json"], "right report"),
        ignored_paths=_REPORT_TIMING_PATHS, probability_roundoff=probability_roundoff,
    )
    report["exact_bytes_match"] = left_artifacts["report.json"] == right_artifacts["report.json"]
    left_export, right_export = left_artifacts["selected.qasm"], right_artifacts["selected.qasm"]
    sections = {
        "request": request,
        "report": report,
        "selected_qasm": {"matches": left_export == right_export, "left": left_export, "right": right_export},
        "software_versions": _compare_objects(left_receipt["software_versions"], right_receipt["software_versions"]),
    }
    matches = all(section["matches"] for section in sections.values())
    return {
        "status": "REPLAY_RECEIPTS_MATCH" if matches else "REPLAY_RECEIPTS_DIFFER",
        "schema_version": 1,
        "left_directory": str(left),
        "right_directory": str(right),
        "sections": sections,
    }

