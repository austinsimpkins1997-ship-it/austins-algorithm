"""Benchmark bounded unknown-count search on synthetic constraints and selected budgets."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil, isclose, pi, sqrt
import platform
from random import Random
from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING, Any

from austins_algorithm.classical_constraint import classical_first_match
from austins_algorithm.constraint_oracle import _cube_operation, synthesize_constraint_oracle
from austins_algorithm.constraint_search import run_constraint_search
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.grover_search import MAX_SEARCH_WIDTH, audit_phase_oracle, run_grover_search
from austins_algorithm.placement_statistics import _placement_exact_interval
from austins_algorithm.unknown_count_planning import plan_unknown_count_search, select_unknown_count_budgets

if TYPE_CHECKING:
    from austins_algorithm.unknown_count_snapshot_cache import SnapshotRoundProfileCache


_QUBITS = 6
_SIZE = 1 << _QUBITS
_SEED_START = 4101
_WARMUP_SEED = 7
_MAX_TRIALS = 128
_FORMULAS = {
    "sparse": ((1,), (-2,), (3,), (-4,), (5,), (-6,)),
    "dense": ((1, 2, 3), (4, 5, 6)),
    "empty": ((1, 2), (1, -2), (-1, 2), (-1, -2)),
}
_BUDGETS = {
    "bounded": {"max_shots": 8, "max_oracle_queries": 5},
    "zero_query": {"max_shots": 8, "max_oracle_queries": 0},
}
_SAMPLERS = ("statevector_sampler", "prepared_probabilities")
_CLASSICAL = ("scan", "propagation")
_BUDGET_POLICIES = ("stop", "affordable")
_POLICY_SAMPLER = "prepared_probabilities"
_POLICY_MODEL_METRICS = {
    "found_rate": "found_probability",
    "shot_budget_exhaustion_rate": "shot_budget_exhaustion_probability",
    "query_budget_exhaustion_rate": "query_budget_exhaustion_probability",
    "shots_per_trial": "expected_shots",
    "oracle_queries_per_trial": "expected_oracle_queries",
    "verifier_calls_per_trial": "expected_verifier_calls",
    "round_adjustments_per_trial": "expected_round_adjustments",
}


def _witnesses(name: str) -> list[str]:
    assignments = {
        "sparse": (21,),
        "dense": tuple(value for value in range(_SIZE) if (value & 0b000111) and (value & 0b111000)),
        "empty": (),
    }
    return [format(value, f"0{_QUBITS}b") for value in assignments[name]]


def _check_search(
    search: dict[str, Any], witnesses: set[str], budget: dict[str, int], seed: int, *, search_qubits: int = _QUBITS,
) -> dict[str, Any]:
    attempts = search["attempts"]
    samples = [row["sample"] for row in attempts]
    rounds = [row["rounds"] for row in attempts]
    verified = [bits in witnesses for bits in samples]
    if (
        search["execution_mode"] != "SIMULATOR" or search["strategy"] != "BBHT_BOUNDED"
        or search["marked_count"] is not None or search["scheduled_rounds"] is not None
        or search["seed"] != seed or any(search[key] != value for key, value in budget.items())
        or search["quantum_hardware_used"] or search["provider_job_submitted"]
        or search["search_qubits"] != search_qubits
        or any(len(bits) != search_qubits or set(bits) - {"0", "1"} for bits in samples)
        or any(type(count) is not int or count < 0 for count in rounds)
        or search["shots_used"] != len(attempts) or search["verifier_calls"] != len(attempts)
        or not 1 <= len(attempts) <= budget["max_shots"]
        or search["counts"] != dict(Counter(samples)) or search["oracle_queries"] != sum(rounds)
        or sum(rounds) > budget["max_oracle_queries"]
        or [row["verified"] for row in attempts] != verified
    ):
        raise RuntimeError(f"unknown-count evidence disagrees with the requested budget or exact reference at seed {seed}")

    reason = search["exhaustion_reason"]
    if any(verified):
        if verified.count(True) != 1 or not verified[-1] or (
            search["status"], search["candidate"], reason
        ) != ("FOUND", samples[-1], None):
            raise RuntimeError(f"invalid accepted witness at seed {seed}")
    elif search["status"] != "BUDGET_EXHAUSTED" or search["candidate"] is not None or (
        reason != ("SHOT_BUDGET" if len(attempts) == budget["max_shots"] else "ORACLE_QUERY_BUDGET")
    ):
        raise RuntimeError(f"invalid exhaustion report at seed {seed}")

    return {key: value for key, value in search.items() if key not in {"simulator", "ideal_sampler"}}


def _declarative_trial(
    constraint: BooleanConstraint, reference: list[str], witnesses: set[str], budget: dict[str, int],
    seed: int, sampler: str, *, query_budget_policy: str = "stop",
) -> tuple[dict[str, Any], dict[str, float | None]]:
    report = run_constraint_search(
        constraint, strategy="unknown", seed=seed, ideal_sampler=sampler,
        query_budget_policy=query_budget_policy, **budget,
    )
    audit = report["exhaustive_oracle_audit"]
    first = reference[0] if reference else None
    if (
        audit["states_checked"] != _SIZE or audit["marked_states"] != reference
        or audit["marked_count"] != len(reference) or report["classical_first_match"]["candidate"] != first
        or report["quantum_hardware_used"] or report["provider_job_submitted"]
    ):
        raise RuntimeError("the declarative audit or scan disagrees with the independent exact reference")
    search = report["search"]
    if reference:
        stable = _check_search(search, witnesses, budget, seed)
        if stable["status"] != report["status"]:
            raise RuntimeError("declarative and controller outcomes disagree")
        used = search.get("ideal_sampler", {}).get("used", "statevector_sampler")
    else:
        if report["status"] != "PROVEN_EMPTY" or search is not None or report["simulator_search_seconds"] is not None:
            raise RuntimeError("the audited empty constraint should finish without invoking the sampler")
        stable, used = None, None

    observation = {
        "status": report["status"],
        "sampler_used": used,
        "synthesis_used": report["oracle"]["synthesis_used"],
        "oracle_qubits": report["oracle"]["qubits"],
        "search": stable,
    }
    seconds = {
        "synthesize": report["oracle"]["build_seconds"], "audit": audit["wall_seconds"],
        "embedded_scan": report["classical_first_match"]["wall_seconds"],
        "controller": report["simulator_search_seconds"], "end_to_end": report["wall_seconds"],
    }
    return observation, seconds


def _controller_only(
    oracle: Any, constraint: BooleanConstraint, budget: dict[str, int], seed: int,
    *, sampler: str = "statevector_sampler", query_budget_policy: str = "stop",
) -> tuple[dict, float]:
    started = perf_counter()
    search = run_grover_search(
        oracle, constraint.qubits, constraint.matches, seed=seed, ideal_sampler=sampler,
        query_budget_policy=query_budget_policy, **budget,
    )
    elapsed = perf_counter() - started
    if search.get("ideal_sampler", {}).get("used", "statevector_sampler") != sampler:
        raise RuntimeError(f"controller did not use its requested sampler at seed {seed}")
    return _check_search(search, set(), budget, seed), elapsed


def _summarize(observations: list[dict], searches: list[dict]) -> dict[str, Any]:
    query_stops = [row for row in searches if row["exhaustion_reason"] == "ORACLE_QUERY_BUDGET"]
    return {
        "outcomes": dict(Counter(row["status"] for row in observations)),
        "exhaustion_reasons": dict(Counter(row["exhaustion_reason"] for row in searches if row["exhaustion_reason"])),
        "shots": sum(row["shots_used"] for row in searches),
        "oracle_queries": sum(row["oracle_queries"] for row in searches),
        "verifier_calls": sum(row["verifier_calls"] for row in searches),
        "query_stops_with_unused_queries": sum(row["oracle_queries"] < row["max_oracle_queries"] for row in query_stops),
        "query_stops_with_unused_shots": sum(row["shots_used"] < row["max_shots"] for row in query_stops),
    }


def _median_timings(timings: list[dict[str, float | None]]) -> dict[str, float | None]:
    medians = {}
    for stage in timings[0]:
        samples = [row[stage] for row in timings if row[stage] is not None]
        medians[stage] = median(samples) if samples else None
    return medians


def run_benchmark(trials: int = 64) -> dict[str, Any]:
    """Run paired trials on fixed formulas and keep timing outside the outcome fingerprint."""
    if type(trials) is not int or not 1 <= trials <= _MAX_TRIALS:
        raise ValueError(f"trials must be an integer from 1 to {_MAX_TRIALS}")
    seeds = list(range(_SEED_START, _SEED_START + trials))
    observations, timings = {}, {}
    for name, clauses in _FORMULAS.items():
        constraint = BooleanConstraint(_QUBITS, clauses)
        reference = _witnesses(name)
        witnesses = set(reference)
        assignments = [format(value, f"0{_QUBITS}b") for value in range(_SIZE)]
        if [bits for bits in assignments if constraint.matches(bits)] != reference:
            raise RuntimeError(f"the {name} formula disagrees with its closed-form witness set")

        for sampler in _SAMPLERS:
            _declarative_trial(constraint, reference, witnesses, _BUDGETS["bounded"], _WARMUP_SEED, sampler)
        oracle = None
        if not reference:
            oracle, _ = synthesize_constraint_oracle(constraint)
            _controller_only(oracle, constraint, _BUDGETS["bounded"], _WARMUP_SEED)

        first = reference[0] if reference else None
        classical, classical_times = {}, {method: [] for method in _CLASSICAL}
        for index in range(trials):
            for method in (_CLASSICAL if index % 2 == 0 else _CLASSICAL[::-1]):
                row = classical_first_match(constraint, method=method)
                classical_times[method].append(row.pop("wall_seconds"))
                if row["candidate"] != first or classical.get(method, row) != row:
                    raise RuntimeError(f"the {method} solver disagrees with the exact first witness or its prior counters")
                classical[method] = row

        scenario, timing = {}, {}
        for budget_name, budget in _BUDGETS.items():
            results = {sampler: [] for sampler in _SAMPLERS}
            samples = {sampler: [] for sampler in _SAMPLERS}
            controller, controller_times = [], []
            for index, seed in enumerate(seeds):
                for sampler in (_SAMPLERS if index % 2 == 0 else _SAMPLERS[::-1]):
                    evidence, seconds = _declarative_trial(constraint, reference, witnesses, budget, seed, sampler)
                    results[sampler].append(evidence)
                    samples[sampler].append(seconds)
                if oracle is not None:
                    evidence, seconds = _controller_only(oracle, constraint, budget, seed)
                    controller.append(evidence)
                    controller_times.append(seconds)
            summary = {
                sampler: _summarize(rows, [row["search"] for row in rows if row["search"] is not None])
                for sampler, rows in results.items()
            }
            disagreements = [
                seed for seed, left, right in zip(seeds, *results.values(), strict=True)
                if (left["status"], left["search"]) != (right["status"], right["search"])
            ]
            scenario[budget_name] = {
                "samplers": results, "summary": summary, "seeded_sampler_differences": disagreements,
                "uniform_full_shot_cap_hit_probability": 1 - (1 - len(reference) / _SIZE) ** budget["max_shots"],
            }
            timing[budget_name] = {
                "samplers": {sampler: {"median": _median_timings(rows), "by_seed": rows}
                             for sampler, rows in samples.items()},
            }
            if controller:
                scenario[budget_name]["empty_controller_without_proof"] = {
                    "trials": controller, "summary": _summarize(controller, controller),
                }
                timing[budget_name]["empty_controller_without_proof"] = {
                    "median": median(controller_times), "by_seed": controller_times,
                }

        observations[name] = {
            "formula": {"schema_version": 1, "qubits": _QUBITS, "clauses": clauses},
            "constraint_sha256": constraint.digest(),
            "reference": {"assignments": _SIZE, "marked": len(reference), "first_witness": first, "witnesses": reference},
            "classical": classical,
            "budgets": scenario,
        }
        timings[name] = {
            "classical": {method: {"median": median(values), "by_seed": values}
                          for method, values in classical_times.items()},
            "budgets": timing,
        }

    configuration = {
        "seeds": seeds, "warmup_seed": _WARMUP_SEED,
        "budgets": {name: dict(budget) for name, budget in _BUDGETS.items()},
        "synthesis": "auto", "diffuser": "standard",
    }
    return _benchmark_report("LOCAL_IDEAL_UNKNOWN_COUNT_BENCHMARK", configuration, observations, timings)


def _observations_digest(semantic: dict) -> str:
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _benchmark_report(experiment: str, configuration: dict, observations: dict, timings: dict) -> dict[str, Any]:
    semantic = {"configuration": configuration, "observations": observations}
    architecture = platform.machine().lower()
    if architecture not in {"amd64", "x86_64", "x86", "i386", "i686", "arm64", "aarch64", "armv7l"}:
        architecture = None
    return {
        "schema_version": 1,
        "experiment": experiment,
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
        "environment": {
            "python": platform.python_version(), "system": platform.system(), "architecture": architecture,
            **{name: version(name) for name in ("qiskit", "numpy")},
        },
        **semantic,
        "observations_sha256": _observations_digest(semantic),
        "timing_seconds": timings,
    }


def _check_policy_pair(legacy: dict, affordable: dict, budget: dict[str, int], seed: int) -> None:
    """Check the legacy prefix and independently reconstruct the adjusted draws."""
    if affordable["query_budget_policy"] != "affordable":
        raise RuntimeError(f"affordable policy was not reported at seed {seed}")
    before = legacy["attempts"]
    after = affordable["attempts"]
    if after[:len(before)] != before:
        raise RuntimeError(f"affordable policy changed the legacy attempt prefix at seed {seed}")

    adjustments = []
    scheduler = Random(seed)
    fallback = Random(seed ^ 0xC3C3C3C3)
    window = 1.0
    limit = min(sqrt(_SIZE), affordable["max_rounds_per_shot"] + 1)
    remaining = budget["max_oracle_queries"]
    for attempt, row in enumerate(after, 1):
        drawn = scheduler.randrange(ceil(window))
        executed = drawn
        if drawn > remaining:
            executed = fallback.randrange(remaining + 1)
            adjustments.append({
                "attempt": attempt, "drawn_rounds": drawn,
                "executed_rounds": executed, "remaining_queries": remaining,
            })
        if row["rounds"] != executed or executed > remaining:
            raise RuntimeError(f"attempt {attempt} disagrees with the affordable schedule at seed {seed}")
        remaining -= executed
        window = min(window * 1.2, limit)

    if affordable["round_adjustments"] != adjustments:
        raise RuntimeError(f"invalid budget adjustment evidence at seed {seed}")
    if legacy["exhaustion_reason"] == "ORACLE_QUERY_BUDGET":
        if len(after) <= len(before) or not adjustments or adjustments[0]["attempt"] != len(before) + 1:
            raise RuntimeError(f"affordable policy did not continue past the legacy stop at seed {seed}")
    else:
        comparable = {key: value for key, value in affordable.items()
                      if key not in {"query_budget_policy", "round_adjustments"}}
        if adjustments or comparable != legacy:
            raise RuntimeError(f"affordable policy changed a legacy result that did not hit the query cap at seed {seed}")
    if affordable["status"] == "BUDGET_EXHAUSTED" and affordable["exhaustion_reason"] != "SHOT_BUDGET":
        raise RuntimeError(f"affordable policy stopped with an affordable attempt remaining at seed {seed}")


def _policy_scenario(
    constraint: BooleanConstraint, reference: list[str], budget: dict[str, int],
    seeds: list[int], *, oracle: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    witnesses = set(reference)
    observations = {policy: [] for policy in _BUDGET_POLICIES}
    timings = {policy: [] for policy in _BUDGET_POLICIES}
    for index, seed in enumerate(seeds):
        order = _BUDGET_POLICIES if index % 2 == 0 else _BUDGET_POLICIES[::-1]
        for policy in order:
            if oracle is None:
                evidence, seconds = _declarative_trial(
                    constraint, reference, witnesses, budget, seed, _POLICY_SAMPLER, query_budget_policy=policy,
                )
                if reference and evidence["sampler_used"] != _POLICY_SAMPLER:
                    raise RuntimeError(f"declarative policy trial did not use its requested sampler at seed {seed}")
            else:
                search, elapsed = _controller_only(
                    oracle, constraint, budget, seed, sampler=_POLICY_SAMPLER, query_budget_policy=policy,
                )
                evidence = {"status": search["status"], "sampler_used": _POLICY_SAMPLER, "search": search}
                seconds = {"controller": elapsed}
            observations[policy].append(evidence)
            timings[policy].append(seconds)

    searches = {policy: [row["search"] for row in rows if row["search"] is not None]
                for policy, rows in observations.items()}
    comparison = {
        "exact_legacy_prefix_trials": 0,
        "legacy_successes_kept": 0,
        "empty_proofs_kept": 0,
        "adjusted_trials": 0,
        "round_adjustments": 0,
        "added_shots": 0,
        "added_oracle_queries": 0,
        "rescued_seeds": [],
    }
    for seed, left, right in zip(seeds, observations["stop"], observations["affordable"], strict=True):
        legacy, affordable = left["search"], right["search"]
        if legacy is None or affordable is None:
            if left != right or left["status"] != "PROVEN_EMPTY":
                raise RuntimeError(f"empty proof changed under the affordable policy at seed {seed}")
            comparison["empty_proofs_kept"] += 1
            continue
        _check_policy_pair(legacy, affordable, budget, seed)
        comparison["exact_legacy_prefix_trials"] += 1
        comparison["legacy_successes_kept"] += legacy["status"] == "FOUND"
        adjustments = affordable["round_adjustments"]
        comparison["adjusted_trials"] += bool(adjustments)
        comparison["round_adjustments"] += len(adjustments)
        comparison["added_shots"] += affordable["shots_used"] - legacy["shots_used"]
        comparison["added_oracle_queries"] += affordable["oracle_queries"] - legacy["oracle_queries"]
        if legacy["status"] != "FOUND" and affordable["status"] == "FOUND":
            comparison["rescued_seeds"].append(seed)
    comparison["rescues"] = len(comparison["rescued_seeds"])

    return {
        "policies": observations,
        "summary": {policy: _summarize(rows, searches[policy]) for policy, rows in observations.items()},
        "comparison": comparison,
    }, {
        "policies": {policy: {"median": _median_timings(rows), "by_seed": rows} for policy, rows in timings.items()},
    }


def _observed_policy_metrics(rows: list[dict], seeds: list[int], budget: dict[str, int]) -> dict[str, float]:
    totals = dict.fromkeys(_POLICY_MODEL_METRICS, 0)
    for seed, row in zip(seeds, rows, strict=True):
        search = row["search"]
        status = row["status"]
        if search is None or search["seed"] != seed or search["status"] != status or any(
            search[key] != value for key, value in budget.items()
        ):
            raise ValueError("the controller observations do not match the panel seeds and budget")
        reason = search["exhaustion_reason"]
        if (status, reason) not in {
            ("FOUND", None), ("BUDGET_EXHAUSTED", "SHOT_BUDGET"), ("BUDGET_EXHAUSTED", "ORACLE_QUERY_BUDGET"),
        }:
            raise ValueError("a controller observation has an unexpected terminal outcome")
        totals["found_rate"] += status == "FOUND"
        totals["shot_budget_exhaustion_rate"] += reason == "SHOT_BUDGET"
        totals["query_budget_exhaustion_rate"] += reason == "ORACLE_QUERY_BUDGET"
        totals["shots_per_trial"] += search["shots_used"]
        totals["oracle_queries_per_trial"] += search["oracle_queries"]
        totals["verifier_calls_per_trial"] += search["verifier_calls"]
        totals["round_adjustments_per_trial"] += len(search.get("round_adjustments", []))
    return {metric: total / len(seeds) for metric, total in totals.items()}


def compare_ideal_budget_model(panel: dict[str, Any]) -> dict[str, Any]:
    """Compare saved matched-seed outcomes with an ideal model without running searches."""
    if panel.get("experiment") != "LOCAL_IDEAL_UNKNOWN_COUNT_BUDGET_POLICY_BENCHMARK":
        raise ValueError("the comparison requires an unknown-count budget policy panel")
    configuration = panel["configuration"]
    observations = panel["observations"]
    digest = _observations_digest({"configuration": configuration, "observations": observations})
    if panel["observations_sha256"] != digest:
        raise ValueError("the policy panel configuration or observations differ from the saved digest")
    seeds = configuration["seeds"]
    if not seeds or len(set(seeds)) != len(seeds) or tuple(configuration["query_budget_policies"]) != _BUDGET_POLICIES:
        raise ValueError("the comparison requires distinct matched seeds for both budget policies")

    cases = {}
    for name, case in observations.items():
        width = case["formula"]["qubits"]
        marked = case["reference"]["marked"]
        if type(width) is not int or not 1 <= width <= MAX_SEARCH_WIDTH:
            raise ValueError("the panel search width exceeds the planner limits")
        if type(marked) is not int or not 0 <= marked <= 1 << width:
            raise ValueError("the panel has an invalid solution count")
        if case["reference"]["assignments"] != 1 << width or len(case["reference"]["witnesses"]) != marked:
            raise ValueError("the panel reference disagrees with its assignment or solution count")
        budgets = {}
        for budget_name, budget in configuration["budgets"].items():
            scenario = case["budgets"][budget_name]
            controller = scenario
            proof = None
            if marked == 0:
                proof_counts = {}
                for policy in _BUDGET_POLICIES:
                    proof_rows = scenario["policies"][policy]
                    if len(proof_rows) != len(seeds) or any(
                        row["status"] != "PROVEN_EMPTY" or row["search"] is not None for row in proof_rows
                    ):
                        raise ValueError("the declarative empty case should prove emptiness before sampling")
                    proof_counts[policy] = len(proof_rows)
                proof = {"status": "PROVEN_EMPTY", "trials_by_policy": proof_counts, "sampled_shots": 0}
                controller = scenario["empty_controller_without_proof"]
            plan = plan_unknown_count_search(width, marked, **budget)
            policies = {}
            for policy in _BUDGET_POLICIES:
                expected = plan["policies"][policy]
                observed = _observed_policy_metrics(controller["policies"][policy], seeds, budget)
                policies[policy] = {
                    "modeled": {metric: expected[field] for metric, field in _POLICY_MODEL_METRICS.items()},
                    "observed": observed,
                }
            comparison = {
                "policies": policies,
                "affordable_minus_stop": {
                    basis: {metric: policies["affordable"][basis][metric] - policies["stop"][basis][metric]
                            for metric in _POLICY_MODEL_METRICS}
                    for basis in ("modeled", "observed")
                },
            }
            if proof is not None:
                comparison["declarative_empty_proof"] = proof
            budgets[budget_name] = comparison
        cases[name] = {
            "search_qubits": width,
            "solution_count": marked,
            "controller_source": "declarative_search" if marked else "empty_controller_without_proof",
            "budgets": budgets,
        }
    return {
        "source_observations_sha256": digest,
        "trials_per_policy": len(seeds),
        "modeled_basis": "Ideal Grover measurement probabilities and independent uniform schedule and fallback draws.",
        "observed_basis": "Outcome fractions and mean work over the same fixed seeds for both policies.",
        "cases": cases,
    }


def run_policy_benchmark(trials: int = 64) -> dict[str, Any]:
    """Compare query-budget policies on matched seeded local ideal trials."""
    if type(trials) is not int or not 1 <= trials <= _MAX_TRIALS:
        raise ValueError(f"trials must be an integer from 1 to {_MAX_TRIALS}")
    seeds = list(range(_SEED_START, _SEED_START + trials))
    observations, timings = {}, {}
    for name, clauses in _FORMULAS.items():
        constraint = BooleanConstraint(_QUBITS, clauses)
        reference = _witnesses(name)
        assignments = [format(value, f"0{_QUBITS}b") for value in range(_SIZE)]
        if [bits for bits in assignments if constraint.matches(bits)] != reference:
            raise RuntimeError(f"the {name} formula disagrees with its closed-form witness set")
        _policy_scenario(constraint, reference, _BUDGETS["bounded"], [_WARMUP_SEED])
        oracle = None
        if not reference:
            oracle, _ = synthesize_constraint_oracle(constraint)
            _policy_scenario(constraint, reference, _BUDGETS["bounded"], [_WARMUP_SEED], oracle=oracle)

        scenarios, times = {}, {}
        for budget_name, budget in _BUDGETS.items():
            scenario, elapsed = _policy_scenario(constraint, reference, budget, seeds)
            if oracle is not None:
                low_level, low_level_times = _policy_scenario(constraint, reference, budget, seeds, oracle=oracle)
                scenario["empty_controller_without_proof"] = low_level
                elapsed["empty_controller_without_proof"] = low_level_times
            scenarios[budget_name], times[budget_name] = scenario, elapsed
        observations[name] = {
            "formula": {"schema_version": 1, "qubits": _QUBITS, "clauses": clauses},
            "constraint_sha256": constraint.digest(),
            "reference": {
                "assignments": _SIZE, "marked": len(reference),
                "first_witness": reference[0] if reference else None, "witnesses": reference,
            },
            "budgets": scenarios,
        }
        timings[name] = {"budgets": times}

    configuration = {
        "seeds": seeds, "warmup_seed": _WARMUP_SEED,
        "budgets": {name: dict(budget) for name, budget in _BUDGETS.items()},
        "synthesis": "auto", "diffuser": "standard", "sampler": _POLICY_SAMPLER,
        "query_budget_policies": list(_BUDGET_POLICIES), "alternating_policy_order": True,
    }
    panel = _benchmark_report("LOCAL_IDEAL_UNKNOWN_COUNT_BUDGET_POLICY_BENCHMARK", configuration, observations, timings)
    panel["ideal_budget_model"] = compare_ideal_budget_model(panel)
    return panel


_HOLDOUT_SEED_START = 0x23100000
_HOLDOUT_FAMILY_CONFIDENCE = 0.95
_SNAPSHOT_SEED_START = 0x23200000
_SNAPSHOT_TRANSPILE_SEED = 7
_HOLDOUT_SUITES = {
    "sparse_and_dense": {
        "search_qubits": 6, "solution_counts": (1, 49), "target_probability": .55,
        "shot_caps": tuple(range(1, 9)), "query_caps": tuple(range(6)),
        "held_out_solution_counts": (0, 2, 16, 32, 50, 64),
    },
    "work_tradeoff": {
        "search_qubits": 2, "solution_counts": (1,), "target_probability": .9,
        "shot_caps": tuple(range(1, 11)), "query_caps": tuple(range(4)),
        "held_out_solution_counts": (2, 3),
    },
}


def _threshold_constraint(width: int, count: int) -> BooleanConstraint:
    """Encode unsigned x < count as a CNF in the package's bit order."""
    if type(count) is not int or not 0 <= count <= 1 << width:
        raise ValueError("count must be an integer inside the search space")
    if count == 0:
        return BooleanConstraint(width, ((),))
    clauses = []
    higher_ones: list[int] = []
    for literal in range(width, 0, -1):
        if (count - 1) & (1 << (literal - 1)):
            higher_ones.append(-literal)
        else:
            clauses.append((*higher_ones, -literal))
    return BooleanConstraint(width, tuple(clauses))


