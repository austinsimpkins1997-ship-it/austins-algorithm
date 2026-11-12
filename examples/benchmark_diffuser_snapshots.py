"""Benchmark matched OpenQASM importer requests on three public IBM snapshots."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any

from austins_algorithm.constraints import load_constraint
from austins_algorithm.core import _bounded_int
from austins_algorithm.diffuser_benchmark import MAX_BENCHMARK_PAIRS, run_diffuser_benchmark
from austins_algorithm.diffuser_benchmark_verification import _read_verified_benchmark
from austins_algorithm.diffuser_replay import save_diffuser_replay
from austins_algorithm.replay_receipt import _LIMITS, _json_bytes
from austins_algorithm.replay_regression import MAX_REGRESSION_TIMEOUT_SECONDS


EXAMPLES = Path(__file__).resolve().parent
SNAPSHOTS = ("manila", "jakarta", "torino")
SENSOR_PRESETS = {
    "jakarta": (((3, 2, 5, 4, 0, 6, 1), (5, 4, 3, 2, 6, 0, 1)), 10001),
    "torino": (((92, 98, 81, 80, 100, 82, 99), (81, 80, 92, 98, 82, 100, 99)), 20001),
}


def matched_requests(model: str, directory: Path) -> tuple[Path, Path]:
    if model == "manila":
        fixture = EXAMPLES / "diffuser-smoke-regression"
        return fixture / "baseline/request.json", fixture / "baseline-native/request.json"
    placements, first_seed = SENSOR_PRESETS[model]
    formula = load_constraint(EXAMPLES / "field_sensor_kit.json")
    standard, native = directory / "standard.json", directory / "native-preferred.json"
    for path, prefer_native in ((standard, False), (native, True)):
        save_diffuser_replay(
            path, formula, model=model, placements=placements, synthesis="auto", transpiler_seed=7,
            witness_probability=0.95, witness_max_shots=256, witness_validation_power=0.8,
            witness_seed_schedule=range(first_seed, first_seed + 198), prefer_native_qasm=prefer_native,
        )
    return standard, native


def variant_sizes(directory: Path, trial: dict[str, Any]) -> list[dict[str, Any]]:
    path = directory / trial["artifacts_directory"] / "report.json"
    with path.open("rb") as stream:
        raw = stream.read(_LIMITS["report.json"] + 1)
    fingerprint = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    if fingerprint != trial["artifacts"]["report.json"]:
        raise ValueError("benchmark report changed after verification")
    report = json.loads(raw)
    sizes = []
    for cell in report["cells"]:
        for diffuser, variant in cell["variants"].items():
            compilation = variant["compilation"]
            openqasm = compilation["openqasm"]
            compiled = compilation["compiled"]
            sizes.append({
                "placement": cell["placement"], "rounds": cell["rounds"], "diffuser": diffuser,
                "active_wires": len(openqasm["export_wire_to_target_qubit"]),
                "openqasm_bytes": openqasm["bytes"], "compiled_depth": compiled["depth"],
                "two_qubit_gates": compiled["two_qubit_gates"],
            })
    return sizes


def benchmark_snapshot(model: str, output: Path, pairs: int, timeout: int) -> dict[str, Any]:
    directory = output / model
    directory.mkdir()
    standard, native = matched_requests(model, directory)
    benchmark = directory / "benchmark"
    run_diffuser_benchmark(standard, native, output_directory=benchmark, pairs=pairs, timeout_seconds=timeout)
    verified, summary, documents = _read_verified_benchmark(benchmark)
    if verified["benchmark_status"] != "BENCHMARK_COMPLETED":
        raise ValueError(f"{model} benchmark stopped at {verified['terminal_trial_status']}; see {benchmark / 'summary.json'}")
    first_trials = {side: next(trial for trial in summary["trials"] if trial["side"] == side)
                    for side in ("left", "right")}
    standard_sizes = variant_sizes(benchmark, first_trials["left"])
    if standard_sizes != variant_sizes(benchmark, first_trials["right"]):
        raise ValueError(f"{model} compiled size metadata differs across the matched requests")
    return {
        "snapshot": documents["left"]["expected"]["snapshot"],
        "benchmark_directory": str(benchmark),
        "benchmark_summary_sha256": verified["summary_sha256"],
        "verified_trials": verified["verified_trials"],
        "environment": summary["environment"],
        "requests": summary["inputs"],
        "compiled_output_sha256": verified["compiled_output_sha256"],
        "openqasm_bytes_total": sum(variant["openqasm_bytes"] for variant in standard_sizes),
        "variant_sizes": standard_sizes,
        "actual_importers_per_trial": {
            "standard": first_trials["left"]["actual_importers"],
            "native_preferred": first_trials["right"]["actual_importers"],
        },
        "timing_summary": verified["timing_summary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory for verified benchmark bundles")
    parser.add_argument("--snapshots", nargs="+", choices=SNAPSHOTS, default=SNAPSHOTS)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        _bounded_int(args.pairs, 1, MAX_BENCHMARK_PAIRS, "pairs")
        _bounded_int(args.timeout_seconds, 1, MAX_REGRESSION_TIMEOUT_SECONDS, "timeout_seconds")
        if len(set(args.snapshots)) != len(args.snapshots):
            raise ValueError("select each snapshot once")
        output = args.output_dir.resolve()
        output.mkdir()
        rows = {}
        for model in args.snapshots:
            print(f"Checking {model} with {args.pairs} alternating pairs.", file=sys.stderr, flush=True)
            rows[model] = benchmark_snapshot(model, output, args.pairs, args.timeout_seconds)
        summary = {
            "schema_version": 1, "status": "DIFFUSER_SNAPSHOT_BENCHMARK_COMPLETED",
            "snapshot_order": list(args.snapshots), "pairs_per_snapshot": args.pairs,
            "timeout_seconds": args.timeout_seconds, "snapshots": rows,
        }
        with (output / "snapshot-summary.json").open("xb") as stream:
            stream.write(_json_bytes(summary))
        print(json.dumps(summary, indent=2, allow_nan=False))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Diffuser snapshot benchmark failed: {exc}\n")


if __name__ == "__main__":
    main()

