"""Rebuild a verified local placement receipt before replaying its measurements."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .ibm_models import _snapshot_identity, _snapshot_simulator, _snapshot_specs
from .placement_receipt import _INTERVAL_TOLERANCE, _VerifiedPlacementBundle
from .placement_receipt_comparison import (
    _SAMPLING_SOFTWARE,
    _cells,
    _changes,
    _circuit,
    _compare_trial,
    _importer,
    _read_side,
    _side_summary,
    _trial_state,
)
from .replay_receipt import _software_versions
from .snapshot_placement import PreparedMod4PlacementCheck, _evaluate_trials, _placement_selection


def _match_identity(label: str, field: str, saved: Any, installed: Any) -> None:
    if saved != installed:
        raise ValueError(f"{label} replay identity differs on {field}")


def _prepare_replay(plan: PreparedMod4PlacementCheck, bundle: _VerifiedPlacementBundle) -> dict[str, Any]:
    plan._prepare_circuits()
    specs = _snapshot_specs()
    models = {}
    for name, evidence in bundle.envelope["snapshots"].items():
        recorded = bundle.envelope["report"]["models"][name]
        snapshot = plan._prepare_snapshot(name, *specs[name])
        backend = snapshot.backend
        identity = _snapshot_identity(backend)
        for field, saved in {
            "backend_name": recorded["backend"],
            "backend_version": recorded["backend_version"],
            "last_update_date": recorded["snapshot_last_update_date"],
            "configuration_properties_sha256": evidence["sha256"],
        }.items():
            _match_identity(name, field, saved, identity[field])
        _match_identity(
            name, "effective_coupling", {tuple(edge) for edge in evidence["coupling"]},
            {tuple(edge) for edge in backend.coupling_map.get_edges()},
        )
        _match_identity(name, "initial_layout", evidence["initial_layout"], list(snapshot.initial_layout))
        _match_identity(name, "insufficient_topology", recorded["status"] == "INSUFFICIENT_TOPOLOGY", not snapshot.layouts)

        layouts = dict(snapshot.layouts)
        cells = []
        for (label, rounds), cell in _cells(bundle, name).items():
            location = f"{name}/{label}/{rounds}"
            program = plan._prepare_program(snapshot, label, layouts[label], rounds)
            compiled = {"compilation": program.report}
            _match_identity(location, "openqasm", evidence["exports"][label][str(rounds)], program.openqasm)
            circuit = _circuit(compiled)
            for field, saved in _circuit(cell).items():
                _match_identity(location, field, saved, circuit[field])
            for field in ("target", "compiled"):
                _match_identity(location, field, cell["compilation"][field], program.report[field])
            cells.append({
                "placement": label,
                "rounds": rounds,
                "circuit": circuit,
                "importer_changes": _changes(_importer(cell), _importer(compiled)),
            })
        models[name] = {"snapshot": identity, "cells": cells}
    return models


def _evaluations_differ(saved: dict[str, Any], replayed: dict[str, Any]) -> bool:
    if any(saved.get(key) != replayed.get(key) for key in (
        "status", "verified_shots", "completed_shots", "verified_hit_rate",
    )):
        return True
    before, after = saved["interval"], replayed["interval"]
    if before is None or after is None:
        return before != after
    return any(
        not math.isclose(left, right, rel_tol=0, abs_tol=_INTERVAL_TOLERANCE)
        for left, right in zip(before, after, strict=True)
    )


def _sample_replay(
    plan: PreparedMod4PlacementCheck, bundle: _VerifiedPlacementBundle, models: dict[str, Any],
    summary: dict[str, int], *, sampling_versions_match: bool,
) -> None:
    from qiskit_aer import AerSimulator

    report = bundle.envelope["report"]
    request = bundle.request
    for name, result in models.items():
        snapshot = plan._snapshots[name]
        simulator = None
        recorded = report["models"][name]
        saved_cells = _cells(bundle, name)
        replayed_placements: dict[str, dict[str, Any]] = {}
        for row in result["cells"]:
            label, rounds = row["placement"], row["rounds"]
            saved = saved_cells[label, rounds]
            before = {trial["seed"]: trial for trial in saved["trials"]}
            seeds = [seed for seed, trial in before.items() if trial["status"] == "COMPLETED"]
            program = snapshot.programs[label, rounds]
            trials = []
            if seeds and not plan._sampling_unconfirmed:
                if simulator is None:
                    simulator = _snapshot_simulator(snapshot.backend, AerSimulator, snapshot.method)
                trials = plan._sample_trials(program, snapshot, simulator, shots=request["shots"], seeds=seeds)

            after = {trial["seed"]: trial for trial in trials}
            row["trials"] = [
                _compare_trial(
                    seed, _trial_state(before.get(seed), True), _trial_state(after.get(seed), True),
                    conditions_match=sampling_versions_match,
                )
                for seed in request["evaluation_seeds"]
            ]
            summary["replayed_trials"] += len(trials)
            summary["completed_replay_trials"] += sum(trial["status"] == "COMPLETED" for trial in trials)
            summary["matched_histograms"] += sum(trial["counts_match"] is True for trial in row["trials"])
            summary["changed_histograms"] += sum(trial["counts_match"] is False for trial in row["trials"])

            evaluation = _evaluate_trials(
                trials, expected_batches=len(request["evaluation_seeds"]),
                confidence=report["interval_confidence_per_cell"], floor=request["minimum_hit_rate"],
            )
            row["reference_evaluation"] = saved["evaluation"]
            row["replay_evaluation"] = evaluation
            summary["changed_cell_evaluations"] += _evaluations_differ(saved["evaluation"], evaluation)
            placement = replayed_placements.setdefault(label, {"rounds": {}})
            placement["rounds"][rounds] = {"compilation": program.report, "evaluation": evaluation}

        status, eligible, selected = _placement_selection(list(replayed_placements.values()))
        if recorded["status"] == "INSUFFICIENT_TOPOLOGY":
            status = "INSUFFICIENT_TOPOLOGY"
        result["reference_decision"] = {key: recorded[key] for key in ("status", "eligible_rounds", "selected_rounds")}
        result["replay_decision"] = {"status": status, "eligible_rounds": eligible, "selected_rounds": selected}
        if status == recorded["status"] == "COMPLETED":
            summary["completed_model_pairs"] += 1
            summary["changed_model_decisions"] += result["reference_decision"] != result["replay_decision"]


def replay_placement_receipt(directory: str | Path, *, check_only: bool = False) -> dict[str, Any]:
    """Verify installed identities, then optionally rerun the saved completed trials."""
    if type(check_only) is not bool:
        raise ValueError("check_only must be a boolean")
    bundle = _read_side(directory)
    current_versions = _software_versions()
    software_changes = _changes(bundle.manifest["software_versions"], current_versions)
    request = bundle.request
    plan = PreparedMod4PlacementCheck(
        model=request["model"], transpiler_seed=request["transpiler_seed"],
        prefer_native_qasm=request["prefer_native_qasm"],
    )
    models = _prepare_replay(plan, bundle)
    summary = dict.fromkeys((
        "replayed_trials", "completed_replay_trials", "matched_histograms", "changed_histograms",
        "changed_cell_evaluations", "completed_model_pairs", "changed_model_decisions",
    ), 0)
    summary["verified_exports"] = sum(len(model["cells"]) for model in models.values())
    sampling_versions_match = all(key not in software_changes for key in _SAMPLING_SOFTWARE)
    decision_versions_match = "scipy" not in software_changes

    status = "PLACEMENT_REPLAY_IDENTITIES_VERIFIED"
    if not check_only:
        _sample_replay(plan, bundle, models, summary, sampling_versions_match=sampling_versions_match)
        if bundle.summary["placement_status"] != "COMPLETED" or summary["completed_model_pairs"] != len(models):
            status = "PLACEMENT_REPLAY_INCOMPLETE"
        elif any(summary[key] for key in ("changed_histograms", "changed_cell_evaluations", "changed_model_decisions")):
            status = "PLACEMENT_REPLAY_DIFFERED"
        elif not (sampling_versions_match and decision_versions_match):
            status = "PLACEMENT_REPLAY_SOFTWARE_CHANGED"
        else:
            status = "PLACEMENT_REPLAY_MATCHED"

    return {
        "status": status,
        "check_only": check_only,
        "reference": _side_summary(bundle),
        "installed_software_versions": current_versions,
        "software_changes": software_changes,
        "matching_sampling_software_versions": sampling_versions_match,
        "matching_decision_software_version": decision_versions_match,
        "local_cancellation_unconfirmed": plan._sampling_unconfirmed,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "summary": summary,
        "models": models,
    }
