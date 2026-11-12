"""Persist trusted, SHA-256-pinned compiled rounds for offline dated studies."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import struct
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from numbers import Real
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from .compilation import (
    MAX_CIRCUIT_INSTRUCTIONS, MEASUREMENT_TOLERANCE, CompiledProgram,
    _DEFAULT_OPTIMIZATION_LEVEL, _NATIVE_QASM_IMPORTER, _check_compiled_target,
    _compact_wires, _compilation_report, _distribution, _verification_report,
)
from .constraints import _unique_object
from .replay_receipt import _SOFTWARE_PACKAGES, _software_versions
from .unknown_count_snapshot_cache import _json_bytes

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2


_MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_MAX_QPY_BYTES = 1024 * 1024
_MAX_ENCODED_QPY = 4 * ((_MAX_QPY_BYTES + 2) // 3)
_MAX_ENTRIES = 256
_MAX_COMPOSITION_SOURCES = 8
_SHA256 = re.compile("[0-9a-f]{64}")
_QPY_HEADER_PREFIX = struct.Struct("!6sBBBBQ")
_BUNDLE_SOFTWARE_PACKAGES = ("rustworkx", "openqasm3", "antlr4-python3-runtime")
_STANDARD_QASM_IMPORTER = "qiskit.qasm3.loads"
_OverlapResolver = Callable[
    [dict[str, dict[str, dict[str, Any]]], dict[str, str | None]], dict[str, dict[str, Any]],
]


def _bundle_software_versions() -> dict[str, str | None]:
    versions = _software_versions()
    for dependency in _BUNDLE_SOFTWARE_PACKAGES:
        try:
            versions[dependency] = version(dependency)
        except PackageNotFoundError:
            versions[dependency] = None
    return versions


def _write_bundle(
    path: Path, programs: dict[str, CompiledProgram], restored: dict[str, dict[str, Any]],
) -> str:
    if not programs:
        raise ValueError("compilation bundle requires at least one completed round")

    from qiskit import qpy

    entries = {}
    for key, program in programs.items():
        if key in restored:
            # QPY can reserialize an equivalent layout with different bytes.
            # Keep the exact pinned row after the current capture verifies it.
            entries[key] = restored[key]
            continue
        payload = BytesIO()
        qpy.dump(program.circuit, payload)
        if payload.tell() > _MAX_QPY_BYTES:
            raise ValueError("compiled round exceeds the QPY bundle size limit")
        entries[key] = {
            "qpy": base64.b64encode(payload.getbuffer()).decode("ascii"),
            "global_phase_hex": float(program.circuit.global_phase).hex(),
            "report": program.report,
        }
    return _publish_bundle(path, _bundle_bytes(_bundle_software_versions(), entries))


def _bundle_bytes(versions: dict[str, str | None], entries: dict[str, Any]) -> bytes:
    document = {"schema_version": 1, "software_versions": versions, "entries": entries}
    return (json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _publish_bundle(path: Path, raw: bytes) -> str:
    if len(raw) > _MAX_BUNDLE_BYTES:
        raise ValueError("compilation bundle exceeds its size limit")

    # Publish completed bytes without replacing an existing artifact, even if
    # another writer claims the destination after the caller's preflight.
    with NamedTemporaryFile(dir=path.parent, prefix=".compilation-bundle-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.link(temporary, path)
        finally:
            stream.close()
            temporary.unlink()
    return hashlib.sha256(raw).hexdigest()


def _read_bundle_document(
    path: Path, expected_sha256: str, *, require_current_software: bool,
) -> dict[str, Any]:
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("compilation bundle requires a lowercase SHA-256 pin")
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular compilation bundle file")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_BUNDLE_BYTES + 1)
    if len(raw) > _MAX_BUNDLE_BYTES:
        raise ValueError("compilation bundle exceeds its size limit")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("compilation bundle does not match the expected SHA-256 digest")

    try:
        bundle = json.loads(raw.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_object(pairs, "bundle"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid compilation bundle JSON") from exc
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "software_versions", "entries"}:
        raise ValueError("invalid compilation bundle fields")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("unsupported compilation bundle schema version")
    versions = bundle["software_versions"]
    if require_current_software:
        if versions != _bundle_software_versions():
            raise ValueError("compilation bundle software versions differ from the current environment")
    elif (
        not isinstance(versions, dict)
        or set(versions) != {"python", "austins_algorithm", *_SOFTWARE_PACKAGES, *_BUNDLE_SOFTWARE_PACKAGES}
        or any(value is not None and (not isinstance(value, str) or not value) for value in versions.values())
        or versions["python"] is None or versions["austins_algorithm"] is None
    ):
        raise ValueError("invalid recorded compilation bundle software versions")
    entries = bundle["entries"]
    if not isinstance(entries, dict) or not 1 <= len(entries) <= _MAX_ENTRIES:
        raise ValueError("compilation bundle round count is outside its limit")
    for key, entry in entries.items():
        if (
            _SHA256.fullmatch(key) is None or not isinstance(entry, dict)
            or set(entry) != {"qpy", "global_phase_hex", "report"}
            or not isinstance(entry["qpy"], str) or not 1 <= len(entry["qpy"]) <= _MAX_ENCODED_QPY
            or not isinstance(entry["global_phase_hex"], str) or not isinstance(entry["report"], dict)
        ):
            raise ValueError("invalid compilation bundle round entry")
        # Reject nonfinite report values and excessively nested input before SDK decoding.
        try:
            _json_bytes(entry["report"])
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid compilation bundle round report") from exc
    return bundle


def _read_bundle(path: Path, expected_sha256: str) -> dict[str, dict[str, Any]]:
    return _read_bundle_document(path, expected_sha256, require_current_software=True)["entries"]


def _bundle_key_inventory(path: str | Path, expected_sha256: str) -> tuple[dict[str, str | None], frozenset[str]]:
    """Keep only the recorded software and keys from a checked bundle envelope."""
    bundle = _read_bundle_document(Path(path), expected_sha256, require_current_software=False)
    return bundle["software_versions"], frozenset(bundle["entries"])


def inspect_snapshot_calibration_compilation_bundle(
    path: str | Path, *, expected_sha256: str,
) -> dict[str, Any]:
    """Inventory a pinned bundle's envelope and recorded compile metadata without the SDK."""
    bundle = _read_bundle_document(Path(path), expected_sha256, require_current_software=False)
    entries = {}
    for key, entry in sorted(bundle["entries"].items()):
        report = entry["report"]
        layout = report.get("initial_logical_to_target_qubit")
        seed = report.get("seed_transpiler")
        export = report.get("openqasm")
        digest = export.get("sha256") if isinstance(export, dict) else None
        entries[key] = {
            "recorded_initial_layout": layout if isinstance(layout, list) and layout and all(
                type(wire) is int and wire >= 0 for wire in layout
            ) else None,
            "recorded_seed_transpiler": seed if type(seed) is int and 0 <= seed <= 2**32 - 1 else None,
            "recorded_openqasm_sha256": digest if isinstance(digest, str) and _SHA256.fullmatch(digest) else None,
        }
    return {
        "schema_version": bundle["schema_version"],
        "status": "SNAPSHOT_CALIBRATION_COMPILATION_BUNDLE_INSPECTED",
        "bundle_sha256": expected_sha256,
        "software_versions": bundle["software_versions"],
        "entry_count": len(entries),
        "entry_keys": list(entries),
        "entries": entries,
        "qpy_decoded": False,
        "runtime_coverage_checked": False,
    }


