"""Compare exact, adaptive ideal, and fixed-threshold snapshot search locally."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import median
from time import perf_counter

from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.optimization import SearchSession, exact_minimum, preview, problem_from_document


def benchmark(model: str | None = None) -> dict:
    fixture = Path(__file__).with_name("weighted_sensor_kit.json")
    problem = problem_from_document(json.loads(fixture.read_bytes()))
    width = len(problem["variables"])
    constraint = BooleanConstraint(width, problem["clauses"])
    weights = [row["cost"] for row in problem["variables"]]
    first = next(f"{index:0{width}b}" for index in range(1 << width) if constraint.matches(f"{index:0{width}b}"))

    def cost(bits: str) -> int:
        return sum(weight for bit, weight in zip(reversed(bits), weights) if bit == "1")

    started = perf_counter()
    exact = exact_minimum(problem)
    exact_seconds = perf_counter() - started
    if exact["bits"] != "110101" or exact["cost"] != 4 or cost(first) != 7:
        raise RuntimeError("Weighted sensor-kit baseline changed")
    trials = []
    for seed in range(24):
        started = perf_counter()
        report = SearchSession(problem, seed=seed, max_shots=24, max_queries=96, max_rounds=6, prove=False).run()
        elapsed = perf_counter() - started
        best = report["best"]
        trials.append({"seed": seed, "best": best, "seconds": elapsed,
                       "shots": report["shots_used"], "oracle_queries": report["queries_used"],
                       "work": report["work"], "matched_exact_cost": best is not None and best["cost"] == exact["cost"]})
    result = {"schema_version": 1, "problem": problem, "classical_first_witness": first,
              "classical_first_cost": cost(first), "classical_exact": exact,
              "classical_exact_seconds": exact_seconds, "ideal_trials": trials,
              "ideal_summary": {"matched_exact_cost": sum(row["matched_exact_cost"] for row in trials),
                                "trials": len(trials), "median_seconds": median(row["seconds"] for row in trials)},
              "snapshot": None, "provider_job_submitted": False, "quantum_hardware_used": False}
    if model is None:
        return result

    from qiskit.quantum_info import Statevector

    from austins_algorithm.grover_search import audit_phase_oracle, build_grover_search_circuit
    from austins_algorithm.ibm_models import run_constraint_snapshot
    from austins_algorithm.weighted_oracle import build_weighted_phase_oracle

    bound = cost(first)

    def improved(bits: str) -> bool:
        return constraint.matches(bits) and cost(bits) < bound

    started = perf_counter()
    oracle, work = build_weighted_phase_oracle(problem, incumbent=bound)
    built = perf_counter()
    audit = audit_phase_oracle(oracle, width, improved)
    audited = perf_counter()
    circuit = build_grover_search_circuit(oracle, width, iterations=1)
    state = Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
    ideal = preview(problem, incumbent=bound, rounds=1)["frames"][-1]
    if not all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(state.probabilities(), ideal["probabilities"])):
        raise RuntimeError("Reversible circuit and ideal workbench probabilities disagree")
    result["snapshot"] = {
        "scope": "Fixed incumbent and one Grover round",
        "incumbent_cost": bound, "oracle_synthesis": work, "oracle_audit": audit,
        "oracle_build_seconds": built - started, "audit_seconds": audited - built,
        "ideal_improvement_probability": ideal["marked_probability"],
        "dated_model": run_constraint_snapshot(circuit, improved, model=model, shots=64),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["manila", "jakarta", "torino"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = json.dumps(benchmark(args.model), indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
    else:
        print(raw, end="")


if __name__ == "__main__":
    main()
