"""Measure fixed-snapshot compilation reuse with matched sampling requests."""

from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.metadata import version
from itertools import accumulate
import json
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
from typing import Any

from benchmark_density_fusion import PRESETS, without_timings

from austins_algorithm.constraint_cover_comparison import (
    PreparedConstraintCoverSnapshot, run_constraint_cover_snapshot,
)
from austins_algorithm.constraints import load_constraint
from austins_algorithm.witness_budget import WitnessBudgetRequest


VALIDATION_OFFSETS = (0, 10_000, 20_000)
PAIR_COUNT = 4


def checked_report(
    report: dict[str, Any], *, prepared: bool, cache_status: str | None,
) -> dict[str, Any]:
    evidence = without_timings(report)
    snapshot = evidence["snapshot"]
    witness = evidence["witness_budget"]
    if snapshot["status"] != "COMPLETED" or witness["status"] != "COMPLETED":
        raise RuntimeError(f"Incomplete run: snapshot={snapshot['status']}, witness={witness['status']}")
    source = snapshot.pop("compilation_source", None)
    if source != ("PREPARED_PLAN" if prepared else None):
        raise RuntimeError("Unexpected compilation source")
    cache = snapshot.pop("density_cache", None)
    if cache_status is None:
        if cache is not None:
            raise RuntimeError("Unexpected cache evidence for this request")
    else:
        expected_bytes = sum(row["matrix_bytes"] for row in snapshot["state_preparation"].values())
        if cache != {"status": cache_status, "matrix_bytes": expected_bytes}:
            raise RuntimeError("Unexpected cache status or matrix payload")
    timings = report["witness_budget"]["timing_seconds"]
    hit = cache_status == "HIT"
    if (
        ("compile_and_audit" in timings) == prepared
        or ("density_preparation" in timings) == hit
        or ("density_replay_setup" in timings) != hit
    ):
        raise RuntimeError("Unexpected compilation or density preparation timing")
    return evidence


def digest_report(report: dict[str, Any]) -> str:
    data = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(data.encode("utf-8")).hexdigest()


