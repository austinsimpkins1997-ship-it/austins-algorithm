"""Compare verified first-witness cost on bundled telemetry constraints."""

from __future__ import annotations

import argparse
import json
import platform
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from math import fsum, isclose
from pathlib import Path
from statistics import median
from time import perf_counter

from austins_algorithm.classical_constraint import classical_first_match
from austins_algorithm.compilation import compile_and_verify, line_target
from austins_algorithm.constraint_oracle import synthesize_constraint_oracle
from austins_algorithm.constraint_search import PreparedConstraintLineSearch
from austins_algorithm.constraints import load_constraint
from austins_algorithm.core import recommended_iterations, theoretical_success_probability
from austins_algorithm.grover_search import (
    _LastAttempt,
    audit_phase_oracle,
    build_grover_search_circuit,
    run_grover_search,
)
from austins_algorithm.witness_budget import (
    WITNESS_BATCH_CONFIDENCE_PER_COVER,
    WitnessBudgetRequest,
    summarize_witness_batches,
)
from field_sensor_kit_baseline import all_valid_kits, first_valid_kit


TARGET_PROBABILITY = 0.95
IDEAL_SHOT_CAP = 4
COMPILER_SEED = 7
TORINO_LAYOUT = (92, 98, 81, 80, 100, 82, 99)


@dataclass(frozen=True)
class Workload:
    name: str
    fixture: str
    digest: str
    search_bits: int
    work_bits: int
    synthesis: str
    first_witness: Callable[[], str]
    all_witnesses: Callable[[], list[str]]
    seeds: range
    fresh_trials: int


def first_telemetry_configuration():
    # Enable the first sensor and the primary uplink. Leave the other loads off.
    return "01000001"


def telemetry_configurations():
    witnesses = []
    for assignment in range(256):
        acoustic, optical, thermal, radar, reserve, boost, primary, backup = (
            bool(assignment & (1 << index)) for index in range(8)
        )
        if (
            (acoustic or optical or thermal or radar)
            and (not radar or reserve or boost)
            and not (boost and primary)
            and (primary or backup)
        ):
            witnesses.append(format(assignment, "08b"))
    return witnesses


SENSOR_KIT = Workload(
    "remote-field-sensor-kit", "field_sensor_kit.json",
    "6cceac28be9aaa4a6115d48389708ff31f7f14f24ef23e96bfdfee834001b667",
    6, 1, "unit-factorized", first_valid_kit, all_valid_kits, range(3101, 3229), 128,
)
TELEMETRY_POWER = Workload(
    "telemetry-power", "telemetry_power.json",
    "965dd34b7798e2f1511065feda899ba7c43b917325c6214b0102281448a6ed92",
    8, 4, "clause-flags", first_telemetry_configuration, telemetry_configurations, range(5101, 5229), 8,
)
WORKLOADS = {"sensor-kit": SENSOR_KIT, "telemetry-power": TELEMETRY_POWER}


def load_fixture(workload=SENSOR_KIT):
    constraint = load_constraint(Path(__file__).with_name(workload.fixture))
    if constraint.digest() != workload.digest:
        raise ValueError(f"benchmark requires the original bundled {workload.name} formula")
    return constraint


def classical_trial(method, workload=SENSOR_KIT):
    started = perf_counter()
    constraint = load_fixture(workload)
    loaded = perf_counter()
    candidate = (
        workload.first_witness() if method == "direct"
        else classical_first_match(constraint, method=method)["candidate"]
    )
    solved = perf_counter()
    if not constraint.matches(candidate):
        raise RuntimeError(f"{method} returned an invalid witness")
    verified = perf_counter()
    return {
        "candidate": candidate,
        "seconds": {
            "load_and_digest": loaded - started,
            "solve": solved - loaded,
            "final_verification": verified - solved,
            "total": verified - started,
        },
    }


