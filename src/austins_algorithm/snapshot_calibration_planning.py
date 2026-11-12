"""Forecast one adaptive budget grid on a matched pair of dated IBM snapshots."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .constraints import BooleanConstraint
from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, QUERY_BUDGET_POLICIES, _unknown_round_choices
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .snapshot_calibration_compilation_bundle import _recorded_calibration_importer
from .snapshot_calibration_comparison import (
    CALIBRATION_COMPARISON, CALIBRATION_MASS_TOLERANCE, CALIBRATION_NOISE_METHOD, MAX_CALIBRATION_BACKEND_QUBITS,
    _calibration_time,
)
from .snapshot_calibration_replay import _replay_calibration_budgets
from .unknown_count_planning import MAX_SELECTION_PROFILES, _selection_axis, _selection_grid
from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES, _check_grid_work
from .unknown_count_snapshot_profile import MAX_VALIDATION_TRIALS, _object, _sha256, _wires
from .unknown_count_snapshot_profile_comparison import _candidate_pair
from .unknown_count_snapshot_selection import _distinct_classical, _summarize_budget_candidates


_LANES = ("earlier", "later")
_TARGET_STATES = ("MEETS_BOTH", "EARLIER_ONLY", "LATER_ONLY", "MEETS_NEITHER")
_VERSIONS = ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")
_CHECK_TOLERANCE = 1e-12
_DELTA_METRICS = ("found_probability", "expected_shots", "expected_oracle_queries", "expected_compiled_two_qubit_gates")


@dataclass(frozen=True)
class _CalibrationProfile:
    layout: tuple[int, ...]
    scope: dict[str, Any]
    probabilities: dict[str, list[tuple[float, float]]]
    weights: dict[str, list[list[float]]]
    gate_costs: list[int]
    programs: list[dict[str, Any]]


def _snapshot_pair(report: dict[str, Any]) -> dict[str, Any]:
    pair = _object(report.get("snapshots"), "calibration snapshots")
    if set(pair) != set(_LANES):
        raise ValueError("calibration report requires earlier and later snapshots")
    for label in _LANES:
        snapshot = _object(pair[label], f"{label} snapshot")
        name = snapshot.get("backend_name")
        version = snapshot.get("backend_version")
        if not isinstance(name, str) or not re.fullmatch(r"ibmq?_[a-z0-9_]+", name):
            raise ValueError(f"{label} snapshot must name an IBM backend")
        if not isinstance(version, str) or not version:
            raise ValueError(f"{label} snapshot must record its backend version")
        _bounded_int(snapshot.get("qubits"), 1, MAX_CALIBRATION_BACKEND_QUBITS, f"{label} backend width")
        for key in ("configuration_sha256", "properties_sha256"):
            _sha256(snapshot.get(key), f"{label} {key}")
    earlier, later = (pair[label] for label in _LANES)
    if any(earlier[key] != later[key] for key in ("backend_name", "qubits")):
        raise ValueError("calibration report must identify the same IBM backend and width on both dates")
    if _calibration_time(earlier.get("last_update_date")) >= _calibration_time(later.get("last_update_date")):
        raise ValueError("calibration report timestamps must be strictly increasing")
    if earlier["properties_sha256"] == later["properties_sha256"]:
        raise ValueError("calibration report must identify distinct dated properties")
    for flag, key in (("configuration_changed", "configuration_sha256"), ("backend_version_changed", "backend_version")):
        if report.get(flag) is not (earlier[key] != later[key]):
            raise ValueError(f"calibration report has an inconsistent {flag} flag")
    return pair


def _verified_program(
    row: dict[str, Any], backend: dict[str, Any], layout: list[int], seed: int, width: int, *,
    prefer_native_qasm: bool,
) -> dict[str, Any]:
    compilation = _object(row.get("compilation"), "round compilation")
    target = _object(compilation.get("target"), "round compile target")
    device_width = backend["qubits"]
    if (
        compilation.get("status") != "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION"
        or compilation.get("provider_job_submitted") is not False
        or compilation.get("quantum_hardware_used") is not False
        or target.get("kind") != "BACKEND_V2" or target.get("name") != backend["backend_name"]
        or type(target.get("qubits")) is not int or target["qubits"] != device_width
        or type(compilation.get("seed_transpiler")) is not int or compilation["seed_transpiler"] != seed
        or _wires(compilation.get("initial_logical_to_target_qubit"), device_width, label="compiled layout") != layout
    ):
        raise ValueError("round compilation must be verified for the recorded backend, layout, and seed")
    export = _object(compilation.get("openqasm"), "round OpenQASM")
    active = _wires(export.get("export_wire_to_target_qubit"), device_width, label="active wires")
    measured = _wires(export.get("classical_bit_to_target_qubit"), device_width, label="measurement map")
    importer = _recorded_calibration_importer(export, prefer_native_qasm)
    if (
        export.get("representation") != "PORTABLE_COMPILED_WIRES"
        or active != sorted(active) or len(active) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
        or len(measured) != width or not set(measured).issubset(active)
    ):
        raise ValueError("round OpenQASM must record a portable active layout and complete measurement map")
    compiled = _object(compilation.get("compiled"), "round compiled metrics")
    program = {
        "openqasm_sha256": _sha256(export.get("sha256"), "round OpenQASM digest"),
        "active_target_wires": active, "measurement_map": measured, "importer": importer,
        "optimization_level": _bounded_int(compilation.get("optimization_level"), 0, 3, "optimization level"),
        "compiled_two_qubit_gates": _bounded_int(compiled.get("two_qubit_gates"), 0, 1_000_000, "compiled two-qubit gates"),
    }
    if prefer_native_qasm:
        program["preferred_importer"] = export["preferred_importer"]
    return program


def _check_number(value: Any, expected: float, label: str, *, tolerance: float = _CHECK_TOLERANCE) -> None:
    if (
        type(value) not in (float, int) or not -2 <= value <= 2
        or not math.isclose(value, expected, rel_tol=0.0, abs_tol=tolerance)
    ):
        raise ValueError(f"calibration report {label} disagrees with its saved probabilities")


def _round_probabilities(
    row: dict[str, Any], outcomes: list[str], witnesses: set[str],
) -> tuple[dict[str, tuple[float, float]], dict[str, list[float]]]:
    distributions = {}
    probabilities = {}
    for label in _LANES:
        lane = _object(row.get(label), f"{label} round distribution")
        values = _object(lane.get("outcome_probabilities"), f"{label} outcome probabilities")
        if values.keys() != set(outcomes) or any(type(p) not in (float, int) or not 0 <= p <= 1 for p in values.values()):
            raise ValueError(f"{label} round must record every audited outcome with a valid probability")
        mass = math.fsum(values.values())
        _check_number(mass, 1.0, f"{label} outcome mass")
        _check_number(lane.get("input_probability_mass"), 1.0, f"{label} input probability mass",
                      tolerance=CALIBRATION_MASS_TOLERANCE)
        hit = math.fsum(values[bits] for bits in outcomes if bits in witnesses)
        miss = math.fsum(values[bits] for bits in outcomes if bits not in witnesses)
        _check_number(lane.get("verified_hit_probability"), hit, f"{label} witness probability")
        total = math.fsum((hit, miss))
        probabilities[label] = hit / total, miss / total
        distributions[label] = values
    deltas = _object(row.get("right_minus_left_outcome_probabilities"), "round outcome deltas")
    if deltas.keys() != set(outcomes):
        raise ValueError("round outcome deltas must cover every audited outcome")
    expected = {bits: distributions["later"][bits] - distributions["earlier"][bits] for bits in outcomes}
    for bits, delta in expected.items():
        _check_number(deltas[bits], delta, f"outcome delta for {bits}")
    _check_number(row.get("right_minus_left_hit_probability"),
                  math.fsum(expected[bits] for bits in outcomes if bits in witnesses), "witness delta")
    _check_number(row.get("outcome_total_variation_distance"),
                  math.fsum(map(abs, expected.values())) / 2, "total variation")
    return probabilities, {label: [values[bits] for bits in outcomes] for label, values in distributions.items()}


def _profile(
    report: dict[str, Any], width: int, constraint_sha256: str, outcomes: list[str], witnesses: set[str],
) -> _CalibrationProfile:
    report = _object(report, "calibration comparison report")
    if (
        type(report.get("schema_version")) is not int or report["schema_version"] != 1
        or report.get("status") != "COMPLETED" or report.get("execution_mode") != "SIMULATOR"
        or report.get("comparison") != CALIBRATION_COMPARISON or report.get("noise_method") != CALIBRATION_NOISE_METHOD
        or report.get("compile_target") != "later" or report.get("provider_job_submitted") is not False
        or report.get("quantum_hardware_used") is not False
    ):
        raise ValueError("budget planning requires a completed offline dated calibration comparison")
    audit = _object(report.get("audit"), "calibration constraint audit")
    if (
        type(report.get("search_qubits")) is not int or report["search_qubits"] != width
        or report.get("constraint_sha256") != constraint_sha256
        or type(audit.get("states_checked")) is not int or audit["states_checked"] != len(outcomes)
        or type(audit.get("marked_count")) is not int or audit["marked_count"] != len(witnesses)
        or audit.get("marked_states") != sorted(witnesses)
    ):
        raise ValueError("calibration audit must match the supplied constraint and its exhaustive witness set")
    pair = _snapshot_pair(report)
    backend = pair["later"]
    versions = _object(report.get("versions"), "calibration SDK versions")
    if any(not isinstance(versions.get(key), str) or not versions[key] for key in _VERSIONS):
        raise ValueError("calibration comparison must record its SDK versions")
    seed = _bounded_int(report.get("seed_transpiler"), 0, 2**32 - 1, "seed_transpiler")
    prefer_native_qasm = "prefer_native_qasm" in report
    if prefer_native_qasm and report["prefer_native_qasm"] is not True:
        raise ValueError("calibration comparison native OpenQASM preference must be true when recorded")
    layout = _wires(report.get("initial_layout"), backend["qubits"], label="initial_layout")
    if not width <= len(layout) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        raise ValueError("calibration initial_layout must fit the search and the active qubit limit")
    max_rounds = _bounded_int(report.get("max_rounds"), 0, math.isqrt(len(outcomes) - 1), "saved max_rounds")
    rounds = _object(report.get("rounds"), "saved calibration rounds")
    if rounds.keys() != {str(index) for index in range(max_rounds + 1)}:
        raise ValueError("calibration comparison must contain all recorded rounds starting at zero")
    programs = []
    probabilities = {label: [] for label in _LANES}
    weights = {label: [] for label in _LANES}
    for index in range(max_rounds + 1):
        row = _object(rounds[str(index)], "saved calibration round")
        programs.append(_verified_program(row, backend, layout, seed, width, prefer_native_qasm=prefer_native_qasm))
        modeled, categorical = _round_probabilities(row, outcomes, witnesses)
        for label in _LANES:
            probabilities[label].append(modeled[label])
            weights[label].append(categorical[label])
    _check_number(report.get("maximum_absolute_hit_probability_difference"),
                  max(abs(row["right_minus_left_hit_probability"]) for row in rounds.values()), "maximum witness delta")
    optimization = programs[0]["optimization_level"]
    if any(program["optimization_level"] != optimization for program in programs):
        raise ValueError("calibration rounds must share a compiler optimization level")
    scope = {"snapshots": pair, "versions": versions, "seed_transpiler": seed,
             "optimization_level": optimization, "noise_method": CALIBRATION_NOISE_METHOD}
    if prefer_native_qasm:
        scope["prefer_native_qasm"] = True
    return _CalibrationProfile(tuple(layout), scope, probabilities, weights,
                               [row["compiled_two_qubit_gates"] for row in programs], programs)


def _selection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    feasible, recommendations, best = _summarize_budget_candidates(rows) if rows else (0, None, None)
    return {"feasible_count": feasible, "recommendations": recommendations, "best_attainable": best}


def _paired_candidate(layout: str, earlier: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    shared = _candidate_pair(earlier, later)["shared"]
    left = {**earlier["hypotheses"][0], "meets_target": earlier["meets_target"]}
    right = {**later["hypotheses"][0], "meets_target": later["meets_target"]}
    state = {
        (True, True): "MEETS_BOTH", (True, False): "EARLIER_ONLY",
        (False, True): "LATER_ONLY", (False, False): "MEETS_NEITHER",
    }[left["meets_target"], right["meets_target"]]
    return {
        **shared, "layout": layout, "target_status": state, "earlier": left, "later": right,
        "later_minus_earlier": {key: right[key] - left[key] for key in _DELTA_METRICS},
    }


def _calibration_budget_grid(
    constraint: BooleanConstraint, target_probability: float, shot_caps: Iterable[int], query_caps: Iterable[int],
    max_rounds_per_shot: int, validation_trials: int,
) -> tuple[dict[str, Any], list[int], int]:
    if not isinstance(constraint, BooleanConstraint):
        raise TypeError("constraint must be a BooleanConstraint")
    if type(target_probability) not in (float, int) or not 0 < target_probability <= 1:
        raise ValueError("target_probability must be greater than zero and at most one")
    width = _bounded_int(constraint.qubits, 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "search width")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")
    _bounded_int(validation_trials, 0, MAX_VALIDATION_TRIALS, "validation_trials")
    shots = _selection_axis(shot_caps, 1, MAX_SEARCH_SHOTS, "shot_caps")
    queries = _selection_axis(query_caps, 0, MAX_ORACLE_QUERIES, "query_caps")
    windows = list(islice(_unknown_round_choices(width, max_rounds_per_shot), shots[-1]))
    options = {"target_probability": float(target_probability), "shot_caps": shots, "query_caps": queries,
               "max_rounds_per_shot": max_rounds_per_shot}
    return options, windows, min(max(windows), queries[-1] + 1)


def _check_calibration_budget_work(options: dict[str, Any], windows: list[int], layouts: int) -> None:
    shots, queries = options["shot_caps"], options["query_caps"]
    if layouts * len(shots) * len(queries) * len(QUERY_BUDGET_POLICIES) * len(_LANES) > MAX_SELECTION_PROFILES:
        raise ValueError(f"calibration grid exceeds {MAX_SELECTION_PROFILES} candidate/snapshot profiles")
    _check_grid_work(windows, shots, queries, layouts * len(_LANES), 1, fixed_tolerances=False)


def plan_snapshot_calibration_budgets(
    reports: Sequence[dict[str, Any]], constraint: BooleanConstraint, *, target_probability: float,
    shot_caps: Iterable[int], query_caps: Iterable[int], max_rounds_per_shot: int = MAX_ITERATIONS,
    validation_trials: int = 0,
) -> dict[str, Any]:
    """Forecast shared fixed caps on both archived models from completed comparison reports."""
    options, windows, required = _calibration_budget_grid(
        constraint, target_probability, shot_caps, query_caps, max_rounds_per_shot, validation_trials,
    )
    profiles, witnesses = _calibration_profiles(reports, constraint)
    _check_calibration_budget_work(options, windows, sum(len(profile.programs) >= required for profile in profiles))
    return _plan_calibration_profiles(profiles, constraint, len(witnesses), options, required, validation_trials)


def _calibration_profiles(
    reports: Sequence[dict[str, Any]], constraint: BooleanConstraint, *, fusion_diagnostic: bool = False,
) -> tuple[list[_CalibrationProfile], set[str]]:
    if not isinstance(reports, Sequence) or not 1 <= len(reports) <= MAX_LAYOUT_PROFILES:
        raise ValueError(f"budget planning requires 1 to {MAX_LAYOUT_PROFILES} calibration comparison reports")
    width = constraint.qubits
    outcomes = [format(value, f"0{width}b") for value in range(1 << width)]
    witnesses = {bits for bits in outcomes if constraint.matches(bits)}
    digest = constraint.digest()
    profiles = []
    for report in reports:
        report = _object(report, "calibration comparison report")
        if not fusion_diagnostic and "density_fusion" in report:
            raise ValueError(
                "fusion-tagged calibration reports require compare_snapshot_calibration_fusion_budgets "
                "or the budget planner's --fusion-reference and --fusion-probability-tolerance flags"
            )
        profiles.append(_profile(report, width, digest, outcomes, witnesses))
    reference = profiles[0]
    if any(profile.scope != reference.scope for profile in profiles):
        raise ValueError("calibration reports must share the same dated pair, SDK versions, and compiler settings")
    if len({profile.layout for profile in profiles}) != len(profiles):
        raise ValueError("calibration reports must have distinct ordered initial layouts")
    return profiles, witnesses


def _plan_calibration_profiles(
    profiles: list[_CalibrationProfile], constraint: BooleanConstraint, count: int, options: dict[str, Any],
    required: int, validation_trials: int,
) -> dict[str, Any]:
    width = constraint.qubits
    digest = constraint.digest()
    reference = profiles[0]

    eligible = [profile for profile in profiles if len(profile.programs) >= required]
    layouts = {}
    candidates = []
    lane_rows = {label: [] for label in _LANES}
    for profile in sorted(profiles, key=lambda item: item.layout):
        layout = ",".join(map(str, profile.layout))
        enough = len(profile.programs) >= required
        layouts[layout] = {
            "initial_layout": list(profile.layout), "status": "ELIGIBLE" if enough else "INSUFFICIENT_ROUND_PROFILES",
            "saved_rounds": list(range(len(profile.programs))), "programs": profile.programs[:required],
        }
        if not enough:
            continue
        rows = {}
        for label in _LANES:
            def round_model(_: int, choices: int) -> tuple[list[tuple[float, float]], list[int]]:
                return profile.probabilities[label][:choices], profile.gate_costs[:choices]

            _, rows[label] = _selection_grid(width, [count], round_model=round_model, **options)
            lane_rows[label].extend({**row, "layout": layout} for row in rows[label])
        candidates.extend(_paired_candidate(layout, a, b) for a, b in zip(rows["earlier"], rows["later"], strict=True))

    classes = Counter(row["target_status"] for row in candidates)
    result = {
        "schema_version": 1,
        "status": "SNAPSHOT_CALIBRATION_BUDGETS_PLANNED" if candidates else "SNAPSHOT_CALIBRATION_BUDGET_PLANNING_UNAVAILABLE",
        "model": "INDEPENDENT_DRAWS_FROM_TWO_SAVED_IBM_SNAPSHOT_MODELS",
        "probability_basis": "SAVED_NORMALIZED_OUTCOME_PROBABILITIES",
        "inputs": {"constraint_sha256": digest, "search_qubits": width, "solution_count": count, **options},
        **reference.scope, "required_rounds": list(range(required)), "layouts": layouts,
        "candidate_count": len(candidates), **_selection(candidates),
        "target_status_counts": {key: classes[key] for key in _TARGET_STATES},
        "lane_selection": {label: _selection(rows) for label, rows in lane_rows.items()},
        "classical_distinct_uniform": {str(cap): _distinct_classical(width, count, cap) for cap in options["shot_caps"]},
        "candidates": candidates, "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if validation_trials:
        by_layout = {",".join(map(str, profile.layout)): profile for profile in eligible}
        result["validation"] = _replay_calibration_budgets(result, by_layout, constraint, validation_trials)
    return result

