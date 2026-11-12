"""Reproduce a paired prepared-density fusion benchmark on public IBM snapshots."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib.metadata import version
import json
from pathlib import Path
import platform
from statistics import median
from typing import Any

import numpy as np
from qiskit_aer import AerSimulator

from austins_algorithm.constraint_cover_comparison import PhaseCoverComparison, run_constraint_cover_snapshot
from austins_algorithm.constraints import load_constraint
from austins_algorithm.ibm_models import _compact_snapshot_inputs, _prepare_density_snapshot, _snapshot_specs
from austins_algorithm.witness_budget import WitnessBudgetRequest


@dataclass(frozen=True)
class Preset:
    fixture: str
    formula_sha256: str
    initial_layout: tuple[int, ...]
    evaluation_seeds: tuple[int, ...]
    validation_seeds: range


PRESETS = {
    "jakarta": Preset(
        "site_interlock.json",
        "cb1541d3e817e003f257998c94dd0bc9421b8baaea6fe0a65875f60811ae2a06",
        (1, 2, 0, 6, 3, 5),
        (211, 223, 227, 229),
        range(1001, 1145),
    ),
    "torino": Preset(
        "field_sensor_kit.json",
        "6cceac28be9aaa4a6115d48389708ff31f7f14f24ef23e96bfdfee834001b667",
        (92, 98, 81, 80, 100, 82, 99),
        (1301, 1303, 1307, 1319),
        range(2001, 2225),
    ),
}
MODES = {"default": False, "fusion": True}
COVERS = ("direct", "complement")
ABSOLUTE_MATRIX_TOLERANCE = 1e-12


def without_timings(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_timings(item)
            for key, item in value.items()
            if key not in {"timing_seconds", "wall_seconds", "aer_experiment_seconds"}
        }
    if isinstance(value, list):
        return [without_timings(item) for item in value]
    return value


def checked_evidence(report: dict, preset: Preset) -> dict:
    snapshot = report["snapshot"]
    witness = report["witness_budget"]
    if snapshot["status"] != "COMPLETED" or witness["status"] != "COMPLETED":
        raise RuntimeError(f"Incomplete run: snapshot={snapshot['status']}, witness={witness['status']}")
    for index, cover in enumerate(COVERS):
        groups = (
            (snapshot["candidates"][cover]["batches"], preset.evaluation_seeds[index::2]),
            (witness["candidates"][cover]["validation_batches"], preset.validation_seeds[index::2]),
        )
        for rows, seeds in groups:
            completed_seeds = [row["seed"] for row in rows if row["status"] == "COMPLETED"]
            if completed_seeds != list(seeds) or len(rows) != len(seeds):
                raise RuntimeError(f"Incomplete or changed batch schedule for {cover}")
        if snapshot["state_preparation"][cover]["status"] != "COMPLETED":
            raise RuntimeError(f"Incomplete state preparation for {cover}")

    evidence = without_timings(report)
    evidence["snapshot"].pop("density_fusion_requested", None)
    for preparation in evidence["snapshot"]["state_preparation"].values():
        for key in ("fusion", "trace_real", "trace_imag"):
            preparation.pop(key, None)
    return evidence


def compare_matrices(model: str, comparison: PhaseCoverComparison) -> dict:
    # Reprepare on the same audited programs after the timed trials.
    factory, _ = _snapshot_specs()[model]
    backend = factory()
    inputs = _compact_snapshot_inputs(backend, comparison.programs, AerSimulator)
    differences = {}
    for cover, (circuit, simulator) in inputs.items():
        matrices = {}
        for mode, fusion in MODES.items():
            prepared, metadata = _prepare_density_snapshot(
                circuit, simulator, backend.name, density_fusion=fusion,
            )
            if prepared is None:
                raise RuntimeError(f"Matrix preparation failed for {cover} ({mode}): {metadata['status']}")
            replay, _ = prepared
            instruction = replay.data[0].operation
            if instruction.name != "set_density_matrix":
                raise RuntimeError(f"Replay lacks its prepared state for {cover} ({mode})")
            matrix = np.asarray(instruction.params[0])
            dimension = 1 << circuit.num_qubits
            if matrix.shape != (dimension, dimension) or not np.isfinite(matrix).all():
                raise RuntimeError(f"Invalid prepared state for {cover} ({mode})")
            matrices[mode] = matrix

        reference, fused = matrices["default"], matrices["fusion"]
        difference = float(np.max(np.abs(reference - fused)))
        trace_difference = float(abs(np.trace(reference) - np.trace(fused)))
        if max(difference, trace_difference) > ABSOLUTE_MATRIX_TOLERANCE:
            raise RuntimeError(
                f"Matrix comparison failed for {cover}: max difference={difference}, trace difference={trace_difference}"
            )
        differences[cover] = {
            "matrix_shape": list(reference.shape),
            "max_absolute_difference": difference,
            "trace_absolute_difference": trace_difference,
            "fusion": metadata.get("fusion"),
        }
    return differences


def benchmark(model: str) -> dict:
    preset = PRESETS[model]
    constraint = load_constraint(Path(__file__).with_name(preset.fixture))
    if constraint.digest() != preset.formula_sha256:
        raise RuntimeError(f"Formula digest changed for {preset.fixture}")

    def run(mode: str) -> PhaseCoverComparison:
        return run_constraint_cover_snapshot(
            constraint, model=model, initial_layout=preset.initial_layout,
            max_active_qubits=len(preset.initial_layout), rounds=2, seed=7, shots=128,
            evaluation_seeds=preset.evaluation_seeds,
            witness_budget=WitnessBudgetRequest(
                0.95, max_shots=256, validation_seeds=tuple(preset.validation_seeds),
            ),
            simulation_method="prepared_density_matrix", prefer_native_qasm=True,
            density_fusion=MODES[mode],
        )

    orders = (tuple(MODES), tuple(reversed(MODES)))
    expected = checked_evidence(run("default").report, preset)
    if checked_evidence(run("fusion").report, preset) != expected:
        raise RuntimeError("Fusion changed the warmup report")
    samples = []
    for index in range(4):
        order = orders[index % len(orders)]
        results = {}
        for mode in order:
            result = run(mode)
            if checked_evidence(result.report, preset) != expected:
                raise RuntimeError(f"Report changed for {model} in timed pair {index + 1} ({mode})")
            results[mode] = result
        samples.append({
            "order": order,
            "seconds": {mode: result.report["witness_budget"]["timing_seconds"] for mode, result in results.items()},
        })

    fields = samples[0]["seconds"]["default"]
    return {
        "model": model,
        "fixture": preset.fixture,
        "formula_sha256": preset.formula_sha256,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "versions": {
            name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy", "scipy")
        },
        "timing_scope": "four alternating pairs after one excluded warmup pair",
        "tested_pairs": len(samples),
        "matching_seeded_batches_per_pair": len(preset.evaluation_seeds) + len(preset.validation_seeds),
        "absolute_matrix_tolerance": ABSOLUTE_MATRIX_TOLERANCE,
        "matrix_differences": compare_matrices(model, results["default"]),
        "samples_seconds": samples,
        "medians_seconds": {
            mode: {field: median(row["seconds"][mode][field] for row in samples) for field in fields}
            for mode in MODES
        },
        "reports": {mode: results[mode].report for mode in MODES},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=PRESETS, default="jakarta")
    parser.add_argument("--output", type=Path, help="write the report to a new UTF-8 JSON file")
    args = parser.parse_args()
    if args.output is not None and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error("--output must be a new file in an existing directory")
    report = json.dumps(benchmark(args.model), indent=2, allow_nan=False)
    if args.output is None:
        print(report)
    else:
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(report + "\n")


if __name__ == "__main__":
    main()