def ideal_trial(seed, workload=SENSOR_KIT):
    started = perf_counter()
    constraint = load_fixture(workload)
    loaded = perf_counter()
    oracle, synthesis = synthesize_constraint_oracle(constraint, synthesis=workload.synthesis)
    built = perf_counter()
    if (constraint.qubits, oracle.num_qubits - constraint.qubits) != (workload.search_bits, workload.work_bits):
        raise RuntimeError("oracle width differs from the prescribed search and work widths")
    audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    if audit["marked_states"] != workload.all_witnesses():
        raise RuntimeError("the oracle audit and domain rules disagree on the solution set")
    audited = perf_counter()
    marked = audit["marked_count"]
    rounds = recommended_iterations(constraint.qubits, marked)
    last_attempt = _LastAttempt()
    search = run_grover_search(
        oracle, constraint.qubits, constraint.matches,
        marked_count=marked, max_shots=IDEAL_SHOT_CAP,
        max_oracle_queries=rounds * IDEAL_SHOT_CAP, seed=seed,
        ideal_sampler="prepared_probabilities", _last_attempt=last_attempt,
    )
    sampled = perf_counter()
    circuit = last_attempt.circuit
    if circuit is None:
        raise RuntimeError("ideal search did not produce a measured circuit")
    compiled = compile_and_verify(
        circuit, target=line_target(circuit.num_qubits, seed=COMPILER_SEED),
        seed=COMPILER_SEED, prefer_native_qasm=True,
    ).report
    verification = compiled["verification"]
    single_shot = fsum(
        p for bits, p in verification["roundtrip_probabilities"].items() if constraint.matches(bits)
    )
    predicted = theoretical_success_probability(constraint.qubits, marked, rounds)
    if not isclose(single_shot, predicted, abs_tol=verification["tolerance"], rel_tol=0):
        raise RuntimeError("compiled ideal witness probability differs from the Grover prediction")
    cap_probability = 1 - (1 - predicted) ** IDEAL_SHOT_CAP
    if cap_probability < TARGET_PROBABILITY:
        raise RuntimeError("the prescribed ideal shot cap falls below the modeled target")
    compiled_at = perf_counter()
    candidate = search["candidate"]
    if candidate is not None and not constraint.matches(candidate):
        raise RuntimeError("ideal search returned an invalid witness")
    verified = perf_counter()
    return {
        "seed": seed,
        "status": search["status"],
        "candidate": candidate,
        "shots_used": search["shots_used"],
        "oracle_queries": search["oracle_queries"],
        "attempts": search["attempts"],
        "ideal_sampler": search["ideal_sampler"],
        "seconds": {
            "load_and_digest": loaded - started,
            "oracle_build": built - loaded,
            "oracle_audit_and_solution_check": audited - built,
            "search_and_per_attempt_verification": sampled - audited,
            "compile_export_and_check": compiled_at - sampled,
            "final_verification": verified - compiled_at,
            "total": verified - started,
        },
    }, {
        "synthesis": synthesis["synthesis_used"],
        "sampler": search["ideal_sampler"],
        "states_audited": audit["states_checked"],
        "marked_count": marked,
        "rounds": rounds,
        "shot_cap": IDEAL_SHOT_CAP,
        "oracle_query_cap": rounds * IDEAL_SHOT_CAP,
        "predicted_single_shot_probability": predicted,
        "exported_single_shot_probability": single_shot,
        "modeled_probability_within_cap": cap_probability,
        "compilation": {
            "target": compiled["target"]["name"],
            "transpiler_seed": compiled["seed_transpiler"],
            "depth": compiled["compiled"]["depth"],
            "two_qubit_gates": compiled["compiled"]["two_qubit_gates"],
            "openqasm": compiled["openqasm"],
            "roundtrip_total_variation_distance": verification["roundtrip_total_variation_distance"],
        },
    }


def summarize_trials(trials):
    return {
        "verified_trials": sum(row["candidate"] is not None for row in trials),
        "median_seconds": {
            phase: median(row["seconds"][phase] for row in trials)
            for phase in trials[0]["seconds"]
        },
        "trials": trials,
    }


def torino_experiment():
    started = perf_counter()
    constraint = load_fixture()
    loaded = perf_counter()
    from austins_algorithm.constraint_cover_comparison import run_constraint_cover_snapshot

    imported = perf_counter()
    report = run_constraint_cover_snapshot(
        constraint, model="torino", initial_layout=TORINO_LAYOUT,
        rounds=2, seed=COMPILER_SEED, max_active_qubits=7,
        shots=128, evaluation_seeds=(1301, 1303, 1307, 1319),
        witness_budget=WitnessBudgetRequest(TARGET_PROBABILITY, max_shots=16, validation_seeds=range(2001, 2225)),
        simulation_method="prepared_density_matrix", prefer_native_qasm=True,
    ).report
    finished = perf_counter()
    witness = report["witness_budget"]
    covers = {}
    for name, candidate in report["candidates"].items():
        compilation = candidate["compilation"]
        completed = [
            batch["timing_seconds"] for batch in witness["candidates"].get(name, {}).get("validation_batches", ())
            if batch["status"] == "COMPLETED"
        ]
        covers[name] = {
            "compiled": compilation["compiled"],
            "openqasm": compilation["openqasm"],
            "median_prepared_batch_seconds": (
                median(row["sample"] + row["verify"] for row in completed) if completed else None
            ),
        }
    return {
        "total_seconds": finished - started,
        "load_and_digest_seconds": loaded - started,
        "cover_module_import_seconds": imported - loaded,
        "snapshot": report["snapshot"],
        "witness_budget": witness,
        "covers": covers,
    }


