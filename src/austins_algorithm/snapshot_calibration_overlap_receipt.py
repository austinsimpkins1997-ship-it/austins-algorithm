"""Verify a pinned compilation overlap record and its exact bundle lineage."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
import json
from pathlib import Path
import re
from typing import Any

from .constraints import _unique_object
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, _unknown_round_choices
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .replay_receipt import _regular_file, _verify_artifact
from .snapshot_calibration_comparison import MAX_CALIBRATION_BACKEND_QUBITS, _calibration_time
from .snapshot_calibration_compilation_bundle import (
    _MAX_BUNDLE_BYTES, _MAX_COMPOSITION_SOURCES, _MAX_ENTRIES, _MAX_QPY_BYTES,
    _bundle_bytes, _read_bundle_document, _recorded_calibration_importer,
)
from .unknown_count_planning import _selection_axis
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
from .unknown_count_snapshot_profile import MAX_VALIDATION_TRIALS, _sha256, _wires


_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_COMPILER_VERSIONS = ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy", "qiskit-qasm3-import")


@dataclass(frozen=True)
class _VerifiedOverlapReceipt:
    report: dict[str, Any]
    receipt_sha256: str
    output_sha256: str
    output_bytes: int
    resolved_rows: dict[str, dict[str, dict[str, Any]]]
    output_entries: dict[str, dict[str, Any]]


def _record(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid overlap receipt {label} fields")
    return value


def _expect(value: Any, expected: Any, label: str) -> None:
    if _json_bytes(value) != _json_bytes(expected):
        raise ValueError(f"overlap receipt {label} disagrees with pinned evidence")


def _read_receipt(path: Path, expected_sha256: str) -> dict[str, Any]:
    _sha256(expected_sha256, "expected overlap receipt SHA-256")
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(_MAX_RECEIPT_BYTES + 1)
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("overlap receipt exceeds its byte limit")
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("overlap receipt does not match the expected SHA-256 digest")
    try:
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_object(pairs, "overlap receipt"))
        _json_bytes(report)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid overlap receipt JSON") from exc
    report = _record(
        report, {"schema_version", "status", "sources", "software_versions", "unique_entry_count",
                 "duplicate_entry_count", "overlaps", "conflicts", "output", "stored_qpy_decoded",
                 "runtime_coverage_checked", "overlap_verification", "study_overlap"}, "report",
    )
    if (
        type(report["schema_version"]) is not int or report["schema_version"] != 1
        or report["status"] != "COMPILATION_BUNDLE_COMPOSED" or report["conflicts"] != {}
        or report["runtime_coverage_checked"] is not False
        or not isinstance(report["overlap_verification"], dict)
    ):
        raise ValueError("overlap receipt must record a successful study composition")
    return report


def _study_request(report: dict[str, Any], resolved: bool) -> dict[str, Any] | None:
    study = _record(
        report["study_overlap"], {"status", "request", "differing_versions", "oracle_audit_performed",
                                  "snapshot_simulation_performed", "provider_job_submitted", "quantum_hardware_used"},
        "study",
    )
    if (
        study["status"] != ("REQUEST_PREPARED" if resolved else "NOT_REQUIRED")
        or study["differing_versions"] != {} or study["oracle_audit_performed"] is not resolved
        or report["stored_qpy_decoded"] is not resolved
        or any(study[field] is not False for field in (
            "snapshot_simulation_performed", "provider_job_submitted", "quantum_hardware_used",
        ))
    ):
        raise ValueError("invalid overlap receipt study status or execution evidence")
    if not resolved:
        _expect(study["request"], None, "unneeded study request")
        return None

    request_fields = {"inputs", "required_rounds", "snapshots", "versions", "compiler_scope_sha256"}
    if isinstance(study["request"], dict) and "initial_layouts" in study["request"]:
        request_fields.add("initial_layouts")
    request = _record(study["request"], request_fields, "study request")
    input_fields = {"constraint_sha256", "search_qubits", "seed_transpiler", "validation_trials",
                    "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot"}
    if isinstance(request["inputs"], dict) and "prefer_native_qasm" in request["inputs"]:
        input_fields.add("prefer_native_qasm")
    inputs = _record(request["inputs"], input_fields, "study inputs")
    if "prefer_native_qasm" in inputs and inputs["prefer_native_qasm"] is not True:
        raise ValueError("overlap receipt prefer_native_qasm must be true when recorded")
    _sha256(inputs["constraint_sha256"], "recorded constraint SHA-256")
    _sha256(request["compiler_scope_sha256"], "recorded compiler scope SHA-256")
    width = _bounded_int(inputs["search_qubits"], 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "recorded search width")
    _bounded_int(inputs["seed_transpiler"], 0, 2**32 - 1, "recorded transpiler seed")
    _bounded_int(inputs["validation_trials"], 0, MAX_VALIDATION_TRIALS, "recorded validation trials")
    round_cap = _bounded_int(inputs["max_rounds_per_shot"], 0, MAX_ITERATIONS, "recorded max rounds per shot")
    if type(inputs["target_probability"]) is not float or not 0 < inputs["target_probability"] <= 1:
        raise ValueError("invalid overlap receipt target probability")
    shots = _selection_axis(inputs["shot_caps"], 1, MAX_SEARCH_SHOTS, "recorded shot caps")
    queries = _selection_axis(inputs["query_caps"], 0, MAX_ORACLE_QUERIES, "recorded query caps")
    _expect(inputs["shot_caps"], shots, "recorded shot caps")
    _expect(inputs["query_caps"], queries, "recorded query caps")
    windows = islice(_unknown_round_choices(width, round_cap), shots[-1])
    _expect(request["required_rounds"], list(range(min(max(windows), queries[-1] + 1))), "required rounds")
    _expect(request["versions"], {key: report["software_versions"][key] for key in _COMPILER_VERSIONS},
            "recorded compiler versions")
    snapshots = _record(request["snapshots"], {"earlier", "later"}, "study snapshots")
    for label, value in snapshots.items():
        snapshot = _record(
            value, {"backend_name", "backend_version", "qubits", "last_update_date", "configuration_sha256",
                    "properties_sha256"}, f"{label} snapshot",
        )
        if (
            not isinstance(snapshot["backend_name"], str) or re.fullmatch(r"ibmq?_[a-z0-9_]+", snapshot["backend_name"]) is None
            or not isinstance(snapshot["backend_version"], str) or not snapshot["backend_version"]
        ):
            raise ValueError(f"invalid overlap receipt {label} backend identity")
        _bounded_int(snapshot["qubits"], width, MAX_CALIBRATION_BACKEND_QUBITS, f"recorded {label} target width")
        _sha256(snapshot["configuration_sha256"], f"recorded {label} configuration SHA-256")
        _sha256(snapshot["properties_sha256"], f"recorded {label} properties SHA-256")
    earlier, later = snapshots["earlier"], snapshots["later"]
    if (
        any(earlier[key] != later[key] for key in ("backend_name", "qubits"))
        or earlier["properties_sha256"] == later["properties_sha256"]
        or _calibration_time(earlier["last_update_date"]) >= _calibration_time(later["last_update_date"])
    ):
        raise ValueError("overlap receipt snapshots must record distinct dates for the same device")
    if "initial_layouts" in request:
        recorded = request["initial_layouts"]
        if not isinstance(recorded, list) or not 1 <= len(recorded) <= MAX_LAYOUT_PROFILES:
            raise ValueError("overlap receipt ordered study layouts exceed their limits")
        layouts = [tuple(_wires(layout, later["qubits"], label="recorded study layout")) for layout in recorded]
        if (
            layouts != sorted(set(layouts))
            or len({len(layout) for layout in layouts}) != 1
            or not width <= len(layouts[0]) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
        ):
            raise ValueError("overlap receipt requires canonical distinct ordered study layouts of one width")
    return request


def _check_resolution(
    key: str, recorded: Any, source_rows: dict[str, dict[str, Any]], request: dict[str, Any],
) -> tuple[tuple[int, ...], int]:
    pins = list(source_rows)
    evidence = _record(
        recorded, {"status", "key", "source_sha256s", "selected_source_sha256", "differing_fields",
                   "sources", "stored_qpy_decoded", "initial_layout", "round"}, f"resolution for {key}",
    )
    _expect(
        {name: evidence[name] for name in ("status", "key", "source_sha256s", "selected_source_sha256",
                                          "differing_fields", "stored_qpy_decoded")},
        {"status": "EQUIVALENT_PHYSICAL_CIRCUITS", "key": key, "source_sha256s": pins,
         "selected_source_sha256": pins[0], "differing_fields": [], "stored_qpy_decoded": True},
        f"resolved selection for {key}",
    )
    later = request["snapshots"]["later"]
    layout = _wires(evidence["initial_layout"], later["qubits"], label="recorded overlap layout")
    if not request["inputs"]["search_qubits"] <= len(layout) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        raise ValueError("recorded overlap layout differs from the study width")
    if "initial_layouts" in request and layout not in request["initial_layouts"]:
        raise ValueError("recorded overlap layout is outside the recorded study layouts")
    round_index = _bounded_int(evidence["round"], 0, request["required_rounds"][-1], "recorded overlap round")
    compilation = source_rows[pins[0]]["report"]
    _recorded_calibration_importer(compilation.get("openqasm"), request["inputs"].get("prefer_native_qasm", False))
    target = compilation.get("target")
    _expect(compilation.get("initial_logical_to_target_qubit"), layout, f"recorded layout for {key}")
    _expect(compilation.get("seed_transpiler"), request["inputs"]["seed_transpiler"], f"recorded seed for {key}")
    if not isinstance(target, dict):
        raise ValueError("recorded overlap source compilation is missing its target")
    _expect({name: target.get(name) for name in ("kind", "name", "qubits")},
            {"kind": "BACKEND_V2", "name": later["backend_name"], "qubits": later["qubits"]},
            f"recorded target for {key}")

    sources = _record(evidence["sources"], set(pins), f"resolved sources for {key}")
    signatures = set()
    for pin, row in source_rows.items():
        record = _record(sources[pin], {"status", "qpy_sha256", "physical_signature_sha256", "stored_qpy_decoded", "error"},
                         f"resolved source {pin}")
        raw_qpy = b64decode(row["qpy"], validate=True)
        if not 1 <= len(raw_qpy) <= _MAX_QPY_BYTES:
            raise ValueError("overlap receipt source QPY payload exceeds its byte limits")
        signature = _sha256(record["physical_signature_sha256"], "recorded physical signature SHA-256")
        signatures.add(signature)
        _expect(record, {"status": "RESTORED", "qpy_sha256": sha256(raw_qpy).hexdigest(),
                         "physical_signature_sha256": signature, "stored_qpy_decoded": True, "error": None},
                f"QPY byte digest and recorded restore for {pin}")
    if len(signatures) != 1:
        raise ValueError(f"recorded physical signatures differ for overlap {key}")
    return tuple(layout), round_index


def _read_verified_overlap_receipt(
    receipt: str | Path, *, expected_receipt_sha256: str, output_bundle: str | Path, expected_output_sha256: str,
    sources: Sequence[tuple[str | Path, str]],
) -> _VerifiedOverlapReceipt:
    report = _read_receipt(Path(receipt), expected_receipt_sha256)
    _sha256(expected_output_sha256, "expected composed output SHA-256")
    if (not isinstance(sources, Sequence) or isinstance(sources, (str, bytes))
            or not 2 <= len(sources) <= _MAX_COMPOSITION_SOURCES):
        raise ValueError(f"overlap receipt verification requires 2 to {_MAX_COMPOSITION_SOURCES} pinned sources")
    paths = {}
    for source in sources:
        if (not isinstance(source, (tuple, list)) or len(source) != 2
                or not isinstance(source[0], (str, Path))):
            raise ValueError("each overlap receipt source requires a path and SHA-256 pin")
        path, pin = source
        _sha256(pin, "expected overlap source SHA-256")
        if pin in paths:
            raise ValueError("overlap receipt requires distinct source SHA-256 pins")
        paths[pin] = Path(path)

    source_reports = []
    selected = {}
    source_pins: dict[str, list[str]] = {}
    resolved_rows: dict[str, dict[str, dict[str, Any]]] = {}
    differing_qpy: set[str] = set()
    versions = None
    output_size = 0
    for pin, path in sorted(paths.items()):
        bundle = _read_bundle_document(path, pin, require_current_software=False)
        if versions is None:
            versions = bundle["software_versions"]
            output_size = len(_bundle_bytes(versions, {}))
        else:
            _expect(bundle["software_versions"], versions, f"recorded stack for {pin}")
        source_reports.append({"sha256": pin, "entry_count": len(bundle["entries"])})
        for key, entry in bundle["entries"].items():
            if key in report["overlap_verification"]:
                resolved_rows.setdefault(key, {})[pin] = entry
            if key in selected:
                previous = selected[key]
                _expect(entry["global_phase_hex"], previous["global_phase_hex"], f"source global phase for {key}")
                _expect(entry["report"], previous["report"], f"source compilation report for {key}")
                if entry["qpy"] != previous["qpy"]:
                    differing_qpy.add(key)
                source_pins[key].append(pin)
                continue
            output_size += len(key) + 3 + len(_json_bytes(entry)) + (1 if selected else 0)
            if len(selected) == _MAX_ENTRIES or output_size > _MAX_BUNDLE_BYTES:
                raise ValueError("reconstructed overlap receipt output exceeds bundle limits")
            selected[key] = entry
            source_pins[key] = [pin]
        del bundle

    _expect(report["sources"], source_reports, "source inventory")
    _expect(report["software_versions"], versions, "recorded source software")
    _expect(report["unique_entry_count"], len(selected), "union entry count")
    overlaps = {key: pins for key, pins in source_pins.items() if len(pins) > 1 and key not in differing_qpy}
    _expect(report["overlaps"], overlaps, "exact source overlaps")
    _expect(report["duplicate_entry_count"], sum(len(pins) - 1 for pins in overlaps.values()), "duplicate entry count")
    _expect(sorted(report["overlap_verification"]), sorted(differing_qpy), "resolved overlap keys")
    request = _study_request(report, bool(differing_qpy))
    if request is not None:
        cells = set()
        for key in sorted(differing_qpy):
            cell = _check_resolution(key, report["overlap_verification"][key], resolved_rows[key], request)
            if cell in cells:
                raise ValueError("overlap receipt assigns multiple keys to one requested layout and round")
            cells.add(cell)

    output_entries = dict(sorted(selected.items()))
    reconstructed = _bundle_bytes(versions, output_entries)
    digest = sha256(reconstructed).hexdigest()
    _expect(expected_output_sha256, digest, "composed output SHA-256")
    output = {"sha256": digest, "bytes": len(reconstructed), "entry_count": len(selected)}
    _expect(report["output"], output, "composed output identity")
    _verify_artifact(Path(output_bundle), {"sha256": digest, "bytes": len(reconstructed)}, capture=False)
    return _VerifiedOverlapReceipt(
        report, expected_receipt_sha256, digest, len(reconstructed), resolved_rows, output_entries,
    )


def verify_snapshot_calibration_overlap_receipt(
    receipt: str | Path, *, expected_receipt_sha256: str, output_bundle: str | Path, expected_output_sha256: str,
    sources: Sequence[tuple[str | Path, str]],
) -> dict[str, Any]:
    """Check composition bytes and recorded overlap evidence with standard-library Python."""
    verified = _read_verified_overlap_receipt(
        receipt, expected_receipt_sha256=expected_receipt_sha256, output_bundle=output_bundle,
        expected_output_sha256=expected_output_sha256, sources=sources,
    )
    report = verified.report
    request = report["study_overlap"]["request"]
    return {
        "schema_version": 1, "status": "SNAPSHOT_CALIBRATION_OVERLAP_RECEIPT_VERIFIED",
        "receipt_sha256": verified.receipt_sha256, "output": report["output"], "sources": report["sources"],
        "software_versions": report["software_versions"], "unique_entry_count": report["unique_entry_count"],
        "duplicate_entry_count": report["duplicate_entry_count"], "resolved_key_count": len(verified.resolved_rows),
        "resolved_keys": sorted(verified.resolved_rows), "study_overlap": report["study_overlap"],
        "recorded_layouts_present": request is not None and "initial_layouts" in request,
        "qpy_decoded": False, "physical_replay_performed": False, "snapshot_simulation_performed": False,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
