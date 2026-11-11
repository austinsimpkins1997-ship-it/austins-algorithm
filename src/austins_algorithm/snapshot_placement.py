"""Check modulo-4 search against a hit-rate floor on IBM snapshots."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from importlib.metadata import version
from time import perf_counter
from typing import Any

from .compilation import compile_and_verify
from .ibm_models import (
    IBM_SNAPSHOT_MODELS,
    MAX_MOD4_SWEEP_SHOTS,
    _local_limits,
    _sample_snapshot,
    _snapshot_simulator,
    _snapshot_specs,
    _validate_snapshot_request,
)

ROUNDS = (1, 2)
PLACEMENTS_PER_MODEL = 2
FAMILY_CONFIDENCE = 0.95


def _alternate_placement(anchor: Sequence[int], edges: Iterable[tuple[int, int]]) -> list[int] | None:
    """Move one logical wire to the nearest spare that keeps the footprint connected."""
    neighbors: dict[int, set[int]] = {}
    for a, b in edges:
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
    footprint = set(anchor)

    def distances(start: int, allowed: set[int]) -> dict[int, int]:
        found = {start: 0}
        pending = deque([start])
        while pending:
            wire = pending.popleft()
            for next_wire in neighbors.get(wire, set()) & allowed:
                if next_wire not in found:
                    found[next_wire] = found[wire] + 1
                    pending.append(next_wire)
        return found

    choices = []
    all_wires = set(neighbors)
    for index, wire in enumerate(anchor):
        for spare, distance in distances(wire, all_wires).items():
            if spare in footprint:
                continue
            changed = (footprint - {wire}) | {spare}
            if len(distances(spare, changed)) == len(changed):
                choices.append((distance, index, spare))
    if not choices:
        return None
    _, index, spare = min(choices)
    layout = list(anchor)
    layout[index] = spare
    return layout


def _evaluate_trials(
    trials: list[dict[str, Any]], *, expected_batches: int, confidence: float, floor: float,
) -> dict[str, Any]:
    if len(trials) != expected_batches or any(trial["status"] != "COMPLETED" for trial in trials):
        return {"status": "INCOMPLETE", "verified_shots": None, "completed_shots": None, "interval": None}

    from scipy.stats import binomtest

    hits = sum(trial["simulation"]["verified_shots"] for trial in trials)
    shots = sum(trial["simulation"]["completed_shots"] for trial in trials)
    interval = binomtest(hits, shots).proportion_ci(confidence_level=confidence, method="exact")
    status = "INCONCLUSIVE"
    if interval.low >= floor:
        status = "MEETS_TARGET"
    elif interval.high < floor:
        status = "BELOW_TARGET"
    return {
        "status": status,
        "verified_shots": hits,
        "completed_shots": shots,
        "verified_hit_rate": hits / shots,
        "interval": [float(interval.low), float(interval.high)],
    }


def run_mod4_placement_check(
    *, minimum_hit_rate: float, model: str = "all", shots: int = 512,
    evaluation_seeds: Sequence[int] = (29, 43), transpiler_seed: int = 7,
) -> dict[str, Any]:
    """Evaluate one and two rounds at fixed placements with an explicit rate requirement."""
    _validate_snapshot_request(model, shots, transpiler_seed)
    if shots > MAX_MOD4_SWEEP_SHOTS:
        raise ValueError(f"placement check shots must be at most {MAX_MOD4_SWEEP_SHOTS} per circuit per seed")
    if (
        isinstance(minimum_hit_rate, bool) or not isinstance(minimum_hit_rate, (int, float))
        or not math.isfinite(minimum_hit_rate) or not 0 < minimum_hit_rate <= 1
    ):
        raise ValueError("minimum_hit_rate must be a number greater than 0 and at most 1")
    if not isinstance(evaluation_seeds, (tuple, list)) or len(evaluation_seeds) != 2:
        raise ValueError("placement check requires two evaluation seeds")
    seeds = tuple(evaluation_seeds)
    for seed in seeds:
        _validate_snapshot_request(model, shots, seed)
    if seeds[0] == seeds[1]:
        raise ValueError("evaluation seeds must differ")

    from qiskit_aer import AerSimulator

    from .grover_search import (
        audit_phase_oracle,
        build_grover_search_circuit,
        build_mod4_range_direct_oracle,
        mod4_range_predicate,
    )

    started = perf_counter()
    oracle = build_mod4_range_direct_oracle()
    audit = audit_phase_oracle(oracle, 4, mod4_range_predicate)
    circuits = {rounds: build_grover_search_circuit(oracle, 4, iterations=rounds) for rounds in ROUNDS}
    specs = _snapshot_specs()
    model_names = IBM_SNAPSHOT_MODELS if model == "all" else (model,)
    cells = len(model_names) * PLACEMENTS_PER_MODEL * len(ROUNDS)
    confidence = 1 - (1 - FAMILY_CONFIDENCE) / cells
    results = {}
    halt = False
    for name in model_names:
        factory, method = specs[name]
        backend = factory()
        anchor = compile_and_verify(circuits[2], backend=backend, seed=transpiler_seed)
        base_layout = anchor.report["initial_logical_to_target_qubit"]
        coupling = backend.coupling_map
        if base_layout is None or coupling is None:
            raise RuntimeError(f"{backend.name} is missing the placement or coupling map")
        alternate = _alternate_placement(base_layout, coupling.get_edges())
        properties = backend.properties()
        details: dict[str, Any] = {
            "status": "INSUFFICIENT_TOPOLOGY" if alternate is None else "PARTIAL",
            "backend": backend.name,
            "backend_version": backend.backend_version,
            "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
            "placements": [],
            "eligible_rounds": [],
            "selected_rounds": None,
        }
        results[name] = details
        if alternate is None:
            continue
        simulator = _snapshot_simulator(backend, AerSimulator, method)
        for label, layout in (("baseline", base_layout), ("alternate", alternate)):
            placement = {"label": label, "initial_logical_to_target_qubit": layout, "rounds": {}}
            details["placements"].append(placement)
            for rounds in ROUNDS:
                program = compile_and_verify(
                    circuits[rounds], backend=backend, seed=transpiler_seed, initial_layout=layout
                )
                trials = []
                for seed in seeds:
                    trial = _sample_snapshot(
                        program, backend, simulator, method=method, shots=shots, seed=seed,
                        verifier=mod4_range_predicate,
                    )
                    trials.append({"seed": seed, **trial})
                    if trial.get("local_job_cancelled") is False:
                        halt = True
                        break
                placement["rounds"][rounds] = {
                    "compilation": program.report,
                    "trials": trials,
                    "evaluation": _evaluate_trials(
                        trials, expected_batches=len(seeds), confidence=confidence, floor=minimum_hit_rate
                    ),
                }
                if halt:
                    break
            if halt:
                break
        if halt:
            break
        if all(
            placement["rounds"][rounds]["evaluation"]["status"] != "INCOMPLETE"
            for placement in details["placements"] for rounds in ROUNDS
        ):
            details["status"] = "COMPLETED"
            eligible = [
                rounds for rounds in ROUNDS
                if all(placement["rounds"][rounds]["evaluation"]["status"] == "MEETS_TARGET"
                       for placement in details["placements"])
            ]
            details["eligible_rounds"] = eligible
            if eligible:
                details["selected_rounds"] = min(eligible, key=lambda rounds: (
                    sum(placement["rounds"][rounds]["compilation"]["compiled"]["two_qubit_gates"]
                        for placement in details["placements"]),
                    rounds,
                ))
    return {
        "status": "COMPLETED" if all(result["status"] == "COMPLETED" for result in results.values()) else "PARTIAL",
        "execution_mode": "SIMULATOR",
        "experiment": "LOCAL_IBM_SNAPSHOT_MOD4_PLACEMENT_CHECK",
        "minimum_hit_rate": minimum_hit_rate,
        "round_candidates": list(ROUNDS),
        "shots_per_circuit_per_seed": shots,
        "evaluation_seeds": list(seeds),
        "transpiler_seed": transpiler_seed,
        "family_confidence": FAMILY_CONFIDENCE,
        "predeclared_cells": cells,
        "interval_confidence_per_cell": confidence,
        "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson",
        "basis_order": "q3 q2 q1 q0",
        "placement_policy": "two-round anchor plus nearest connected one-wire move, ties by logical then physical index",
        "selection_policy": "pass at both placements, minimize summed two-qubit gates, break ties by fewer rounds",
        "limits": {**_local_limits(), "shots": MAX_MOD4_SWEEP_SHOTS, "samples_per_model": 8},
        "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
        "oracle_audit": audit,
        "models": results,
        "models_not_run": [name for name in model_names if name not in results],
        "local_cancellation_unconfirmed": halt,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "wall_seconds": perf_counter() - started,
    }