def benchmark_prepared_line(ideal_trials, protocol, workload=SENSOR_KIT):
    started = perf_counter()
    constraint = load_fixture(workload)
    plan = PreparedConstraintLineSearch(
        constraint, synthesis=workload.synthesis, compiler_seed=COMPILER_SEED, prefer_native_qasm=True,
    )
    compiled = plan.compilation_report
    if (
        compiled["openqasm"] != protocol["compilation"]["openqasm"]
        or compiled["compiled"]["depth"] != protocol["compilation"]["depth"]
        or compiled["compiled"]["two_qubit_gates"] != protocol["compilation"]["two_qubit_gates"]
    ):
        raise RuntimeError("prepared and per-request compilations disagree")
    prepared = perf_counter()
    fields = ("seed", "status", "candidate", "shots_used", "oracle_queries", "attempts", "ideal_sampler")
    trials = []
    originals = {row["seed"]: row for row in ideal_trials}
    for seed in workload.seeds:
        requested_at = perf_counter()
        row = plan.run(
            seed=seed, max_shots=IDEAL_SHOT_CAP,
            max_oracle_queries=protocol["oracle_query_cap"],
        )
        candidate = row["candidate"]
        if candidate is not None and not constraint.matches(candidate):
            raise RuntimeError("prepared search returned an invalid witness")
        verified = perf_counter()
        original = originals.get(seed)
        if original is not None and any(row[key] != original[key] for key in fields):
            raise RuntimeError("prepared and per-request searches disagree")
        trials.append({**{key: row[key] for key in fields}, "seconds": verified - requested_at})

    baseline = fsum(row["seconds"]["total"] for row in ideal_trials)
    elapsed = fsum(row["seconds"] for row in trials)
    setup = prepared - started
    report = {
        "timing_scope": "warm prepared arm after fresh trials, paired by seed, before Torino",
        "setup_seconds": setup,
        "median_search_and_verification_seconds": median(row["seconds"] for row in trials),
        "total_search_and_verification_seconds": elapsed,
        "amortized_seconds_per_request": (setup + elapsed) / len(trials),
        "fresh_requests_total_seconds": baseline,
        "prepared_requests_total_including_setup_seconds": setup + elapsed,
        "openqasm_sha256": compiled["openqasm"]["sha256"],
        "verified_trials": sum(row["candidate"] is not None for row in trials),
        "trials": trials,
    }
    if len(ideal_trials) != len(trials):
        report["paired_fresh_requests"] = len(ideal_trials)
        report["paired_prepared_total_including_setup_seconds"] = setup + fsum(
            row["seconds"] for row in trials if row["seed"] in originals
        )
        report["ideal_reliability"] = summarize_ideal_reliability(trials)
    return report


def summarize_ideal_reliability(rows):
    # Each completed request has zero or one hit because it stops at the first witness.
    batches = [
        {
            "status": "COMPLETED",
            "simulation": {
                "verified_shots": int(row["candidate"] is not None),
                "completed_shots": row["shots_used"],
            },
        }
        for row in rows
    ]
    return {
        "one_sided_exact_confidence": WITNESS_BATCH_CONFIDENCE_PER_COVER,
        **summarize_witness_batches(batches, planned_batches=len(rows), target_probability=TARGET_PROBABILITY),
    }


