"""Write and independently verify a local modulo-4 placement receipt."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from .compilation import (
    MAX_CIRCUIT_INSTRUCTIONS,
    MAX_EXACT_QUBITS,
    MEASUREMENT_TOLERANCE,
    _DEFAULT_OPTIMIZATION_LEVEL,
    _NATIVE_QASM_IMPORTER,
    _distance,
)
from .constraint_replay import _parse_request, _require_fields
from .core import _bounded_int, theoretical_success_probability
from .device_experiment import summarize_search_counts
from .grover_search import mod4_range_predicate
from .ibm_models import (
    IBM_SNAPSHOT_MODELS,
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
    MAX_MOD4_SWEEP_SHOTS,
    MAX_MPS_BOND_DIMENSION,
    _local_limits,
    _snapshot_payload,
    _wilson_interval,
)
from .placement_statistics import _placement_exact_interval
from .replay_receipt import _json_bytes, _read_verified_receipt, _write_replay_receipt
from .snapshot_placement import FAMILY_CONFIDENCE, ROUNDS, _alternate_placement, _validate_placement_sampling

if TYPE_CHECKING:
    from .snapshot_placement import _SnapshotPlacements


_REQUEST_FIELDS = {
    "schema_version", "workflow", "model", "minimum_hit_rate", "shots",
    "evaluation_seeds", "transpiler_seed", "prefer_native_qasm",
}
_SNAPSHOT_FIELDS = {"calibration", "sha256", "coupling", "initial_layout", "exports"}
_WORKFLOW = "MOD4_PLACEMENT_CHECK"
_STANDARD_IMPORTER = "qiskit.qasm3.loads"
_GATE_WIRES = {"id": 1, "x": 1, "sx": 1, "rz": 1, "cx": 2, "cz": 2}
_GATE = re.compile(r"([a-z][a-z0-9]*)(?:\(([^()]*)\))? (q\[\d+\](?:, q\[\d+\])*);")
_WIRE = re.compile(r"q\[(\d+)\]")
_MEASURE = re.compile(r"c\[(\d+)\] = measure q\[(\d+)\];")
_INTERVAL_TOLERANCE = 2e-11


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _expect(record: Any, label: str, /, **fields: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"invalid placement receipt {label}")
    for key, expected in fields.items():
        if key not in record or _canonical(record[key]) != _canonical(expected):
            raise ValueError(f"placement receipt {label} disagrees on {key}")


def _indices(value: Any, width: int, count: int | None = None) -> list[int]:
    if (
        not isinstance(value, list)
        or (count is not None and len(value) != count)
        or any(type(index) is not int or not 0 <= index < width for index in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("placement receipt has an invalid wire map")
    return value


def _coupling(value: Any, width: int) -> set[tuple[int, int]]:
    if not isinstance(value, list) or len(value) > width * (width - 1):
        raise ValueError("placement receipt has an invalid coupling map")
    edges = {tuple(_indices(edge, width, 2)) for edge in value}
    if len(edges) != len(value):
        raise ValueError("placement receipt has duplicate coupling edges")
    return edges


def _verify_calibration(name: str, evidence: Any, model: Any) -> tuple[int, set[tuple[int, int]], set[str]]:
    evidence = _require_fields(evidence, _SNAPSHOT_FIELDS, "placement snapshot")
    calibration = _require_fields(evidence["calibration"], {"configuration", "properties"}, "placement calibration")
    if sha256(_canonical(calibration)).hexdigest() != evidence["sha256"]:
        raise ValueError("placement receipt calibration SHA-256 differs")
    config, properties = calibration["configuration"], calibration["properties"]
    backend_name = f"fake_{name}"
    _expect(config, "snapshot configuration", backend_name=backend_name)
    backend_version = config["backend_version"]
    if not isinstance(backend_version, str) or not backend_version:
        raise ValueError("placement receipt has an invalid backend version")
    width = _bounded_int(config["n_qubits"], 4, 512, "snapshot width")
    date = None
    if properties is not None:
        stamp = _require_fields(properties["last_update_date"], {"date"}, "placement snapshot date")
        date = stamp["date"]
        if not isinstance(date, str) or not date:
            raise ValueError("placement receipt has an invalid snapshot date")
    _expect(
        model, "snapshot model", backend=backend_name, backend_version=backend_version,
        snapshot_last_update_date=date,
    )
    effective = _coupling(evidence["coupling"], width)
    recorded = _coupling(config["coupling_map"], width)
    if {tuple(sorted(edge)) for edge in effective} - {tuple(sorted(edge)) for edge in recorded}:
        raise ValueError("placement receipt effective coupling differs from its calibration")
    basis = config["basis_gates"]
    if not isinstance(basis, list) or not basis or any(not isinstance(gate, str) or not gate for gate in basis):
        raise ValueError("placement receipt has an invalid snapshot gate basis")
    return width, effective, set(basis)


def _export_counts(
    text: str, active: list[int], classical: list[int], coupling: set[tuple[int, int]],
) -> tuple[dict[str, int], int]:
    lines = text.splitlines()
    if (
        len(lines) > MAX_CIRCUIT_INSTRUCTIONS + 4
        or lines[:4] != ["OPENQASM 3.0;", 'include "stdgates.inc";', "bit[4] c;", f"qubit[{len(active)}] q;"]
    ):
        raise ValueError("unsupported placement OpenQASM header or instruction count")
    measurements: dict[int, int] = {}
    measured_wires: set[int] = set()
    used_wires: set[int] = set()
    counts: Counter[str] = Counter()
    two_qubit_gates = 0
    for line in lines[4:]:
        measure = _MEASURE.fullmatch(line)
        if measure is not None:
            bit, wire = map(int, measure.groups())
            if bit >= 4 or wire >= len(active) or bit in measurements or wire in measured_wires:
                raise ValueError("invalid terminal measurement in placement OpenQASM")
            measurements[bit] = active[wire]
            measured_wires.add(wire)
            used_wires.add(wire)
            counts["measure"] += 1
            continue
        gate = _GATE.fullmatch(line)
        if gate is None:
            raise ValueError("unsupported instruction in placement OpenQASM")
        name, parameter, operands = gate.groups()
        wires = [int(index) for index in _WIRE.findall(operands)]
        if (
            len(wires) != _GATE_WIRES.get(name)
            or len(set(wires)) != len(wires)
            or any(index >= len(active) or index in measured_wires for index in wires)
            or (name == "rz") != (parameter is not None)
            or (parameter is not None and not math.isfinite(float(parameter)))
        ):
            raise ValueError("invalid gate or wire in placement OpenQASM")
        counts[name] += 1
        used_wires.update(wires)
        if len(wires) == 2:
            if tuple(active[index] for index in wires) not in coupling:
                raise ValueError("placement OpenQASM two-qubit gate uses an uncoupled wire pair")
            two_qubit_gates += 1
    if used_wires != set(range(len(active))) or measurements != dict(enumerate(classical)):
        raise ValueError("placement OpenQASM does not preserve the recorded wire maps")
    return dict(counts), two_qubit_gates


def _verify_distributions(verification: Any, rounds: int) -> None:
    tolerance = MEASUREMENT_TOLERANCE
    _expect(verification, "compiler verification", tolerance=tolerance, bit_order="Highest classical bit index first")
    keys = [f"{state:04b}" for state in range(16)]
    hit_rate = theoretical_success_probability(4, 2, rounds)
    ideal = {bits: hit_rate / 2 if mod4_range_predicate(bits) else (1 - hit_rate) / 14 for bits in keys}
    distributions = []
    for name in ("original_probabilities", "compiled_probabilities", "roundtrip_probabilities"):
        values = verification[name]
        if (
            not isinstance(values, dict) or set(values) - set(keys)
            or any(type(p) not in (float, int) or not 0 <= p <= 1 + tolerance for p in values.values())
            or not math.isclose(math.fsum(values.values()), 1, rel_tol=0, abs_tol=tolerance)
            or _distance(values, ideal) > tolerance
        ):
            raise ValueError("placement receipt has invalid ideal measurement probabilities")
        distributions.append(values)
    original, compiled, imported = distributions
    for name, distance in (
        ("compilation_total_variation_distance", _distance(original, compiled)),
        ("export_total_variation_distance", _distance(compiled, imported)),
        ("roundtrip_total_variation_distance", _distance(original, imported)),
    ):
        recorded = verification[name]
        if (
            type(recorded) not in (float, int) or not 0 <= recorded <= tolerance
            or not math.isclose(recorded, distance, abs_tol=1e-15, rel_tol=0)
        ):
            raise ValueError("placement receipt has inconsistent round-trip verification")


def _verify_compilation(
    record: Any, text: Any, *, name: str, width: int, layout: list[int],
    coupling: set[tuple[int, int]], basis: set[str], request: dict[str, Any], rounds: int,
) -> tuple[int, int]:
    _expect(
        record, "compilation", status="VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION",
        seed_transpiler=request["transpiler_seed"], optimization_level=_DEFAULT_OPTIMIZATION_LEVEL,
        initial_logical_to_target_qubit=layout, provider_job_submitted=False, quantum_hardware_used=False,
    )
    _expect(record["target"], "compiler target", kind="BACKEND_V2", name=f"fake_{name}", qubits=width)
    qasm = record["openqasm"]
    active = _indices(qasm["export_wire_to_target_qubit"], width)
    classical = _indices(qasm["classical_bit_to_target_qubit"], width, 4)
    if not 4 <= len(active) <= MAX_EXACT_QUBITS or active != sorted(active) or not set(classical) <= set(active):
        raise ValueError("placement receipt has an invalid active-wire map")
    if not isinstance(text, str):
        raise ValueError("placement receipt lacks its OpenQASM text")
    data = text.encode("utf-8")
    _expect(
        qasm, "OpenQASM identity", representation="PORTABLE_COMPILED_WIRES",
        bytes=len(data), sha256=sha256(data).hexdigest(),
    )
    if request["prefer_native_qasm"]:
        _expect(qasm, "OpenQASM preference", preferred_importer=_NATIVE_QASM_IMPORTER)
    elif "preferred_importer" in qasm:
        raise ValueError("placement receipt records an unrequested importer preference")
    if qasm["importer"] not in {_STANDARD_IMPORTER, _NATIVE_QASM_IMPORTER}:
        raise ValueError("placement receipt has an invalid actual importer")
    counts, gates = _export_counts(text, active, classical, coupling)
    if set(counts) - {"measure"} - basis:
        raise ValueError("placement OpenQASM uses a gate outside the snapshot basis")
    _expect(record["compiled"], "compiled resources", qubits=width, two_qubit_gates=gates, operations=counts)
    _verify_distributions(record["verification"], rounds)
    return len(active), gates


def _verify_trials(
    trials: Any, *, name: str, active: int, request: dict[str, Any],
) -> tuple[int, int, bool]:
    seeds, shots = request["evaluation_seeds"], request["shots"]
    if not isinstance(trials, list) or not 1 <= len(trials) <= len(seeds):
        raise ValueError("placement receipt has an invalid seed schedule")
    hits = completed = 0
    halt = False
    method = "matrix_product_state" if name == "torino" else "density_matrix"
    for trial, seed in zip(trials, seeds):
        if halt:
            raise ValueError("placement receipt has work after unconfirmed cancellation")
        _expect(trial, "seed trial", seed=seed)
        if active > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
            _expect(trial, "compile-only trial", status="COMPILE_ONLY_ACTIVE_QUBIT_LIMIT", simulation=None)
            if "local_job_cancelled" in trial:
                raise ValueError("placement receipt claims cancellation of a compile-only trial")
            continue
        if trial["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT":
            _expect(trial, "timeout", simulation=None)
            cancelled = trial["local_job_cancelled"]
            if type(cancelled) is not bool:
                raise ValueError("placement receipt lacks a valid cancellation result")
            halt = not cancelled
            continue
        _expect(trial, "completed trial", status="COMPLETED")
        if "local_job_cancelled" in trial:
            raise ValueError("placement receipt claims cancellation of a completed trial")
        simulation = trial["simulation"]
        checked = summarize_search_counts(simulation["counts"], requested_shots=shots, width=4, verifier=mod4_range_predicate)
        if checked["completed_shots"] != shots:
            raise ValueError("placement receipt has a short histogram")
        _expect(
            simulation, "histogram", **checked, execution_mode="SIMULATOR", method=method,
            simulated_qubits=active, verified_hit_rate_wilson_95=_wilson_interval(checked["verified_shots"], shots),
        )
        if method == "matrix_product_state":
            _expect(simulation, "MPS sampling limits", max_bond_dimension=MAX_MPS_BOND_DIMENSION, truncation_threshold=0.0)
        completed += shots
        hits += checked["verified_shots"]
    if len(trials) != len(seeds) and not halt:
        raise ValueError("placement receipt ends the seed schedule without an unconfirmed cancellation")
    return hits, completed, halt


def _verify_evaluation(
    row: Any, hits: int, completed: int, *, request: dict[str, Any], confidence: float,
) -> str:
    if completed != 2 * request["shots"]:
        _expect(row, "incomplete cell", status="INCOMPLETE", verified_shots=None, completed_shots=None, interval=None)
        if "verified_hit_rate" in row:
            raise ValueError("placement receipt has a hit rate on an incomplete cell")
        return "INCOMPLETE"
    _expect(row, "pooled counts", verified_shots=hits, completed_shots=completed, verified_hit_rate=hits / completed)
    interval = row["interval"]
    if (
        not isinstance(interval, list) or len(interval) != 2
        or any(type(value) not in (int, float) or not 0 <= value <= 1 for value in interval)
        or interval[0] > interval[1]
        or any(abs(saved - computed) > _INTERVAL_TOLERANCE for saved, computed in zip(
            interval, _placement_exact_interval(hits, completed, confidence), strict=True,
        ))
    ):
        raise ValueError("placement receipt has a different exact confidence interval")
    floor = request["minimum_hit_rate"]
    status = "MEETS_TARGET" if interval[0] >= floor else "BELOW_TARGET" if interval[1] < floor else "INCONCLUSIVE"
    _expect(row, "interval decision", status=status)
    return status


def _verify_model(
    name: str, model: dict[str, Any], evidence: dict[str, Any], request: dict[str, Any], confidence: float,
) -> tuple[int, int, bool]:
    width, coupling, basis = _verify_calibration(name, evidence, model)
    anchor = _indices(evidence["initial_layout"], width, 4)
    alternate = _alternate_placement(anchor, coupling)
    placements, exports = model["placements"], evidence["exports"]
    if alternate is None:
        _expect(model, "topology outcome", status="INSUFFICIENT_TOPOLOGY", placements=[], eligible_rounds=[], selected_rounds=None)
        if exports != {}:
            raise ValueError("placement receipt records exports for an unavailable placement")
        return 0, 0, False
    if not isinstance(placements, list) or not 1 <= len(placements) <= 2 or not isinstance(exports, dict):
        raise ValueError("placement receipt has an invalid placement list")
    if list(exports) != ["baseline", "alternate"][:len(placements)]:
        raise ValueError("placement receipt has missing or extraneous OpenQASM placements")

    decisions = {rounds: [] for rounds in ROUNDS}
    gate_totals = dict.fromkeys(ROUNDS, 0)
    circuits = completed_shots = 0
    halt = False
    for placement, (label, layout) in zip(placements, (("baseline", anchor), ("alternate", alternate))):
        if halt:
            raise ValueError("placement receipt has work after unconfirmed cancellation")
        _expect(placement, "placement order and layout", label=label, initial_logical_to_target_qubit=layout)
        cells = placement["rounds"]
        names = [str(rounds) for rounds in ROUNDS]
        if not isinstance(cells, dict) or not cells or list(cells) != names[:len(cells)]:
            raise ValueError("placement receipt has an invalid round schedule")
        if not isinstance(exports[label], dict) or list(exports[label]) != list(cells):
            raise ValueError("placement receipt has missing or extraneous OpenQASM rounds")
        for rounds in ROUNDS[:len(cells)]:
            if halt:
                raise ValueError("placement receipt has work after unconfirmed cancellation")
            key = str(rounds)
            cell = cells[key]
            active, gates = _verify_compilation(
                cell["compilation"], exports[label][key], name=name, width=width, layout=layout,
                coupling=coupling, basis=basis, request=request, rounds=rounds,
            )
            circuits += 1
            hits, completed, halt = _verify_trials(cell["trials"], name=name, active=active, request=request)
            completed_shots += completed
            gate_totals[rounds] += gates
            decisions[rounds].append(_verify_evaluation(cell["evaluation"], hits, completed, request=request, confidence=confidence))
        if len(cells) != len(ROUNDS) and not halt:
            raise ValueError("placement receipt truncates a placement without an unconfirmed cancellation")
    if len(placements) != 2 and not halt:
        raise ValueError("placement receipt truncates its placement schedule without an unconfirmed cancellation")

    complete = not halt and all("INCOMPLETE" not in values for values in decisions.values())
    eligible = [rounds for rounds in ROUNDS if decisions[rounds] == ["MEETS_TARGET", "MEETS_TARGET"]] if complete else []
    selected = min(eligible, key=lambda rounds: (gate_totals[rounds], rounds)) if eligible else None
    _expect(model, "placement selection", status="COMPLETED" if complete else "PARTIAL", eligible_rounds=eligible, selected_rounds=selected)
    return circuits, completed_shots, halt


def _verify_evidence(request: Any, evidence: Any, versions: dict[str, Any]) -> dict[str, Any]:
    request = _require_fields(request, _REQUEST_FIELDS, "placement request")
    _expect(request, "request version", schema_version=1, workflow=_WORKFLOW)
    if type(request["prefer_native_qasm"]) is not bool:
        raise ValueError("placement receipt has an invalid importer preference")
    model, shots, seed = (request[key] for key in ("model", "shots", "transpiler_seed"))
    seeds = _validate_placement_sampling(model, shots, request["minimum_hit_rate"], request["evaluation_seeds"], seed)
    requested = list(IBM_SNAPSHOT_MODELS) if model == "all" else [model]
    cell_count = len(requested) * 2 * len(ROUNDS)
    confidence = 1 - (1 - FAMILY_CONFIDENCE) / cell_count
    evidence = _require_fields(evidence, {"schema_version", "report", "snapshots"}, "placement report envelope")
    _expect(evidence, "report envelope version", schema_version=1)
    report, snapshots = evidence["report"], evidence["snapshots"]
    marked = [f"{state:04b}" for state in range(16) if mod4_range_predicate(f"{state:04b}")]
    _expect(
        report, "request and workflow", experiment="LOCAL_IBM_SNAPSHOT_MOD4_PLACEMENT_CHECK",
        execution_mode="SIMULATOR", provider_job_submitted=False, quantum_hardware_used=False,
        minimum_hit_rate=request["minimum_hit_rate"], shots_per_circuit_per_seed=shots,
        evaluation_seeds=list(seeds), transpiler_seed=seed, round_candidates=list(ROUNDS), basis_order="q3 q2 q1 q0",
        family_confidence=FAMILY_CONFIDENCE, predeclared_cells=cell_count, interval_confidence_per_cell=confidence,
        interval_method="Bonferroni-adjusted two-sided Clopper-Pearson",
        placement_policy="two-round anchor plus nearest connected one-wire move, ties by logical then physical index",
        selection_policy="pass at both placements, minimize summed two-qubit gates, break ties by fewer rounds",
        oracle_audit={"states_checked": 16, "marked_count": len(marked), "marked_states": marked},
        limits={**_local_limits(), "shots": MAX_MOD4_SWEEP_SHOTS, "samples_per_model": 8},
        versions={name: versions[name] for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
    )
    models = report["models"]
    if not isinstance(models, dict) or not models or list(models) != requested[:len(models)]:
        raise ValueError("placement receipt has an invalid snapshot schedule")
    if not isinstance(snapshots, dict) or list(snapshots) != list(models):
        raise ValueError("placement receipt has missing or extraneous snapshot evidence")

    circuits = completed_shots = 0
    halt = False
    for name, model_report in models.items():
        if halt:
            raise ValueError("placement receipt has work after unconfirmed cancellation")
        count, actual_shots, halt = _verify_model(name, model_report, snapshots[name], request, confidence)
        circuits += count
        completed_shots += actual_shots
    omitted = requested[len(models):]
    if omitted and not halt:
        raise ValueError("placement receipt omits snapshots without an unconfirmed cancellation")
    status = "COMPLETED" if all(model["status"] == "COMPLETED" for model in models.values()) else "PARTIAL"
    _expect(report, "completion", status=status, models_not_run=omitted, local_cancellation_unconfirmed=halt)
    return {
        "placement_status": status,
        "verified_exports": circuits,
        "completed_shots": completed_shots,
        "selected_rounds": {name: model["selected_rounds"] for name, model in models.items()},
        "snapshot_sha256": {name: snapshot["sha256"] for name, snapshot in snapshots.items()},
        "models_not_run": omitted,
        "local_cancellation_unconfirmed": halt,
    }


def _write_placement_receipt(
    directory: Path, *, report: dict[str, Any], model: str, prefer_native_qasm: bool,
    snapshots: dict[str, _SnapshotPlacements],
) -> None:
    request = {
        "schema_version": 1, "workflow": _WORKFLOW, "model": model, "prefer_native_qasm": prefer_native_qasm,
        "minimum_hit_rate": report["minimum_hit_rate"], "shots": report["shots_per_circuit_per_seed"],
        "evaluation_seeds": report["evaluation_seeds"], "transpiler_seed": report["transpiler_seed"],
    }
    captured = {}
    for name, details in report["models"].items():
        snapshot = snapshots[name]
        payload = _snapshot_payload(snapshot.backend)
        captured[name] = {
            "calibration": json.loads(payload),
            "sha256": sha256(payload).hexdigest(),
            "coupling": [list(edge) for edge in snapshot.backend.coupling_map.get_edges()],
            "initial_layout": list(snapshot.initial_layout),
            "exports": {
                placement["label"]: {
                    str(rounds): snapshot.programs[placement["label"], rounds].openqasm
                    for rounds in placement["rounds"]
                }
                for placement in details["placements"]
            },
        }
    envelope = {"schema_version": 1, "report": report, "snapshots": captured}
    _verify_evidence(request, _parse_request(_json_bytes(envelope), label="placement report"), report["versions"])
    _write_replay_receipt(directory, request=_json_bytes(request), report=envelope, openqasm=None)


@dataclass(frozen=True)
class _VerifiedPlacementBundle:
    directory: Path
    manifest: dict[str, Any]
    request: dict[str, Any]
    envelope: dict[str, Any]
    summary: dict[str, Any]


def _read_verified_placement_receipt(directory: Path) -> _VerifiedPlacementBundle:
    receipt, payloads = _read_verified_receipt(directory, capture_json=True)
    if receipt["artifacts"]["selected.qasm"] is not None:
        raise ValueError("placement receipt uses embedded per-circuit exports")
    try:
        request = _parse_request(payloads["request.json"], label="placement request")
        envelope = _parse_request(payloads["report.json"], label="placement report")
        summary = _verify_evidence(request, envelope, receipt["software_versions"])
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid placement receipt evidence") from exc
    return _VerifiedPlacementBundle(directory, receipt, request, envelope, summary)


def verify_placement_receipt(directory: str | Path) -> dict[str, Any]:
    """Check a saved receipt's bytes and placement calculation without running a simulator."""
    bundle = _read_verified_placement_receipt(Path(directory))
    return {"status": "PLACEMENT_RECEIPT_VERIFIED", "receipt_directory": str(bundle.directory), **bundle.summary}
