"""Compare exact classical first-witness solvers on five bounded CNF workloads."""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from platform import machine, platform, python_implementation, python_version
from statistics import median
from time import perf_counter_ns
from typing import Any

from austins_algorithm.classical_constraint import classical_first_match
from austins_algorithm.constraints import BooleanConstraint, load_constraint


WARMUP_CALLS = 64
BATCHES = 9
CALLS_PER_BATCH = 64
METHODS = ("scan", "propagation")


def _measure_case(
    name: str, constraint: BooleanConstraint, witness: str | None, scan_assignments: int,
) -> dict[str, Any]:
    scan = classical_first_match(constraint, method="scan")
    propagation = classical_first_match(constraint, method="propagation")
    if scan["candidate"] != witness or propagation["candidate"] != witness:
        raise RuntimeError(f"{name} returned an unexpected first witness")
    if scan["assignments_checked"] != scan_assignments:
        raise RuntimeError(f"{name} scan examined an unexpected number of assignments")

    for method in METHODS:
        for _ in range(WARMUP_CALLS):
            classical_first_match(constraint, method=method)

    timings: dict[str, list[float]] = {method: [] for method in METHODS}
    for batch in range(BATCHES):
        order = METHODS if batch % 2 == 0 else METHODS[::-1]
        for method in order:
            start = perf_counter_ns()
            for _ in range(CALLS_PER_BATCH):
                classical_first_match(constraint, method=method)
            timings[method].append((perf_counter_ns() - start) / CALLS_PER_BATCH / 1000)

    return {
        "workload": name,
        "qubits": constraint.qubits,
        "clauses": constraint.clauses,
        "constraint_sha256": constraint.digest(),
        "candidate": witness,
        "scan": {
            "assignments_checked": scan["assignments_checked"],
            "median_microseconds_per_call": median(timings["scan"]),
        },
        "propagation": {
            "nodes_visited": propagation["nodes_visited"],
            "forced_assignments": propagation["forced_assignments"],
            "conflicts": propagation["conflicts"],
            "median_microseconds_per_call": median(timings["propagation"]),
        },
    }


def main() -> None:
    sensor_kit = load_constraint(Path(__file__).with_name("field_sensor_kit.json"))
    forced_chain = ((8,), *[(-variable, variable - 1) for variable in range(8, 1, -1)])
    blocked_triples = tuple(
        tuple(variable * sign for variable, sign in zip((8, 7, 6), signs))
        for signs in product((1, -1), repeat=3)
    )
    cases = (
        ("immediate_witness", BooleanConstraint(8, ((-1, -3), (-4, -8))), "00000000", 1),
        ("field_sensor_kit", sensor_kit, "001010", 11),
        ("forced_chain", BooleanConstraint(8, forced_chain), "11111111", 256),
        ("late_branch_witness", BooleanConstraint(8, ((8, 7), (8, -7), (4, 2))), "10000010", 131),
        ("blocked_triples", BooleanConstraint(8, blocked_triples), None, 256),
    )
    report = {
        "execution_mode": "CLASSICAL",
        "environment": {
            "python_version": python_version(),
            "python_implementation": python_implementation(),
            "platform": platform(),
            "machine": machine(),
        },
        "timing": {
            "warmup_calls_per_method_per_workload": WARMUP_CALLS,
            "batches_per_method_per_workload": BATCHES,
            "calls_per_batch": CALLS_PER_BATCH,
            "order": "Alternate scan-first and propagation-first batches.",
            "scope": "Full solver calls on prebuilt constraints, including solver setup and result reporting.",
            "statistic": "Median batch duration divided by calls per batch, in microseconds.",
        },
        "workloads": [_measure_case(*case) for case in cases],
    }
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

