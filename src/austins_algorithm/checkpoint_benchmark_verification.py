"""Audit schema-1 local benchmark evidence without rerunning its workers.

A stopped or failed benchmark can pass this record audit.  Every available
scientific report must still validate; VERIFICATION_FAILED does not excuse a
corrupted report.  Timings, memory, code identity and execution remain recorded
observations, not authenticated measurements or a hardware experiment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .checkpoint_benchmark_records import _read_checkpoint_benchmark_records, _read_json
from .constraints import MAX_CONSTRAINT_BYTES, _constraint_from_document
from .core import _bounded_int
from .snapshot_calibration_fusion import _validate_fusion_probability_tolerance
from .snapshot_calibration_fusion import _CAPTURE_CHANGES, _compare_capture_pair, _unchanged
from .snapshot_calibration_fusion_checkpoint_store import (
    _fusion_generation_terminal, _read_calibration_fusion_generations,
)
from .snapshot_calibration_incremental_store import _read_incremental_calibration_generations
from .snapshot_calibration_planning import _VERSIONS, _calibration_profiles
from .snapshot_calibration_study import _preflight_study
from .snapshot_calibration_study_captures import _STATIC_ROUND_FIELDS, _TIMEOUT_FIELDS, _read_capture
from .snapshot_calibration_study_verification import _verify_study_document
from .unknown_count_snapshot_cache import _json_bytes
from .unknown_count_snapshot_profile import _sha256


_SUMMARY_FIELDS = {
    "schema_version", "status", "scenario", "reference_generation_sha256", "fusion_generation_sha256", "stages",
    "reports", "exact_report_bytes_match", "verification", "limits", "storage", "measurement_scope", "timings",
    "density_preparation_attempts", "provider_job_submitted", "quantum_hardware_used",
}
_STATUSES = {
    "BENCHMARK_COMPLETED", "BENCHMARK_INCOMPLETE", "BENCHMARK_TIMED_OUT", "BENCHMARK_WORKER_FAILED",
    "BENCHMARK_REPORTS_DIFFER", "BENCHMARK_VERIFICATION_FAILED",
}
_STUDY_FIELDS = {
    "initial_layouts", "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot", "seed_transpiler",
    "validation_trials", "prefer_native_qasm", "batch_density", "fusion_probability_tolerance",
}
_FLAGS = {"provider_job_submitted": False, "quantum_hardware_used": False}
_REFERENCE_FIELDS = {
    "schema_version", "status", "generation_sha256", "completed_layouts", "remaining_layouts", "failure",
    "study", "study_output", *_FLAGS,
}
_FUSION_FIELDS = _REFERENCE_FIELDS | {"reference_generation_sha256", "attempted_layouts"}


def _expect(actual: Any, expected: Any, label: str) -> None:
    if _json_bytes(actual) != _json_bytes(expected):
        raise ValueError(f"checkpoint benchmark {label} differs")


def _record(value: Any, fields: set[str], label: str, optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not fields <= value.keys() <= fields | optional:
        raise ValueError(f"invalid checkpoint benchmark {label} fields")
    return value


def _local(record: dict, label: str) -> None:
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError(f"unsupported checkpoint benchmark {label} schema")
    for field, expected in _FLAGS.items():
        if record[field] is not expected:
            raise ValueError(f"checkpoint benchmark {label} must describe local work")


def _error(value: Any, label: str, *, layout: str | None = None) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("message"), str):
        raise ValueError(f"invalid checkpoint benchmark {label}")
    if "error_type" in value and (not isinstance(value["error_type"], str) or not value["error_type"]):
        raise ValueError(f"invalid checkpoint benchmark {label} type")
    if layout is not None and value.get("layout") != layout:
        raise ValueError(f"checkpoint benchmark {label} layout differs")


def _scenario(records, constraint, constraint_pin: dict) -> tuple[dict, list[str], int]:
    scenario = _record(records.scenario, {"schema_version", "constraint", "earlier", "later", "study", "limits"}, "scenario")
    if type(scenario["schema_version"]) is not int or scenario["schema_version"] != 1:
        raise ValueError("unsupported checkpoint benchmark scenario schema")
    formula = scenario["constraint"]
    if not isinstance(formula, list) or len(formula) != 2 or not isinstance(formula[0], str) or not formula[0]:
        raise ValueError("invalid checkpoint benchmark constraint label")
    _expect(constraint_pin["sha256"], _sha256(formula[1], "benchmark raw constraint SHA-256"), "raw constraint pin")
    study = _record(scenario["study"], _STUDY_FIELDS, "scenario study")
    for field in ("prefer_native_qasm", "batch_density"):
        if type(study[field]) is not bool:
            raise ValueError(f"checkpoint benchmark {field} must be boolean")
    tolerance = _validate_fusion_probability_tolerance(study["fusion_probability_tolerance"])
    layouts, options, required = _preflight_study(
        constraint, **{field: study[field] for field in (
            "initial_layouts", "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot",
            "seed_transpiler", "validation_trials",
        )}, paired_fusion=True,
    )
    _expect(study, {**study, **options, "initial_layouts": [list(layout) for layout in layouts],
                    "fusion_probability_tolerance": tolerance}, "normalized study")
    limits = _record(scenario["limits"], {"reference_layouts_per_process", "fusion_layouts_per_process", "worker_timeout_seconds"}, "scenario limits")
    for field in ("reference_layouts_per_process", "fusion_layouts_per_process"):
        _bounded_int(limits[field], 1, 8, field)
    _bounded_int(limits["worker_timeout_seconds"], 1, 3600, "worker_timeout_seconds")
    for label in ("earlier", "later"):
        archive = _record(scenario[label], {"configuration", "properties", "configuration_sha256", "properties_sha256"}, "archive labels")
        for field in ("configuration", "properties"):
            if not isinstance(archive[field], str) or not archive[field]:
                raise ValueError("invalid checkpoint benchmark archive path label")
            _sha256(archive[field + "_sha256"], "benchmark archive SHA-256")
    inputs = {"constraint_sha256": constraint.digest(), "search_qubits": constraint.qubits,
              "seed_transpiler": study["seed_transpiler"], "validation_trials": study["validation_trials"], **options}
    for field in ("prefer_native_qasm", "batch_density"):
        if study[field]:
            inputs[field] = True
    return inputs, [",".join(map(str, layout)) for layout in layouts], required


def _bind_snapshots(snapshots: dict, scenario: dict) -> None:
    if not isinstance(snapshots, dict) or snapshots.keys() != {"earlier", "later"}:
        raise ValueError("invalid checkpoint benchmark snapshot evidence")
    for label in snapshots:
        for field in ("configuration_sha256", "properties_sha256"):
            _expect(snapshots[label].get(field), scenario[label][field], f"{label} {field}")


def _checked_capture(sealed, key, inputs, required, scenario, constraint, *, fused=False, reference=None, versions=None):
    outcomes = [format(state, f"0{constraint.qubits}b") for state in range(1 << constraint.qubits)]
    witnesses = [bits for bits in outcomes if constraint.matches(bits)]
    audit = {"states_checked": len(outcomes), "marked_count": len(witnesses), "marked_states": witnesses}
    report, _ = _read_capture(sealed, tuple(map(int, key.split(","))), inputs, required, audit, fused=fused)
    _bind_snapshots(report["snapshots"], scenario)
    if versions is not None:
        _expect(report["versions"], {name: versions.get(name) for name in _VERSIONS}, "capture software versions")
    if report["status"] == "COMPLETED":
        profiles, _ = _calibration_profiles([report], constraint, fusion_diagnostic=fused)
        if reference is not None:
            left, _ = _calibration_profiles([reference], constraint)
            _compare_capture_pair(reference, report, left[0], profiles[0])
    elif reference is not None:
        if not _unchanged(reference, report, _CAPTURE_CHANGES | _TIMEOUT_FIELDS | {"status"}):
            raise ValueError("failed benchmark fusion capture differs from its reference scope")
        for index, row in report["rounds"].items():
            _expect(row, {field: reference["rounds"][index][field] for field in _STATIC_ROUND_FIELDS},
                    "failed fusion capture compilation or calibration")
    return report


def _bind_request(request: dict, inputs: dict, keys: list[str], required: int, scenario: dict, versions: dict | None) -> None:
    _record(request, {"inputs", "required_rounds", "snapshots", "versions", "compiler_scope_sha256", "initial_layouts",
                      "fusion_probability_tolerance"}, "generation request")
    _expect(request["inputs"], inputs, "generation inputs")
    _expect(request["required_rounds"], list(range(required)), "generation rounds")
    _expect(request["initial_layouts"], scenario["study"]["initial_layouts"], "generation layouts")
    _expect(request["fusion_probability_tolerance"], scenario["study"]["fusion_probability_tolerance"], "generation tolerance")
    _sha256(request["compiler_scope_sha256"], "benchmark compiler scope SHA-256")
    _bind_snapshots(request["snapshots"], scenario)
    if versions is not None:
        compiler_versions = {"numpy", "qiskit", "qiskit-aer", "qiskit-ibm-runtime", "qiskit-qasm3-import"}
        if not isinstance(request["versions"], dict) or request["versions"].keys() != compiler_versions:
            raise ValueError("invalid checkpoint benchmark compiler software versions")
        _expect(request["versions"], {name: versions.get(name) for name in compiler_versions}, "generation software versions")


def _chains(records, constraint, inputs: dict, keys: list[str], required: int, versions: dict | None):
    summary, root = records.summary, records.root / "checkpoints"
    reference_tip, fusion_tip = summary["reference_generation_sha256"], summary["fusion_generation_sha256"]
    references = () if reference_tip is None else _read_incremental_calibration_generations(root, reference_tip)
    by_reference = {None: 0, **{row.sha256: index for index, row in enumerate(references, 1)}}
    reference_captures = {}
    if references:
        _bind_request(references[-1].document["request"], inputs, keys, required, records.scenario, versions)
        for key, generation in zip(keys, references):
            if versions is not None:
                _expect(generation.bundle.software_versions, versions, "compilation bundle software versions")
            capture = generation.document["capture"]
            _checked_capture(capture, key, inputs, required, records.scenario, constraint,
                             versions=generation.document["request"]["versions"])
            reference_captures[key] = capture
    fusions = () if fusion_tip is None else _read_calibration_fusion_generations(root, fusion_tip)
    if fusions and len(references) != len(keys):
        raise ValueError("checkpoint benchmark fusion lacks its complete reference chain")
    for generation in fusions:
        _expect(generation.document["reference_generation_sha256"], reference_tip, "fusion reference handoff")
    by_fusion = {None: 0, **{row.sha256: index for index, row in enumerate(fusions, 1)}}
    return references, by_reference, reference_captures, fusions, by_fusion


def _failure_capture(result, role, current, keys, reference_captures, inputs, required, scenario, constraint, versions):
    failure = result.get("failure")
    if failure is None:
        return
    key = keys[current]
    if role.startswith("reference"):
        if not isinstance(failure, dict) or failure.get("layout") != key:
            raise ValueError("invalid benchmark failed reference evidence")
        if "error_type" in failure:
            _record(failure, {"layout", "error_type", "message"}, "reference failure", {"capture"})
            _error(failure, "reference failure", layout=key)
        else:
            _record(failure, {"layout", "capture"}, "reference unavailable capture")
        if "capture" in failure:
            report = _checked_capture(failure["capture"], key, inputs, required, scenario, constraint, versions=versions)
            if "error_type" not in failure and report["status"] == "COMPLETED":
                raise ValueError("incomplete benchmark reference lacks a failure or unavailable capture")
    else:
        _record(failure, {"layout", "error_type", "message", "attempt"}, "fusion publication failure")
        attempt = failure["attempt"]
        if not isinstance(attempt, dict) or attempt.keys() not in ({"capture"}, {"failure"}):
            raise ValueError("invalid benchmark unpublished fusion attempt")
        if "capture" in attempt:
            _checked_capture(attempt["capture"], key, inputs, required, scenario, constraint, fused=True,
                             reference=reference_captures[key]["report"], versions=versions)
        else:
            _record(attempt["failure"], {"error_type", "message"}, "unpublished fusion error")
            _error(attempt["failure"], "unpublished fusion error")


def _metrics(stage, records, required: int, count: int) -> None:
    metrics = stage.metrics
    if metrics is None:
        return
    role, scenario = stage.record["role"], records.scenario
    expected = {"scenario_sha256": records.scenario_fingerprint["sha256"],
                **{field: scenario[field] for field in ("constraint", "earlier", "later")}}
    _expect({field: metrics["inputs"][field] for field in expected}, expected, "worker scenario and archive labels")
    counters = metrics["counters"]
    if role == "baseline":
        cap = 4 * count * required
        forbidden = ("restores",)
    else:
        chunk = scenario["limits"]["fusion_layouts_per_process" if role == "fusion" else "reference_layouts_per_process"]
        cap = 2 * min(chunk, count) * required
        forbidden = ("compiles", "reference_preparations") if role == "fusion" else ("fusion_preparations",)
    _expect(metrics["density_preparation_limit"], cap, "worker preparation cap")
    if sum(counters[field] for field in ("reference_preparations", "fusion_preparations")) > cap:
        raise ValueError("checkpoint benchmark worker preparation cap exceeded")
    if any(counters[field] != 0 for field in forbidden):
        raise ValueError("checkpoint benchmark counter is incompatible with its worker role")
    if counters["compiles"] > count * required or counters["restores"] > count * required:
        raise ValueError("checkpoint benchmark compilation or restore count exceeds its round bound")


def _result_header(result: dict, fields: set[str], label: str, optional: set[str] = frozenset()) -> None:
    _record(result, fields, label, optional)
    _local(result, label)
    if result["study"] is not None:
        raise ValueError("checkpoint benchmark worker result must reference its separate study output")


def _progress(pin: str | None, positions: dict, previous: int, limit: int, label: str) -> int:
    if pin not in positions:
        raise ValueError(f"checkpoint benchmark {label} pin is not in its declared final lineage")
    current = positions[pin]
    if not previous <= current <= previous + limit:
        raise ValueError(f"checkpoint benchmark {label} chunk progress exceeds its bound")
    return current


def _study_output(stage, inputs: dict, keys: list[str], required: int, scenario: dict, constraint) -> dict | None:
    result = stage.result
    if stage.study is None:
        if result is not None and result.get("study_output") is not None:
            raise ValueError("checkpoint benchmark worker study output is missing")
        return None
    if result is not None:
        output = _record(result.get("study_output"), {"path", "bytes", "sha256"}, "worker study output")
        _expect(output, {"path": "study.json", **stage.fingerprints["study.json"]}, "worker study fingerprint")
        _expect(result.get("fusion_status"), stage.study["fusion"]["status"], "worker fusion status")
    study = stage.study
    _expect(study.get("inputs"), inputs, "study inputs")
    _expect(study.get("required_rounds"), list(range(required)), "study round horizon")
    _expect(study.get("coverage", {}).get("requested_layouts"), keys, "study requested layouts")
    _expect(study.get("fusion", {}).get("probability_tolerance"), scenario["study"]["fusion_probability_tolerance"], "study tolerance")
    for capture in study.get("captures", {}).values():
        _bind_snapshots(capture["report"]["snapshots"], scenario)
        if stage.metrics is not None:
            _expect(capture["report"]["versions"], {name: stage.metrics["versions"].get(name)
                    for name in capture["report"]["versions"]}, "study capture software versions")
    fingerprint = stage.fingerprints["study.json"]
    return _verify_study_document(study, constraint, fingerprint["sha256"], fingerprint["bytes"])


def _reference_result(result: dict, previous: int, positions: dict, keys: list[str], chunk: int) -> tuple[int, bool]:
    _result_header(result, _REFERENCE_FIELDS, "reference result")
    current = _progress(result["generation_sha256"], positions, previous, chunk, "reference")
    _expect(result["completed_layouts"], keys[:current], "reference completed prefix")
    _expect(result["remaining_layouts"], keys[current:], "reference remaining suffix")
    if result["study_output"] is not None:
        raise ValueError("reference-only benchmark worker cannot emit a paired study")
    status = result["status"]
    if status in {"CALIBRATION_STUDY_INCREMENTAL_PAUSED", "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED"}:
        _expect(current, min(len(keys), previous + chunk), "reference chunk advance")
        _expect(status, "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED" if current == len(keys)
                else "CALIBRATION_STUDY_INCREMENTAL_PAUSED", "reference pause state")
        _expect(result["failure"], None, "successful reference failure")
        return current, False
    if status not in {"CALIBRATION_STUDY_INCREMENTAL_REFERENCE_INCOMPLETE", "CALIBRATION_STUDY_INCREMENTAL_PUBLICATION_FAILED"}:
        raise ValueError("unsupported checkpoint benchmark reference result status")
    if current == len(keys) or current == previous + chunk or not isinstance(result["failure"], dict):
        raise ValueError("invalid checkpoint benchmark incomplete reference boundary")
    _expect(result["failure"].get("layout"), keys[current], "incomplete reference layout")
    return current, True


def _fusion_result(result: dict, previous: int, positions: dict, generations, keys: list[str], chunk: int, reference_tip: str):
    _result_header(result, _FUSION_FIELDS, "fusion result", {"fusion_status"})
    _expect(result["reference_generation_sha256"], reference_tip, "fusion worker reference pin")
    current = _progress(result["generation_sha256"], positions, previous, chunk, "fusion")
    _expect(result["attempted_layouts"], keys[:current], "fusion attempted prefix")
    complete = [keys[index] for index, row in enumerate(generations[:current])
                if "capture" in row.document and row.document["capture"]["report"]["status"] == "COMPLETED"]
    _expect(result["completed_layouts"], complete, "fusion completed layouts")
    _expect(result["remaining_layouts"], keys[current:], "fusion remaining suffix")
    terminal = bool(current) and _fusion_generation_terminal(generations[current - 1].document)
    status = result["status"]
    if status == "CALIBRATION_STUDY_FUSION_CHECKPOINT_PUBLICATION_FAILED":
        if terminal or current == len(keys) or current == previous + chunk:
            raise ValueError("invalid checkpoint benchmark failed fusion publication boundary")
        _error(result["failure"], "fusion publication failure", layout=keys[current])
        if result["study_output"] is not None:
            raise ValueError("failed fusion publication cannot emit a completed study")
        return current, True, False
    expected = "CALIBRATION_STUDY_FUSION_CHECKPOINT_TERMINAL" if terminal else (
        "CALIBRATION_STUDY_FUSION_CHECKPOINT_COMPLETED" if current == len(keys) else "CALIBRATION_STUDY_FUSION_CHECKPOINT_PAUSED")
    _expect(status, expected, "fusion continuation state")
    if not terminal:
        _expect(current, min(len(keys), previous + chunk), "fusion chunk advance")
    _expect(result["failure"], None, "fusion publication failure")
    has_study = terminal or current == len(keys)
    if (result["study_output"] is not None) is not has_study:
        raise ValueError("checkpoint benchmark fusion study availability differs from its state")
    return current, terminal or (has_study and result.get("fusion_status") != "CALIBRATION_FUSION_STUDY_CHECKED"), has_study and not terminal


def verify_checkpoint_benchmark(constraint_path: str | Path, directory: str | Path, *, expected_summary_sha256: str) -> dict:
    """Verify available saved evidence; a passing audit does not complete a stopped run."""
    records = _read_checkpoint_benchmark_records(directory, expected_summary_sha256=expected_summary_sha256)
    summary = _record(records.summary, _SUMMARY_FIELDS, "summary", {"error"})
    _local(summary, "summary")
    if summary["status"] not in _STATUSES:
        raise ValueError("unsupported checkpoint benchmark recorded status")
    original = _record(summary["scenario"], {"path", "bytes", "sha256"}, "original scenario label")
    if not isinstance(original["path"], str) or not original["path"] or type(original["bytes"]) is not int or not 0 <= original["bytes"] <= 1024 * 1024:
        raise ValueError("invalid checkpoint benchmark original scenario label")
    _sha256(original["sha256"], "original benchmark scenario SHA-256")
    _expect(summary["measurement_scope"], "One uninterrupted worker versus multiple fresh continuation workers; resource observations only",
            "measurement scope label")
    constraint_document, constraint_pin = _read_json(constraint_path, max_bytes=MAX_CONSTRAINT_BYTES)
    constraint = _constraint_from_document(constraint_document)
    inputs, keys, required = _scenario(records, constraint, constraint_pin)
    _expect(summary["limits"], {"required_rounds": required, "maximum_density_preparations": 8 * len(keys) * required}, "aggregate limits")
    for field in ("reference_generation_sha256", "fusion_generation_sha256"):
        if summary[field] is not None:
            _sha256(summary[field], "benchmark generation SHA-256")
    first_metrics = next((stage.metrics for stage in records.stages
                          if stage.metrics is not None and stage.record["role"].startswith("reference")), None)
    versions = None if first_metrics is None else first_metrics["versions"]
    references, reference_positions, reference_captures, fusions, fusion_positions = _chains(records, constraint, inputs, keys, required, versions)
    previous_reference = previous_fusion = 0
    reference_pin = fusion_pin = None
    phase, stop, done, identity = "baseline", None, False, None
    report_verifications, reports = {}, {}
    for index, stage in enumerate(records.stages):
        record, result, role = stage.record, stage.result, stage.record["role"]
        if stop is not None or done:
            raise ValueError("checkpoint benchmark records later work after its stopping boundary")
        expected_role = "baseline" if index == 0 else ("reference-start" if phase == "reference" and previous_reference == 0 else
                        "reference-resume" if phase == "reference" else "fusion")
        _expect(role, expected_role, "worker phase order")
        prior_reference = previous_reference
        prior_fusion = previous_fusion
        _metrics(stage, records, required, len(keys))
        verified = _study_output(stage, inputs, keys, required, records.scenario, constraint)
        if verified is not None:
            name = "baseline" if role == "baseline" else "continued"
            if name in reports:
                raise ValueError("checkpoint benchmark emits more than one report per comparison arm")
            reports[name], report_verifications[name] = stage, verified
        if role == "baseline" and result is not None:
            _record(result, {"status", "study", "study_output"}, "baseline result", {"fusion_status"})
            if stage.study is None or result["study"] is not None:
                raise ValueError("checkpoint benchmark baseline result lacks its study")
            _expect(result["status"], stage.study["status"], "baseline result status")
            phase = "reference"
            if result["status"] != "CALIBRATION_STUDY_COMPLETED" or result.get("fusion_status") != "CALIBRATION_FUSION_STUDY_CHECKED":
                stop = "INCOMPLETE"
        elif role.startswith("reference") and result is not None:
            previous_reference, incomplete = _reference_result(
                result, previous_reference, reference_positions, keys, records.scenario["limits"]["reference_layouts_per_process"],
            )
            reference_pin = result["generation_sha256"]
            if incomplete:
                stop = "INCOMPLETE"
                _failure_capture(result, role, previous_reference, keys, reference_captures, inputs, required,
                                 records.scenario, constraint, None if stage.metrics is None else stage.metrics["versions"])
            if previous_reference == len(keys):
                phase = "fusion"
        elif role == "fusion" and result is not None:
            previous_fusion, incomplete, done = _fusion_result(
                result, previous_fusion, fusion_positions, fusions, keys,
                records.scenario["limits"]["fusion_layouts_per_process"], reference_pin,
            )
            fusion_pin = result["generation_sha256"]
            if incomplete:
                stop, done = "INCOMPLETE", False
                if result["failure"] is not None:
                    _failure_capture(result, role, previous_fusion, keys, reference_captures, inputs, required,
                                     records.scenario, constraint, None if stage.metrics is None else stage.metrics["versions"])
            if stage.study is not None:
                _expect(stage.study["captures"], reference_captures, "continued reference captures")
                saved_captures = {keys[i]: row.document["capture"] for i, row in enumerate(fusions[:previous_fusion]) if "capture" in row.document}
                saved_failures = {keys[i]: row.document["failure"] for i, row in enumerate(fusions[:previous_fusion]) if "failure" in row.document}
                _expect(stage.study["fusion"]["captures"], saved_captures, "continued fusion captures")
                _expect(stage.study["fusion"]["failures"], saved_failures, "continued fusion failures")
        if stage.metrics is not None and record["status"] == "COMPLETED":
            counts = stage.metrics["counters"]
            expected_counts = {}
            if role == "baseline" and result is not None and result["status"] == "CALIBRATION_STUDY_COMPLETED":
                expected_counts = {"compiles": len(keys) * required}
            elif role.startswith("reference") and result is not None and result["status"] in {
                "CALIBRATION_STUDY_INCREMENTAL_PAUSED", "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED",
            }:
                expected_counts = {"compiles": (previous_reference - prior_reference) * required,
                                   "restores": previous_reference * required}
            elif role == "fusion" and result is not None and result["status"] != "CALIBRATION_STUDY_FUSION_CHECKPOINT_PUBLICATION_FAILED":
                expected_counts = {"restores": len(keys) * required}
            for name, value in expected_counts.items():
                _expect(counts[name], value, f"successful {role} {name} count")
            # A batch can split at a different active-wire group.  The minimum
            # assumes maximal groups of three; it is not an exact job claim.
            minimum_per_layout = 2 * ((required + 2) // 3 if records.scenario["study"]["batch_density"] else required)
            minimum_preparations = {}
            if role == "baseline" and result is not None and result["status"] == "CALIBRATION_STUDY_COMPLETED":
                minimum_preparations["reference_preparations"] = len(keys) * minimum_per_layout
                completed_fusion = len(stage.study["fusion"]["coverage"]["completed_layouts"])
                minimum_preparations["fusion_preparations"] = completed_fusion * minimum_per_layout
            elif role.startswith("reference") and expected_counts:
                minimum_preparations["reference_preparations"] = (previous_reference - prior_reference) * minimum_per_layout
            elif role == "fusion" and result is not None:
                completed_fusion = sum("capture" in row.document and row.document["capture"]["report"]["status"] == "COMPLETED"
                                       for row in fusions[prior_fusion:previous_fusion])
                minimum_preparations["fusion_preparations"] = completed_fusion * minimum_per_layout
            if any(counts[name] < minimum for name, minimum in minimum_preparations.items()):
                raise ValueError("checkpoint benchmark successful density preparation count is below its bound")
            current = {"versions": stage.metrics["versions"], "source_files": stage.metrics["inputs"]["source_files"]}
            if identity is not None and _json_bytes(identity) != _json_bytes(current):
                stop, done = "WORKER_FAILED", False
            identity = current
            if stage.metrics["error"] is not None:
                raise ValueError("successful checkpoint benchmark worker records an unhandled error")
        if record["status"] == "TIMED_OUT":
            if record["returncode"] is not None or record["cancellation_status"] != "UNKNOWN":
                raise ValueError("invalid checkpoint benchmark timeout cancellation evidence")
            stop, done = "TIMED_OUT", False
        elif record["status"] == "FAILED":
            if record["returncode"] == 0 or record["cancellation_status"] is not None:
                raise ValueError("invalid checkpoint benchmark failed worker evidence")
            stop, done = "WORKER_FAILED", False
        elif record["returncode"] != 0 or record["cancellation_status"] is not None:
            raise ValueError("invalid checkpoint benchmark completed worker evidence")
        elif result is None or stage.metrics is None:
            stop, done = "WORKER_FAILED", False
    _expect(summary["reference_generation_sha256"], reference_pin, "last known reference tip")
    _expect(summary["fusion_generation_sha256"], fusion_pin, "last known fusion tip")
    prepared = sum(stage.metrics["counters"][field] for stage in records.stages if stage.metrics is not None
                   for field in ("reference_preparations", "fusion_preparations"))
    _expect(summary["density_preparation_attempts"], prepared, "preparation sum")
    if prepared > summary["limits"]["maximum_density_preparations"]:
        raise ValueError("checkpoint benchmark aggregate preparation cap exceeded")
    _expect(summary["timings"], {"scope": "Parent wall time per process, including startup, input checks and output",
            **{name: sum(stage.record["wall_seconds"] for stage in records.stages if stage.record["role"] in roles)
               for name, roles in (("baseline_seconds", {"baseline"}), ("reference_seconds", {"reference-start", "reference-resume"}),
                                   ("fusion_seconds", {"fusion"}))}}, "recorded timing sums")
    all_files = records.storage["fingerprints"]
    checkpoint_files = [row for name, row in all_files.items() if name.startswith("checkpoints/")]
    _expect(summary["storage"], {"checkpoint_store": {"files": len(checkpoint_files), "bytes": sum(row["bytes"] for row in checkpoint_files)},
            "all_evidence": {field: records.storage[field] for field in ("files", "bytes", "excludes")}}, "storage inventory")
    claimed = summary["reports"]
    if not isinstance(claimed, dict) or not claimed.keys() <= {"baseline", "continued"}:
        raise ValueError("invalid checkpoint benchmark comparison report claims")
    for name, fingerprint in claimed.items():
        _record(fingerprint, {"path", "bytes", "sha256"}, "comparison report label")
        if not isinstance(fingerprint["path"], str) or not fingerprint["path"]:
            raise ValueError("invalid checkpoint benchmark report path label")
        if name not in reports:
            raise ValueError("checkpoint benchmark claims a missing comparison report")
        _expect({field: fingerprint[field] for field in ("bytes", "sha256")}, reports[name].fingerprints["study.json"], "comparison report fingerprint")
    match = None if reports.keys() != {"baseline", "continued"} else (
        reports["baseline"].fingerprints["study.json"] == reports["continued"].fingerprints["study.json"])
    status = summary["status"]
    if status == "BENCHMARK_VERIFICATION_FAILED":
        _error(summary.get("error"), "recorded verifier failure")
    elif stop is not None:
        _expect(status, "BENCHMARK_" + stop, "stopped benchmark status")
    elif not done:
        raise ValueError("checkpoint benchmark ends without a recorded stopping boundary")
    if status in {"BENCHMARK_COMPLETED", "BENCHMARK_REPORTS_DIFFER"}:
        if not done or claimed.keys() != {"baseline", "continued"}:
            raise ValueError("checkpoint benchmark comparison lacks completed arms")
        _expect(summary["exact_report_bytes_match"], match, "raw report comparison")
        _expect(status, "BENCHMARK_COMPLETED" if match else "BENCHMARK_REPORTS_DIFFER", "comparison outcome")
    elif status != "BENCHMARK_VERIFICATION_FAILED":
        _expect(claimed, {}, "stopped comparison reports")
        _expect(summary["exact_report_bytes_match"], None, "unreached comparison")
        _expect(summary["verification"], None, "unreached report verification")
    elif summary["exact_report_bytes_match"] is not None:
        _expect(summary["exact_report_bytes_match"], match, "recorded comparison before verifier failure")
    if summary["verification"] is not None:
        _expect(summary["verification"], report_verifications, "recorded paired report verification")
    elif status == "BENCHMARK_COMPLETED":
        raise ValueError("completed checkpoint benchmark omitted its paired report verification")
    return {
        "schema_version": 1, "status": "CHECKPOINT_BENCHMARK_RECORD_VERIFIED", "recorded_status": status,
        "checks_scope": "AVAILABLE_RECORDED_EVIDENCE", "recorded_complete": status == "BENCHMARK_COMPLETED",
        "summary_sha256": records.summary_fingerprint["sha256"], "summary_bytes": records.summary_fingerprint["bytes"],
        "scenario_sha256": records.scenario_fingerprint["sha256"], "constraint_file_sha256": constraint_pin["sha256"],
        "local_evidence_fingerprints": records.storage["fingerprints"],
        "constraint_sha256": constraint.digest(), "stage_count": len(records.stages), "reports": report_verifications,
        "reference_generation_sha256": reference_pin, "fusion_generation_sha256": fusion_pin,
        "checks": {"stage_content_bindings": "PARSED_CONTENT_MATCHES_SUMMARY", "stage_order_and_handoffs_verified": True,
                   "reachable_reference_layouts": len(references), "reachable_fusion_attempts": len(fusions),
                   "storage_inventory_verified": True, "recorded_sums_and_limits_verified": True,
                   "available_report_count": len(report_verifications), "raw_report_fingerprints_match": match},
        "evidence": {"timing": "RECORDED_ONLY", "memory": "RECORDED_ONLY", "worker_execution": "RECORDED_ONLY",
                     "source_identity": "RECORDED_ONLY", "archive_inputs": "RECORDED_ONLY", "original_scenario": "RECORDED_ONLY",
                     "stored_qpy_decoded": False, "sdk_runtime_replayed": False,
                     "limit": "Available scientific reports must validate even when recorded_status is BENCHMARK_VERIFICATION_FAILED"},
        **_FLAGS,
    }
