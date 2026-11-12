"""Profile full and pruned Aer gate noise on two dated Jakarta workloads."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from functools import wraps
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter_ns
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    from qiskit.providers import BackendV2

    from austins_algorithm.compilation import CompiledProgram
    from austins_algorithm.snapshot_calibration_comparison import SnapshotCalibrationFiles


_AER_VERSION = "0.18.2"
_MAX_PAIRS = 8
_SEED = 7
_CASES = ((5, (0, 1, 2, 3, 5, 4)), (6, (0, 1, 2, 3, 5, 4, 6)))
_ROUND_GROUPS = ((0,), (1, 2, 3))
_ARMS = ("full", "pruned")
_JOB_STAGES = ("submit", "caller_wait", "worker", "assembly", "worker_noise_serialization", "controller", "result_format")


@dataclass(frozen=True)
class _Case:
    programs: dict[int, CompiledProgram]
    backends: dict[str, BackendV2]
    witnesses: frozenset[str]
    inputs: dict[str, Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _offline(event: str, _args: tuple[Any, ...]) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
        raise RuntimeError(f"offline density profile blocked {event}")


def _check_aer() -> str:
    from qiskit_aer import AerSimulator
    from qiskit_aer.backends import aer_simulator, aerbackend
    from qiskit_aer.jobs import AerJob
    from qiskit_aer.noise import NoiseModel

    actual = version("qiskit-aer")
    if actual != _AER_VERSION:
        raise RuntimeError(f"worker instrumentation requires qiskit-aer {_AER_VERSION}; found {actual}")
    hooks = (
        (AerSimulator, "run"), (AerJob, "result"), (NoiseModel, "to_dict"),
        (aerbackend.AerBackend, "_execute_circuits_job"), (aerbackend.AerBackend, "_format_results"),
        (aerbackend, "assemble_circuits"), (aer_simulator, "cpp_execute_circuits"),
    )
    if any(not callable(getattr(owner, name, None)) for owner, name in hooks):
        raise RuntimeError("pinned Aer worker instrumentation hooks are unavailable")
    return actual


def _prepare_case(width: int, layout: tuple[int, ...],
                  archives: tuple[SnapshotCalibrationFiles, SnapshotCalibrationFiles]) -> _Case:
    from austins_algorithm.constraints import BooleanConstraint
    from austins_algorithm.snapshot_calibration_comparison import (
        _prepare_calibration_comparison, _program_calibration, build_grover_search_circuit, compile_and_verify,
    )

    constraint = BooleanConstraint(width, ((1, 2), (-1,)))
    prepared = _prepare_calibration_comparison(
        constraint, *archives, initial_layouts=(layout,), max_rounds=3,
        seed_transpiler=_SEED, prefer_native_qasm=True,
    )
    if any(item["backend_name"] != "ibmq_jakarta" for item in prepared.scope["snapshots"].values()):
        raise ValueError("this profile requires two dated Jakarta snapshots")
    audit = prepared.scope["audit"]
    expected = [format(value, f"0{width}b") for value in range(1 << width) if value & 3 == 2]
    if audit["marked_states"] != expected:
        raise RuntimeError("Jakarta audit differs from the known suffix-10 witness set")

    programs = {}
    round_evidence = {}
    for rounds in range(4):
        logical = build_grover_search_circuit(prepared.oracle, width, iterations=rounds)
        program = compile_and_verify(
            logical, backend=prepared.backends["later"], initial_layout=layout, seed=_SEED, prefer_native_qasm=True,
        )
        compiled, qasm = program.report["compiled"], program.report["openqasm"]
        active = qasm["export_wire_to_target_qubit"]
        if len(active) != width + (rounds > 0) or qasm["importer"] != "qiskit.qasm3.loads_experimental":
            raise RuntimeError(f"round {rounds} did not compile with the expected active width and native importer")
        for backend in prepared.backends.values():
            _program_calibration(program, backend)
        programs[rounds] = program
        round_evidence[str(rounds)] = {
            "active_target_wires": active, "measurement_map": qasm["classical_bit_to_target_qubit"],
            "openqasm_sha256": qasm["sha256"], "openqasm_importer": qasm["importer"],
            "compiled_depth": compiled["depth"], "two_qubit_gates": compiled["two_qubit_gates"],
        }
    return _Case(programs, prepared.backends, frozenset(expected), {
        "search_qubits": width, "constraint_sha256": constraint.digest(), "audit": audit,
        "initial_layout": list(layout), "rounds": round_evidence,
        "snapshots": prepared.scope["snapshots"], "limits": prepared.scope["limits"],
    })


def _noise_inventory(simulator: Any, circuits: list[Any], rounds: tuple[int, ...],
                     active: list[int]) -> dict[str, Any]:
    from qiskit.circuit import Gate

    used = {(item.operation.name, tuple(circuit.find_bit(bit).index for bit in item.qubits))
            for circuit in circuits for item in circuit.data if isinstance(item.operation, Gate)}
    channels = simulator.options.noise_model.to_dict()["errors"]
    gate_noise = set()
    for row in channels:
        if row["type"] != "qerror":
            continue
        if not row.get("gate_qubits"):
            raise RuntimeError("expected a compact noise model with local gate channels")
        gate_noise.update((name, tuple(wires)) for name in row["operations"] for wires in row["gate_qubits"])
    return {
        "rounds": list(rounds), "active_target_wires": active,
        "gate_channels": [[name, list(wires)] for name, wires in sorted(gate_noise)],
        "gate_channel_count": len(gate_noise),
        "used_gate_channels": [[name, list(wires)] for name, wires in sorted(gate_noise & used)],
        "readout_channels": [row for row in channels if row["type"] == "roerror"],
    }


def _measure(case: _Case, arm: str) -> tuple[dict[str, Any], dict[tuple[str, int], tuple[Any, ...]]]:
    import numpy as np
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_aer.backends import aer_simulator, aerbackend
    from qiskit_aer.jobs import AerJob
    from qiskit_aer.noise import NoiseModel

    from austins_algorithm import ibm_models as ibm

    calls: Counter[str] = Counter()
    ticks: Counter[str] = Counter()
    circuits_submitted = 0
    original_run = AerSimulator.run
    original_to_dict = NoiseModel.to_dict
    original_cpp = aer_simulator.cpp_execute_circuits

    def instrument(function, stage):
        @wraps(function)
        def measured(*args, **kwargs):
            start = perf_counter_ns()
            try:
                return function(*args, **kwargs)
            finally:
                ticks[stage] += perf_counter_ns() - start
                calls[stage] += 1
        return measured

    def run(simulator, circuits, **options):
        nonlocal circuits_submitted
        circuits_submitted += 1 if isinstance(circuits, QuantumCircuit) else len(circuits)
        return original_run(simulator, circuits, **options)

    serialize = instrument(original_to_dict, "worker_noise_serialization")

    def to_dict(noise_model, serializable=False):
        return (serialize if serializable else original_to_dict)(noise_model, serializable=serializable)

    def execute(controller, aer_circuits, noise_model, config):
        # Forward the pinned SDK's original helper. Only the controller call gets its own timer.
        timed = SimpleNamespace(execute=instrument(controller.execute, "controller"))
        return original_cpp(timed, aer_circuits, noise_model, config)

    inventory = {}
    replays = {}
    group_inputs = {}
    with ExitStack() as stack:
        hooks = (
            (AerSimulator, "run", run, "submit"), (AerJob, "result", AerJob.result, "caller_wait"),
            (aerbackend.AerBackend, "_execute_circuits_job", aerbackend.AerBackend._execute_circuits_job, "worker"),
            (aerbackend, "assemble_circuits", aerbackend.assemble_circuits, "assembly"),
            (ibm, "_compact_snapshot_inputs", ibm._compact_snapshot_inputs, "compact_noise_setup"),
            (ibm, "_snapshot_density_replay", ibm._snapshot_density_replay, "density_replay_validation"),
            (ibm, "_prepared_snapshot_probabilities", ibm._prepared_snapshot_probabilities, "analytical_probabilities"),
        )
        for owner, name, function, stage in hooks:
            stack.enter_context(patch.object(owner, name, instrument(function, stage)))
        stack.enter_context(patch.object(aerbackend.AerBackend, "_format_results",
                                         staticmethod(instrument(aerbackend.AerBackend._format_results, "result_format"))))
        stack.enter_context(patch.object(NoiseModel, "to_dict", to_dict))
        stack.enter_context(patch.object(aer_simulator, "cpp_execute_circuits", execute))
        start = perf_counter_ns()
        for snapshot, backend in case.backends.items():
            compact = ibm._compact_snapshot_inputs(
                backend, case.programs, AerSimulator, prune_unused_gate_noise=arm == "pruned",
            )
            if compact[0][1] is compact[1][1] or not compact[1][1] is compact[2][1] is compact[3][1]:
                raise RuntimeError(f"{snapshot} did not produce the expected singleton and three-round groups")
            group_inputs[snapshot] = compact
            for rounds in _ROUND_GROUPS:
                circuit, simulator = compact[rounds[0]]
                if len(rounds) == 1:
                    single, status = ibm._prepare_density_snapshot(circuit, simulator, backend.name)
                    rows = [single] if single is not None else None
                else:
                    circuits = [compact[index][0] for index in rounds]
                    rows, status = ibm._prepare_density_snapshot_batch(circuits, simulator, backend.name)
                if rows is None:
                    raise RuntimeError(f"{snapshot} rounds {rounds} stopped with {status['status']}")
                for index, (replay, sampler) in zip(rounds, rows, strict=True):
                    probability = ibm._prepared_snapshot_probabilities(replay, sampler, verifier=case.witnesses.__contains__)
                    replays[snapshot, index] = (replay, sampler, probability)
        wall = perf_counter_ns() - start

    if circuits_submitted != 8 or any(calls[stage] != 4 for stage in _JOB_STAGES):
        raise RuntimeError(f"{arm} did not profile every worker stage for four jobs and eight circuits")
    for snapshot, compact in group_inputs.items():
        inventory[snapshot] = [
            _noise_inventory(compact[rounds[0]][1], [compact[index][0] for index in rounds], rounds,
                             case.inputs["rounds"][str(rounds[0])]["active_target_wires"])
            for rounds in _ROUND_GROUPS
        ]
    signatures = {}
    for key, (replay, sampler, probability) in replays.items():
        initial, *terminal = replay.data
        state = np.asarray(initial.operation.params[0])
        width = 1 << replay.num_qubits
        if state.shape != (width, width) or not np.isfinite(state).all():
            raise RuntimeError(f"{arm} returned an invalid density matrix for {key}")
        mapping = tuple((replay.find_bit(item.qubits[0]).index, replay.find_bit(item.clbits[0]).index)
                        for item in terminal)
        signatures[key] = (state.dtype.str, state.shape, state.tobytes(), mapping,
                           sampler.options.noise_model.to_dict(), probability)
    return {"arm": arm, "wall_seconds": wall / 1e9, "circuits_submitted": circuits_submitted,
            "calls": dict(calls), "inclusive_seconds": {stage: value / 1e9 for stage, value in ticks.items()},
            "noise_inventory": inventory}, signatures


def _profile(case: _Case, pairs: int) -> dict[str, Any]:
    full, reference = _measure(case, "full")
    pruned, candidate = _measure(case, "pruned")
    if reference != candidate or len(reference) != 8:
        raise RuntimeError("pruned and full arms differ in density bytes, measurement maps, readout, or probabilities")
    pruned_channels = 0
    for snapshot, groups in full["noise_inventory"].items():
        for left, right in zip(groups, pruned["noise_inventory"][snapshot], strict=True):
            if (left["used_gate_channels"] != right["used_gate_channels"]
                    or any(channel not in left["gate_channels"] for channel in right["gate_channels"])
                    or left["readout_channels"] != right["readout_channels"]):
                raise RuntimeError(f"{snapshot} pruning changed used gate channels or readout")
            pruned_channels += left["gate_channel_count"] - right["gate_channel_count"]
    if not pruned_channels:
        raise RuntimeError("this workload did not exercise gate noise pruning")
    inventories = {"full": full["noise_inventory"], "pruned": pruned["noise_inventory"]}
    measurements = []
    for pair in range(pairs):
        for arm in _ARMS if pair % 2 == 0 else reversed(_ARMS):
            row, observed = _measure(case, arm)
            if observed != reference or row["noise_inventory"] != inventories[arm]:
                raise RuntimeError(f"{arm} evidence differs from the warmed reference")
            row.pop("noise_inventory")
            measurements.append({"pair": pair + 1, **row})
    grouped = {arm: [row for row in measurements if row["arm"] == arm] for arm in _ARMS}
    return {
        "inputs": case.inputs, "noise_inventory": inventories,
        "exact_comparison": {
            f"{snapshot}:{rounds}": {
                "density_sha256": sha256(state).hexdigest(), "density_shape": list(shape), "density_dtype": dtype,
                "compact_measurement_map": mapping, "readout_sha256": _digest(readout),
                "probability_report_sha256": _digest(probability),
                "probability_bins": len(probability["outcome_probabilities"]),
            }
            for (snapshot, rounds), (dtype, shape, state, mapping, readout, probability) in reference.items()
        },
        "warmup": [full, pruned], "measurements": measurements,
        "medians": {
            arm: {"wall_seconds": median(row["wall_seconds"] for row in rows),
                  "inclusive_seconds": {stage: median(row["inclusive_seconds"][stage] for row in rows)
                                        for stage in rows[0]["inclusive_seconds"]}}
            for arm, rows in grouped.items()
        },
        "paired_median_wall_savings_seconds": median(
            left["wall_seconds"] - right["wall_seconds"]
            for left, right in zip(grouped["full"], grouped["pruned"], strict=True)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    archive_args = ("CONFIG", "PROPERTIES", "CONFIG_SHA256", "PROPERTIES_SHA256")
    parser.add_argument("--earlier", nargs=4, required=True, metavar=archive_args)
    parser.add_argument("--later", nargs=4, required=True, metavar=archive_args)
    parser.add_argument("--pairs", type=int, default=5, help=f"number of warm measured pairs, 1 to {_MAX_PAIRS}")
    args = parser.parse_args()
    if not 1 <= args.pairs <= _MAX_PAIRS:
        parser.error(f"--pairs must be from 1 to {_MAX_PAIRS}")
    sys.dont_write_bytecode = True
    os.environ["QISKIT_PARALLEL"] = "FALSE"
    sys.addaudithook(_offline)
    try:
        from austins_algorithm.snapshot_calibration_comparison import SnapshotCalibrationFiles

        aer_version = _check_aer()
        earlier, later = (
            SnapshotCalibrationFiles(Path(config), Path(properties), config_pin, properties_pin)
            for config, properties, config_pin, properties_pin in (args.earlier, args.later)
        )
        report = {
            "schema_version": 1, "status": "LOCAL_CALIBRATION_DENSITY_WORKER_PROFILE_COMPLETED",
            "environment": {
                "python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
                "logical_cpu_count": os.cpu_count(), "qiskit_parallel": os.environ["QISKIT_PARALLEL"],
                "thread_environment": {name: os.environ.get(name) for name in ("OMP_NUM_THREADS", "RAYON_NUM_THREADS")},
                "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")},
                "aer_worker_instrumentation_version": aer_version,
            },
            "pairs": args.pairs, "compiler_seed": _SEED,
            "timing_scope": {
                "sampling": "one excluded warmup pair followed by alternating arms in one process",
                "wall": "fresh compact noise setup, Aer density jobs, replay validation and analytical probabilities",
                "excluded": "SDK startup, archive loading, oracle audit, Qiskit transpilation and parity checks",
                "inclusive": "worker stages nest inside the worker, which overlaps the caller wait",
                "controller": "original Aer controller.execute call via a forwarding proxy",
                "noise_serialization": "NoiseModel.to_dict(serializable=True) in the pinned worker",
            },
            "cases": {str(width): _profile(_prepare_case(width, layout, (earlier, later)), args.pairs)
                      for width, layout in _CASES},
            "provider_job_submitted": False, "quantum_hardware_used": False,
        }
        print(json.dumps(report, indent=2, allow_nan=False))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, f"Calibration density worker profile failed: {exc}\n")


if __name__ == "__main__":
    main()