def benchmark(model: str, *, density_cache: bool = False) -> dict[str, Any]:
    preset = PRESETS[model]
    constraint = load_constraint(Path(__file__).with_name(preset.fixture))
    if constraint.digest() != preset.formula_sha256:
        raise RuntimeError(f"Formula digest changed for {preset.fixture}")

    compilation = {
        "model": model, "initial_layout": preset.initial_layout, "rounds": 2, "seed": 7,
        "max_active_qubits": len(preset.initial_layout), "prefer_native_qasm": True,
    }
    requests = [
        {
            "shots": 128, "evaluation_seeds": preset.evaluation_seeds,
            "witness_budget": WitnessBudgetRequest(
                0.95, max_shots=256, validation_seeds=tuple(seed + offset for seed in preset.validation_seeds),
            ),
            "simulation_method": "prepared_density_matrix", "density_fusion": True,
        }
        for offset in VALIDATION_OFFSETS
    ]

    baseline, candidate = ("uncached", "cached") if density_cache else ("fresh", "prepared")

    def series(mode: str) -> tuple[dict, list[dict], list[dict[str, str]]]:
        setup = None
        plan = None
        if mode != "fresh":
            started = perf_counter()
            plan = PreparedConstraintCoverSnapshot(constraint, **compilation, cache_density=mode == "cached")
            setup = perf_counter() - started
        rows, reports, exports = [], [], []
        for request_index, request in enumerate(requests):
            started = perf_counter()
            if plan is None:
                fresh = run_constraint_cover_snapshot(constraint, **compilation, **request)
                elapsed = perf_counter() - started
                report = fresh.report
                openqasm = {name: program.openqasm for name, program in fresh.programs.items()}
            else:
                report = plan.run(**request)
                elapsed = perf_counter() - started
                openqasm = plan.openqasm

            cache_status = None
            if mode == "cached":
                cache_status = "FILLED" if request_index == 0 else "HIT"
            evidence = checked_report(report, prepared=plan is not None, cache_status=cache_status)
            witness = report["witness_budget"]
            for index, name in enumerate(("direct", "complement")):
                batches = report["snapshot"]["candidates"][name]["batches"]
                holdout = witness["candidates"][name]["validation_batches"]
                evaluation = request["evaluation_seeds"][index::2]
                validation = request["witness_budget"].validation_seeds[index::2]
                if tuple(row["seed"] for row in batches if row["status"] == "COMPLETED") != evaluation:
                    raise RuntimeError(f"Incomplete or changed calibration schedule for {name}")
                if tuple(row["seed"] for row in holdout if row["status"] == "COMPLETED") != validation:
                    raise RuntimeError(f"Incomplete or changed holdout schedule for {name}")

            rows.append({
                "wall_seconds": elapsed,
                "stages_seconds": witness["timing_seconds"],
                "evidence_sha256": digest_report(evidence),
            })
            if cache_status is not None:
                rows[-1]["density_cache"] = report["snapshot"]["density_cache"]
            reports.append(evidence)
            exports.append(openqasm)
        cumulative = [(setup or 0.0) + elapsed for elapsed in accumulate(row["wall_seconds"] for row in rows)]
        return {
            "setup_seconds": setup,
            "requests": rows,
            "cumulative_seconds": cumulative,
        }, reports, exports

    _, reference, expected_exports = series(baseline)
    _, warm_reports, warm_exports = series(candidate)
    if warm_reports != reference or warm_exports != expected_exports:
        raise RuntimeError(f"Warmup changed the reports or exports for {model}")

    pairs = []
    for index in range(PAIR_COUNT):
        order = (baseline, candidate) if index % 2 == 0 else (candidate, baseline)
        pair = {"order": order}
        for mode in order:
            row, reports, exports = series(mode)
            if reports != reference or exports != expected_exports:
                raise RuntimeError(f"Pair {index + 1} changed the reports or exports for {model} ({mode})")
            pair[mode] = row
        pairs.append(pair)

    cumulative = []
    for index in range(len(requests)):
        before = [pair[baseline]["cumulative_seconds"][index] for pair in pairs]
        after = [pair[candidate]["cumulative_seconds"][index] for pair in pairs]
        savings = [uncached - cached for uncached, cached in zip(before, after)]
        cumulative.append({
            "requests": index + 1,
            f"{baseline}_median_seconds": median(before),
            f"{candidate}_median_seconds": median(after),
            "paired_median_savings_seconds": median(savings),
            "paired_savings_range_seconds": [min(savings), max(savings)],
            f"{candidate}_faster_pairs": sum(saved > 0 for saved in savings),
        })
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
        "holdout_seed_offsets": VALIDATION_OFFSETS,
        "timing_scope": "four alternating pairs after a warmup pair, with one new plan per prepared series",
        "plan_setup_median_seconds": median(pair[candidate]["setup_seconds"] for pair in pairs),
        "cumulative": cumulative,
        "first_faster_paired_median": next(
            (row["requests"] for row in cumulative if row["paired_median_savings_seconds"] > 0), None,
        ),
        "pairs": pairs,
        "reference_reports": reference,
        "exports": expected_exports[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=PRESETS, default="jakarta")
    parser.add_argument("--density-cache", action="store_true", help="compare prepared plans with density reuse off and on")
    parser.add_argument("--output", type=Path, help="write the report to a new UTF-8 JSON file")
    args = parser.parse_args()
    if args.output is not None and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error("--output must be a new file in an existing directory")
    report = json.dumps(benchmark(args.model, density_cache=args.density_cache), indent=2, allow_nan=False)
    if args.output is None:
        print(report)
    else:
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(report + "\n")


if __name__ == "__main__":
    main()