def benchmark_prepared_classical(workload):
    started = perf_counter()
    constraint = load_fixture(workload)
    candidate = workload.first_witness()
    setup = perf_counter() - started
    timings = []
    for _ in workload.seeds:
        started = perf_counter()
        if not constraint.matches(candidate):
            raise RuntimeError("prepared direct constructor returned an invalid witness")
        timings.append(perf_counter() - started)
    return {
        "candidate": candidate,
        "verified_trials": len(timings),
        "setup_seconds": setup,
        "median_verification_seconds": median(timings),
        "total_including_setup_seconds": setup + fsum(timings),
        "verification_seconds": timings,
    }


def torino_width_check(workload, protocol):
    from austins_algorithm.ibm_models import run_constraint_snapshot

    started = perf_counter()
    constraint = load_fixture(workload)
    oracle, _ = synthesize_constraint_oracle(constraint, synthesis=workload.synthesis)
    circuit = build_grover_search_circuit(oracle, constraint.qubits, iterations=protocol["rounds"])
    result = run_constraint_snapshot(
        circuit, constraint.matches, model="torino", seed=COMPILER_SEED, shots=IDEAL_SHOT_CAP,
    )
    result["request_seconds"] = perf_counter() - started
    if result["status"] != "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT" or result["simulation"] is not None:
        raise RuntimeError("Torino width check requires a compile-only result")
    verification = result["compilation"]["verification"]
    hit_probability = fsum(
        p for bits, p in verification["roundtrip_probabilities"].items() if constraint.matches(bits)
    )
    if not isclose(hit_probability, protocol["predicted_single_shot_probability"],
                   abs_tol=verification["tolerance"], rel_tol=0):
        raise RuntimeError("Torino compilation changed the ideal witness probability")
    result["ideal_witness_probability"] = hit_probability
    return result


def run_benchmark(*, torino=False, reuse_verified_line=False, workload=SENSOR_KIT):
    methods = ("direct", "propagation", "scan", "ideal")
    trials = {method: [] for method in methods}
    for method in methods[:-1]:
        classical_trial(method, workload)
    _, protocol = ideal_trial(workload.seeds.start - 1, workload)  # Excluded warmup.
    # Equal counts by position and by ordered predecessor within a trial.
    orderings = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))
    fresh_seeds = workload.seeds[:workload.fresh_trials]
    for index, seed in enumerate(fresh_seeds):
        for position in orderings[index % len(orderings)]:
            method = methods[position]
            if method == "ideal":
                trial, current_protocol = ideal_trial(seed, workload)
                if current_protocol != protocol:
                    raise RuntimeError("ideal configuration changed across trials")
            else:
                trial = classical_trial(method, workload)
            trials[method].append(trial)

    contract = ("any six-bit witness" if workload is SENSOR_KIT else "any eight-bit witness")
    report = {
        "workload": workload.name,
        "formula_sha256": workload.digest,
        "contract": f"{contract} verified against the original CNF",
        "target_probability": TARGET_PROBABILITY,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "versions": {name: version(name) for name in ("qiskit", "numpy", "scipy")},
        },
        "timing_scope": "warm process, counterbalanced order, through verification before JSON serialization",
        "methods": {name: summarize_trials(rows) for name, rows in trials.items()},
        "ideal_protocol": protocol,
        "ideal_reliability": summarize_ideal_reliability(trials["ideal"]),
        "torino_experiment": None,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    if workload is TELEMETRY_POWER:
        report["widths"] = {"search_qubits": workload.search_bits, "logical_work_qubits": workload.work_bits}
    if reuse_verified_line:
        report["prepared_line"] = benchmark_prepared_line(trials["ideal"], protocol, workload)
        if workload is TELEMETRY_POWER:
            report["prepared_classical"] = benchmark_prepared_classical(workload)
    if torino:
        report["torino_experiment"] = (
            torino_experiment() if workload is SENSOR_KIT else torino_width_check(workload, protocol)
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=WORKLOADS, default="sensor-kit")
    parser.add_argument(
        "--torino", action="store_true",
        help="also run the local Torino protocol (compile only for telemetry-power)",
    )
    parser.add_argument(
        "--reuse-verified-line", action="store_true",
        help="also time repeated ideal searches with one verified line compilation",
    )
    args = parser.parse_args()
    with warnings.catch_warnings():
        # The selected importer is recorded with each export in the report.
        warnings.filterwarnings(
            "ignore", message="This is an experimental native version of the OpenQASM 3 importer.",
            module=r"qiskit\.qasm3",
        )
        report = run_benchmark(
            torino=args.torino, reuse_verified_line=args.reuse_verified_line, workload=WORKLOADS[args.workload],
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

