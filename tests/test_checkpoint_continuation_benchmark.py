"""Check bounded checkpoint benchmark inputs and controlled worker orchestration."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(PACKAGE / "src"))
sys.path.insert(0, str(PACKAGE / "examples"))

import benchmark_checkpoint_continuation as benchmark
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.unknown_count_snapshot_cache import _json_bytes
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT
from test_snapshot_calibration_planning import _report
import test_snapshot_calibration_study as fixtures


_LAYOUTS = [[0, 1], [0, 2], [0, 3], [1, 0], [1, 2], [1, 3], [2, 0], [2, 1]]
_KEYS = [",".join(map(str, layout)) for layout in _LAYOUTS]


class CheckpointContinuationBenchmarkTests(unittest.TestCase):
    capture = fixtures.SnapshotCalibrationStudyTests.capture

    def setUp(self):
        fixtures.SnapshotCalibrationStudyTests.setUp(self)
        self.scenario = self.directory / "scenario.json"
        self.output = self.directory / "benchmark"
        constraint = self.directory / "constraint.json"
        constraint_raw = _json_bytes({"schema_version": 1, "qubits": 2, "clauses": [[-1], [-2]]})
        constraint.write_bytes(constraint_raw)

        def archive(files):
            return {
                "configuration": files.configuration.name, "properties": files.properties.name,
                "configuration_sha256": files.configuration_sha256, "properties_sha256": files.properties_sha256,
            }

        self.manifest = {
            "schema_version": 1, "constraint": [constraint.name, sha256(constraint_raw).hexdigest()],
            "earlier": archive(self.earlier), "later": archive(self.later),
            "study": {
                "initial_layouts": deepcopy(_LAYOUTS), "target_probability": .7,
                "shot_caps": [2], "query_caps": [1], "max_rounds_per_shot": 1,
                "seed_transpiler": 7, "validation_trials": 3, "prefer_native_qasm": False,
                "batch_density": False, "fusion_probability_tolerance": 0.0,
            },
            "limits": {
                "reference_layouts_per_process": 2, "fusion_layouts_per_process": 2,
                "worker_timeout_seconds": 30,
            },
        }
        self.write_scenario(self.manifest)

    def write_scenario(self, document):
        self.scenario.write_bytes(_json_bytes(document))

    def run_benchmark(self):
        return benchmark.run_benchmark(self.scenario, self.output)

    def assert_rejected_before_workers(self, diagnostic):
        with (
            patch.object(benchmark, "_run_worker") as worker,
            self.assertRaisesRegex(ValueError, diagnostic),
        ):
            self.run_benchmark()
        worker.assert_not_called()
        self.assertFalse(self.output.exists())

    def paired_report(self, *, alternate=False):
        def capture(prepared, layout):
            if not alternate:
                return self.capture(prepared, layout)
            report = _report(layout, hits=((.625, .375), (.75, .875)))
            report.update(deepcopy(prepared.scope))
            return report

        def reference(prepared, layout, **_options):
            return capture(prepared, layout), tuple(layout)

        def fused(prepared, layout):
            return _marked(capture(prepared, layout))

        options = self.manifest["study"]
        with (
            patch.object(study, "_capture_calibration_layout_reference", new=reference),
            patch.object(study, "_capture_calibration_layout_fusion", new=fused),
        ):
            return study.run_snapshot_calibration_study(
                _CONSTRAINT, self.earlier, self.later, **options,
            )

    def controlled_worker(self, *, fail=None, time_out=None, partial=False, different_report=False):
        baseline = self.paired_report()
        resumed = self.paired_report(alternate=True) if different_report else baseline
        calls = []
        occurrences = {}
        reference_count = fusion_count = 0

        def pin(label, count):
            return sha256(f"controlled {label} fixture {count}".encode("utf-8")).hexdigest()

        def launch(command, **options):
            nonlocal reference_count, fusion_count
            role = command[command.index("--worker") + 1]
            destination = Path(command[command.index("--directory") + 1])
            scenario = Path(command[command.index("--scenario") + 1])
            scenario_pin = command[command.index("--expected-scenario-sha256") + 1]
            self.assertEqual(command[0], sys.executable)
            self.assertIn("-I", command)
            self.assertIn("-B", command)
            self.assertEqual(options["timeout"], self.manifest["limits"]["worker_timeout_seconds"])
            self.assertEqual(sha256(scenario.read_bytes()).hexdigest(), scenario_pin)
            self.assertTrue(destination.is_relative_to(self.output))
            self.assertTrue(destination.is_dir())
            self.assertNotIn(destination, [call["directory"] for call in calls])
            occurrences[role] = occurrences.get(role, 0) + 1
            occurrence = occurrences[role]
            calls.append({"role": role, "directory": destination, "command": list(command)})
            if role == "reference-resume":
                previous = command[command.index("--reference-generation-sha256") + 1]
                self.assertEqual(previous, pin("reference", reference_count))
            if role == "fusion":
                reference = command[command.index("--reference-generation-sha256") + 1]
                self.assertEqual(reference, pin("reference", len(_LAYOUTS)))
                if fusion_count:
                    previous = command[command.index("--fusion-generation-sha256") + 1]
                    self.assertEqual(previous, pin("fusion", fusion_count))
            if time_out == (role, occurrence):
                raise subprocess.TimeoutExpired(command, options["timeout"])
            if fail == (role, occurrence):
                return subprocess.CompletedProcess(command, 7)

            result = {
                "schema_version": 1, "study": None, "provider_job_submitted": False, "quantum_hardware_used": False,
            }
            report = None
            if role == "baseline":
                result.update(status="CALIBRATION_STUDY_COMPLETED", fusion_status="CALIBRATION_FUSION_STUDY_CHECKED")
                report = baseline
            elif role in ("reference-start", "reference-resume"):
                reference_count = min(len(_LAYOUTS), reference_count + self.manifest["limits"]["reference_layouts_per_process"])
                result.update(
                    status="CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED" if reference_count == len(_LAYOUTS)
                    else "CALIBRATION_STUDY_INCREMENTAL_PAUSED",
                    generation_sha256=pin("reference", reference_count),
                    completed_layouts=_KEYS[:reference_count], remaining_layouts=_KEYS[reference_count:], failure=None,
                )
            else:
                self.assertEqual(role, "fusion")
                fusion_count = min(len(_LAYOUTS), fusion_count + self.manifest["limits"]["fusion_layouts_per_process"])
                complete = fusion_count == len(_LAYOUTS)
                result.update(
                    status="CALIBRATION_STUDY_FUSION_CHECKPOINT_COMPLETED" if complete or partial
                    else "CALIBRATION_STUDY_FUSION_CHECKPOINT_PAUSED",
                    reference_generation_sha256=pin("reference", reference_count),
                    generation_sha256=pin("fusion", fusion_count), attempted_layouts=_KEYS[:fusion_count],
                    completed_layouts=_KEYS[:fusion_count], remaining_layouts=_KEYS[fusion_count:], failure=None,
                )
                if complete or partial:
                    result["fusion_status"] = "CALIBRATION_FUSION_STUDY_PARTIAL" if partial else "CALIBRATION_FUSION_STUDY_CHECKED"
                    report = resumed
            if report is not None:
                raw = _json_bytes(report)
                (destination / "study.json").write_bytes(raw)
                result["study_output"] = {"path": "study.json", "sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
            (destination / "result.json").write_bytes(_json_bytes(result))
            (destination / "metrics.json").write_bytes(_json_bytes({
                "schema_version": 1, "role": role, "seconds": .01,
                "counters": {"compiles": 0, "restores": 0, "reference_preparations": 0, "fusion_preparations": 0},
                "density_preparation_limit": 64,
                "memory": {"before": {}, "after": {}}, "versions": {},
                "inputs": {"scenario_sha256": scenario_pin, "source_files": {}}, "error": None,
                "provider_job_submitted": False, "quantum_hardware_used": False,
            }))
            return subprocess.CompletedProcess(command, 0)

        return launch, calls, baseline

    def test_preflight_rejects_duplicate_and_nonfinite_json_without_creating_output(self):
        original = self.scenario.read_bytes()
        duplicate = original.decode("utf-8").replace('"schema_version":1', '"schema_version":1,"schema_version":1', 1)
        self.scenario.write_text(duplicate, encoding="utf-8")
        self.assert_rejected_before_workers(r"(?i)(duplicate|unique|invalid.*JSON)")
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                document = deepcopy(self.manifest)
                document["study"]["target_probability"] = value
                self.scenario.write_text(json.dumps(document), encoding="utf-8")
                self.assert_rejected_before_workers(r"(?i)(finite|invalid.*JSON|probability)")

    def test_preflight_rejects_unpinned_changed_constraint_and_archive_bytes(self):
        for label, path in (
            ("constraint", self.directory / self.manifest["constraint"][0]),
            ("earlier configuration", self.earlier.configuration),
            ("earlier properties", self.earlier.properties),
            ("later configuration", self.later.configuration),
            ("later properties", self.later.properties),
        ):
            with self.subTest(label=label):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b" ")
                    self.assert_rejected_before_workers(r"(?i)(SHA-256|digest|pin)")
                finally:
                    path.write_bytes(original)

    def test_layout_chunk_round_and_timeout_limits_reject_before_workers(self):
        cases = [
            ("nine layouts", "study", "initial_layouts", [*_LAYOUTS, [2, 3]], r"(?i)(layout|1 to 8)"),
            ("duplicate layout", "study", "initial_layouts", [*_LAYOUTS[:7], _LAYOUTS[0]], r"(?i)(distinct|layout)"),
            ("boolean rounds", "study", "max_rounds_per_shot", True, r"(?i)round"),
            ("negative rounds", "study", "max_rounds_per_shot", -1, r"(?i)round"),
            ("unbounded rounds", "study", "max_rounds_per_shot", 100000, r"(?i)round"),
            ("zero reference chunk", "limits", "reference_layouts_per_process", 0, r"(?i)(reference|layout|chunk)"),
            ("boolean reference chunk", "limits", "reference_layouts_per_process", True, r"(?i)(reference|layout|chunk)"),
            ("nine fusion chunk", "limits", "fusion_layouts_per_process", 9, r"(?i)(fusion|layout|chunk)"),
            ("boolean fusion chunk", "limits", "fusion_layouts_per_process", False, r"(?i)(fusion|layout|chunk)"),
            ("zero timeout", "limits", "worker_timeout_seconds", 0, r"(?i)timeout"),
            ("boolean timeout", "limits", "worker_timeout_seconds", True, r"(?i)timeout"),
        ]
        for label, section, field, value, diagnostic in cases:
            with self.subTest(label=label):
                document = deepcopy(self.manifest)
                document[section][field] = value
                self.write_scenario(document)
                self.assert_rejected_before_workers(diagnostic)

    def test_existing_destination_is_rejected_before_workers(self):
        self.output.mkdir()
        sentinel = self.output / "existing.txt"
        sentinel.write_bytes(b"preserve existing benchmark")
        with patch.object(benchmark, "_run_worker") as worker, self.assertRaises(FileExistsError):
            self.run_benchmark()
        worker.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"preserve existing benchmark")

    def test_eight_layouts_run_in_distinct_stages_with_explicit_pin_handoffs(self):
        launch, calls, expected = self.controlled_worker()
        with patch.object(benchmark.subprocess, "run", new=launch):
            result = self.run_benchmark()
        self.assertEqual(result["status"], "BENCHMARK_COMPLETED")
        self.assertTrue(result["exact_report_bytes_match"])
        self.assertEqual([call["role"] for call in calls], [
            "baseline", "reference-start", *(["reference-resume"] * 3), *(["fusion"] * 4),
        ])
        self.assertEqual(len(result["stages"]), len(calls))
        self.assertEqual(len({call["directory"] for call in calls}), len(calls))
        self.assertEqual(len(expected["coverage"]["completed_layouts"]), 8)
        self.assertEqual(len(expected["fusion"]["coverage"]["completed_layouts"]), 8)
        for call in calls:
            self.assertTrue((call["directory"] / "result.json").is_file())
            self.assertTrue((call["directory"] / "metrics.json").is_file())

    def test_worker_failure_timeout_and_partial_result_stop_without_retry(self):
        scenarios = (
            ({"fail": ("reference-resume", 1)}, "BENCHMARK_WORKER_FAILED", 3),
            ({"time_out": ("fusion", 2)}, "BENCHMARK_TIMED_OUT", 7),
            ({"partial": True}, "BENCHMARK_INCOMPLETE", 6),
        )
        for index, (options, status, expected_calls) in enumerate(scenarios):
            with self.subTest(options=options):
                self.output = self.directory / f"failed-{index}"
                launch, calls, _ = self.controlled_worker(**options)
                with patch.object(benchmark.subprocess, "run", new=launch):
                    result = self.run_benchmark()
                self.assertEqual(result["status"], status)
                self.assertEqual(len(calls), expected_calls)
                reference_count = 2 if "fail" in options else 8
                self.assertEqual(result["reference_generation_sha256"], sha256(
                    f"controlled reference fixture {reference_count}".encode("utf-8"),
                ).hexdigest())
                fusion_pin = None if "fail" in options else sha256(b"controlled fusion fixture 2").hexdigest()
                self.assertEqual(result["fusion_generation_sha256"], fusion_pin)
                self.assertEqual(len(result["stages"]), expected_calls)
                if "time_out" in options:
                    self.assertEqual(result["stages"][-1]["status"], "TIMED_OUT")
                    self.assertEqual(result["stages"][-1]["cancellation_status"], "UNKNOWN")
                self.assertFalse(result["exact_report_bytes_match"])
                self.assertEqual(json.loads((self.output / "summary.json").read_bytes()), result)

    def test_different_reports_and_failed_verification_keep_distinct_statuses(self):
        launch, calls, _ = self.controlled_worker(different_report=True)
        with patch.object(benchmark.subprocess, "run", new=launch):
            result = self.run_benchmark()
        self.assertEqual(result["status"], "BENCHMARK_REPORTS_DIFFER")
        self.assertFalse(result["exact_report_bytes_match"])
        self.assertEqual(len(calls), 9)
        for report in result["reports"].values():
            verified = benchmark.verify_snapshot_calibration_study(
                _CONSTRAINT, report["path"], expected_sha256=report["sha256"],
            )
            self.assertEqual(verified["status"], "PAIRED_CALIBRATION_STUDY_VERIFIED")

        self.output = self.directory / "verification-failure"
        launch, calls, _ = self.controlled_worker()
        verify = benchmark.verify_snapshot_calibration_study
        checked = []

        def fail_second_verification(*args, **options):
            result = verify(*args, **options)
            checked.append(result)
            if len(checked) == 2:
                raise ValueError("controlled report verification failure")
            return result

        with (
            patch.object(benchmark.subprocess, "run", new=launch),
            patch.object(benchmark, "verify_snapshot_calibration_study", new=fail_second_verification),
        ):
            result = self.run_benchmark()
        self.assertEqual(result["status"], "BENCHMARK_VERIFICATION_FAILED")
        self.assertTrue(result["exact_report_bytes_match"])
        self.assertEqual(len(calls), 9)
        self.assertIsNotNone(result["reference_generation_sha256"])
        self.assertIsNotNone(result["fusion_generation_sha256"])
        self.assertEqual(len(checked), 2)

    def test_cli_prints_failed_worker_evidence_and_exits_nonzero(self):
        launch, calls, _ = self.controlled_worker(fail=("baseline", 1))
        argv = [
            "benchmark_checkpoint_continuation.py", "--scenario", str(self.scenario), "--directory", str(self.output),
        ]
        with (
            patch.object(benchmark.subprocess, "run", new=launch), patch.object(sys, "argv", argv),
            patch.object(sys, "addaudithook") as audit_hook, redirect_stdout(StringIO()) as stdout,
            self.assertRaises(SystemExit) as failed,
        ):
            benchmark.main()
        self.assertEqual(failed.exception.code, 1)
        audit_hook.assert_called_once_with(benchmark._offline)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "BENCHMARK_WORKER_FAILED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["stages"][0]["returncode"], 7)
        self.assertIsNone(result["reference_generation_sha256"])
        self.assertIsNone(result["fusion_generation_sha256"])
        self.assertEqual(json.loads((self.output / "summary.json").read_bytes()), result)

    def test_worker_density_guard_blocks_extra_preparation_and_records_failure(self):
        for role in ("reference-start", "fusion"):
            with self.subTest(role=role):
                stage = self.directory / f"guard-{role}"
                stage.mkdir()
                single = Mock()
                batched = Mock()
                comparison = SimpleNamespace(
                    __file__=__file__, compile_and_verify=Mock(),
                    _prepare_density_snapshot=single, _prepare_density_snapshot_batch=batched,
                )

                def attempt_extra_preparation(*_args, **_options):
                    preparers = (comparison._prepare_density_snapshot, comparison._prepare_density_snapshot_batch)
                    for index in range(9):
                        preparers[index % 2](density_fusion=role == "fusion")
                    return {"status": "unreachable", "study": None}

                modules = {
                    "snapshot_calibration_comparison": comparison,
                    "snapshot_calibration_study": SimpleNamespace(__file__=__file__),
                    "snapshot_calibration_study_checkpoint": SimpleNamespace(
                        __file__=__file__, _restore_compiled_round=Mock(),
                    ),
                    "snapshot_calibration_study_incremental": SimpleNamespace(
                        __file__=__file__, run_snapshot_calibration_study_incremental=attempt_extra_preparation,
                    ),
                    "snapshot_calibration_study_fusion_checkpoint": SimpleNamespace(
                        __file__=__file__, continue_snapshot_calibration_study_fusion_checkpoint=attempt_extra_preparation,
                    ),
                }
                args = SimpleNamespace(
                    worker=role, scenario=self.scenario, expected_scenario_sha256=sha256(self.scenario.read_bytes()).hexdigest(),
                    directory=stage, reference_generation_sha256="0" * 64, fusion_generation_sha256=None,
                )
                with (
                    patch.object(sys, "addaudithook") as audit_hook,
                    patch("austins_algorithm.snapshot_calibration_compilation_bundle._bundle_software_versions",
                          return_value=fixtures._VERSIONS),
                    patch.object(benchmark.importlib, "import_module",
                                 new=lambda name: modules[name.rsplit(".", 1)[-1]]),
                    patch.object(benchmark, "_process_memory", return_value={}),
                    self.assertRaisesRegex(RuntimeError, "density preparation bound exceeded"),
                ):
                    benchmark._worker(args)
                audit_hook.assert_called_once_with(benchmark._offline)
                self.assertEqual(single.call_count, 4)
                self.assertEqual(batched.call_count, 4)
                metrics = json.loads((stage / "metrics.json").read_bytes())
                self.assertEqual(metrics["density_preparation_limit"], 8)
                expected = dict.fromkeys(("compiles", "restores", "reference_preparations", "fusion_preparations"), 0)
                expected["fusion_preparations" if role == "fusion" else "reference_preparations"] = 8
                self.assertEqual(metrics["counters"], expected)
                self.assertEqual(metrics["error"]["error_type"], "RuntimeError")
                self.assertIn("density preparation bound exceeded", metrics["error"]["message"])
                self.assertFalse((stage / "result.json").exists())

