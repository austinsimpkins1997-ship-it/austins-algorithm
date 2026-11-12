"""Validate saved snapshot panels and carry their selection evidence portably."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from itertools import islice
from pathlib import Path
from typing import Any

from .core import MAX_ITERATIONS, _bounded_int
from .grover_search import MAX_ORACLE_QUERIES, MAX_SEARCH_SHOTS, QUERY_BUDGET_POLICIES, _unknown_round_choices
from .ibm_models import MAX_IBM_SNAPSHOT_ACTIVE_QUBITS
from .unknown_count_snapshot import SNAPSHOT_SEARCH_MODELS


MAX_SNAPSHOT_PANEL_BYTES = 16 * 1024 * 1024
MAX_VALIDATION_TRIALS = 128
_EXPERIMENT = "LOCAL_UNKNOWN_COUNT_SELECTION_SNAPSHOT_COMPARISON"
_BUNDLE = "LOCAL_UNKNOWN_COUNT_SNAPSHOT_PROFILE_BUNDLE"
_COMPLETED = ("COMPLETED", "COMPLETED_WITH_BLOCKED_CASES")
_BUNDLE_FIELDS = {"schema_version", "artifact", "source", "configuration", "suites",
                  "provider_job_submitted", "quantum_hardware_used", "bundle_sha256"}


def _digest(value: Any) -> str:
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot panel contains values that cannot be hashed as finite JSON") from exc
    return hashlib.sha256(data).hexdigest()


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("snapshot panel contains a duplicate JSON key")
    return obj


def _reject_nonfinite(token: str) -> None:
    raise ValueError(f"snapshot panel contains a nonfinite value: {token}")


def load_snapshot_selection_panel(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read a bounded JSON panel or bundle and return its exact file digest."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_SNAPSHOT_PANEL_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_PANEL_BYTES:
        raise ValueError(f"snapshot panel exceeds {MAX_SNAPSHOT_PANEL_BYTES} bytes")
    try:
        panel = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("snapshot panel must be a finite UTF-8 JSON document") from exc
    return _object(panel, "snapshot panel"), hashlib.sha256(raw).hexdigest()


def _check_expected_observations(claimed: str, expected: str | None) -> None:
    if expected is not None and claimed != _sha256(expected, "expected observations digest"):
        raise ValueError("snapshot panel does not match the expected observations digest")


def _panel_header(
    panel: dict[str, Any], expected_digest: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if (
        type(panel.get("schema_version")) is not int or panel["schema_version"] != 1
        or panel.get("experiment") != _EXPERIMENT
        or panel.get("status") not in ("COMPLETED", "COMPLETED_WITH_BLOCKED_CASES")
        or panel.get("provider_job_submitted") is not False or panel.get("quantum_hardware_used") is not False
    ):
        raise ValueError("a completed offline unknown-count snapshot panel is required")
    configuration = _object(panel.get("configuration"), "panel configuration")
    if configuration.get("model") not in SNAPSHOT_SEARCH_MODELS:
        raise ValueError("source snapshot panel must name a supported local model")
    observations = _object(panel.get("observations"), "panel observations")
    claimed = _sha256(panel.get("observations_sha256"), "observations_sha256")
    _check_expected_observations(claimed, expected_digest)
    if claimed != _digest({"configuration": configuration, "observations": observations}):
        raise ValueError("snapshot panel observations digest does not match its contents")
    return claimed, configuration, observations


def _suite_parts(
    entry: dict[str, Any],
) -> tuple[dict[str, Any], int, list[int], dict[int, dict[str, Any]]]:
    selection = _object(entry.get("selection"), "source ideal selection")
    inputs = _object(selection.get("inputs"), "source ideal selection inputs")
    width = _bounded_int(inputs.get("search_qubits"), 1, MAX_IBM_SNAPSHOT_ACTIVE_QUBITS, "search_qubits")
    declared = inputs.get("solution_counts")
    if not isinstance(declared, list) or not 1 <= len(declared) <= (1 << width) + 1:
        raise ValueError("source selection must declare its solution counts")
    counts = [_bounded_int(value, 0, 1 << width, "declared solution count") for value in declared]
    if len(counts) != len(set(counts)):
        raise ValueError("declared solution counts must be distinct")

    cases = {}
    for key, case in _object(entry.get("cases"), "snapshot cases").items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or len(key) > 3:
            raise ValueError("snapshot case IDs must be decimal solution counts")
        count = _bounded_int(int(key), 0, 1 << width, "case solution count")
        case = _object(case, "snapshot case")
        expected_role = "DECLARED" if count in counts else "HOLDOUT"
        if key != str(count) or case.get("role") != expected_role:
            raise ValueError("snapshot case IDs and roles must match the declared counts")
        cases[count] = case
    if not set(counts).issubset(cases):
        raise ValueError("snapshot panel is missing a declared case")
    return entry, width, sorted(counts), cases


def _wires(value: Any, width: int, *, label: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must contain distinct target wires")
    wires = [_bounded_int(wire, 0, width - 1, label) for wire in value]
    if len(set(wires)) != len(wires):
        raise ValueError(f"{label} must contain distinct target wires")
    return wires


def _round_profile(
    entry: dict[str, Any], width: int, count: int, anchor: list[int], snapshot: dict[str, Any], seed: int,
) -> dict[str, Any]:
    compilation = _object(entry.get("compilation"), "round compilation")
    target = _object(compilation.get("target"), "round target")
    target_width = _bounded_int(target.get("qubits"), width, 127, "snapshot target width")
    if (
        compilation.get("status") != "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION"
        or target.get("name") != snapshot["backend_name"] or compilation.get("seed_transpiler") != seed
        or _wires(compilation.get("initial_logical_to_target_qubit"), target_width, label="initial layout") != anchor
        or _object(entry.get("density_preparation"), "density preparation").get("status") != "COMPLETED"
    ):
        raise ValueError("snapshot round needs a verified compile, matching target and anchor, and completed density")
    export = _object(compilation.get("openqasm"), "round OpenQASM")
    active = _wires(export.get("export_wire_to_target_qubit"), target_width, label="active wires")
    measured = _wires(export.get("classical_bit_to_target_qubit"), target_width, label="measurement map")
    if len(active) > MAX_IBM_SNAPSHOT_ACTIVE_QUBITS or len(measured) != width or not set(measured).issubset(active):
        raise ValueError("snapshot round measurement map must fit its verified active wires")
    qasm = _sha256(export.get("sha256"), "round OpenQASM digest")
    compiled = _object(compilation.get("compiled"), "compiled resources")
    gates = _bounded_int(compiled.get("two_qubit_gates"), 0, 1_000_000, "compiled two-qubit gates")

    result = _object(entry.get("snapshot_probabilities"), "snapshot probabilities")
    distribution = _object(result.get("outcome_probabilities"), "outcome probabilities")
    outcomes = [format(index, f"0{width}b") for index in range(1 << width)]
    if distribution.keys() != set(outcomes):
        raise ValueError("snapshot round must contain every outcome in the audited bit order")
    values = [distribution[bits] for bits in outcomes]
    if any(type(value) not in (int, float) or not 0 <= value <= 1 for value in values):
        raise ValueError("snapshot outcome probabilities must be finite and nonnegative")
    mass = math.fsum(values)
    if not math.isclose(mass, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("snapshot outcome probability mass must equal one")
    hit = math.fsum(values[:count]) / mass
    miss = math.fsum(values[count:]) / mass
    recorded = result.get("verified_hit_probability")
    if (
        type(recorded) not in (int, float) or not 0 <= recorded <= 1
        or not math.isclose(recorded, hit, rel_tol=0.0, abs_tol=1e-9)
    ):
        raise ValueError("snapshot hit probability disagrees with the audited witness distribution")
    return {
        "probabilities": (hit, miss), "weights": values, "gates": gates,
        "evidence": {
            "openqasm_sha256": qasm, "active_target_wires": active, "measurement_map": measured,
            "compiled_two_qubit_gates": gates, "verified_hit_probability": hit,
        },
    }


def _case_scope(
    case: dict[str, Any], width: int, count: int, configuration: dict[str, Any],
) -> tuple[dict[str, Any], int, dict[str, Any], dict[str, Any], str]:
    comparison = _object(case.get("comparison"), "snapshot comparison")
    seed = _bounded_int(configuration.get("seed_transpiler"), 0, 2**32 - 1, "source transpiler seed")
    if (
        comparison.get("execution_mode") != "SIMULATOR"
        or comparison.get("search_qubits") != width or comparison.get("seed_transpiler") != seed
        or comparison.get("provider_job_submitted") is not False or comparison.get("quantum_hardware_used") is not False
    ):
        raise ValueError(f"solution count {count} requires matching offline snapshot metadata")
    reference = _object(case.get("reference"), "snapshot threshold reference")
    witnesses = [format(index, f"0{width}b") for index in range(count)]
    if (
        reference.get("solution_count") != count or reference.get("states_checked") != 1 << width
        or reference.get("witnesses") != witnesses
    ):
        raise ValueError(f"solution count {count} has an inconsistent threshold or oracle audit")
    snapshot = _object(comparison.get("snapshot"), "snapshot identity")
    if (
        snapshot.get("model") != configuration.get("model")
        or any(not isinstance(snapshot.get(key), str) or not snapshot[key]
               for key in ("model", "backend_name", "backend_version", "last_update_date"))
    ):
        raise ValueError("snapshot identity must match the configured local model")
    _sha256(snapshot.get("configuration_properties_sha256"), "snapshot configuration digest")
    versions = _object(comparison.get("versions"), "snapshot versions")
    if any(not isinstance(versions.get(key), str) or not versions[key]
           for key in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")):
        raise ValueError("snapshot profile requires the simulator and compiler versions")

    digest = _sha256(reference.get("constraint_sha256"), "threshold constraint digest")
    return comparison, seed, snapshot, versions, digest


def _case_profile(
    case: dict[str, Any], width: int, count: int, configuration: dict[str, Any],
) -> dict[str, Any]:
    comparison = _object(case.get("comparison"), "snapshot comparison")
    if comparison.get("status") != "COMPLETED":
        raise ValueError(f"solution count {count} requires a complete offline snapshot profile")
    comparison, seed, snapshot, versions, digest = _case_scope(case, width, count, configuration)
    witnesses = [format(index, f"0{width}b") for index in range(count)]
    if comparison.get("audit") != {"states_checked": 1 << width, "marked_count": count, "marked_states": witnesses}:
        raise ValueError(f"solution count {count} has an inconsistent threshold or oracle audit")
    raw_rounds = _object(comparison.get("compilations"), "round compilations")
    if not 1 <= len(raw_rounds) <= math.ceil(math.sqrt(1 << width)):
        raise ValueError("snapshot round profiles exceed the search-width schedule")
    rounds = list(range(len(raw_rounds)))
    if raw_rounds.keys() != {str(rounds) for rounds in rounds} or comparison.get("rounds_to_prepare") != rounds:
        raise ValueError("snapshot round profiles must be contiguous from round 0")
    anchor = _wires(comparison.get("anchor_layout"), 127, label="anchor layout")
    if not width <= len(anchor) <= MAX_IBM_SNAPSHOT_ACTIVE_QUBITS:
        raise ValueError("snapshot anchor must fit the audited search width")
    profiles = [_round_profile(_object(raw_rounds[str(rounds)], "round profile"), width, count, anchor, snapshot, seed)
                for rounds in rounds]
    return {
        "snapshot": snapshot, "versions": versions, "rounds": profiles, "anchor_layout": anchor,
        "constraint_sha256": digest,
    }

def _source_plan_budgets(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plans = _object(entry.get("plans", {}), "source ideal plans")
    if len(plans) > 4:
        raise ValueError("snapshot panel has too many source ideal plans")
    normalized = {}
    for name, plan in plans.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("source ideal plan names must contain 1 to 100 characters")
        budget = _object(_object(plan, "source ideal plan").get("selected_budget"), "source ideal budget")
        policy = budget.get("policy")
        if policy not in QUERY_BUDGET_POLICIES:
            raise ValueError("source ideal plan requires a supported budget policy")
        normalized[name] = {
            "policy": policy,
            "max_shots": _bounded_int(budget.get("max_shots"), 1, MAX_SEARCH_SHOTS, "source ideal max_shots"),
            "max_oracle_queries": _bounded_int(budget.get("max_oracle_queries"), 0, MAX_ORACLE_QUERIES,
                                               "source ideal max_oracle_queries"),
            "max_rounds_per_shot": _bounded_int(budget.get("max_rounds_per_shot"), 0, MAX_ITERATIONS,
                                               "source ideal max_rounds_per_shot"),
        }
    return normalized


def _source_trial_seeds(case: dict[str, Any]) -> list[int]:
    seeds = _object(case.get("comparison"), "snapshot comparison").get("seeds")
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= MAX_VALIDATION_TRIALS:
        raise ValueError("source snapshot case requires its recorded trial seeds")
    normalized = [_bounded_int(seed, 0, 2**32 - 1, "source trial seed") for seed in seeds]
    if len(set(normalized)) != len(normalized):
        raise ValueError("source snapshot case has duplicate trial seeds")
    return normalized


def _project_round(entry: dict[str, Any]) -> dict[str, Any]:
    compiled = entry["compilation"]
    export = compiled["openqasm"]
    return {
        "compilation": {
            "status": compiled["status"],
            "target": {key: compiled["target"][key] for key in ("name", "qubits")},
            "seed_transpiler": compiled["seed_transpiler"],
            "initial_logical_to_target_qubit": compiled["initial_logical_to_target_qubit"],
            "compiled": {"two_qubit_gates": compiled["compiled"]["two_qubit_gates"]},
            "openqasm": {key: export[key] for key in ("sha256", "export_wire_to_target_qubit",
                                                     "classical_bit_to_target_qubit")},
        },
        "density_preparation": {"status": entry["density_preparation"]["status"]},
        "snapshot_probabilities": {
            key: entry["snapshot_probabilities"][key] for key in ("outcome_probabilities", "verified_hit_probability")
        },
    }


def _project_case(case: dict[str, Any], width: int, count: int, configuration: dict[str, Any]) -> dict[str, Any]:
    comparison = _object(case.get("comparison"), "snapshot comparison")
    if comparison.get("status") == "INSUFFICIENT_TARGET_WIDTH":
        _case_scope(case, width, count, configuration)
        target_width = {"manila": 5, "jakarta": 7}[configuration["model"]]
        _bounded_int(comparison.get("circuit_qubits"), max(width, target_width + 1), 12, "blocked circuit width")
        if comparison.get("audit") is not None or comparison.get("anchor_layout") is not None or (
            comparison.get("compilations") != {} or comparison.get("plans", {}) != {}
        ):
            raise ValueError("width-blocked snapshot cases must contain no audit, layout, or round evidence")
        audit, anchor, rounds, compilations = None, None, [], {}
    else:
        _case_profile(case, width, count, configuration)
        audit = comparison["audit"]
        anchor = comparison["anchor_layout"]
        rounds = comparison["rounds_to_prepare"]
        compilations = {key: _project_round(value) for key, value in comparison["compilations"].items()}
    projected = {
        key: comparison[key] for key in ("status", "execution_mode", "search_qubits", "snapshot", "versions",
                                        "seed_transpiler", "provider_job_submitted", "quantum_hardware_used")
    }
    projected.update(seeds=_source_trial_seeds(case), audit=audit, anchor_layout=anchor,
                     rounds_to_prepare=rounds, compilations=compilations)
    if comparison["status"] == "INSUFFICIENT_TARGET_WIDTH":
        projected["circuit_qubits"] = comparison["circuit_qubits"]
    reference = case["reference"]
    return {
        "role": case["role"],
        "reference": {key: reference[key] for key in ("states_checked", "solution_count", "witnesses", "constraint_sha256")},
        "comparison": projected,
    }


def _project_suites(
    observations: dict[str, Any], configuration: dict[str, Any], panel_status: str,
) -> dict[str, Any]:
    if not observations:
        raise ValueError("snapshot panel must contain at least one suite")
    projected = {}
    identity = None
    has_blocked = False
    has_completed = False
    for name, suite in observations.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("snapshot suite names must contain 1 to 100 characters")
        entry, width, counts, cases = _suite_parts(_object(suite, "snapshot suite"))
        source_plans = [{"name": plan, "budget": budget} for plan, budget in _source_plan_budgets(entry).items()]
        required_rounds = 1
        for plan in source_plans:
            budget = plan["budget"]
            windows = islice(_unknown_round_choices(width, budget["max_rounds_per_shot"]), budget["max_shots"])
            required_rounds = max(required_rounds, min(max(windows), budget["max_oracle_queries"] + 1))
        kept = {}
        for count, case in sorted(cases.items()):
            profile = _project_case(case, width, count, configuration)
            comparison = profile["comparison"]
            scope = comparison["snapshot"], comparison["versions"]
            if identity is not None and scope != identity:
                raise ValueError("snapshot profiles must share one calibration identity and software stack")
            identity = scope
            has_blocked |= comparison["status"] == "INSUFFICIENT_TARGET_WIDTH"
            has_completed |= comparison["status"] == "COMPLETED"
            if comparison["status"] == "COMPLETED" and len(comparison["compilations"]) < required_rounds:
                raise ValueError(f"solution count {count} is missing a round for the recorded source budget")
            kept[str(count)] = profile
        projected[name] = {
            "search_qubits": width, "solution_counts": counts, "source_plans": source_plans, "cases": kept,
        }
    if not has_completed or has_blocked != (panel_status == "COMPLETED_WITH_BLOCKED_CASES"):
        raise ValueError("snapshot case coverage disagrees with the panel completion status")
    return projected


def export_snapshot_profile_bundle(
    panel: dict[str, Any], *, expected_observations_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify a full panel and export only the records used for offline selection."""
    panel = _object(panel, "snapshot panel")
    digest, configuration, observations = _panel_header(panel, expected_observations_sha256)
    configuration = {key: configuration.get(key) for key in ("model", "seed_transpiler")}
    suites = _project_suites(observations, configuration, panel["status"])
    bundle = deepcopy({
        "schema_version": 1,
        "artifact": _BUNDLE,
        "source": {"experiment": _EXPERIMENT, "schema_version": 1, "observations_sha256": digest,
                   "status": panel["status"]},
        "configuration": configuration,
        "suites": suites,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    })
    bundle["bundle_sha256"] = _digest(bundle)
    return bundle


