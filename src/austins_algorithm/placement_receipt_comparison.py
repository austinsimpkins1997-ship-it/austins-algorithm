"""Compare verified placement receipts by snapshot, placement, round and seed."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from .ibm_models import IBM_SNAPSHOT_MODELS
from .placement_receipt import _VerifiedPlacementBundle, _canonical, _read_verified_placement_receipt
from .snapshot_placement import ROUNDS


_SAMPLING_SOFTWARE = ("python", "austins_algorithm", "qiskit", "qiskit-aer", "qiskit-ibm-runtime", "numpy")
_DECISION_FIELDS = ("minimum_hit_rate", "predeclared_cells", "family_confidence", "interval_confidence_per_cell", "interval_method")
_TRIAL_OUTCOMES = (
    "COUNTS_MATCHED", "COUNTS_CHANGED", "SAMPLING_CONDITIONS_CHANGED",
    "LEFT_ONLY_PLANNED_TRIAL", "RIGHT_ONLY_PLANNED_TRIAL", "TRIAL_STATE_CHANGED", "UNMEASURED_PAIR",
)
_IMPORTER_FIELDS = ("importer", "preferred_importer")


def _read_side(path: str | Path) -> _VerifiedPlacementBundle:
    try:
        return _read_verified_placement_receipt(Path(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _changes(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {key: {"left": value, "right": right[key]} for key, value in left.items() if value != right[key]}


def _side_summary(bundle: _VerifiedPlacementBundle) -> dict[str, Any]:
    return {
        "receipt_directory": str(bundle.directory),
        "request": bundle.request,
        "software_versions": bundle.manifest["software_versions"],
        "artifacts": bundle.manifest["artifacts"],
        **bundle.summary,
    }


def _model_summary(bundle: _VerifiedPlacementBundle, name: str) -> dict[str, Any]:
    recorded = bundle.envelope["report"]["models"].get(name)
    if recorded is None:
        requested = bundle.request["model"] in ("all", name)
        return {
            "status": "NOT_RUN" if requested else "NOT_REQUESTED",
            "snapshot": None,
            "eligible_rounds": None,
            "selected_rounds": None,
        }
    snapshot = bundle.envelope["snapshots"][name]
    return {
        "status": recorded["status"],
        "snapshot": {
            "backend": recorded["backend"],
            "backend_version": recorded["backend_version"],
            "snapshot_last_update_date": recorded["snapshot_last_update_date"],
            "calibration_sha256": snapshot["sha256"],
            "effective_coupling_sha256": sha256(_canonical(sorted(snapshot["coupling"]))).hexdigest(),
        },
        "eligible_rounds": recorded["eligible_rounds"],
        "selected_rounds": recorded["selected_rounds"],
    }


def _cells(bundle: _VerifiedPlacementBundle, name: str) -> dict[tuple[str, int], dict[str, Any]]:
    model = bundle.envelope["report"]["models"].get(name, {})
    return {
        (placement["label"], int(rounds)): cell
        for placement in model.get("placements", [])
        for rounds, cell in placement["rounds"].items()
    }


def _circuit(cell: dict[str, Any]) -> dict[str, Any]:
    compilation = cell["compilation"]
    qasm = compilation["openqasm"]
    return {
        "initial_logical_to_target_qubit": compilation["initial_logical_to_target_qubit"],
        "openqasm_sha256": qasm["sha256"],
        "export_wire_to_target_qubit": qasm["export_wire_to_target_qubit"],
        "classical_bit_to_target_qubit": qasm["classical_bit_to_target_qubit"],
        "two_qubit_gates": compilation["compiled"]["two_qubit_gates"],
    }


def _importer(cell: dict[str, Any]) -> dict[str, Any]:
    qasm = cell["compilation"]["openqasm"]
    return {key: qasm.get(key) for key in _IMPORTER_FIELDS}


def _cell_summary(cell: dict[str, Any] | None) -> dict[str, Any] | None:
    if cell is None:
        return None
    evaluation = cell["evaluation"]
    return {
        **_circuit(cell),
        **_importer(cell),
        "evaluation": {key: evaluation.get(key) for key in (
            "status", "verified_shots", "completed_shots", "verified_hit_rate", "interval",
        )},
    }


def _trial_state(trial: dict[str, Any] | None, planned: bool) -> dict[str, Any]:
    if trial is None:
        return {"status": "UNATTEMPTED" if planned else "NOT_PLANNED"}
    state = {"status": trial["status"]}
    if trial["status"] == "COMPLETED":
        simulation = trial["simulation"]
        state.update({key: simulation[key] for key in ("counts", "completed_shots", "verified_shots")})
    elif trial["status"] == "LOCAL_RESULT_WAIT_TIMED_OUT":
        state["local_job_cancelled"] = trial["local_job_cancelled"]
    return state


def _compare_trial(seed: int, left: dict[str, Any], right: dict[str, Any], conditions_match: bool | None) -> dict[str, Any]:
    counts_match = deltas = None
    if left["status"] == "NOT_PLANNED":
        status = "RIGHT_ONLY_PLANNED_TRIAL"
    elif right["status"] == "NOT_PLANNED":
        status = "LEFT_ONLY_PLANNED_TRIAL"
    elif left["status"] == right["status"] == "COMPLETED":
        old, new = left["counts"], right["counts"]
        counts_match = old == new
        status = "SAMPLING_CONDITIONS_CHANGED"
        if conditions_match:
            status = "COUNTS_MATCHED" if counts_match else "COUNTS_CHANGED"
            deltas = {bits: new.get(bits, 0) - old.get(bits, 0)
                      for bits in sorted(old.keys() | new.keys()) if new.get(bits, 0) != old.get(bits, 0)}
    else:
        status = "UNMEASURED_PAIR" if left == right else "TRIAL_STATE_CHANGED"
    return {"seed": seed, "status": status, "left": left, "right": right, "counts_match": counts_match, "count_deltas": deltas}


def _compare_cell(
    key: tuple[str, int], left: dict[str, Any] | None, right: dict[str, Any] | None, *,
    left_seeds: set[int], right_seeds: set[int], common_changes: dict[str, Any],
) -> dict[str, Any]:
    sampling_changes = importer_changes = conditions_match = None
    if left is not None and right is not None:
        sampling_changes = {**common_changes, **_changes(_circuit(left), _circuit(right))}
        importer_changes = _changes(_importer(left), _importer(right))
        conditions_match = not sampling_changes

    before = {} if left is None else {trial["seed"]: trial for trial in left["trials"]}
    after = {} if right is None else {trial["seed"]: trial for trial in right["trials"]}
    trials = [
        _compare_trial(
            seed, _trial_state(before.get(seed), seed in left_seeds), _trial_state(after.get(seed), seed in right_seeds),
            conditions_match,
        )
        for seed in sorted(left_seeds | right_seeds)
    ]
    return {
        "placement": key[0], "rounds": key[1],
        "left": _cell_summary(left), "right": _cell_summary(right),
        "matching_sampling_conditions": conditions_match,
        "sampling_condition_changes": sampling_changes, "importer_changes": importer_changes,
        "trials": trials,
    }


def _compare_model(
    name: str, left: _VerifiedPlacementBundle, right: _VerifiedPlacementBundle, sampling_changes: dict[str, Any],
) -> dict[str, Any]:
    before, after = _model_summary(left, name), _model_summary(right, name)
    snapshot_changes = None
    if before["snapshot"] is not None and after["snapshot"] is not None:
        snapshot_changes = _changes(before["snapshot"], after["snapshot"])
    decision_changed = None
    if before["status"] == after["status"] == "COMPLETED":
        decision_changed = any(before[key] != after[key] for key in ("eligible_rounds", "selected_rounds"))
    left_seeds = set(left.request["evaluation_seeds"]) if before["status"] != "NOT_REQUESTED" else set()
    right_seeds = set(right.request["evaluation_seeds"]) if after["status"] != "NOT_REQUESTED" else set()
    left_cells, right_cells = _cells(left, name), _cells(right, name)
    cells = [
        _compare_cell(
            (label, rounds), left_cells.get((label, rounds)), right_cells.get((label, rounds)),
            left_seeds=left_seeds, right_seeds=right_seeds, common_changes={**sampling_changes, **(snapshot_changes or {})},
        )
        for label in ("baseline", "alternate")
        for rounds in ROUNDS
        if (label, rounds) in left_cells or (label, rounds) in right_cells
    ]
    return {
        "model": name, "left": before, "right": after,
        "snapshot_changes": snapshot_changes, "decision_changed": decision_changed,
        "cells": cells,
    }


def _decision_context(bundle: _VerifiedPlacementBundle) -> dict[str, Any]:
    report = bundle.envelope["report"]
    return {**{key: report[key] for key in _DECISION_FIELDS}, "scipy": bundle.manifest["software_versions"]["scipy"]}


def compare_placement_receipts(left: str | Path, right: str | Path) -> dict[str, Any]:
    """Verify and compare two local receipts without loading the simulator."""
    before, after = _read_side(left), _read_side(right)
    request_changes = _changes(before.request, after.request)
    software_changes = _changes(before.manifest["software_versions"], after.manifest["software_versions"])
    sampling_versions = {key: software_changes[key] for key in _SAMPLING_SOFTWARE if key in software_changes}
    sampling_changes = {"software_versions": sampling_versions} if sampling_versions else {}
    if "shots" in request_changes:
        sampling_changes["shots"] = request_changes["shots"]
    models = [
        _compare_model(name, before, after, sampling_changes)
        for name in IBM_SNAPSHOT_MODELS
        if any(bundle.request["model"] in ("all", name) for bundle in (before, after))
    ]

    summary = {"paired_cells": 0, "left_only_cells": 0, "right_only_cells": 0, "trial_outcomes": dict.fromkeys(_TRIAL_OUTCOMES, 0)}
    for model in models:
        for cell in model["cells"]:
            coverage = "right_only_cells" if cell["left"] is None else "left_only_cells" if cell["right"] is None else "paired_cells"
            summary[coverage] += 1
            for trial in cell["trials"]:
                summary["trial_outcomes"][trial["status"]] += 1
    return {
        "status": "PLACEMENT_RECEIPTS_COMPARED",
        "left": _side_summary(before), "right": _side_summary(after),
        "request_changes": request_changes, "software_changes": software_changes,
        "decision_context_changes": _changes(_decision_context(before), _decision_context(after)),
        "summary": summary, "models": models,
    }