def compose_snapshot_calibration_compilation_bundles(
    destination: str | Path, *, bundles: Sequence[tuple[str | Path, str]],
) -> dict[str, Any]:
    """Publish the exact union of independently pinned, compatible bundle rows."""
    return _compose_bundles(destination, bundles)


def _compose_bundles(
    destination: str | Path, bundles: Sequence[tuple[str | Path, str]], *,
    resolve_conflicts: _OverlapResolver | None = None,
) -> dict[str, Any]:
    if (not isinstance(bundles, Sequence) or isinstance(bundles, (str, bytes))
            or not 2 <= len(bundles) <= _MAX_COMPOSITION_SOURCES):
        raise ValueError(f"compilation bundle composition requires 2 to {_MAX_COMPOSITION_SOURCES} bundles")
    sources = {}
    for candidate in bundles:
        if (not isinstance(candidate, (tuple, list)) or len(candidate) != 2
                or not isinstance(candidate[0], (str, Path)) or not isinstance(candidate[1], str)
                or _SHA256.fullmatch(candidate[1]) is None):
            raise ValueError("each composition source requires a path and lowercase SHA-256 pin")
        path, pin = candidate
        if pin in sources:
            raise ValueError("compilation bundle composition requires distinct source SHA-256 pins")
        sources[pin] = Path(path)

    output_path = Path(destination)
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"compilation bundle already exists: {output_path}")

    software_versions = None
    output_bytes = 0
    source_reports = []
    entries = {}
    entry_sources: dict[str, list[str]] = {}
    differences: dict[str, set[str]] = {}
    alternate_qpy: dict[str, dict[str, str]] = {}
    for pin, path in sorted(sources.items()):
        bundle = _read_bundle_document(path, pin, require_current_software=False)
        recorded = bundle["software_versions"]
        if software_versions is None:
            software_versions = recorded
            output_bytes = len(_bundle_bytes(software_versions, {}))
        elif recorded != software_versions:
            raise ValueError(
                f"compilation bundles {source_reports[0]['sha256']} and {pin} have different recorded software versions"
            )
        source_reports.append({"sha256": pin, "entry_count": len(bundle["entries"])})
        for key, entry in bundle["entries"].items():
            if key in entries:
                previous = entries[key]
                changed = {field for field in ("qpy", "global_phase_hex") if entry[field] != previous[field]}
                if _json_bytes(entry["report"]) != _json_bytes(previous["report"]):
                    changed.add("report")
                if changed:
                    all_changes = differences.setdefault(key, set())
                    all_changes.update(changed)
                    if resolve_conflicts is not None:
                        if all_changes == {"qpy"}:
                            alternate_qpy.setdefault(key, {})[pin] = entry["qpy"]
                        else:
                            alternate_qpy.pop(key, None)
                entry_sources[key].append(pin)
                continue

            if len(entries) == _MAX_ENTRIES:
                raise ValueError("composed compilation bundle round count exceeds its limit")
            # Each new member adds its quoted key, a colon, the compact row,
            # and a comma when the entries object is already nonempty.
            output_bytes += len(key) + 3 + len(_json_bytes(entry)) + (1 if entries else 0)
            if output_bytes > _MAX_BUNDLE_BYTES:
                raise ValueError("composed compilation bundle exceeds its size limit")
            entries[key] = entry
            entry_sources[key] = [pin]
        del bundle

    conflicts = {
        key: {"source_sha256s": entry_sources[key], "differing_fields": sorted(fields)}
        for key, fields in sorted(differences.items())
    }
    overlaps = {
        key: pins for key, pins in sorted(entry_sources.items())
        if len(pins) > 1 and key not in conflicts
    }
    overlap_verification = {}
    if resolve_conflicts is not None and alternate_qpy:
        # Keep alternate encoded payloads only for QPY-only conflicts. The
        # report and phase values already match exactly under canonical JSON.
        eligible = {
            key: {
                pin: {**entries[key], "qpy": alternates.get(pin, entries[key]["qpy"])}
                for pin in entry_sources[key]
            }
            for key, alternates in sorted(alternate_qpy.items())
        }
        overlap_verification = resolve_conflicts(eligible, software_versions)
        for key, evidence in overlap_verification.items():
            if key not in eligible:
                raise RuntimeError("compilation overlap verification returned an unexpected key")
            if evidence["status"] == "EQUIVALENT_PHYSICAL_CIRCUITS":
                if evidence["selected_source_sha256"] != entry_sources[key][0]:
                    raise RuntimeError("compilation overlap verification changed the selected source")
                del conflicts[key]
    report = {
        "schema_version": 1,
        "status": "COMPILATION_BUNDLE_COMPOSITION_CONFLICT" if conflicts else "COMPILATION_BUNDLE_COMPOSED",
        "sources": source_reports,
        "software_versions": software_versions,
        "unique_entry_count": len(entries),
        "duplicate_entry_count": sum(len(pins) - 1 for pins in overlaps.values()),
        "overlaps": overlaps,
        "conflicts": conflicts,
        "output": None,
        "stored_qpy_decoded": False,
        "runtime_coverage_checked": False,
    }
    if resolve_conflicts is not None:
        decode_states = {row.get("stored_qpy_decoded", False) for row in overlap_verification.values()}
        report["stored_qpy_decoded"] = True if True in decode_states else None if None in decode_states else False
        report["overlap_verification"] = overlap_verification
    if not conflicts:
        raw = _bundle_bytes(software_versions, dict(sorted(entries.items())))
        pin = _publish_bundle(output_path, raw)
        report["output"] = {"sha256": pin, "bytes": len(raw), "entry_count": len(entries)}
    return report


