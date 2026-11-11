"""Measure repeated ideal search with and without a plan-owned probability table."""

from importlib.metadata import version
import json
from math import fsum
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
import warnings

from austins_algorithm.constraint_oracle import synthesize_constraint_oracle
from austins_algorithm.constraint_search import PreparedConstraintLineSearch
from austins_algorithm.constraints import load_constraint
from austins_algorithm.core import recommended_iterations
from austins_algorithm.grover_search import audit_phase_oracle, run_grover_search


SEEDS = (0, 1, 2**31, 2**32 - 1, *range(4101, 4129))
SHOT_CAP = 4


def benchmark(fixture, synthesis):
    constraint = load_constraint(Path(__file__).with_name(fixture))
    constraint_digest = constraint.digest()
    oracle, _ = synthesize_constraint_oracle(constraint, synthesis=synthesis)
    marked_count = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)["marked_count"]
    rounds = recommended_iterations(constraint.qubits, marked_count)
    budget = {"max_shots": SHOT_CAP, "max_oracle_queries": SHOT_CAP * rounds}

    def uncached(seed):
        return run_grover_search(
            oracle, constraint.qubits, constraint.matches, marked_count=marked_count,
            seed=seed, ideal_sampler="prepared_probabilities", **budget,
        )

    uncached(4100)  # Warm the simulator imports without preparing the plan's table.
    started = perf_counter()
    plan = PreparedConstraintLineSearch(constraint, synthesis=synthesis, prefer_native_qasm=True)
    setup = perf_counter() - started
    compiled = plan.compilation_report
    digest = compiled["openqasm"]["sha256"]

    def reused(seed):
        return plan.run(seed=seed, **budget)

    methods = {"fresh_table": uncached, "shared_table": reused}
    rows = []
    for index, seed in enumerate(SEEDS):
        order = tuple(methods) if index % 2 == 0 else tuple(reversed(methods))
        timings = {}
        results = {}
        for method in order:
            started = perf_counter()
            result = methods[method](seed)
            timings[method] = perf_counter() - started
            results[method] = result
        report = results["shared_table"]
        if report.pop("prepared_compilation_sha256") != digest:
            raise RuntimeError("prepared search returned a different compilation digest")
        if report.pop("constraint_sha256") != constraint_digest:
            raise RuntimeError("prepared search returned a different formula digest")
        if results["fresh_table"] != report:
            raise RuntimeError(f"search reports differ for {fixture} at seed {seed}")
        if report["ideal_sampler"]["used"] != "prepared_probabilities":
            raise RuntimeError(f"probability sampling was not used for {fixture} at seed {seed}")
        if report["candidate"] is not None and not constraint.matches(report["candidate"]):
            raise RuntimeError(f"invalid witness for {fixture} at seed {seed}")
        rows.append({"seed": seed, "report": report, "seconds": timings})

    return {
        "fixture": fixture,
        "formula_sha256": constraint_digest,
        "synthesis": synthesis,
        "logical_circuit_qubits": oracle.num_qubits,
        "search_qubits": constraint.qubits,
        "marked_count": marked_count,
        "rounds": rounds,
        "budget": budget,
        "openqasm": compiled["openqasm"],
        "plan_setup_seconds": setup,
        "first_paired_request_seconds": rows[0]["seconds"],
        "median_later_request_seconds": {
            method: median(row["seconds"][method] for row in rows[1:]) for method in methods
        },
        "total_request_seconds": {
            method: fsum(row["seconds"][method] for row in rows) for method in methods
        },
        "trials": rows,
    }


def main():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="This is an experimental native version of the OpenQASM 3 importer.",
            module=r"qiskit\.qasm3",
        )
        workloads = [
            benchmark("field_sensor_kit.json", "unit-factorized"),
            benchmark("site_interlock.json", "clause-flags"),
        ]
    print(json.dumps({
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "versions": {name: version(name) for name in ("qiskit", "numpy")},
        "timing_scope": "warm paired requests, alternating order, plan setup measured separately",
        "workloads": workloads,
    }, indent=2))


if __name__ == "__main__":
    main()

