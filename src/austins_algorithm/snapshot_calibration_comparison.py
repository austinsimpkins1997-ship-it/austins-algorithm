"""Compare archived IBM calibrations with one verified circuit per round."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version
from importlib.util import find_spec
from itertools import groupby, islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .compilation import compile_and_verify
from .constraint_oracle import build_constraint_phase_oracle
from .constraints import BooleanConstraint, _unique_object
from .core import _bounded_int
from .grover_search import audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, _MAX_DENSITY_BATCH_ROUNDS, _PREPARED_DENSITY_FUSION_OPTIONS,
    _StaticSnapshotChannelCache, _compact_snapshot_inputs, _local_limits,
    _prepare_density_snapshot, _prepare_density_snapshot_batch, _prepared_snapshot_probabilities,
)
from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from .unknown_count_snapshot import _initial_layout
from .unknown_count_snapshot_cache import _compilation_scope, _json_bytes, _round_profile_key

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_ibm_runtime.fake_provider.fake_backend import FakeBackendV2

    from .compilation import CompiledProgram


MAX_CALIBRATION_FILE_BYTES = 4 * 1024 * 1024
MAX_CALIBRATION_BACKEND_QUBITS = 256
CALIBRATION_MASS_TOLERANCE = 1e-9
CALIBRATION_COMPARISON = "MATCHED_CIRCUITS_ON_TWO_ARCHIVED_IBM_SNAPSHOTS"
CALIBRATION_NOISE_METHOD = "AER_COMPACT_STATIC_DENSITY_MATRIX_LOCAL_READOUT"


@dataclass(frozen=True)
class SnapshotCalibrationFiles:
    """Local IBM configuration and properties JSON files with caller-provided SHA-256 pins."""

    configuration: str | Path
    properties: str | Path
    configuration_sha256: str
    properties_sha256: str


@dataclass(frozen=True)
class _PreparedCalibrationComparison:
    layouts: tuple[tuple[int, ...], ...]
    oracle: QuantumCircuit
    backends: dict[str, FakeBackendV2]
    scope: dict[str, Any]
    static_channels: dict[str, _StaticSnapshotChannelCache]
    compilation_cache: SnapshotCalibrationCompilationCache | None = None
    compilation_key_scope: bytes | None = None
    batch_density: bool = False
    density_fusion: bool = False


@dataclass
class _PendingCalibrationFusion:
    prepared: _PreparedCalibrationComparison
    report: dict[str, Any] | None
    compact_by_date: dict[str, dict[int, tuple[QuantumCircuit, AerSimulator]]]


def _finite_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("snapshot calibration JSON requires finite numbers")
    return value


def _read_archive(path: str | Path, expected: str, label: str) -> dict[str, Any]:
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError(f"{label} requires a lowercase SHA-256 digest")
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_CALIBRATION_FILE_BYTES + 1)
    if len(raw) > MAX_CALIBRATION_FILE_BYTES:
        raise ValueError(f"{label} exceeds {MAX_CALIBRATION_FILE_BYTES} bytes")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"{label} does not match the expected SHA-256 digest")
    try:
        document = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=lambda pairs: _unique_object(pairs, label),
            parse_float=_finite_json_float, parse_constant=_finite_json_float,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{label} must be a finite UTF-8 JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _calibration_time(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            time = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if time.utcoffset() is not None:
                return time
        except ValueError:
            pass
    raise ValueError("last_update_date must be a timezone-aware calibration timestamp")


def _load_snapshot(
    files: SnapshotCalibrationFiles, label: str,
) -> tuple[dict[str, Any], dict[str, Any], datetime, dict[str, Any]]:
    if not isinstance(files, SnapshotCalibrationFiles):
        raise TypeError(f"{label} must be SnapshotCalibrationFiles")
    config = _read_archive(files.configuration, files.configuration_sha256, f"{label} configuration")
    props = _read_archive(files.properties, files.properties_sha256, f"{label} properties")
    name = config.get("backend_name")
    if (
        not isinstance(name, str) or not re.fullmatch(r"ibmq?_[a-z0-9_]+", name)
        or props.get("backend_name") != name or config.get("simulator") is not False
    ):
        raise ValueError(f"{label} configuration and properties must identify the same IBM hardware backend")
    backend_version = config.get("backend_version")
    if not isinstance(backend_version, str) or not backend_version or props.get("backend_version") != backend_version:
        raise ValueError(f"{label} configuration and properties must share one backend version")
    width = _bounded_int(config.get("n_qubits"), 1, MAX_CALIBRATION_BACKEND_QUBITS, f"{label} backend width")
    qubits = props.get("qubits")
    if not isinstance(qubits, list) or len(qubits) != width:
        raise ValueError(f"{label} configuration width disagrees with the properties qubit count")
    date = _calibration_time(props.get("last_update_date"))
    evidence = {
        "backend_name": name, "backend_version": backend_version, "qubits": width,
        "last_update_date": date.isoformat(), "configuration_sha256": files.configuration_sha256,
        "properties_sha256": files.properties_sha256,
    }
    return config, props, date, evidence


def _offline_backend(config: dict[str, Any], properties: dict[str, Any]):
    from qiskit_ibm_runtime.fake_provider.fake_backend import FakeBackendV2

    class ArchivedBackend(FakeBackendV2):
        backend_name = config["backend_name"]
        conf_filename = "configuration.json"
        props_filename = "properties.json"

        def _load_json(self, filename: str) -> dict[str, Any]:
            # Runtime decodes this dictionary in place. Keep the validated input intact.
            return deepcopy({self.conf_filename: config, self.props_filename: properties}[filename])

    try:
        backend = ArchivedBackend()
        width = backend.num_qubits
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"could not decode the archived IBM calibration target: {exc}") from exc
    if width != config["n_qubits"]:
        raise ValueError("decoded IBM target width differs from the archived configuration")
    return backend


def _program_calibration(program, backend) -> dict[str, Any]:
    circuit, target = program.circuit, backend.target
    physical_wires = program.report["openqasm"]["export_wire_to_target_qubit"]
    qubits = {}
    for wire in physical_wires:
        props = None if target.qubit_properties is None else target.qubit_properties[wire]
        if props is None or any(
            value is None or not math.isfinite(value) or value <= 0 for value in (props.t1, props.t2)
        ):
            raise ValueError(f"snapshot calibration is missing usable T1/T2 on wire {wire}")
        qubits[str(wire)] = {"t1_seconds": props.t1, "t2_seconds": props.t2}

    operations = {}
    for item in circuit.data:
        op = item.operation
        if op.name == "barrier":
            continue
        wires = tuple(circuit.find_bit(bit).index for bit in item.qubits)
        if not target.instruction_supported(operation_name=op.name, qargs=wires, parameters=op.params):
            raise ValueError(f"snapshot target does not support compiled {op.name} on wires {wires}")
        props = target[op.name].get(wires)
        if (
            props is None or props.error is None or not math.isfinite(props.error) or not 0 <= props.error <= 1
            or props.duration is None or not math.isfinite(props.duration) or props.duration < 0
        ):
            raise ValueError(f"snapshot calibration is missing usable error or duration for {op.name} on wires {wires}")
        operations[op.name, wires] = {"operation": op.name, "wires": list(wires), "error": props.error,
                                      "duration_seconds": props.duration}
    return {"qubits": qubits, "operations": [operations[key] for key in sorted(operations)]}


def _distribution_difference(
    earlier: dict[str, float], later: dict[str, float], witnesses: set[str],
) -> dict[str, Any]:
    if not earlier or earlier.keys() != later.keys() or not witnesses.issubset(earlier):
        raise ValueError("snapshot distributions must share all outcomes and contain the audited witnesses")
    outcomes = sorted(earlier)
    result = {}
    for label, distribution in (("earlier", earlier), ("later", later)):
        if any(not math.isfinite(value) or value < 0 for value in distribution.values()):
            raise ValueError("snapshot outcome probabilities must be finite and nonnegative")
        mass = math.fsum(distribution.values())
        if not math.isclose(mass, 1.0, rel_tol=0, abs_tol=CALIBRATION_MASS_TOLERANCE):
            raise ValueError("snapshot outcome probabilities must have unit mass")
        probabilities = {bits: distribution[bits] / mass for bits in outcomes}
        result[label] = {
            "outcome_probabilities": probabilities, "input_probability_mass": mass,
            "verified_hit_probability": math.fsum(probabilities[bits] for bits in outcomes if bits in witnesses),
        }
    delta = {
        bits: result["later"]["outcome_probabilities"][bits] - result["earlier"]["outcome_probabilities"][bits]
        for bits in outcomes
    }
    result.update(
        right_minus_left_outcome_probabilities=delta,
        right_minus_left_hit_probability=math.fsum(delta[bits] for bits in outcomes if bits in witnesses),
        outcome_total_variation_distance=min(1.0, math.fsum(map(abs, delta.values())) / 2),
    )
    return result


def compare_snapshot_calibrations(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layout: Sequence[int], max_rounds: int = 1, seed_transpiler: int = 7,
    compilation_cache: SnapshotCalibrationCompilationCache | None = None, prefer_native_qasm: bool = False,
    batch_density: bool = False, density_fusion: bool = False,
) -> dict[str, Any]:
    """Compile or reuse each audited round on the later target and simulate it on both dates."""
    fusion_options = {"density_fusion": density_fusion} if density_fusion is not False else {}
    prepared = _prepare_calibration_comparison(
        constraint, earlier, later, initial_layouts=(initial_layout,), max_rounds=max_rounds,
        seed_transpiler=seed_transpiler, compilation_cache=compilation_cache, prefer_native_qasm=prefer_native_qasm,
        batch_density=batch_density, **fusion_options,
    )
    return _capture_calibration_layout(prepared, prepared.layouts[0])


def _prepare_calibration_comparison(
    constraint: BooleanConstraint, earlier: SnapshotCalibrationFiles, later: SnapshotCalibrationFiles, *,
    initial_layouts: Sequence[Sequence[int]], max_rounds: int, seed_transpiler: int,
    compilation_cache: SnapshotCalibrationCompilationCache | None = None, prefer_native_qasm: bool = False,
    batch_density: bool = False, density_fusion: bool = False,
) -> _PreparedCalibrationComparison:
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if compilation_cache is not None and not isinstance(compilation_cache, SnapshotCalibrationCompilationCache):
        raise TypeError("compilation_cache must be a SnapshotCalibrationCompilationCache")
    if type(prefer_native_qasm) is not bool:
        raise TypeError("prefer_native_qasm must be a boolean")
    if type(batch_density) is not bool:
        raise TypeError("batch_density must be a boolean")
    if type(density_fusion) is not bool:
        raise TypeError("density_fusion must be a boolean")
    width = _bounded_int(constraint.qubits, 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "search width")
    _bounded_int(max_rounds, 0, math.isqrt((1 << width) - 1), "max_rounds")
    _bounded_int(seed_transpiler, 0, 2**32 - 1, "seed_transpiler")
    early_config, early_props, early_date, early = _load_snapshot(earlier, "earlier")
    late_config, late_props, late_date, late = _load_snapshot(later, "later")
    if early["backend_name"] != late["backend_name"] or early["qubits"] != late["qubits"]:
        raise ValueError("calibration snapshots must identify the same IBM backend and width")
    if early_date >= late_date:
        raise ValueError("calibration timestamps must be strictly increasing")
    layouts = []
    for layout in initial_layouts:
        if not isinstance(layout, Sequence) or not width <= len(layout) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            raise ValueError("initial_layout must supply one physical wire per oracle wire within the active limit")
        layouts.append(tuple(_initial_layout(layout, len(layout), early["qubits"])))

    oracle = build_constraint_phase_oracle(constraint)
    if any(len(layout) != oracle.num_qubits for layout in layouts):
        raise ValueError("initial_layout requires one physical wire per oracle wire, including clause flags")
    audit = audit_phase_oracle(oracle, width, constraint.matches)
    backends = {"earlier": _offline_backend(early_config, early_props), "later": _offline_backend(late_config, late_props)}
    scope = {
        "seed_transpiler": seed_transpiler, "max_rounds": max_rounds,
        "constraint_sha256": constraint.digest(), "search_qubits": width, "audit": audit,
        "snapshots": {"earlier": early, "later": late},
        "configuration_changed": early["configuration_sha256"] != late["configuration_sha256"],
        "backend_version_changed": early["backend_version"] != late["backend_version"],
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")},
        "limits": _local_limits(),
    }
    if prefer_native_qasm:
        scope["prefer_native_qasm"] = True
    static_channels = (
        {label: _StaticSnapshotChannelCache(backend.target) for label, backend in backends.items()}
        if len(layouts) > 1 else {}
    )
    compilation_key_scope = None
    if compilation_cache is not None:
        from qiskit import qasm3

        compilation_key_scope = _json_bytes({
            **_compilation_scope(backends["later"], late, scope["versions"], seed_transpiler),
            "prefer_native_qasm": prefer_native_qasm,
            "purpose": "dated-calibration-compiled-round",
            "standard_importer_available": find_spec("qiskit_qasm3_import") is not None,
            "native_importer_available": hasattr(qasm3, "loads_experimental"),
        })
    return _PreparedCalibrationComparison(
        tuple(layouts), oracle, backends, scope, static_channels, compilation_cache, compilation_key_scope,
        batch_density=batch_density, density_fusion=density_fusion,
    )


def _calibration_round_key(
    prepared: _PreparedCalibrationComparison, layout: tuple[int, ...], circuit: QuantumCircuit,
) -> str | None:
    scope = prepared.compilation_key_scope
    return None if scope is None else _round_profile_key(circuit, layout, scope)


def _calibration_density_rounds(
    compact: Mapping[int, tuple[QuantumCircuit, AerSimulator]], backend_name: str, *,
    batch_density: bool, density_fusion: bool = False,
) -> Iterator[tuple[tuple[int, ...], list[tuple[QuantumCircuit, AerSimulator]] | None, dict[str, Any]]]:
    limit = _MAX_DENSITY_BATCH_ROUNDS if batch_density else 1
    fusion_options = {"density_fusion": True} if density_fusion else {}
    # Only adjacent rounds with the same compact physical wires and dated noise
    # share a simulator. A new layout or date always gets its own lane.
    for _, group in groupby(compact.items(), key=lambda item: id(item[1][1])):
        while rows := list(islice(group, limit)):
            indices = tuple(index for index, _ in rows)
            simulator = rows[0][1][1]
            circuits = [circuit for _, (circuit, _) in rows]
            if len(circuits) == 1:
                replay, preparation = _prepare_density_snapshot(circuits[0], simulator, backend_name, **fusion_options)
                yield indices, None if replay is None else [replay], preparation
            else:
                replays, preparation = _prepare_density_snapshot_batch(circuits, simulator, backend_name, **fusion_options)
                yield indices, replays, preparation


def _capture_calibration_layout(
    prepared: _PreparedCalibrationComparison, layout: tuple[int, ...], *,
    round_inputs: Sequence[tuple[QuantumCircuit, str | None]] | None = None,
    verified_programs: Mapping[str, CompiledProgram] | None = None,
) -> dict[str, Any]:
    report, programs, compiled = _compile_calibration_layout(
        prepared, layout, density_fusion=prepared.density_fusion,
        round_inputs=round_inputs, verified_programs=verified_programs,
    )
    if report["status"] == "PENDING":
        _simulate_calibration_layout(prepared, report, programs)
    if report["status"] == "COMPLETED" and prepared.compilation_cache is not None and compiled:
        prepared.compilation_cache._publish_completed(compiled)
    return report


def _capture_calibration_layout_reference(
    prepared: _PreparedCalibrationComparison, layout: tuple[int, ...], *,
    round_inputs: Sequence[tuple[QuantumCircuit, str | None]] | None = None,
    verified_programs: Mapping[str, CompiledProgram] | None = None,
) -> tuple[dict[str, Any], _PendingCalibrationFusion | None]:
    """Complete the default capture and retain only the setup needed by its fused arm."""
    report, programs, compiled = _compile_calibration_layout(
        prepared, layout, density_fusion=False, round_inputs=round_inputs, verified_programs=verified_programs,
    )
    if report["status"] != "PENDING":
        return report, None
    pending = _PendingCalibrationFusion(prepared, deepcopy(report), {})
    _simulate_calibration_layout(prepared, report, programs, compact_by_date=pending.compact_by_date)
    if report["status"] != "COMPLETED":
        return report, None
    if prepared.compilation_cache is not None and compiled:
        prepared.compilation_cache._publish_completed(compiled)
    return report, pending


def _capture_calibration_layout_fusion(
    prepared: _PreparedCalibrationComparison, pending: _PendingCalibrationFusion,
) -> dict[str, Any]:
    """Run the fused arm on the same verified circuits and compact simulators."""
    if pending.prepared is not prepared:
        raise ValueError("pending fusion capture belongs to a different calibration request")
    report, compact_by_date = pending.report, pending.compact_by_date
    if report is None:
        raise ValueError("pending fusion capture has already been consumed")
    if compact_by_date.keys() != prepared.backends.keys():
        raise RuntimeError("pending fusion capture requires every reference simulator")
    pending.report = None
    pending.compact_by_date = {}
    _record_requested_fusion(report, prepared)
    _simulate_calibration_layout(prepared, report, None, compact_by_date=compact_by_date)
    return report


def _capture_restored_calibration_layout_fusion(
    prepared: _PreparedCalibrationComparison, reference: dict[str, Any], programs: Mapping[int, CompiledProgram],
) -> dict[str, Any]:
    """Run the fused arm from verified physical rounds, rebuilding only its compact setup."""
    static_rounds = {
        key: {field: row[field] for field in ("compilation", "target_calibrations")}
        for key, row in reference["rounds"].items()
    }
    report = deepcopy({
        **reference, "status": "PENDING", "rounds": static_rounds,
        "maximum_absolute_hit_probability_difference": None,
    })
    _record_requested_fusion(report, prepared)
    _simulate_calibration_layout(prepared, report, programs)
    return report


def _record_requested_fusion(report: dict[str, Any], prepared: _PreparedCalibrationComparison) -> None:
    report["density_fusion"] = {
        "requested": dict(_PREPARED_DENSITY_FUSION_OPTIONS), "observed": {label: {} for label in prepared.backends},
    }


def _compile_calibration_layout(
    prepared: _PreparedCalibrationComparison, layout: tuple[int, ...], *, density_fusion: bool,
    round_inputs: Sequence[tuple[QuantumCircuit, str | None]] | None,
    verified_programs: Mapping[str, CompiledProgram] | None,
) -> tuple[dict[str, Any], dict[int, CompiledProgram], dict[str, CompiledProgram]]:
    report = {
        "schema_version": 1, "status": "PENDING", "execution_mode": "SIMULATOR",
        "comparison": CALIBRATION_COMPARISON, "noise_method": CALIBRATION_NOISE_METHOD,
        "compile_target": "later", "initial_layout": list(layout), **deepcopy(prepared.scope),
        "rounds": {}, "maximum_absolute_hit_probability_difference": None,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if prepared.batch_density:
        report["batch_density"] = True
    if density_fusion:
        _record_requested_fusion(report, prepared)
    max_rounds, width, seed_transpiler = report["max_rounds"], report["search_qubits"], report["seed_transpiler"]
    backends = prepared.backends

    programs = {}
    compiled = {}
    cache = prepared.compilation_cache
    prefer_native_qasm = prepared.scope.get("prefer_native_qasm", False)
    if round_inputs is not None and len(round_inputs) != max_rounds + 1:
        raise ValueError("prepared round inputs differ from the capture horizon")
    for rounds in range(max_rounds + 1):
        if round_inputs is None:
            circuit = build_grover_search_circuit(prepared.oracle, width, iterations=rounds)
            key = _calibration_round_key(prepared, layout, circuit)
        else:
            circuit, key = round_inputs[rounds]
        program = None if key is None or verified_programs is None else verified_programs.get(key)
        publish = program is not None
        if program is None and cache is not None and key is not None:
            program, publish = cache._get(
                key, circuit, backends["later"], layout, seed_transpiler, prefer_native_qasm=prefer_native_qasm,
            )
        if program is None:
            program = compile_and_verify(
                circuit, backend=backends["later"], initial_layout=layout, seed=seed_transpiler,
                prefer_native_qasm=prefer_native_qasm,
            )
        if key is not None and publish:
            compiled[key] = program
        programs[rounds] = program
        report["rounds"][str(rounds)] = {"compilation": deepcopy(program.report)}
        if len(program.report["openqasm"]["export_wire_to_target_qubit"]) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            report.update(status="COMPILE_ONLY_ACTIVE_QUBIT_LIMIT", blocked_round=rounds)
            break
        report["rounds"][str(rounds)]["target_calibrations"] = {
            label: _program_calibration(program, backend) for label, backend in backends.items()
        }
    return report, programs, compiled


def _simulate_calibration_layout(
    prepared: _PreparedCalibrationComparison, report: dict[str, Any], programs: Mapping[int, CompiledProgram] | None, *,
    compact_by_date: dict[str, dict[int, tuple[QuantumCircuit, AerSimulator]]] | None = None,
) -> None:
    from qiskit_aer import AerSimulator

    witnesses = set(report["audit"]["marked_states"])
    observed_fusion = report.get("density_fusion", {}).get("observed")
    fusion_options = {"density_fusion": True} if observed_fusion is not None else {}
    round_reports = report["rounds"]
    distributions, readout = {}, {}
    for label, backend in prepared.backends.items():
        distributions[label], readout[label] = {}, {}
        compact = None if compact_by_date is None else compact_by_date.get(label)
        if compact is None:
            if programs is None:
                raise RuntimeError("fusion capture requires a prebuilt compact simulator")
            compact = _compact_snapshot_inputs(
                backend, programs, AerSimulator, static_channels=prepared.static_channels.get(label),
                prune_unused_gate_noise=True,
            )
            if compact_by_date is not None:
                compact_by_date[label] = compact
        for indices, replays, preparation in _calibration_density_rounds(
            compact, backend.name, batch_density=prepared.batch_density, **fusion_options,
        ):
            if replays is None:
                report["status"] = preparation["status"]
                if len(indices) == 1:
                    report["blocked_round"] = indices[0]
                else:
                    report["blocked_rounds"] = list(indices)
                report.update(blocked_snapshot=label, local_job_cancelled=preparation["local_job_cancelled"])
                if "local_job_cancellation_error" in preparation:
                    report["local_job_cancellation_error"] = preparation["local_job_cancellation_error"]
                return
            if observed_fusion is not None:
                observed = (preparation.get("fusion"),) if len(indices) == 1 else preparation["fusion_by_experiment"]
                observed_fusion[label].update(
                    (str(rounds), fusion or None) for rounds, fusion in zip(indices, observed, strict=True)
                )
            for rounds, replay in zip(indices, replays, strict=True):
                probabilities = _prepared_snapshot_probabilities(*replay, verifier=witnesses.__contains__)
                distributions[label][rounds] = probabilities["outcome_probabilities"]
                physical = round_reports[str(rounds)]["compilation"]["openqasm"]["export_wire_to_target_qubit"]
                readout[label][rounds] = {
                    str(physical[wire]): channel["probabilities"]
                    for channel in replay[1].options.noise_model.to_dict()["errors"]
                    for (wire,) in channel["gate_qubits"]
                }

    for key, row in round_reports.items():
        rounds = int(key)
        row["readout_assignment_matrices"] = {label: channels[rounds] for label, channels in readout.items()}
        row.update(_distribution_difference(
            distributions["earlier"][rounds], distributions["later"][rounds], witnesses,
        ))
    report["maximum_absolute_hit_probability_difference"] = max(
        abs(row["right_minus_left_hit_probability"]) for row in round_reports.values()
    )
    report["status"] = "COMPLETED"