def _load_physical_circuit(encoded: str) -> QuantumCircuit:
    from qiskit import QuantumCircuit, qpy

    try:
        raw = base64.b64decode(encoded, validate=True)
        magic, version, _, _, _, count = _QPY_HEADER_PREFIX.unpack_from(raw)
    except (ValueError, struct.error) as exc:
        raise ValueError("invalid compilation bundle QPY encoding") from exc
    if len(raw) > _MAX_QPY_BYTES or magic != b"QISKIT" or version != qpy.QPY_VERSION or count != 1:
        raise ValueError("compilation bundle requires one bounded circuit in the current QPY format")
    try:
        with BytesIO(raw) as stream:
            circuits = qpy.load(stream)
            if stream.tell() != len(raw):
                raise ValueError("unexpected trailing QPY bytes")
    except (qpy.QpyError, ValueError, TypeError, RuntimeError, OverflowError, EOFError, struct.error) as exc:
        raise ValueError("could not decode compilation bundle QPY") from exc
    if len(circuits) != 1 or not isinstance(circuits[0], QuantumCircuit):
        raise ValueError("compilation bundle QPY did not contain one circuit")
    return circuits[0]


def _recorded_calibration_importer(export: Any, prefer_native_qasm: bool) -> str:
    """Validate recorded importer identity and preference without loading the SDK."""
    importer = export.get("importer") if isinstance(export, dict) else None
    if importer not in (_STANDARD_QASM_IMPORTER, _NATIVE_QASM_IMPORTER):
        raise ValueError("invalid recorded calibration OpenQASM importer")
    if (
        prefer_native_qasm and export.get("preferred_importer") != _NATIVE_QASM_IMPORTER
        or not prefer_native_qasm and "preferred_importer" in export
    ):
        raise ValueError("recorded calibration OpenQASM importer preference differs from the request")
    return importer