def _threshold_phase_oracle(width: int, count: int) -> Any:
    """Mark x < count with disjoint prefix phases and no work wires."""
    from qiskit import QuantumCircuit

    oracle = QuantumCircuit(width, name="threshold_prefix_phase")
    if count == 1 << width:
        oracle.global_phase = pi
        return oracle
    prefix = ()
    for wire in reversed(range(width)):
        bit = bool(count & (1 << wire))
        if bit:
            _cube_operation(oracle, prefix + ((wire, False),))
        prefix += ((wire, bit),)
    return oracle


def _holdout_plans(selection: dict[str, Any]) -> dict[str, Any]:
    plans: dict[str, Any] = {}
    for objective, row in selection["recommendations"].items():
        if row is None:
            raise RuntimeError("the fixed holdout grid must have a modeled feasible plan")
        signature = {
            "policy": row["policy"], "max_shots": row["max_shots"], "max_oracle_queries": row["max_oracle_queries"],
            "max_rounds_per_shot": selection["inputs"]["max_rounds_per_shot"],
        }
        for plan in plans.values():
            if plan["selected_budget"] == signature:
                plan["selected_by"].append(objective)
                break
        else:
            plans[objective] = {"selected_budget": signature, "selected_by": [objective]}
    return plans


def _holdout_case(
    width: int, count: int, seeds: list[int], plans: dict[str, Any], target: float, confidence: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    constraint = _threshold_constraint(width, count)
    reference = [format(value, f"0{width}b") for value in range(count)]
    witnesses = set(reference)
    if any(constraint.matches(format(value, f"0{width}b")) != (value < count) for value in range(1 << width)):
        raise RuntimeError("threshold constraint disagrees with the independent integer comparison")

    started = perf_counter()
    oracle, synthesis = synthesize_constraint_oracle(constraint, synthesis="unit-reduced")
    audit = audit_phase_oracle(oracle, width, constraint.matches)
    setup_seconds = perf_counter() - started
    if audit != {"states_checked": 1 << width, "marked_count": count, "marked_states": reference}:
        raise RuntimeError("phase oracle disagrees with the exhaustive threshold reference")

    arms, timing = {}, {}
    for name, plan in plans.items():
        selected = plan["selected_budget"]
        policy = selected["policy"]
        budget = {key: selected[key] for key in ("max_shots", "max_oracle_queries", "max_rounds_per_shot")}
        modeled = plan_unknown_count_search(width, count, **budget)["policies"][policy]
        rows, seconds = [], []
        for seed in seeds:
            started = perf_counter()
            report = run_grover_search(
                oracle, width, constraint.matches, seed=seed, ideal_sampler=_POLICY_SAMPLER,
                query_budget_policy=policy, **budget,
            )
            seconds.append(perf_counter() - started)
            if (
                report.get("query_budget_policy", "stop") != policy
                or report.get("ideal_sampler", {}).get("used") != _POLICY_SAMPLER
            ):
                raise RuntimeError(f"holdout controller did not use its selected policy and sampler at seed {seed}")
            if policy == "affordable" and report["exhaustion_reason"] == "ORACLE_QUERY_BUDGET":
                raise RuntimeError(f"affordable controller ended before using its shot cap at seed {seed}")
            rows.append(_check_search(report, witnesses, budget, seed, search_qubits=width))

        observed = _observed_policy_metrics([{"status": row["status"], "search": row} for row in rows], seeds, budget)
        hits = sum(row["status"] == "FOUND" for row in rows)
        lower, upper = _placement_exact_interval(hits, len(seeds), confidence)
        if lower >= target:
            target_comparison = "INTERVAL_AT_OR_ABOVE_TARGET"
        elif upper < target:
            target_comparison = "INTERVAL_BELOW_TARGET"
        else:
            target_comparison = "INTERVAL_OVERLAPS_TARGET"
        arms[name] = {
            "modeled": modeled, "observed": {"witnesses": hits, "trials": len(seeds), **observed},
            "interval": {"lower": lower, "upper": upper, "target_comparison": target_comparison,
                         "model_probability_inside": lower <= modeled["found_probability"] <= upper},
            "trials": rows,
        }
        timing[name] = {"median_controller": median(seconds), "controller_by_seed": seconds}
    case = {
        "reference": {
            "states_checked": 1 << width, "solution_count": count, "witnesses": reference,
            "constraint_sha256": constraint.digest(), "synthesis_used": synthesis["synthesis_used"],
        },
        "seeds": seeds, "plans": arms,
    }
    return case, {"oracle_synthesis_and_audit": setup_seconds, "plans": timing}


def run_selection_holdout_benchmark(trials: int = 64) -> dict[str, Any]:
    """Test fixed selected budgets on fresh seeds and undeclared solution counts."""
    if type(trials) is not int or not 1 <= trials <= _MAX_TRIALS:
        raise ValueError(f"trials must be an integer from 1 to {_MAX_TRIALS}")

    suites = {}
    for name, suite in _HOLDOUT_SUITES.items():
        selection = select_unknown_count_budgets(**{key: value for key, value in suite.items()
                                                   if key != "held_out_solution_counts"})
        suites[name] = {"selection": selection, "plans": _holdout_plans(selection)}
    arms = sum(len(suites[name]["plans"]) * (len(suite["solution_counts"]) + len(suite["held_out_solution_counts"]))
               for name, suite in _HOLDOUT_SUITES.items())
    confidence = 1 - (1 - _HOLDOUT_FAMILY_CONFIDENCE) / arms
    next_seed = _HOLDOUT_SEED_START
    timings = {}
    for name, suite in _HOLDOUT_SUITES.items():
        entry = suites[name]
        entry["cases"] = {}
        timings[name] = {}
        for role, counts in (("DECLARED", suite["solution_counts"]), ("HOLDOUT", suite["held_out_solution_counts"])):
            for count in counts:
                seeds = list(range(next_seed, next_seed + trials))
                next_seed += _MAX_TRIALS
                case, elapsed = _holdout_case(
                    suite["search_qubits"], count, seeds, entry["plans"], suite["target_probability"], confidence,
                )
                entry["cases"][str(count)] = {"role": role, **case}
                timings[name][str(count)] = elapsed

    configuration = {
        "trials_per_arm": trials,
        "sampler": _POLICY_SAMPLER,
        "controller_only": True,
        "seed_schedule": {"start": _HOLDOUT_SEED_START, "case_stride": _MAX_TRIALS, "matched_within_case": True},
        "confidence_interval": {
            "method": "CLOPPER_PEARSON_BONFERRONI",
            "family_confidence": _HOLDOUT_FAMILY_CONFIDENCE,
            "per_arm_confidence": confidence,
            "arms": arms,
            "unit": "one independently seeded full search",
        },
    }
    return _benchmark_report("LOCAL_IDEAL_UNKNOWN_COUNT_SELECTION_HOLDOUT", configuration, suites, timings)


def _snapshot_holdout_case(
    width: int, count: int, seeds: list[int], plans: dict[str, Any], model: str,
    *, cache: SnapshotRoundProfileCache | None = None, initial_layout: Sequence[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from austins_algorithm.unknown_count_snapshot import compare_unknown_count_snapshot

    constraint = _threshold_constraint(width, count)
    states = [format(value, f"0{width}b") for value in range(1 << width)]
    reference = states[:count]
    if [bits for bits in states if constraint.matches(bits)] != reference:
        raise RuntimeError("threshold constraint disagrees with the independent integer comparison")

    oracle = _threshold_phase_oracle(width, count)
    budgets = {name: plan["selected_budget"] for name, plan in plans.items()}
    comparison_options: dict[str, Any] = {}
    if cache is not None:
        comparison_options["cache"] = cache
    if initial_layout is not None:
        comparison_options["initial_layout"] = initial_layout
    comparison = compare_unknown_count_snapshot(
        oracle, width, constraint.matches, plans=budgets, seeds=seeds,
        model=model, seed_transpiler=_SNAPSHOT_TRANSPILE_SEED, **comparison_options,
    )
    if comparison["seeds"] != seeds or comparison["requested_plans"] != budgets or (
        comparison["snapshot"]["model"] != model or comparison["seed_transpiler"] != _SNAPSHOT_TRANSPILE_SEED
        or comparison["provider_job_submitted"] or comparison["quantum_hardware_used"]
    ):
        raise RuntimeError("the snapshot comparison disagrees with its benchmark request")
    audit = comparison["audit"]
    completed = comparison["status"] == "COMPLETED"
    if (completed or audit is not None) and audit != {
        "states_checked": 1 << width, "marked_count": count, "marked_states": reference,
    }:
        raise RuntimeError("the snapshot phase oracle disagrees with the exhaustive threshold reference")

    if completed:
        if initial_layout is not None and comparison["anchor_layout"] != list(initial_layout):
            raise RuntimeError("the snapshot comparison did not use its requested initial layout")
        if comparison["plans"].keys() != budgets.keys():
            raise RuntimeError("the snapshot comparison did not return every requested plan")
        for name, budget in budgets.items():
            caps = {key: budget[key] for key in ("max_shots", "max_oracle_queries", "max_rounds_per_shot")}
            modeled = plan_unknown_count_search(width, count, **caps)["policies"][budget["policy"]]
            lane = comparison["plans"][name]["lanes"]["ideal"]
            if any(not isclose(value, lane["modeled"][key], rel_tol=0, abs_tol=1e-8) for key, value in modeled.items()):
                raise RuntimeError(f"compiled ideal model disagrees with the original planner for {name}")

    elapsed = comparison.pop("timing_seconds")
    return {
        "reference": {
            "states_checked": 1 << width, "solution_count": count, "witnesses": reference,
            "constraint_sha256": constraint.digest(), "synthesis_used": "disjoint-prefix",
        },
        "comparison": comparison,
    }, elapsed


def run_selection_snapshot_benchmark(
    trials: int = 64, *, model: str = "jakarta", cache: SnapshotRoundProfileCache | None = None,
    suite: str | None = None, initial_layout: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compare adaptive runs, optionally selecting one suite and a fixed layout."""
    if type(trials) is not int or not 1 <= trials <= _MAX_TRIALS:
        raise ValueError(f"trials must be an integer from 1 to {_MAX_TRIALS}")
    if suite is not None and (not isinstance(suite, str) or suite not in _HOLDOUT_SUITES):
        raise ValueError(f"suite must be one of {', '.join(_HOLDOUT_SUITES)}")
    layout = None
    if initial_layout is not None:
        if suite is None:
            raise ValueError("initial_layout requires a named snapshot suite")
        width = _HOLDOUT_SUITES[suite]["search_qubits"]
        message = f"initial_layout must contain {width} distinct nonnegative integer wires"
        if (
            not isinstance(initial_layout, Sequence) or isinstance(initial_layout, (str, bytes, bytearray))
            or len(initial_layout) != width
        ):
            raise ValueError(message)
        layout = list(initial_layout)
        if len(layout) != width or any(type(wire) is not int or wire < 0 for wire in layout) or len(set(layout)) != width:
            raise ValueError(message)

    from austins_algorithm.unknown_count_snapshot import SNAPSHOT_SEARCH_MODELS

    if model not in SNAPSHOT_SEARCH_MODELS:
        raise ValueError(f"model must be one of {', '.join(SNAPSHOT_SEARCH_MODELS)}")

    case_options: dict[str, Any] = {}
    if cache is not None:
        case_options["cache"] = cache
    if layout is not None:
        case_options["initial_layout"] = layout
    suites, timings = {}, {}
    next_seed = _SNAPSHOT_SEED_START
    statuses: Counter[str] = Counter()
    unexpected_incomplete = False
    local_timeout = False
    for name, workload in _HOLDOUT_SUITES.items():
        declared = workload["solution_counts"]
        held_out = workload["held_out_solution_counts"]
        if suite is not None and name != suite:
            next_seed += (len(declared) + len(held_out)) * _MAX_TRIALS
            continue
        selection = select_unknown_count_budgets(**{key: value for key, value in workload.items()
                                                   if key != "held_out_solution_counts"})
        plans = _holdout_plans(selection)
        cases = {}
        suites[name] = {"selection": selection, "plans": plans, "cases": cases}
        timings[name] = {}
        width = workload["search_qubits"]
        for role, counts in (("DECLARED", declared), ("HOLDOUT", held_out)):
            for count in counts:
                seeds = list(range(next_seed, next_seed + trials))
                next_seed += _MAX_TRIALS
                if local_timeout:
                    status = "NOT_RUN_AFTER_LOCAL_TIMEOUT"
                    statuses[status] += 1
                    cases[str(count)] = {
                        "role": role, "reference": {"solution_count": count},
                        "comparison": {"status": status, "seeds": seeds},
                    }
                    timings[name][str(count)] = None
                    continue
                case, elapsed = _snapshot_holdout_case(width, count, seeds, plans, model, **case_options)
                comparison = case["comparison"]
                status = comparison["status"]
                statuses[status] += 1
                local_timeout = status == "LOCAL_RESULT_WAIT_TIMED_OUT"
                if model == "manila" and width == 6:
                    unexpected_incomplete |= not (
                        status == "INSUFFICIENT_TARGET_WIDTH" and comparison["circuit_qubits"] == width
                        and comparison["audit"] is None and not comparison["compilations"] and not comparison["plans"]
                    )
                else:
                    unexpected_incomplete |= status != "COMPLETED"
                cases[str(count)] = {"role": role, **case}
                timings[name][str(count)] = elapsed

    configuration = {
        "model": model, "seed_transpiler": _SNAPSHOT_TRANSPILE_SEED, "trials_per_arm": trials,
        "seed_schedule": {"start": _SNAPSHOT_SEED_START, "case_stride": _MAX_TRIALS, "matched_within_case": True},
        "sampling": "categorical draws from compiled ideal and snapshot density/readout distributions",
        "controller_only": True,
    }
    if suite is not None:
        configuration["suite"] = suite
    if layout is not None:
        configuration["initial_layout"] = layout
    panel = _benchmark_report("LOCAL_UNKNOWN_COUNT_SELECTION_SNAPSHOT_COMPARISON", configuration, suites, timings)
    panel["status"] = (
        "INCOMPLETE" if unexpected_incomplete else
        "COMPLETED_WITH_BLOCKED_CASES" if statuses["INSUFFICIENT_TARGET_WIDTH"] else "COMPLETED"
    )
    panel["case_statuses"] = dict(statuses)
    return panel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=64, help="number of fixed seeds per formula and budget")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--compare-budget-policies", action="store_true", help="compare stop and affordable policies with one matched sampler",
    )
    mode.add_argument(
        "--validate-budget-selection", action="store_true", help="test selected budgets with fresh seeds and held-out counts",
    )
    mode.add_argument(
        "--compare-budget-snapshot", action="store_true", help="compare selected budgets on ideal and IBM snapshot distributions",
    )
    parser.add_argument("--snapshot-model", choices=("jakarta", "manila"), help="local model for snapshot comparison (default: jakarta)")
    parser.add_argument("--snapshot-suite", choices=tuple(_HOLDOUT_SUITES), help="run one snapshot benchmark suite")
    parser.add_argument(
        "--snapshot-layout", type=int, nargs="+", metavar="WIRE", help="fixed physical wires for the selected suite",
    )
    parser.add_argument(
        "--reuse-round-profiles", action="store_true", help="reuse eligible profiles within a snapshot comparison",
    )
    args = parser.parse_args()
    if not 1 <= args.trials <= _MAX_TRIALS:
        parser.error(f"--trials must be from 1 to {_MAX_TRIALS}")
    if args.snapshot_model is not None and not args.compare_budget_snapshot:
        parser.error("--snapshot-model requires --compare-budget-snapshot")
    if args.snapshot_suite is not None and not args.compare_budget_snapshot:
        parser.error("--snapshot-suite requires --compare-budget-snapshot")
    if args.snapshot_layout is not None and not args.compare_budget_snapshot:
        parser.error("--snapshot-layout requires --compare-budget-snapshot")
    if args.snapshot_layout is not None and args.snapshot_suite is None:
        parser.error("--snapshot-layout requires --snapshot-suite")
    if args.reuse_round_profiles and not args.compare_budget_snapshot:
        parser.error("--reuse-round-profiles requires --compare-budget-snapshot")
    if args.compare_budget_snapshot:
        options: dict[str, Any] = {"model": args.snapshot_model or "jakarta"}
        if args.snapshot_suite is not None:
            options["suite"] = args.snapshot_suite
        if args.snapshot_layout is not None:
            options["initial_layout"] = args.snapshot_layout
        if args.reuse_round_profiles:
            from austins_algorithm.unknown_count_snapshot_cache import SnapshotRoundProfileCache

            options["cache"] = SnapshotRoundProfileCache()
        try:
            report = run_selection_snapshot_benchmark(args.trials, **options)
        except ValueError as exc:
            if args.snapshot_layout is None:
                raise
            parser.error(str(exc))
        print(json.dumps(report, indent=2, allow_nan=False))
        if report["status"] == "INCOMPLETE":
            raise SystemExit(1)
        return
    if args.validate_budget_selection:
        benchmark = run_selection_holdout_benchmark
    else:
        benchmark = run_policy_benchmark if args.compare_budget_policies else run_benchmark
    print(json.dumps(benchmark(args.trials), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
