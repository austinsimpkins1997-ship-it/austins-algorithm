"""Compare locally verified observations from two Manila smoke run indexes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .smoke_environment_comparison import _changes, _compare_reports, _decode_report, _map_changes
from .smoke_run_index import _VerifiedSmokeRunIndex, _phase_specs, _read_verified_smoke_run_index


@dataclass(frozen=True)
class _BundleEvidence:
    recorded: dict[str, Any]
    observed: dict[str, Any]
    omitted: bool

    def available(self, path: str) -> bool:
        if self.omitted:
            return False
        for inventory in (self.recorded, self.observed):
            if "." in inventory["gaps"] or path in inventory["gaps"]:
                return False
        return self.recorded["files"].get(path) == self.observed["files"].get(path)


def _bundle(run: _VerifiedSmokeRunIndex, name: str) -> _BundleEvidence:
    return _BundleEvidence(
        run.record["bundles"][name],
        run.observations[name],
        run.verification["bundles"][name]["status"] == "NOT_PROVIDED",
    )


def _phase_changes(name: str, left: _BundleEvidence, right: _BundleEvidence) -> tuple[dict[str, Any], dict[str, Any]]:
    changes, unavailable = {}, {}
    for phase, (exit_file, status_file, _) in _phase_specs(name).items():
        before, after = left.observed["phases"][phase], right.observed["phases"][phase]
        outcomes, observations, missing = {}, {}, {"left": [], "right": []}
        for path, state_key, value_key in (
            (exit_file, "exit_observation", "exit_code"), (status_file, "status_observation", "reported_status"),
        ):
            known_left = left.available(path) and before[state_key] != "UNAVAILABLE"
            known_right = right.available(path) and after[state_key] != "UNAVAILABLE"
            if not known_left:
                missing["left"].append(state_key)
            if not known_right:
                missing["right"].append(state_key)
            if not (known_left and known_right):
                continue
            if before[state_key] != after[state_key]:
                observations[state_key] = {"left": before[state_key], "right": after[state_key]}
            if before[state_key] == after[state_key] == "OBSERVED" and before[value_key] != after[value_key]:
                outcomes[value_key] = {"left": before[value_key], "right": after[value_key]}
        if outcomes or observations:
            changes[phase] = {"outcomes": outcomes, "observations": observations}
        if any(missing.values()):
            unavailable[phase] = missing
    return changes, unavailable


def _bundle_changes(name: str, left: _BundleEvidence, right: _BundleEvidence) -> dict[str, Any]:
    paths = sorted({
        path
        for bundle in (left, right)
        for inventory in (bundle.recorded, bundle.observed)
        for field in ("files", "gaps")
        for path in inventory[field]
        if path != "."
    })
    missing = {
        "left": [path for path in paths if not left.available(path)],
        "right": [path for path in paths if not right.available(path)],
    }
    unavailable_paths = set(missing["left"]) | set(missing["right"])
    files = _map_changes(
        {path: value for path, value in left.recorded["files"].items() if path not in unavailable_paths},
        {path: value for path, value in right.recorded["files"].items() if path not in unavailable_paths},
    )
    phases, unavailable = _phase_changes(name, left, right)
    before, after = left.recorded, right.recorded
    return {
        "artifact_name": before["artifact_name"],
        "recorded_directory_changes": _changes(
            {"directory_observation": before["directory_observation"]},
            {"directory_observation": after["directory_observation"]},
        ),
        "recorded_upload_changes": _changes(before["upload"], after["upload"]),
        "file_changes": files,
        "unavailable_files": missing,
        "phase_changes": phases,
        "unavailable_phases": unavailable,
    }


def _read_side(
    index: str | Path, artifacts: str | Path, side: str, *, runner_layout: bool,
) -> _VerifiedSmokeRunIndex:
    try:
        return _read_verified_smoke_run_index(index, artifacts, runner_layout=runner_layout)
    except ValueError as exc:
        raise ValueError(f"{side} smoke run: {exc}") from None


def _environment(
    side: str, run: _VerifiedSmokeRunIndex,
) -> tuple[dict[str, Any], tuple[dict[str, Any], dict[str, Any]] | None]:
    path = "environment.json"
    bundle = _bundle(run, "environment")
    if path not in bundle.recorded["files"]:
        status = "NOT_RECORDED" if bundle.available(path) else "UNAVAILABLE"
        return {"status": status}, None
    if run.environment_bytes is None:
        return {"status": "UNAVAILABLE"}, None
    try:
        report, info = _decode_report(run.environment_bytes, side)
    except ValueError as exc:
        return {"status": "INVALID_REPORT", "diagnostic": str(exc)}, None

    checks = {"matched": [], "unavailable": [], "conflicts": {}}
    for field, recorded in run.record["run"].items():
        observed = report["runner"][field]
        if recorded is None or observed is None:
            checks["unavailable"].append(field)
        elif recorded.lower() == observed.lower():
            checks["matched"].append(field)
        else:
            checks["conflicts"][field] = {"index": recorded, "environment": observed}
    if checks["conflicts"]:
        return {"status": "RUN_IDENTITY_CONFLICT", "identity_checks": checks}, None
    return {"status": "AVAILABLE", "identity_checks": checks}, (report, info)


def compare_smoke_run_indexes(
    left_index: str | Path, left_artifacts: str | Path, right_index: str | Path, right_artifacts: str | Path, *,
    left_runner_layout: bool = False, right_runner_layout: bool = False,
) -> dict[str, Any]:
    """Compare indexed evidence after checking each side against its local files."""
    left = _read_side(left_index, left_artifacts, "left", runner_layout=left_runner_layout)
    right = _read_side(right_index, right_artifacts, "right", runner_layout=right_runner_layout)
    result = {
        "schema_version": 1,
        "left": {"index": left.fingerprint, "verification": left.verification},
        "right": {"index": right.fingerprint, "verification": right.verification},
    }
    verification = {run.verification["status"] for run in (left, right)}
    if "SMOKE_RUN_INDEX_MISMATCH" in verification:
        return {**result, "status": "SMOKE_RUN_COMPARISON_UNAVAILABLE"}

    bundles = {name: _bundle_changes(name, _bundle(left, name), _bundle(right, name))
               for name in left.record["bundles"]}
    left_state, left_environment = _environment("left", left)
    right_state, right_environment = _environment("right", right)
    environment = {"status": "UNAVAILABLE", "left": left_state, "right": right_state}
    if left_environment is not None and right_environment is not None:
        environment.update(status="COMPARED", comparison=_compare_reports(*left_environment, *right_environment))

    steps = _changes(left.record["steps"], right.record["steps"])
    identity = _changes(left.record["run"], right.record["run"])
    before, after = left.record["observed_at_utc"], right.record["observed_at_utc"]
    if before != after:
        identity["observed_at_utc"] = {"left": before, "right": after}
    status = "SMOKE_RUN_INDEXES_COMPARED"
    if (
        verification != {"SMOKE_RUN_INDEX_VERIFIED"} or environment["status"] != "COMPARED"
        or any(row["unavailable_phases"] or any(row["unavailable_files"].values()) for row in bundles.values())
    ):
        status = "SMOKE_RUN_INDEXES_PARTIALLY_COMPARED"
    return {
        **result,
        "status": status,
        "summary": {
            "changed_steps": len(steps),
            "changed_phases": sum(len(row["phase_changes"]) for row in bundles.values()),
            **{f"{kind}_files": sum(len(row["file_changes"][kind]) for row in bundles.values())
               for kind in ("added", "removed", "changed")},
        },
        "recorded_step_changes": steps,
        "run_identity_changes": identity,
        "bundles": bundles,
        "environment": environment,
    }
