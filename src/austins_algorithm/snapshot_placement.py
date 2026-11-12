"""Check modulo-4 search against a hit-rate floor on IBM snapshots."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .compilation import CompiledProgram, _transpile_initial_layout, compile_and_verify
from .ibm_models import (
    IBM_SNAPSHOT_MODELS,
    MAX_MOD4_SWEEP_SHOTS,
    _local_limits,
    _sample_snapshot,
    _snapshot_simulator,
    _snapshot_specs,
    _validate_snapshot_request,
)

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_ibm_runtime.fake_provider import FakeBackendV2


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


def _placement_selection(placements: list[dict[str, Any]]) -> tuple[str, list[int], int | None]:
    if len(placements) != PLACEMENTS_PER_MODEL or any(
        cell["evaluation"]["status"] == "INCOMPLETE"
        for placement in placements for cell in placement["rounds"].values()
    ) or any(len(placement["rounds"]) != len(ROUNDS) for placement in placements):
        return "PARTIAL", [], None

    eligible = [
        rounds for rounds in ROUNDS
        if all(placement["rounds"][rounds]["evaluation"]["status"] == "MEETS_TARGET" for placement in placements)
    ]
    selected = min(eligible, default=None, key=lambda rounds: (
        sum(placement["rounds"][rounds]["compilation"]["compiled"]["two_qubit_gates"] for placement in placements),
        rounds,
    ))
    return "COMPLETED", eligible, selected


def _validate_placement_sampling(
    model: str, shots: int, minimum_hit_rate: float, evaluation_seeds: Sequence[int], transpiler_seed: int,
) -> tuple[int, ...]:
    _validate_snapshot_request(model, shots, transpiler_seed)
    if shots > MAX_MOD4_SWEEP_SHOTS:
        raise ValueError(f"placement check shots must be at most {MAX_MOD4_SWEEP_SHOTS} per circuit per seed")
    if (
        isinstance(minimum_hit_rate, bool) or not isinstance(minimum_hit_rate, (int, float))
        or not 0 < minimum_hit_rate <= 1 or not math.isfinite(minimum_hit_rate)
    ):
        raise ValueError("minimum_hit_rate must be a number greater than 0 and at most 1")
    if not isinstance(evaluation_seeds, (tuple, list)) or len(evaluation_seeds) != 2:
        raise ValueError("placement check requires two evaluation seeds")
    seeds = tuple(evaluation_seeds)
    for seed in seeds:
        _validate_snapshot_request(model, shots, seed)
    if seeds[0] == seeds[1]:
        raise ValueError("evaluation seeds must differ")
    return seeds


@dataclass(frozen=True)
class _SnapshotPlacements:
    backend: FakeBackendV2
    method: str
    initial_layout: tuple[int, ...]
    layouts: tuple[tuple[str, tuple[int, ...]], ...]
    programs: dict[tuple[str, int], CompiledProgram] = field(default_factory=dict)


class PreparedMod4PlacementCheck:
    """Reuse verified placements and circuits across local requests.

    The first request prepares each cell when it is reached. Each request
    runs its own seeded jobs on newly constructed snapshot simulators.
    """

    def __init__(
        self, *, model: str = "all", transpiler_seed: int = 7, prefer_native_qasm: bool = False,
    ) -> None:
        _validate_snapshot_request(model, 1, transpiler_seed)
        if not isinstance(prefer_native_qasm, bool):
            raise ValueError("prefer_native_qasm must be a boolean")
        self._model = model
        self._model_names = IBM_SNAPSHOT_MODELS if model == "all" else (model,)
        self._transpiler_seed = transpiler_seed
        self._prefer_native_qasm = prefer_native_qasm
        self._circuits: dict[int, QuantumCircuit] = {}
        self._oracle_audit: dict[str, Any] = {}
        self._snapshots: dict[str, _SnapshotPlacements] = {}
        self._sampling_unconfirmed = False
        self._lock = Lock()

    def run(
        self, *, minimum_hit_rate: float, shots: int = 512, evaluation_seeds: Sequence[int] = (29, 43),
        receipt_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Sample the plan and optionally save the request, report, and retained evidence."""
        seeds = _validate_placement_sampling(
            self._model, shots, minimum_hit_rate, evaluation_seeds, self._transpiler_seed,
        )
        with self._lock:
            if self._sampling_unconfirmed:
                raise RuntimeError("placement plan cannot be reused after unconfirmed simulator cancellation")
            if receipt_dir is None:
                return self._run(minimum_hit_rate=minimum_hit_rate, shots=shots, seeds=seeds)

            from .placement_receipt import _write_placement_receipt
            from .replay_receipt import _new_receipt_directory

            with _new_receipt_directory(receipt_dir) as directory:
                report = self._run(minimum_hit_rate=minimum_hit_rate, shots=shots, seeds=seeds)
                _write_placement_receipt(
                    directory, report=report, model=self._model,
                    prefer_native_qasm=self._prefer_native_qasm, snapshots=self._snapshots,
                )
            return report

    def _prepare_circuits(self) -> None:
        if self._circuits:
            return
        from .grover_search import (
            audit_phase_oracle,
            build_grover_search_circuit,
            build_mod4_range_direct_oracle,
            mod4_range_predicate,
        )

        oracle = build_mod4_range_direct_oracle()
        audit = audit_phase_oracle(oracle, 4, mod4_range_predicate)
        self._circuits = {
            rounds: build_grover_search_circuit(oracle, 4, iterations=rounds) for rounds in ROUNDS
        }
        self._oracle_audit = audit

    def _prepare_snapshot(
        self, name: str, factory: Callable[[], FakeBackendV2], method: str,
    ) -> _SnapshotPlacements:
        snapshot = self._snapshots.get(name)
        if snapshot is None:
            backend = factory()
            base = _transpile_initial_layout(self._circuits[2], backend=backend, seed=self._transpiler_seed)
            coupling = backend.coupling_map
            if base is None or coupling is None:
                raise RuntimeError(f"{backend.name} is missing the placement or coupling map")
            alternate = _alternate_placement(base, coupling.get_edges())
            layouts = () if alternate is None else (("baseline", tuple(base)), ("alternate", tuple(alternate)))
            snapshot = _SnapshotPlacements(backend, method, tuple(base), layouts)
            self._snapshots[name] = snapshot
        return snapshot

    def _prepare_program(
        self, snapshot: _SnapshotPlacements, label: str, layout: tuple[int, ...], rounds: int,
    ) -> CompiledProgram:
        key = (label, rounds)
        program = snapshot.programs.get(key)
        if program is None:
            program = compile_and_verify(
                self._circuits[rounds], backend=snapshot.backend, seed=self._transpiler_seed,
                initial_layout=layout, prefer_native_qasm=self._prefer_native_qasm,
            )
            snapshot.programs[key] = program
        return program

    def _sample_trials(
        self, program: CompiledProgram, snapshot: _SnapshotPlacements, simulator: AerSimulator, *,
        shots: int, seeds: Sequence[int],
    ) -> list[dict[str, Any]]:
        from .grover_search import mod4_range_predicate

        trials = []
        for seed in seeds:
            try:
                trial = _sample_snapshot(
                    program, snapshot.backend, simulator, method=snapshot.method, shots=shots, seed=seed,
                    verifier=mod4_range_predicate,
                )
            except (KeyboardInterrupt, SystemExit):
                self._sampling_unconfirmed = True
                raise
            trials.append({"seed": seed, **trial})
            if trial.get("local_job_cancelled") is False:
                self._sampling_unconfirmed = True
                break
        return trials

    def _run(self, *, minimum_hit_rate: float, shots: int, seeds: tuple[int, ...]) -> dict[str, Any]:
        from qiskit_aer import AerSimulator

        started = perf_counter()
        self._prepare_circuits()

        specs = _snapshot_specs()
        cells = len(self._model_names) * PLACEMENTS_PER_MODEL * len(ROUNDS)
        confidence = 1 - (1 - FAMILY_CONFIDENCE) / cells
        results = {}
        halt = False
        for name in self._model_names:
            snapshot = self._prepare_snapshot(name, *specs[name])

            backend = snapshot.backend
            properties = backend.properties()
            details: dict[str, Any] = {
                "status": "PARTIAL" if snapshot.layouts else "INSUFFICIENT_TOPOLOGY",
                "backend": backend.name,
                "backend_version": backend.backend_version,
                "snapshot_last_update_date": None if properties is None else properties.last_update_date.isoformat(),
                "placements": [],
                "eligible_rounds": [],
                "selected_rounds": None,
            }
            results[name] = details
            if not snapshot.layouts:
                continue
            simulator = _snapshot_simulator(backend, AerSimulator, snapshot.method)
            for label, layout in snapshot.layouts:
                placement = {"label": label, "initial_logical_to_target_qubit": list(layout), "rounds": {}}
                details["placements"].append(placement)
                for rounds in ROUNDS:
                    program = self._prepare_program(snapshot, label, layout, rounds)
                    trials = self._sample_trials(program, snapshot, simulator, shots=shots, seeds=seeds)
                    halt = self._sampling_unconfirmed
                    placement["rounds"][rounds] = {
                        "compilation": deepcopy(program.report),
                        "trials": trials,
                        "evaluation": _evaluate_trials(
                            trials, expected_batches=len(seeds), confidence=confidence, floor=minimum_hit_rate,
                        ),
                    }
                    if halt:
                        break
                if halt:
                    break
            if halt:
                break
            details["status"], details["eligible_rounds"], details["selected_rounds"] = _placement_selection(
                details["placements"],
            )
        return {
            "status": "COMPLETED" if all(result["status"] == "COMPLETED" for result in results.values()) else "PARTIAL",
            "execution_mode": "SIMULATOR",
            "experiment": "LOCAL_IBM_SNAPSHOT_MOD4_PLACEMENT_CHECK",
            "minimum_hit_rate": minimum_hit_rate,
            "round_candidates": list(ROUNDS),
            "shots_per_circuit_per_seed": shots,
            "evaluation_seeds": list(seeds),
            "transpiler_seed": self._transpiler_seed,
            "family_confidence": FAMILY_CONFIDENCE,
            "predeclared_cells": cells,
            "interval_confidence_per_cell": confidence,
            "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson",
            "basis_order": "q3 q2 q1 q0",
            "placement_policy": "two-round anchor plus nearest connected one-wire move, ties by logical then physical index",
            "selection_policy": "pass at both placements, minimize summed two-qubit gates, break ties by fewer rounds",
            "limits": {**_local_limits(), "shots": MAX_MOD4_SWEEP_SHOTS, "samples_per_model": 8},
            "versions": {name: version(name) for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
            "oracle_audit": deepcopy(self._oracle_audit),
            "models": results,
            "models_not_run": [name for name in self._model_names if name not in results],
            "local_cancellation_unconfirmed": halt,
            "provider_job_submitted": False,
            "quantum_hardware_used": False,
            "wall_seconds": perf_counter() - started,
        }


def run_mod4_placement_check(
    *, minimum_hit_rate: float, model: str = "all", shots: int = 512,
    evaluation_seeds: Sequence[int] = (29, 43), transpiler_seed: int = 7, prefer_native_qasm: bool = False,
    receipt_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate one and two rounds at fixed placements with an explicit rate requirement."""
    plan = PreparedMod4PlacementCheck(
        model=model, transpiler_seed=transpiler_seed, prefer_native_qasm=prefer_native_qasm,
    )
    return plan.run(
        minimum_hit_rate=minimum_hit_rate, shots=shots, evaluation_seeds=evaluation_seeds, receipt_dir=receipt_dir,
    )