def _bundle_observations(suites: dict[str, Any]) -> dict[str, Any]:
    entries = {}
    for name, raw in suites.items():
        suite = _object(raw, "profile bundle suite")
        if set(suite) != {"search_qubits", "solution_counts", "source_plans", "cases"}:
            raise ValueError("profile bundle suite has unexpected fields")
        plans = suite["source_plans"]
        if not isinstance(plans, list) or len(plans) > 4:
            raise ValueError("profile bundle source_plans must be an ordered list of up to four budgets")
        ordered = {}
        for item in plans:
            plan = _object(item, "profile bundle source plan")
            key = plan.get("name")
            if set(plan) != {"name", "budget"} or not isinstance(key, str) or key in ordered:
                raise ValueError("profile bundle source plan names must be distinct")
            ordered[key] = {"selected_budget": plan["budget"]}
        entries[name] = {
            "selection": {"inputs": {"search_qubits": suite["search_qubits"], "solution_counts": suite["solution_counts"]}},
            "plans": ordered, "cases": suite["cases"],
        }
    return entries


def _read_bundle(
    bundle: dict[str, Any], expected_bundle_sha256: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    bundle = _object(bundle, "snapshot profile bundle")
    if (
        set(bundle) != _BUNDLE_FIELDS or bundle.get("artifact") != _BUNDLE
        or type(bundle.get("schema_version")) is not int or bundle["schema_version"] != 1
        or bundle.get("provider_job_submitted") is not False or bundle.get("quantum_hardware_used") is not False
    ):
        raise ValueError("an offline snapshot profile bundle version 1 is required")
    claimed = _sha256(bundle["bundle_sha256"], "bundle_sha256")
    if expected_bundle_sha256 is not None and claimed != _sha256(expected_bundle_sha256, "expected bundle digest"):
        raise ValueError("snapshot profile bundle does not match the expected bundle digest")
    if claimed != _digest({key: value for key, value in bundle.items() if key != "bundle_sha256"}):
        raise ValueError("snapshot profile bundle digest does not match its contents")
    source = _object(bundle["source"], "profile bundle source")
    if (
        set(source) != {"experiment", "schema_version", "observations_sha256", "status"}
        or source["experiment"] != _EXPERIMENT or type(source["schema_version"]) is not int
        or source["schema_version"] != 1 or source["status"] not in _COMPLETED
    ):
        raise ValueError("profile bundle requires a completed offline snapshot source identity")
    _sha256(source["observations_sha256"], "source observations digest")
    configuration = _object(bundle["configuration"], "profile bundle configuration")
    if set(configuration) != {"model", "seed_transpiler"} or configuration["model"] not in SNAPSHOT_SEARCH_MODELS:
        raise ValueError("profile bundle requires a supported snapshot configuration")
    suites = _object(bundle["suites"], "profile bundle suites")
    observations = _bundle_observations(suites)
    if _project_suites(observations, configuration, source["status"]) != suites:
        raise ValueError("snapshot profile bundle contains data outside the selection projection")
    return claimed, source, configuration, observations


def verify_snapshot_profile_bundle(
    bundle: dict[str, Any], *, source_panel: dict[str, Any] | None = None,
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify bundle structure and optionally check its derivation against the source panel."""
    digest, source, configuration, observations = _read_bundle(bundle, expected_bundle_sha256)
    if source_panel is not None and export_snapshot_profile_bundle(
        source_panel, expected_observations_sha256=source["observations_sha256"],
    ) != bundle:
        raise ValueError("snapshot profile bundle differs from the projection of the supplied source panel")
    suites = {}
    for name, entry in observations.items():
        _, width, counts, cases = _suite_parts(entry)
        completed = [count for count, case in sorted(cases.items()) if case["comparison"]["status"] == "COMPLETED"]
        suites[name] = {
            "search_qubits": width, "declared_counts": counts, "completed_counts": completed,
            "blocked_counts": sorted(set(cases) - set(completed)),
        }
    return {
        "status": "SNAPSHOT_PROFILE_BUNDLE_VERIFIED",
        "bundle_sha256": digest,
        "source_observations_sha256": source["observations_sha256"],
        "source_panel_matched": source_panel is not None,
        "model": configuration["model"],
        "suites": suites,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
    }


def _selection_suite(
    document: dict[str, Any], suite: str, expected_observations_sha256: str | None,
    expected_bundle_sha256: str | None,
) -> tuple[str, str | None, dict[str, Any], dict[str, Any], int, list[int], dict[int, dict[str, Any]]]:
    if document.get("artifact") == _BUNDLE:
        bundle_digest, source, configuration, observations = _read_bundle(document, expected_bundle_sha256)
        observed = source["observations_sha256"]
        _check_expected_observations(observed, expected_observations_sha256)
    else:
        if expected_bundle_sha256 is not None:
            raise ValueError("expected bundle digest requires a snapshot profile bundle")
        observed, configuration, observations = _panel_header(document, expected_observations_sha256)
        bundle_digest = None
    if not isinstance(suite, str) or suite not in observations:
        raise ValueError("suite must name a suite in the snapshot panel or profile bundle")
    entry, width, counts, cases = _suite_parts(_object(observations[suite], "snapshot suite"))
    return observed, bundle_digest, configuration, entry, width, counts, cases
