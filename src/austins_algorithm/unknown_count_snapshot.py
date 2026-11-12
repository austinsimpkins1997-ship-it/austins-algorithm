"""Compare full adaptive searches on ideal and historical IBM noise models."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from importlib.metadata import version
from itertools import islice
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .compilation import _transpile_initial_layout, compile_and_verify
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import (
    _run_search_attempts, _unknown_round_choices, _validate_query_budget_policy,
    audit_phase_oracle, build_grover_search_circuit,
)
from .ibm_models import (
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, _compact_snapshot_inputs, _local_limits,
    _prepare_density_snapshot, _prepared_snapshot_probabilities, _snapshot_identity, _snapshot_specs,
)
from .unknown_count_planning import _policy_checkpoints
from .unknown_count_snapshot_cache import SnapshotRoundProfileCache, _cache_scope, _round_profile_key

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_ibm_runtime.fake_provider import FakeBackendV2

SNAPSHOT_SEARCH_MODELS = ("manila", "jakarta")
MAX_SNAPSHOT_SEARCH_TRIALS = 128
MAX_SNAPSHOT_SEARCH_PLANS = 4
MAX_SNAPSHOT_SEARCH_SHOTS = 32
MAX_SNAPSHOT_SEARCH_QUERIES = 64
_BUDGET_KEYS = {"policy", "max_shots", "max_oracle_queries", "max_rounds_per_shot"}


def _validate_experiment(
    plans: Mapping[str, Mapping[str, Any]], seeds: Sequence[int], model: str, seed_transpiler: int,
) -> tuple[dict[str, dict[str, Any]], list[int]]:
    if model not in SNAPSHOT_SEARCH_MODELS:
        raise ValueError(f"model must be one of {', '.join(SNAPSHOT_SEARCH_MODELS)}")
    _bounded_int(seed_transpiler, 0, 2**32 - 1, "seed_transpiler")
    if not isinstance(plans, Mapping) or not 1 <= len(plans) <= MAX_SNAPSHOT_SEARCH_PLANS:
        raise ValueError(f"plans must contain 1 to {MAX_SNAPSHOT_SEARCH_PLANS} named budgets")
    copied = {}
    for name, budget in plans.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("plan names must contain 1 to 100 characters")
        if not isinstance(budget, Mapping) or set(budget) != _BUDGET_KEYS:
            raise ValueError("each plan requires policy, max_shots, max_oracle_queries, and max_rounds_per_shot")
        _validate_query_budget_policy(budget["policy"], known_count=False)
        _bounded_int(budget["max_shots"], 1, MAX_SNAPSHOT_SEARCH_SHOTS, "max_shots")
        _bounded_int(budget["max_oracle_queries"], 0, MAX_SNAPSHOT_SEARCH_QUERIES, "max_oracle_queries")
        _bounded_int(budget["max_rounds_per_shot"], 0, MAX_ITERATIONS, "max_rounds_per_shot")
        copied[name] = dict(budget)
    if not isinstance(seeds, Sequence) or not 1 <= len(seeds) <= MAX_SNAPSHOT_SEARCH_TRIALS:
        raise ValueError(f"seeds must contain 1 to {MAX_SNAPSHOT_SEARCH_TRIALS} trial seeds")
    trial_seeds = [_bounded_int(seed, 0, 2**32 - 1, "trial seed") for seed in seeds]
    if len(set(trial_seeds)) != len(trial_seeds):
        raise ValueError("trial seeds must be distinct")
    return copied, trial_seeds


def _modeled_and_sampled(
    programs: dict[int, dict[str, Any]], distributions: dict[int, dict[str, float]],
    width: int, verifier: Callable[[str], bool], witnesses: set[str],
    budget: dict[str, Any], windows: list[int], seeds: list[int],
) -> tuple[dict[str, Any], dict[str, float]]:
    from numpy import asarray
    from numpy.random import default_rng

    modeled_started = perf_counter()
    weights = {}
    outcomes = [format(value, f"0{width}b") for value in range(1 << width)]
    probabilities = []
    outcome_set = set(outcomes)
    for rounds in range(min(max(windows) - 1, budget["max_oracle_queries"]) + 1):
        distribution = distributions[rounds]
        values = [distribution.get(bits, 0.0) for bits in outcomes]
        mass = math.fsum(values)
        if (
            not math.isclose(mass, 1.0, abs_tol=1e-9, rel_tol=0.0)
            or any(value < 0 or not math.isfinite(value) for value in values)
            or distribution.keys() - outcome_set
        ):
            raise RuntimeError("round distribution must have normalized, nonnegative bitstring probabilities")
        hit = math.fsum(value for bits, value in zip(outcomes, values) if bits in witnesses) / mass
        miss = math.fsum(value for bits, value in zip(outcomes, values) if bits not in witnesses) / mass
        probabilities.append((hit, miss))
        weights[rounds] = asarray(values, dtype=float) / mass

    def measure(rounds: int, seed: int) -> str:
        return outcomes[default_rng(seed).choice(len(outcomes), p=weights[rounds])]

    policy = budget["policy"]
    caps = {key: budget[key] for key in ("max_shots", "max_oracle_queries", "max_rounds_per_shot")}
    modeled = _policy_checkpoints(windows, caps["max_oracle_queries"], probabilities, policy, [caps["max_shots"]])
    modeled_seconds = perf_counter() - modeled_started
    sample_started = perf_counter()
    trials = [
        {"seed": seed, **_run_search_attempts(
            width, verifier, measure, fixed_rounds=None, seed=seed, query_budget_policy=policy, **caps,
        )}
        for seed in seeds
    ]
    sample_seconds = perf_counter() - sample_started
    summary_started = perf_counter()
    rounds_used = Counter(row["rounds"] for trial in trials for row in trial["attempts"])
    terminal = Counter(trial["exhaustion_reason"] or "FOUND" for trial in trials)
    for trial in trials:
        if any(attempt["verified"] != (attempt["sample"] in witnesses) for attempt in trial["attempts"]):
            raise RuntimeError("trial verifier disagrees with the exhaustive audit")
    observed = {
        "witnesses": terminal["FOUND"],
        "trials": len(seeds),
        "found_rate": terminal["FOUND"] / len(seeds),
        "shot_budget_exhaustion_rate": terminal["SHOT_BUDGET"] / len(seeds),
        "query_budget_exhaustion_rate": terminal["ORACLE_QUERY_BUDGET"] / len(seeds),
        "shots": sum(trial["shots_used"] for trial in trials),
        "oracle_queries": sum(trial["oracle_queries"] for trial in trials),
        "verifier_calls": sum(trial["verifier_calls"] for trial in trials),
        "round_adjustments": sum(len(trial.get("round_adjustments", ())) for trial in trials),
        "shots_by_round": {str(rounds): count for rounds, count in sorted(rounds_used.items())},
        "compiled_two_qubit_gates": sum(
            count * programs[rounds]["compilation"]["compiled"]["two_qubit_gates"]
            for rounds, count in rounds_used.items()
        ),
    }
    return (
        {"modeled": modeled[caps["max_shots"]], "observed": observed, "trials": trials},
        {"model_and_weights": modeled_seconds, "sample_and_verify": sample_seconds,
         "audit_and_aggregate": perf_counter() - summary_started},
    )


def _initial_layout(layout: Sequence[int], width: int, target_width: int) -> list[int]:
    if not isinstance(layout, Sequence) or len(layout) != width:
        raise ValueError("initial_layout requires one target qubit per oracle wire")
    wires = [_bounded_int(wire, 0, target_width - 1, "initial_layout wire") for wire in layout]
    if len(set(wires)) != len(wires):
        raise ValueError("initial_layout wires must be distinct")
    return wires


def _prepare_round_profiles(
    phase_oracle: QuantumCircuit, witnesses: set[str], backend: FakeBackendV2,
    simulator_type: type[AerSimulator], report: dict[str, Any],
    initial_layout: Sequence[int] | None, cache: SnapshotRoundProfileCache | None,
) -> bool:
    width, seed = report["search_qubits"], report["seed_transpiler"]
    timings = report["timing_seconds"]
    highest_round = report["rounds_to_prepare"][-1]
    cache_run = {"hits": 0, "misses": 0, "bypassed": 0, "published": 0, "keys": {}}
    guard = nullcontext() if cache is None else cache._prepare()
    with guard:
        try:
            anchor_started = perf_counter()
            anchor_circuit = build_grover_search_circuit(phase_oracle, width, iterations=highest_round)
            if initial_layout is None:
                anchor = _transpile_initial_layout(anchor_circuit, backend=backend, seed=seed)
                if anchor is None:
                    raise RuntimeError("the snapshot compiler did not report an initial layout")
            else:
                anchor = _initial_layout(initial_layout, phase_oracle.num_qubits, backend.num_qubits)
            report["anchor_layout"] = anchor
            timings["anchor_layout"] = perf_counter() - anchor_started
            scope = None
            if cache is not None:
                identity_started = perf_counter()
                try:
                    scope = _cache_scope(backend, report["snapshot"], report["versions"], seed)
                except (AttributeError, TypeError, ValueError):
                    # If a target cannot be fingerprinted, use the uncached path.
                    pass
                timings["cache_identity"] = perf_counter() - identity_started

            compiled, pending = {}, {}
            for rounds in report["rounds_to_prepare"]:
                label = str(rounds)
                circuit_started = perf_counter()
                circuit = anchor_circuit if rounds == highest_round else build_grover_search_circuit(
                    phase_oracle, width, iterations=rounds,
                )
                round_timing = {"circuit_build": perf_counter() - circuit_started}
                timings["rounds"][label] = round_timing
                if cache is not None:
                    lookup_started = perf_counter()
                    key = None if scope is None else _round_profile_key(circuit, anchor, scope)
                    entry = None if key is None else cache._get(key)
                    round_timing["cache_lookup"] = perf_counter() - lookup_started
                    cache_run["keys"][label] = key
                    if entry is not None:
                        distribution = entry["snapshot_probabilities"]["outcome_probabilities"]
                        entry["snapshot_probabilities"]["verified_hit_probability"] = min(
                            1.0, math.fsum(value for bits, value in distribution.items() if bits in witnesses),
                        )
                        report["compilations"][label] = entry
                        cache_run["hits"] += 1
                        continue
                    if key is not None:
                        pending[rounds] = key
                    cache_run["bypassed" if key is None else "misses"] += 1

                compile_started = perf_counter()
                program = compile_and_verify(circuit, backend=backend, initial_layout=anchor, seed=seed)
                round_timing["compile_export_verify"] = perf_counter() - compile_started
                compiled[rounds] = program
                report["compilations"][label] = {"compilation": program.report}
                active = program.report["openqasm"]["export_wire_to_target_qubit"]
                if len(active) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
                    report.update(status="COMPILE_ONLY_ACTIVE_QUBIT_LIMIT", blocked_round=rounds)
                    return False

            if compiled:
                noise_started = perf_counter()
                compact = _compact_snapshot_inputs(backend, compiled, simulator_type)
                timings["noise_model_setup"] = perf_counter() - noise_started
                for rounds, (circuit, simulator) in compact.items():
                    label = str(rounds)
                    try:
                        replay, preparation = _prepare_density_snapshot(circuit, simulator, backend.name)
                    except (KeyboardInterrupt, SystemExit):
                        if cache is not None:
                            cache._unconfirmed = True
                        raise
                    timings["rounds"][label]["density_preparation"] = preparation.pop("wall_seconds")
                    entry = report["compilations"][label]
                    entry["density_preparation"] = preparation
                    if replay is None:
                        cancelled = preparation["local_job_cancelled"]
                        if cache is not None and not cancelled:
                            cache._unconfirmed = True
                        report.update(status=preparation["status"], blocked_round=rounds, local_job_cancelled=cancelled)
                        return False
                    model_started = perf_counter()
                    entry["snapshot_probabilities"] = _prepared_snapshot_probabilities(
                        *replay, verifier=witnesses.__contains__,
                    )
                    timings["rounds"][label]["readout_distribution"] = perf_counter() - model_started

            if cache is not None and pending:
                staged = {}
                for rounds, key in pending.items():
                    entry = report["compilations"][str(rounds)]
                    distribution = entry["snapshot_probabilities"]["outcome_probabilities"]
                    staged[key] = {**entry, "snapshot_probabilities": {"outcome_probabilities": distribution}}
                cache_run["published"] = cache._publish(staged)
            return True
        finally:
            if cache is not None:
                timings["round_profile_cache"] = {**cache_run, **cache.stats()}


def compare_unknown_count_snapshot(
    phase_oracle: QuantumCircuit, search_qubits: int, verifier: Callable[[str], bool], *,
    plans: Mapping[str, Mapping[str, Any]], seeds: Sequence[int], model: str = "jakarta", seed_transpiler: int = 7,
    initial_layout: Sequence[int] | None = None, cache: SnapshotRoundProfileCache | None = None,
) -> dict[str, Any]:
    """Compile reached round choices once and compare complete seeded searches.

    The compiled circuit supplies the ideal distribution and the snapshot
    density/readout distribution. Each lane makes its own stop decision from
    one fresh categorical measurement per executed attempt. The oracle and
    deterministic verifier must agree across the exhaustively audited space.
    An explicit initial layout fixes the starting physical wires. A caller-owned
    cache can reuse verified probabilities while each request audits its oracle
    and independently runs its supplied seeds and plans.
    """
    _bounded_int(search_qubits, 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "search_qubits")
    budgets, trial_seeds = _validate_experiment(plans, seeds, model, seed_transpiler)
    if not callable(verifier):
        raise TypeError("verifier must be callable")
    if cache is not None and not isinstance(cache, SnapshotRoundProfileCache):
        raise TypeError("cache must be a SnapshotRoundProfileCache")

    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator

    if not isinstance(phase_oracle, QuantumCircuit):
        raise TypeError("phase_oracle must be a Qiskit QuantumCircuit")
    started = perf_counter()
    factory, _ = _snapshot_specs()[model]
    backend = factory()
    windows = {name: list(islice(_unknown_round_choices(search_qubits, budget["max_rounds_per_shot"]),
                                 budget["max_shots"])) for name, budget in budgets.items()}
    highest_round = max(min(max(windows[name]) - 1, budget["max_oracle_queries"]) for name, budget in budgets.items())
    timings: dict[str, Any] = {"rounds": {}, "plans": {}}
    report: dict[str, Any] = {
        "status": "PENDING",
        "execution_mode": "SIMULATOR",
        "sampling": "CATEGORICAL_FROM_PREPARED_IDEAL_AND_DENSITY_READOUT_DISTRIBUTIONS",
        "compiled_gate_work_basis": "NOMINAL_GATE_COUNTS_WEIGHTED_BY_CATEGORICAL_ATTEMPTS",
        "search_qubits": search_qubits,
        "snapshot": {"model": model, **_snapshot_identity(backend)},
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")},
        "limits": {**_local_limits(), "trials_per_plan": MAX_SNAPSHOT_SEARCH_TRIALS,
                   "plans": MAX_SNAPSHOT_SEARCH_PLANS, "shots_per_trial": MAX_SNAPSHOT_SEARCH_SHOTS,
                   "oracle_queries_per_trial": MAX_SNAPSHOT_SEARCH_QUERIES},
        "requested_plans": budgets,
        "seeds": trial_seeds,
        "seed_transpiler": seed_transpiler,
        "rounds_to_prepare": list(range(highest_round + 1)),
        "audit": None, "anchor_layout": None, "compilations": {}, "plans": {},
        "timing_seconds": timings,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if phase_oracle.num_qubits > min(backend.num_qubits, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS):
        report.update(status="INSUFFICIENT_TARGET_WIDTH", circuit_qubits=phase_oracle.num_qubits)
        timings["wall"] = perf_counter() - started
        return report

    audit_started = perf_counter()
    audit = audit_phase_oracle(phase_oracle, search_qubits, verifier)
    witnesses = set(audit["marked_states"])
    report["audit"] = audit
    timings["oracle_audit"] = perf_counter() - audit_started
    if not _prepare_round_profiles(phase_oracle, witnesses, backend, AerSimulator, report, initial_layout, cache):
        timings["wall"] = perf_counter() - started
        return report
    round_reports = {int(rounds): entry for rounds, entry in report["compilations"].items()}
    distributions = {
        "ideal": {rounds: entry["compilation"]["verification"]["compiled_probabilities"]
                  for rounds, entry in round_reports.items()},
        "snapshot": {rounds: entry["snapshot_probabilities"]["outcome_probabilities"]
                     for rounds, entry in round_reports.items()},
    }

    for name, budget in budgets.items():
        lanes = {}
        timings["plans"][name] = {}
        for lane, distribution in distributions.items():
            lanes[lane], timings["plans"][name][lane] = _modeled_and_sampled(
                round_reports, distribution, search_qubits, verifier, witnesses, budget, windows[name], trial_seeds,
            )
        paired = Counter(
            ("found" if ideal["status"] == "FOUND" else "miss") + "_" +
            ("found" if noisy["status"] == "FOUND" else "miss")
            for ideal, noisy in zip(lanes["ideal"]["trials"], lanes["snapshot"]["trials"], strict=True)
        )
        report["plans"][name] = {"budget": budget, "lanes": lanes, "paired_witness_outcomes": dict(sorted(paired.items()))}
    report["status"] = "COMPLETED"
    timings["wall"] = perf_counter() - started
    return report
