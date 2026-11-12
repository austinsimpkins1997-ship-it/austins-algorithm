"""Compare a local paired study with explicitly pinned continuation in fresh processes.

Install the package and its IBM extra in the interpreter used to run this file.
Supply a pinned local scenario with --scenario and a new evidence directory with --directory.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes
from dataclasses import asdict
from hashlib import sha256
import importlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
from typing import Any
from unittest.mock import patch

from austins_algorithm.constraints import _unique_object, load_constraint
from austins_algorithm.core import _bounded_int
from austins_algorithm.replay_receipt import _regular_file
from austins_algorithm.snapshot_calibration_comparison import MAX_CALIBRATION_FILE_BYTES, SnapshotCalibrationFiles
from austins_algorithm.snapshot_calibration_fusion import _validate_fusion_probability_tolerance
from austins_algorithm.snapshot_calibration_study import _preflight_study
from austins_algorithm.snapshot_calibration_study_verification import verify_snapshot_calibration_study
from austins_algorithm.unknown_count_snapshot_profile import _sha256


_MAX_JSON_BYTES = 64 * 1024 * 1024
_STUDY_FIELDS = {
    "initial_layouts", "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot", "seed_transpiler",
    "validation_trials", "prefer_native_qasm", "batch_density", "fusion_probability_tolerance",
}
_ARCHIVE_FIELDS = {"configuration", "properties", "configuration_sha256", "properties_sha256"}
_LIMIT_FIELDS = {"reference_layouts_per_process", "fusion_layouts_per_process", "worker_timeout_seconds"}
_ROLES = ("baseline", "reference-start", "reference-resume", "fusion")
_COUNTERS = ("compiles", "restores", "reference_preparations", "fusion_preparations")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("scenario and evidence numbers must be finite")
    return value


def _read(path: Path, *, expected_sha256: str | None = None, limit: int = _MAX_JSON_BYTES) -> tuple[dict, dict]:
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"JSON file exceeds {limit} bytes: {path}")
    fingerprint = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
    if expected_sha256 is not None and fingerprint["sha256"] != _sha256(expected_sha256, "expected file SHA-256"):
        raise ValueError(f"file does not match its expected SHA-256: {path}")
    try:
        document = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"invalid finite JSON: {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return document, fingerprint


def _fields(value: Any, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError(f"invalid {label} fields")
    return value


def _pinned_path(base: Path, value: Any, pin: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("pinned source path must be a nonempty string")
    path = base / value
    _read(path, expected_sha256=pin, limit=MAX_CALIBRATION_FILE_BYTES)
    return str(path.resolve())


def _study_preflight(constraint, study):
    return _preflight_study(
        constraint, **{key: study[key] for key in (
            "initial_layouts", "target_probability", "shot_caps", "query_caps", "max_rounds_per_shot",
            "seed_transpiler", "validation_trials",
        )}, paired_fusion=True,
    )


def _load_scenario(path: str | Path, *, expected_sha256: str | None = None) -> dict:
    source = Path(path)
    document, _ = _read(source, expected_sha256=expected_sha256, limit=1024 * 1024)
    _fields(document, {"schema_version", "constraint", "earlier", "later", "study", "limits"}, "scenario")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported scenario schema_version")
    constraint = document["constraint"]
    if not isinstance(constraint, list) or len(constraint) != 2:
        raise ValueError("scenario constraint requires a path and raw SHA-256")
    formula = _pinned_path(source.parent, constraint[0], constraint[1])
    archives = {}
    for label in ("earlier", "later"):
        archive = _fields(document[label], _ARCHIVE_FIELDS, f"{label} archive")
        archives[label] = {
            **archive,
            **{field: _pinned_path(source.parent, archive[field], archive[f"{field}_sha256"])
               for field in ("configuration", "properties")},
        }
    study = _fields(document["study"], _STUDY_FIELDS, "study")
    for flag in ("prefer_native_qasm", "batch_density"):
        if type(study[flag]) is not bool:
            raise ValueError(f"{flag} must be a boolean")
    tolerance = _validate_fusion_probability_tolerance(study["fusion_probability_tolerance"])
    layouts, options, _ = _study_preflight(load_constraint(formula), study)
    limits = _fields(document["limits"], _LIMIT_FIELDS, "limits")
    for name in ("reference_layouts_per_process", "fusion_layouts_per_process"):
        _bounded_int(limits[name], 1, 8, name)
    _bounded_int(limits["worker_timeout_seconds"], 1, 3600, "worker_timeout_seconds")
    return {
        "schema_version": 1, "constraint": [formula, constraint[1]], **archives,
        "study": {**study, **options, "initial_layouts": [list(layout) for layout in layouts],
                  "fusion_probability_tolerance": tolerance}, "limits": dict(limits),
    }


def _save(path: Path, document: dict) -> dict:
    raw = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(raw)
    return {"path": path.name, "bytes": len(raw), "sha256": sha256(raw).hexdigest()}


def _offline(event: str, _arguments: Any) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
        raise RuntimeError(f"local checkpoint benchmark attempted {event}")


def _process_memory() -> dict:
    fields = ("working_set_bytes", "peak_working_set_bytes", "private_usage_bytes")
    if os.name != "nt":
        return {**dict.fromkeys(fields), "reason": "Windows GetProcessMemoryInfo is unavailable on this platform"}
    try:
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in (
                            "peak_working_set", "working_set", "quota_peak_paged_pool", "quota_paged_pool",
                            "quota_peak_nonpaged_pool", "quota_nonpaged_pool", "pagefile_usage",
                            "peak_pagefile_usage", "private_usage",
                        )]]

        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return dict(zip(fields, (counters.working_set, counters.peak_working_set, counters.private_usage), strict=True), reason=None)
    except (OSError, AttributeError) as error:
        return {**dict.fromkeys(fields), "reason": f"{type(error).__name__}: {error}"}


def _worker(args: argparse.Namespace) -> None:
    sys.addaudithook(_offline)
    scenario_path = Path(args.scenario)
    scenario = _load_scenario(scenario_path, expected_sha256=args.expected_scenario_sha256)
    modules = {name: importlib.import_module(f"austins_algorithm.{name}") for name in (
        "snapshot_calibration_comparison", "snapshot_calibration_study", "snapshot_calibration_study_checkpoint",
        "snapshot_calibration_study_incremental", "snapshot_calibration_study_fusion_checkpoint",
    )}
    from austins_algorithm.snapshot_calibration_compilation_bundle import _bundle_software_versions

    stage = Path(args.directory)
    constraint = load_constraint(scenario["constraint"][0])
    archives = [SnapshotCalibrationFiles(**scenario[label]) for label in ("earlier", "later")]
    study = scenario["study"]
    normalized = _study_preflight(constraint, study)
    if args.worker == "baseline":
        preparation_limit = 4 * len(normalized[0]) * normalized[2]
    else:
        chunk = scenario["limits"]["fusion_layouts_per_process" if args.worker == "fusion" else "reference_layouts_per_process"]
        preparation_limit = 2 * min(chunk, len(normalized[0])) * normalized[2]
    counters = Counter(dict.fromkeys(_COUNTERS, 0))
    before = _process_memory()
    started = perf_counter()
    error_record = None

    def counted(function, name):
        def call(*values, **options):
            counter = name(options) if callable(name) else name
            if counter.endswith("preparations") and sum(counters[field] for field in _COUNTERS[2:]) >= preparation_limit:
                raise RuntimeError("checkpoint benchmark density preparation bound exceeded")
            counters[counter] += 1
            return function(*values, **options)
        return call

    try:
        with ExitStack() as stack:
            comparison, checkpoint = modules["snapshot_calibration_comparison"], modules["snapshot_calibration_study_checkpoint"]
            stack.enter_context(patch.object(comparison, "compile_and_verify", counted(comparison.compile_and_verify, "compiles")))
            stack.enter_context(patch.object(checkpoint, "_restore_compiled_round", counted(checkpoint._restore_compiled_round, "restores")))
            for name in ("_prepare_density_snapshot", "_prepare_density_snapshot_batch"):
                stack.enter_context(patch.object(comparison, name, counted(getattr(comparison, name), lambda options:
                    "fusion_preparations" if options.get("density_fusion") else "reference_preparations")))
            common = (constraint, *archives)
            root = stage.parent / "checkpoints"
            if args.worker == "baseline":
                report = modules["snapshot_calibration_study"].run_snapshot_calibration_study(*common, **study)
                result = {"status": report["status"], "study": report}
            elif args.worker == "reference-start":
                result = modules["snapshot_calibration_study_incremental"].run_snapshot_calibration_study_incremental(
                    *common, directory=root, **study, reference_only=True,
                    max_new_reference_layouts=scenario["limits"]["reference_layouts_per_process"],
                )
            elif args.worker == "reference-resume":
                result = modules["snapshot_calibration_study_incremental"].resume_snapshot_calibration_study_incremental(
                    *common, directory=root, expected_generation_sha256=args.reference_generation_sha256,
                    reference_only=True, max_new_reference_layouts=scenario["limits"]["reference_layouts_per_process"],
                )
            else:
                result = modules["snapshot_calibration_study_fusion_checkpoint"].continue_snapshot_calibration_study_fusion_checkpoint(
                    *common, directory=root, expected_reference_generation_sha256=args.reference_generation_sha256,
                    expected_fusion_generation_sha256=args.fusion_generation_sha256,
                    max_new_fusion_layouts=scenario["limits"]["fusion_layouts_per_process"],
                )
        report = result.pop("study", None)
        result["study"] = None
        result["study_output"] = None
        if report is not None:
            result["fusion_status"] = report["fusion"]["status"]
            result["study_output"] = _save(stage / "study.json", report)
        _save(stage / "result.json", result)
    except BaseException as error:
        error_record = {"error_type": type(error).__name__, "message": str(error)}
        raise
    finally:
        sources = {}
        for name, module in modules.items():
            raw = Path(module.__file__).read_bytes()
            sources[name] = {"path": str(module.__file__), "bytes": len(raw), "sha256": sha256(raw).hexdigest()}
        raw = Path(__file__).read_bytes()
        sources["benchmark_example"] = {"path": str(Path(__file__).resolve()), "bytes": len(raw), "sha256": sha256(raw).hexdigest()}
        _save(stage / "metrics.json", {
            "schema_version": 1, "role": args.worker, "seconds": perf_counter() - started,
            "seconds_scope": "API work and result output after input verification and package imports",
            "counters": dict(counters), "density_preparation_limit": preparation_limit,
            "memory": {"before": before, "after": _process_memory()},
            "memory_scope": "Windows peak working set since process start, including imports; private usage is an endpoint",
            "python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor() or None,
            "thread_settings": {name: os.environ.get(name) for name in ("QISKIT_PARALLEL", "OMP_NUM_THREADS", "RAYON_NUM_THREADS")},
            "versions": _bundle_software_versions(), "inputs": {"scenario_sha256": args.expected_scenario_sha256,
                "constraint": scenario["constraint"], "earlier": asdict(archives[0]), "later": asdict(archives[1]), "source_files": sources},
            "error": error_record, "provider_job_submitted": False, "quantum_hardware_used": False,
        })


def _run_worker(role, scenario_path, scenario_sha256, stage_directory, *, reference_pin=None, fusion_pin=None, timeout_seconds):
    stage = Path(stage_directory)
    stage.mkdir()
    command = [sys.executable, "-I", "-X", "utf8", "-B", str(Path(__file__).resolve()), "--worker", role,
               "--scenario", str(scenario_path), "--expected-scenario-sha256", scenario_sha256, "--directory", str(stage)]
    for option, value in (("--reference-generation-sha256", reference_pin), ("--fusion-generation-sha256", fusion_pin)):
        if value is not None:
            command.extend((option, value))
    started = perf_counter()
    record = {"role": role, "directory": stage.name, "status": "FAILED", "returncode": None,
              "result": None, "metrics": None, "cancellation_status": None}
    with (stage / "stdout.log").open("xb") as stdout, (stage / "stderr.log").open("xb") as stderr:
        try:
            process = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout_seconds,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            record.update(returncode=process.returncode, status="COMPLETED" if process.returncode == 0 else "FAILED")
        except subprocess.TimeoutExpired:
            record.update(status="TIMED_OUT", cancellation_status="UNKNOWN")
        except OSError as error:
            record["error"] = {"error_type": type(error).__name__, "message": str(error)}
    record["wall_seconds"] = perf_counter() - started
    for name in ("result", "metrics"):
        path = stage / f"{name}.json"
        if path.is_file():
            record[name] = _read(path)[0]
    return record


def _storage(root: Path) -> dict:
    files = [path for path in root.rglob("*") if path.is_file()]
    return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}


def run_benchmark(scenario: str | Path, directory: str | Path) -> dict:
    source, output = Path(scenario).resolve(), Path(directory).absolute()
    _, original_pin = _read(source, limit=1024 * 1024)
    normalized = _load_scenario(source, expected_sha256=original_pin["sha256"])
    constraint = load_constraint(normalized["constraint"][0])
    layouts, _, required = _study_preflight(constraint, normalized["study"])
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"benchmark directory already exists: {output}")
    if not output.parent.is_dir():
        raise FileNotFoundError(f"benchmark parent directory does not exist: {output.parent}")
    output.mkdir()
    normalized_path = output / "scenario.json"
    pin = _save(normalized_path, normalized)["sha256"]
    result = {"schema_version": 1, "status": "BENCHMARK_INCOMPLETE", "scenario": {"path": str(source), **original_pin},
              "reference_generation_sha256": None, "fusion_generation_sha256": None, "stages": [], "reports": {},
              "exact_report_bytes_match": None, "verification": None,
              "limits": {"required_rounds": required, "maximum_density_preparations": 8 * len(layouts) * required},
              "provider_job_submitted": False, "quantum_hardware_used": False}

    def run(role):
        stage = output / f"{len(result['stages']) + 1:02d}-{role}"
        record = _run_worker(role, normalized_path, pin, stage, reference_pin=result["reference_generation_sha256"],
                             fusion_pin=result["fusion_generation_sha256"], timeout_seconds=normalized["limits"]["worker_timeout_seconds"])
        result["stages"].append(record)
        if record["result"] is not None:
            observed = record["result"].get("generation_sha256")
            if observed is not None:
                _sha256(observed, "returned generation SHA-256")
                result["fusion_generation_sha256" if role == "fusion" else "reference_generation_sha256"] = observed
        if record["status"] != "COMPLETED":
            result["status"] = "BENCHMARK_TIMED_OUT" if record["status"] == "TIMED_OUT" else "BENCHMARK_WORKER_FAILED"
            return None
        if record["result"] is None or record["metrics"] is None:
            result.update(status="BENCHMARK_WORKER_FAILED", error={"message": "worker omitted its result or metrics"})
            return None
        counts = record["metrics"]["counters"]
        if any(type(counts.get(name)) is not int or counts[name] < 0 for name in _COUNTERS):
            raise ValueError("worker preparation and operation counters must be nonnegative integers")
        prepared = sum(stage["metrics"]["counters"][name] for stage in result["stages"] if stage["metrics"] is not None
                       for name in _COUNTERS[2:])
        if prepared > result["limits"]["maximum_density_preparations"]:
            raise ValueError("aggregate checkpoint benchmark density preparation bound exceeded")
        first = result["stages"][0]["metrics"]
        if record["metrics"]["versions"] != first["versions"] or record["metrics"]["inputs"]["source_files"] != first["inputs"]["source_files"]:
            result.update(status="BENCHMARK_WORKER_FAILED", error={"message": "worker software or source identity changed"})
            return None
        return record

    try:
        baseline = run("baseline")
        if baseline is None:
            return result
        base = baseline["result"]
        if not base or base.get("status") != "CALIBRATION_STUDY_COMPLETED" or base.get("fusion_status") != "CALIBRATION_FUSION_STUDY_CHECKED":
            return result
        count = len(normalized["study"]["initial_layouts"])
        for index in range(math.ceil(count / normalized["limits"]["reference_layouts_per_process"])):
            reference = run("reference-start" if index == 0 else "reference-resume")
            if reference is None:
                return result
            state = reference["result"]
            if not state or state.get("status") not in {"CALIBRATION_STUDY_INCREMENTAL_PAUSED", "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED"}:
                return result
            if state["status"] == "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED":
                break
        else:
            return result
        for _ in range(math.ceil(count / normalized["limits"]["fusion_layouts_per_process"])):
            continued = run("fusion")
            if continued is None:
                return result
            state = continued["result"]
            if not state or state.get("status") not in {"CALIBRATION_STUDY_FUSION_CHECKPOINT_PAUSED", "CALIBRATION_STUDY_FUSION_CHECKPOINT_COMPLETED"}:
                return result
            if state["status"] == "CALIBRATION_STUDY_FUSION_CHECKPOINT_COMPLETED":
                if state.get("fusion_status") != "CALIBRATION_FUSION_STUDY_CHECKED":
                    return result
                break
        else:
            return result
        paths = {"baseline": output / baseline["directory"] / "study.json", "continued": output / continued["directory"] / "study.json"}
        raw = {}
        for name, path in paths.items():
            _, fingerprint = _read(path)
            worker_result = base if name == "baseline" else state
            expected = {key: worker_result["study_output"][key] for key in ("bytes", "sha256")}
            if fingerprint != expected:
                raise ValueError(f"{name} study differs from its worker fingerprint")
            result["reports"][name] = {"path": str(path), **fingerprint}
            raw[name] = path.read_bytes()
            if {"bytes": len(raw[name]), "sha256": sha256(raw[name]).hexdigest()} != fingerprint:
                raise ValueError(f"{name} study bytes changed during comparison")
        result["exact_report_bytes_match"] = raw["baseline"] == raw["continued"]
        if not result["exact_report_bytes_match"]:
            result["status"] = "BENCHMARK_REPORTS_DIFFER"
            return result
        result["verification"] = {
            name: verify_snapshot_calibration_study(constraint, path, expected_sha256=result["reports"][name]["sha256"])
            for name, path in paths.items()
        }
        result["status"] = "BENCHMARK_COMPLETED"
        return result
    except (OSError, RuntimeError, ValueError) as error:
        result.update(status="BENCHMARK_VERIFICATION_FAILED", error={"error_type": type(error).__name__, "message": str(error)})
        return result
    finally:
        result["storage"] = {"checkpoint_store": _storage(output / "checkpoints"),
                             "all_evidence": {**_storage(output), "excludes": ["summary.json"]}}
        result["measurement_scope"] = "One uninterrupted worker versus multiple fresh continuation workers; resource observations only"
        result["timings"] = {
            "scope": "Parent wall time per process, including startup, input checks and output",
            **{name: sum(stage["wall_seconds"] for stage in result["stages"] if stage["role"] in roles)
               for name, roles in (("baseline_seconds", {"baseline"}), ("reference_seconds", {"reference-start", "reference-resume"}),
                                   ("fusion_seconds", {"fusion"}))},
        }
        result["density_preparation_attempts"] = sum(
            stage["metrics"]["counters"].get(name, 0) for stage in result["stages"] if stage["metrics"] is not None
            for name in _COUNTERS[2:]
        )
        _save(output / "summary.json", result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--worker", choices=_ROLES, help=argparse.SUPPRESS)
    parser.add_argument("--expected-scenario-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--reference-generation-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--fusion-generation-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        _worker(args)
        return
    sys.addaudithook(_offline)
    result = run_benchmark(args.scenario, args.directory)
    print(json.dumps(result, indent=2, allow_nan=False))
    if result["status"] != "BENCHMARK_COMPLETED":
        parser.exit(1)


if __name__ == "__main__":
    main()
