"""Audit and compare the two exact terminal phase covers on one target."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from math import fsum
from threading import Lock
from time import perf_counter
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .classical_constraint import classical_first_match, reduce_constraint_units
from .compilation import MAX_EXACT_QUBITS, MEASUREMENT_TOLERANCE, CompiledProgram, compile_and_verify
from .constraint_factorization import analyze_constraint_factorization
from .constraint_oracle import _factored_oracle
from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import audit_phase_oracle, build_grover_search_circuit
from .ibm_models import (
    MAX_CONSTRAINT_SNAPSHOT_SHOTS,
    MAX_IBM_SNAPSHOT_ACTIVE_QUBITS,
    _PREPARED_DENSITY_FUSION_OPTIONS,
    _SnapshotDensityReplay,
    _compact_snapshot_inputs,
    _local_limits,
    _prepare_density_snapshot,
    _sample_snapshot,
    _snapshot_hit_rate_difference,
    _snapshot_identity,
    _snapshot_simulator,
    _snapshot_specs,
    _summarize_snapshot_batches,
    _validate_snapshot_request,
)

from .witness_budget import (
    DEFAULT_COVER_EVALUATION_SEEDS,
    WITNESS_BATCH_CONFIDENCE_PER_COVER,
    WitnessBudgetRequest,
    _normalize_evaluation_seeds,
    _split_cover_seeds,
    _validate_seed_overlap,
    _witness_futility_stop,
    plan_witness_budget,
    summarize_witness_batches,
)

if TYPE_CHECKING:
    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target


COVER_CONFIDENCE_PER_ARM = 0.975
COVER_SNAPSHOT_METHODS = ("density_matrix", "matrix_product_state", "compact_density_matrix", "prepared_density_matrix")


@dataclass(frozen=True)
class PhaseCoverComparison:
    """Measured resource report and the exact compiled programs it describes."""

    report: dict[str, Any]
    selected: CompiledProgram | None
    programs: Mapping[str, CompiledProgram] = field(default_factory=dict)


def compare_constraint_phase_covers(
    constraint: BooleanConstraint, *, target: Target | None = None, backend: BackendV2 | None = None,
    rounds: int = 2, seed: int = 7, initial_layout: Sequence[int] | None = None,
    max_active_qubits: int | None = None,
    prefer_native_qasm: bool = False,
) -> PhaseCoverComparison:
    """Compile both covers with the standard diffuser and rank verified candidates.

    Each phase oracle is audited against the original formula. The selected
    program is the already verified compilation, including its portable export.
    """
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if (target is None) == (backend is None):
        raise ValueError("supply exactly one Qiskit target or backend")
    _bounded_int(rounds, 1, MAX_ITERATIONS, "rounds")
    _bounded_int(seed, 0, 2**32 - 1, "seed")
    if max_active_qubits is not None:
        _bounded_int(max_active_qubits, 1, MAX_EXACT_QUBITS, "max_active_qubits")

    from qiskit.providers import BackendV2
    from qiskit.transpiler import Target

    if target is not None and not isinstance(target, Target):
        raise TypeError("target must be a Qiskit Target")
    if backend is not None and not isinstance(backend, BackendV2):
        raise TypeError("backend must be a Qiskit BackendV2")
    active_target = target if target is not None else backend.target

    reduction = reduce_constraint_units(constraint)
    plan = analyze_constraint_factorization(reduction.constraint, include_complement=True)
    if not plan.eligible:
        raise ValueError(f"phase cover comparison unavailable: {plan.status}")
    direct, _ = _factored_oracle(plan)
    if active_target.num_qubits is not None and direct.num_qubits > active_target.num_qubits:
        raise ValueError(f"target width {active_target.num_qubits} is smaller than the {direct.num_qubits}-wire circuit")
    alternate, phase_cover = _factored_oracle(plan, phase_cover_policy="complement")
    oracles = {"direct": direct}
    if phase_cover is not None:
        oracles["complement"] = alternate

    # Audit both oracles before compiling either candidate.
    audits = {name: audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
              for name, oracle in oracles.items()}
    if "complement" in audits and audits["direct"] != audits["complement"]:
        raise RuntimeError("phase cover audits disagree on the original formula")
    programs = {}
    candidates = {}
    for name, oracle in oracles.items():
        circuit = build_grover_search_circuit(oracle, constraint.qubits, iterations=rounds)
        program = compile_and_verify(
            circuit, target=target, backend=backend, seed=seed, initial_layout=initial_layout,
            prefer_native_qasm=prefer_native_qasm,
        )
        active = len(program.report["openqasm"]["export_wire_to_target_qubit"])
        programs[name] = program
        candidates[name] = {
            "active_qubits": active,
            "eligible": max_active_qubits is None or active <= max_active_qubits,
            "oracle_audit": audits[name],
            "compilation": program.report,
        }

    distance = None
    if phase_cover is not None:
        a, b = (program.report["verification"]["roundtrip_probabilities"] for program in programs.values())
        distance = fsum(abs(a.get(bits, 0) - b.get(bits, 0)) for bits in a.keys() | b.keys()) / 2
        if distance > MEASUREMENT_TOLERANCE:
            raise RuntimeError("compiled phase covers disagree on the ideal measurement distribution")

    eligible = [name for name, row in candidates.items() if row["eligible"]]
    selected = min(eligible, key=lambda name: (
        candidates[name]["compilation"]["compiled"]["two_qubit_gates"],
        candidates[name]["compilation"]["compiled"]["depth"],
        candidates[name]["active_qubits"],
        name != "direct",
    ), default=None)
    status = "COMPLETED" if phase_cover is not None else "DIRECT_ONLY"
    if selected is None:
        status = "NO_COVER_FITS"
    report = {
        "status": status,
        "execution_mode": "COMPILE_ONLY",
        "experiment": "CONSTRAINT_PHASE_COVER_COMPARISON",
        "constraint_sha256": constraint.digest(),
        "rounds": rounds,
        "diffuser": "standard",
        "transpiler_seed": seed,
        "qiskit_version": version("qiskit"),
        "layout_policy": "automatic per candidate" if initial_layout is None else "shared initial layout",
        "max_active_qubits": max_active_qubits,
        "selection_policy": "fewest two-qubit gates, then depth, then active qubits, then direct cover",
        "forced_literals": list(reduction.forced_literals),
        "root_contradiction": reduction.contradiction,
        "factorization_status": plan.status,
        "alternate_available": phase_cover is not None,
        "phase_cover_profiles": None if phase_cover is None else {
            key: phase_cover[key] for key in ("terminal_component_variables", "direct_arities", "complement_arities")
        },
        "candidates": candidates,
        "cross_cover_total_variation_distance": distance,
        "selected_cover": selected,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    return PhaseCoverComparison(report, programs.get(selected), MappingProxyType(programs))


def _run_witness_validation(
    result: PhaseCoverComparison, request: WitnessBudgetRequest,
    samplers: Mapping[str, Callable[..., dict[str, Any]]],
) -> None:
    witness = result.report["witness_budget"]
    timings = witness["timing_seconds"]
    started = perf_counter()
    snapshot_candidates = result.report["snapshot"]["candidates"]
    for name, candidate in snapshot_candidates.items():
        budget = plan_witness_budget(
            candidate["summary"]["interval"][0],
            target_probability=request.target_probability, max_shots=request.max_shots,
        )
        required = budget["shots_required"]
        gates = result.programs[name].report["compiled"]["two_qubit_gates"]
        witness["candidates"][name] = {
            "budget": budget,
            "nominal_two_qubit_gate_executions": None if required is None else required * gates,
            "validation": None,
            "validation_batches": [],
            "batch_reliability": None,
            "completed_nominal_two_qubit_gate_executions": 0,
            "verified_witness": None,
        }
    timings["budget_planning"] = perf_counter() - started
    if all(row["budget"]["shots_required"] is None for row in witness["candidates"].values()):
        witness["status"] = "NO_FEASIBLE_BUDGET"
        return

    started = perf_counter()
    witness["status"] = "PARTIAL_VALIDATION"
    try:
        completed = True
        stopped = False
        for name, fresh_seeds in witness["validation_seed_schedule"].items():
            row = witness["candidates"][name]
            required = row["budget"]["shots_required"]
            if required is None:
                continue
            check_futility = request.stop_on_futility
            misses = 0
            for index, fresh_seed in enumerate(fresh_seeds, start=1):
                sample = samplers[name](shots=required, seed=fresh_seed)
                batch = {"seed": fresh_seed, **sample}
                row["validation_batches"].append(batch)
                if row["validation"] is None:
                    row["validation"] = batch
                if sample.get("local_job_cancelled") is False:
                    witness["local_cancellation_unconfirmed"] = True
                    return
                if sample["status"] != "COMPLETED":
                    completed = False
                    check_futility = False
                    continue
                if row["verified_witness"] is None:
                    row["verified_witness"] = next(iter(sample["simulation"]["verified_candidates"]), None)
                if not check_futility or index == len(fresh_seeds):
                    continue

                missed = sample["simulation"]["verified_shots"] == 0
                misses += missed
                if index > 1 and not missed:
                    continue
                futility = _witness_futility_stop(fresh_seeds, index, misses, request.target_probability)
                if futility is not None:
                    row["futility_stop"] = futility
                    stopped = True
                    break
        if completed:
            witness["status"] = "STOPPED_FOR_FUTILITY" if stopped else "COMPLETED"
    finally:
        for name, row in witness["candidates"].items():
            if row["budget"]["shots_required"] is None:
                continue
            summary = summarize_witness_batches(
                row["validation_batches"], planned_batches=witness["validation_batches_per_cover"],
                target_probability=request.target_probability,
            )
            if "futility_stop" in row:
                summary["status"] = "STOPPED_FOR_FUTILITY"
            row["batch_reliability"] = summary
            gates = result.programs[name].report["compiled"]["two_qubit_gates"]
            row["completed_nominal_two_qubit_gate_executions"] = summary["completed_shots"] * gates
        timings["holdout_validation"] = perf_counter() - started


def _phase_cover_replay_exports(comparison: PhaseCoverComparison) -> dict[str, dict[str, Any]]:
    exports = {}
    for name, program in comparison.programs.items():
        exported = program.report["openqasm"]
        exports[name] = {
            "sha256": exported["sha256"],
            "initial_logical_to_target_qubit": program.report["initial_logical_to_target_qubit"],
            "export_wire_to_target_qubit": exported["export_wire_to_target_qubit"],
            "classical_bit_to_target_qubit": exported["classical_bit_to_target_qubit"],
        }
    return exports


def run_constraint_cover_snapshot(
    constraint: BooleanConstraint, *, model: str, initial_layout: Sequence[int],
    rounds: int = 2, seed: int = 7, max_active_qubits: int | None = None,
    shots: int = 64, evaluation_seeds: Sequence[int] = DEFAULT_COVER_EVALUATION_SEEDS,
    witness_budget: WitnessBudgetRequest | None = None,
    simulation_method: str | None = None,
    prefer_native_qasm: bool = False,
    density_fusion: bool = False,
    _expected_snapshot: Mapping[str, Any] | None = None,
    _expected_exports: Mapping[str, Any] | None = None,
    _check_only: bool = False,
) -> PhaseCoverComparison:
    """Compare fixed-layout covers and optionally validate a planned witness budget."""
    started = perf_counter()
    if initial_layout is None:
        raise ValueError("phase cover sampling requires a shared initial layout")
    seeds, witness = _prepare_snapshot_request(
        constraint, model=model, seed=seed, shots=shots, evaluation_seeds=evaluation_seeds,
        witness_budget=witness_budget, simulation_method=simulation_method, density_fusion=density_fusion,
    )
    compilation = _compile_snapshot_covers(
        constraint, model=model, rounds=rounds, seed=seed, initial_layout=initial_layout,
        max_active_qubits=max_active_qubits, prefer_native_qasm=prefer_native_qasm,
        expected_snapshot=_expected_snapshot,
    )
    if _expected_exports is not None and _phase_cover_replay_exports(compilation.comparison) != _expected_exports:
        raise ValueError("replay compiled OpenQASM or wire maps differ from the saved request")
    if _check_only:
        if witness is not None:
            _verify_snapshot_witness_reference(compilation.comparison, witness)
        return compilation.comparison
    return _sample_snapshot_covers(
        constraint.matches, compilation, model=model, shots=shots, seeds=seeds, witness=witness,
        witness_budget=witness_budget, simulation_method=simulation_method, density_fusion=density_fusion,
        started=started, compilation_reused=False,
    )


class PreparedConstraintCoverSnapshot:
    """Own a verified compilation for repeated requests on one offline snapshot."""

    def __init__(
        self, constraint: BooleanConstraint, *, model: str, initial_layout: Sequence[int],
        rounds: int = 2, seed: int = 7, max_active_qubits: int | None = None,
        prefer_native_qasm: bool = False, cache_density: bool = False,
    ) -> None:
        _validate_named_snapshot(model, 1, seed)
        if not isinstance(cache_density, bool):
            raise TypeError("cache_density must be a boolean")
        if not isinstance(constraint, BooleanConstraint):
            raise TypeError("constraint must be a BooleanConstraint")
        if not isinstance(initial_layout, Sequence) or isinstance(initial_layout, (str, bytes, bytearray)):
            raise ValueError("initial_layout must be a sequence of target qubit indices")
        layout = tuple(initial_layout)
        self._constraint = BooleanConstraint(constraint.qubits, constraint.clauses)
        self._model = model
        self._seed = seed
        self._compilation = _compile_snapshot_covers(
            self._constraint, model=model, rounds=rounds, seed=seed, initial_layout=layout,
            max_active_qubits=max_active_qubits, prefer_native_qasm=prefer_native_qasm,
        )
        # The exhaustive audit records keys accepted by the original formula.
        audit = self._compilation.comparison.report["candidates"]["direct"]["oracle_audit"]
        self._verify_candidate = frozenset(audit["marked_states"]).__contains__
        self._lock = Lock()
        self._density_cache: _DensityCache | None = {} if cache_density else None

    @property
    def compilation_report(self) -> dict[str, Any]:
        """Return a detached report for the verified phase covers."""
        return deepcopy(self._compilation.comparison.report)

    @property
    def openqasm(self) -> dict[str, str]:
        """Return the verified portable OpenQASM by cover name."""
        return {name: program.openqasm for name, program in self._compilation.comparison.programs.items()}

    def run(
        self, *, shots: int = 64, evaluation_seeds: Sequence[int] = DEFAULT_COVER_EVALUATION_SEEDS,
        witness_budget: WitnessBudgetRequest | None = None, simulation_method: str | None = None,
        density_fusion: bool = False,
    ) -> dict[str, Any]:
        """Sample fresh batches and return a detached report for this request."""
        started = perf_counter()
        seeds, witness = _prepare_snapshot_request(
            self._constraint, model=self._model, seed=self._seed, shots=shots,
            evaluation_seeds=evaluation_seeds, witness_budget=witness_budget,
            simulation_method=simulation_method, density_fusion=density_fusion,
        )
        with self._lock:
            result = _sample_snapshot_covers(
                self._verify_candidate, self._compilation, model=self._model, shots=shots,
                seeds=seeds, witness=witness, witness_budget=witness_budget,
                simulation_method=simulation_method, density_fusion=density_fusion,
                started=started, compilation_reused=True, density_cache=self._density_cache,
            )
        return result.report


@dataclass(frozen=True)
class _SnapshotCompilation:
    backend: BackendV2
    default_method: str
    comparison: PhaseCoverComparison
    timing_seconds: dict[str, float]


def _compile_snapshot_covers(
    constraint: BooleanConstraint, *, model: str, rounds: int, seed: int, initial_layout: Sequence[int],
    max_active_qubits: int | None, prefer_native_qasm: bool,
    expected_snapshot: Mapping[str, Any] | None = None,
) -> _SnapshotCompilation:
    started = perf_counter()
    factory, default_method = _snapshot_specs()[model]
    backend = factory()
    if expected_snapshot is not None and _snapshot_identity(backend) != expected_snapshot:
        raise ValueError("local snapshot identity differs from the saved replay request")
    compile_started = perf_counter()
    comparison = compare_constraint_phase_covers(
        constraint, backend=backend, rounds=rounds, seed=seed, initial_layout=initial_layout,
        max_active_qubits=max_active_qubits, prefer_native_qasm=prefer_native_qasm,
    )
    return _SnapshotCompilation(backend, default_method, comparison, {
        "backend_setup": compile_started - started,
        "compile_and_audit": perf_counter() - compile_started,
    })


def _validate_named_snapshot(model: str, shots: int, seed: int) -> None:
    _validate_snapshot_request(model, shots, seed)
    if model == "all":
        raise ValueError("phase cover sampling requires one named IBM snapshot")


def _prepare_snapshot_request(
    constraint: BooleanConstraint, *, model: str, seed: int, shots: int,
    evaluation_seeds: Sequence[int], witness_budget: WitnessBudgetRequest | None,
    simulation_method: str | None, density_fusion: bool,
) -> tuple[dict[str, list[int]], dict[str, Any] | None]:
    _validate_named_snapshot(model, shots, seed)
    if simulation_method is not None and simulation_method not in COVER_SNAPSHOT_METHODS:
        raise ValueError(f"simulation_method must be one of {', '.join(COVER_SNAPSHOT_METHODS)}")
    if not isinstance(density_fusion, bool):
        raise TypeError("density_fusion must be a boolean")
    if density_fusion and simulation_method != "prepared_density_matrix":
        raise ValueError("density_fusion requires prepared_density_matrix")
    if shots > MAX_CONSTRAINT_SNAPSHOT_SHOTS:
        raise ValueError(f"phase cover sampling shots must be at most {MAX_CONSTRAINT_SNAPSHOT_SHOTS} per batch")
    evaluation_seeds = _normalize_evaluation_seeds(evaluation_seeds)
    witness = None
    if witness_budget is not None:
        if not isinstance(witness_budget, WitnessBudgetRequest):
            raise TypeError("witness_budget must be a WitnessBudgetRequest")
        _validate_seed_overlap(witness_budget.validation_seeds, evaluation_seeds)
        baseline_started = perf_counter()
        baseline = classical_first_match(constraint, method="propagation")
        validation_schedule = _split_cover_seeds(witness_budget.validation_seeds)
        witness = {
            "status": "CALIBRATION_INCOMPLETE",
            "target_probability": witness_budget.target_probability,
            "max_shots_per_validation": witness_budget.max_shots,
            "validation_seeds": {name: seeds[0] for name, seeds in validation_schedule.items()},
            "validation_seed_schedule": validation_schedule,
            "validation_batches_per_cover": len(validation_schedule["direct"]),
            "calibration_confidence_per_cover": COVER_CONFIDENCE_PER_ARM,
            "batch_confidence_per_cover": WITNESS_BATCH_CONFIDENCE_PER_COVER,
            "batch_family_confidence": 0.95,
            "classical_reference": baseline,
            "candidates": {},
            "local_cancellation_unconfirmed": False,
            "timing_seconds": {"classical_reference": perf_counter() - baseline_started},
        }
        if witness_budget.stop_on_futility:
            witness["stop_on_futility"] = True

    return _split_cover_seeds(evaluation_seeds), witness


@dataclass(frozen=True)
class _CachedDensity:
    replay: _SnapshotDensityReplay
    preparation: dict[str, Any]


_DensityCache = dict[bool, dict[str, _CachedDensity]]


def _verify_snapshot_witness_reference(comparison: PhaseCoverComparison, witness: dict[str, Any]) -> None:
    marks = comparison.report["candidates"]["direct"]["oracle_audit"]["marked_states"]
    if witness["classical_reference"]["candidate"] != next(iter(marks), None):
        raise RuntimeError("classical first witness disagrees with the exhaustive oracle audit")


def _snapshot_sampling_limit(comparison: PhaseCoverComparison) -> str | None:
    if not comparison.report["alternate_available"]:
        return "NO_ALTERNATE_COVER"
    if any(
        not row["eligible"] or row["active_qubits"] > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
        for row in comparison.report["candidates"].values()
    ):
        return "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"
    return None


def _sample_snapshot_covers(
    verifier: Callable[[str], bool], compilation: _SnapshotCompilation, *, model: str, shots: int,
    seeds: dict[str, list[int]], witness: dict[str, Any] | None, witness_budget: WitnessBudgetRequest | None,
    simulation_method: str | None, density_fusion: bool, started: float, compilation_reused: bool,
    density_cache: _DensityCache | None = None,
) -> PhaseCoverComparison:
    backend, comparison = compilation.backend, compilation.comparison
    requested_method = compilation.default_method if simulation_method is None else simulation_method
    reuse_density = requested_method == "prepared_density_matrix"
    use_compact_wires = reuse_density or requested_method == "compact_density_matrix"
    method = "density_matrix" if use_compact_wires else requested_method
    if witness is not None:
        if not compilation_reused:
            witness["timing_seconds"].update(compilation.timing_seconds)
        _verify_snapshot_witness_reference(comparison, witness)

    properties = backend.properties()
    snapshot = {
        "status": "PARTIAL",
        "model": model,
        "backend_version": backend.backend_version,
        "last_update_date": None if properties is None else properties.last_update_date.isoformat(),
        "method": method,
        "wire_representation": "compact" if use_compact_wires else "physical",
        "shots_per_batch": shots,
        "evaluation_seeds": seeds,
        "confidence_per_cover": COVER_CONFIDENCE_PER_ARM,
        "difference_family_confidence": 0.95,
        "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson; subtract opposite endpoints",
        "limits": {**_local_limits(), "shots": MAX_CONSTRAINT_SNAPSHOT_SHOTS, "max_sampling_batches": 4},
        "candidates": {
            name: {"compilation_sha256": program.report["openqasm"]["sha256"], "batches": [], "summary": None}
            for name, program in comparison.programs.items()
        },
        "complement_minus_direct": None,
        "observed_hit_leader": None,
        "local_cancellation_unconfirmed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }
    if reuse_density:
        snapshot["state_preparation"] = {}
    if density_fusion:
        snapshot["density_fusion_requested"] = dict(_PREPARED_DENSITY_FUSION_OPTIONS)
    compile_report = comparison.report
    if compilation_reused:
        snapshot["compilation_source"] = "PREPARED_PLAN"
        compile_report = deepcopy(compile_report)
    report = {**compile_report, "snapshot": snapshot}
    if witness is not None:
        report["witness_budget"] = witness
    result = PhaseCoverComparison(report, comparison.selected, comparison.programs)
    try:
        sampling_limit = _snapshot_sampling_limit(comparison)
        if sampling_limit is not None:
            snapshot["status"] = sampling_limit
            if witness is not None:
                witness["status"] = sampling_limit
                if sampling_limit == "NO_ALTERNATE_COVER" and witness["classical_reference"]["candidate"] is None:
                    witness["status"] = "PROVEN_EMPTY"
            return result

        simulator_started = perf_counter()
        from qiskit_aer import AerSimulator

        report["execution_mode"] = "SIMULATOR"
        snapshot["versions"] = {name: version(name) for name in ("qiskit-aer", "qiskit-ibm-runtime", "scipy")}
        if use_compact_wires:
            cached = None
            if reuse_density and density_cache is not None:
                cached = density_cache.get(density_fusion)
                snapshot["density_cache"] = {"status": "MISS", "matrix_bytes": 0}
            if cached is None:
                prepared = _compact_snapshot_inputs(backend, comparison.programs, AerSimulator)
                if witness is not None:
                    witness["timing_seconds"]["noise_model_setup"] = perf_counter() - simulator_started
                if reuse_density:
                    preparation_started = perf_counter()
                    try:
                        for name, (circuit, simulator) in prepared.items():
                            readout_input, preparation = _prepare_density_snapshot(
                                circuit, simulator, backend.name, density_fusion=density_fusion,
                            )
                            snapshot["state_preparation"][name] = preparation
                            if readout_input is None:
                                snapshot["status"] = "STATE_PREPARATION_INCOMPLETE"
                                snapshot["local_cancellation_unconfirmed"] = not preparation["local_job_cancelled"]
                                if witness is not None:
                                    witness["status"] = snapshot["status"]
                                    witness["local_cancellation_unconfirmed"] = snapshot["local_cancellation_unconfirmed"]
                                return result
                            prepared[name] = readout_input
                    finally:
                        if witness is not None:
                            witness["timing_seconds"]["density_preparation"] = perf_counter() - preparation_started
                if reuse_density and density_cache is not None:
                    # Publish only after both covers have passed preparation and capture.
                    entries = {
                        name: _CachedDensity(
                            _SnapshotDensityReplay.capture(circuit, simulator),
                            deepcopy({key: value for key, value in snapshot["state_preparation"][name].items()
                                      if key != "wall_seconds"}),
                        )
                        for name, (circuit, simulator) in prepared.items()
                    }
                    density_cache[density_fusion] = entries
                    snapshot["density_cache"] = {
                        "status": "FILLED", "matrix_bytes": sum(len(entry.replay.matrix) for entry in entries.values()),
                    }
            else:
                prepared = {name: entry.replay.materialize() for name, entry in cached.items()}
                snapshot["state_preparation"] = {name: deepcopy(entry.preparation) for name, entry in cached.items()}
                snapshot["density_cache"] = {
                    "status": "HIT", "matrix_bytes": sum(len(entry.replay.matrix) for entry in cached.values()),
                }
                if witness is not None:
                    witness["timing_seconds"]["density_replay_setup"] = perf_counter() - simulator_started
            samplers = {
                name: partial(
                    _sample_snapshot, comparison.programs[name], backend, simulator, method=method,
                    verifier=verifier, sampling_circuit=circuit,
                )
                for name, (circuit, simulator) in prepared.items()
            }
        else:
            simulator = _snapshot_simulator(backend, AerSimulator, method)
            if witness is not None:
                witness["timing_seconds"]["noise_model_setup"] = perf_counter() - simulator_started
            samplers = {
                name: partial(_sample_snapshot, program, backend, simulator, method=method, verifier=verifier)
                for name, program in comparison.programs.items()
            }
        calibration_started = perf_counter()
        try:
            for name, sample_cover in samplers.items():
                record = snapshot["candidates"][name]
                for evaluation_seed in seeds[name]:
                    sample = sample_cover(shots=shots, seed=evaluation_seed)
                    record["batches"].append({"seed": evaluation_seed, **sample})
                    if sample.get("local_job_cancelled") is False:
                        snapshot["local_cancellation_unconfirmed"] = True
                        return result
                record["summary"] = _summarize_snapshot_batches(record["batches"], COVER_CONFIDENCE_PER_ARM)
        finally:
            if witness is not None:
                witness["timing_seconds"]["calibration"] = perf_counter() - calibration_started

        direct = snapshot["candidates"]["direct"]["summary"]
        complement = snapshot["candidates"]["complement"]["summary"]
        difference = _snapshot_hit_rate_difference(direct, complement)
        if difference is None:
            return result
        snapshot["complement_minus_direct"] = difference
        leader = "tie"
        if difference["estimate"] > 0:
            leader = "complement"
        elif difference["estimate"] < 0:
            leader = "direct"
        snapshot["observed_hit_leader"] = leader
        snapshot["status"] = "COMPLETED"
        if witness_budget is not None:
            _run_witness_validation(result, witness_budget, samplers)
        return result
    finally:
        if witness is not None:
            witness["timing_seconds"]["workflow_wall"] = perf_counter() - started