def _restore_compiled_round(
    entry: dict[str, Any], logical: QuantumCircuit, backend: BackendV2, layout: tuple[int, ...], seed: int, *,
    prefer_native_qasm: bool = False,
) -> CompiledProgram:
    from importlib.util import find_spec

    from qiskit import qasm3
    from qiskit.circuit import Barrier, Gate, Measure

    recorded = entry["report"]
    importer = _recorded_calibration_importer(recorded.get("openqasm"), prefer_native_qasm)
    standard_available = find_spec("qiskit_qasm3_import") is not None
    if importer == _STANDARD_QASM_IMPORTER:
        available = standard_available
    else:
        available = getattr(qasm3, "loads_experimental", None) is not None and (
            prefer_native_qasm or not standard_available
        )
    if not available:
        raise ValueError("recorded calibration OpenQASM importer is incompatible with the current request")

    compiled = _load_physical_circuit(entry["qpy"])
    target = backend.target
    if (
        compiled.num_qubits != target.num_qubits or compiled.num_clbits != logical.num_clbits
        or len(compiled.data) > MAX_CIRCUIT_INSTRUCTIONS or compiled.num_parameters
    ):
        raise ValueError("compilation bundle circuit differs from the requested physical dimensions or limits")
    phase = float(compiled.global_phase)
    if not math.isfinite(phase) or phase.hex() != entry["global_phase_hex"]:
        raise ValueError("compilation bundle circuit global phase differs from its evidence")
    initial_mapping = _check_compiled_target(compiled, target, list(layout))
    for item in compiled.data:
        operation = item.operation
        if type(operation) is Barrier:
            continue
        if (
            not isinstance(operation, (Gate, Measure))
            or operation.base_class is not target.operation_from_name(operation.name).base_class
            or any(not isinstance(value, Real) or not math.isfinite(value) for value in operation.params)
        ):
            raise ValueError("compilation bundle requires bound stock target instructions")

    original, _ = _distribution(_compact_wires(logical)[0])
    portable, active = _compact_wires(compiled)
    actual, measured = _distribution(portable)
    # The caller's trusted pin binds the earlier importer result. Recheck the
    # original and physical probabilities and regenerate the exact export here.
    evidence = recorded.get("verification")
    roundtrip = evidence.get("roundtrip_probabilities") if isinstance(evidence, dict) else None
    if (
        not isinstance(roundtrip, dict) or not roundtrip
        or any(len(bits) != logical.num_clbits or set(bits) - {"0", "1"} for bits in roundtrip)
        or any(type(value) is not float or not 0.0 <= value <= 1.0 + MEASUREMENT_TOLERANCE
               for value in roundtrip.values())
        or not math.isclose(math.fsum(roundtrip.values()), 1.0, rel_tol=0.0, abs_tol=MEASUREMENT_TOLERANCE)
    ):
        raise ValueError("invalid compilation bundle OpenQASM probability evidence")
    verification = _verification_report(original, actual, roundtrip.copy())
    text = qasm3.dumps(portable, disable_constants=True)
    report = _compilation_report(
        logical, compiled, target, backend=backend, seed=seed, optimization_level=_DEFAULT_OPTIMIZATION_LEVEL,
        initial_mapping=initial_mapping, text=text, importer=importer, active=active, measured=measured,
        verification=verification,
    )
    if prefer_native_qasm:
        report["openqasm"]["preferred_importer"] = _NATIVE_QASM_IMPORTER
    if _json_bytes(report) != _json_bytes(recorded):
        raise ValueError("compilation bundle verification differs from the recorded round report")
    return CompiledProgram(compiled, text, report)
