"""Audit retained checkpoint benchmark records using local SDK-free fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(PACKAGE / "src"))
sys.path.insert(0, str(PACKAGE / "examples"))

import benchmark_checkpoint_continuation as benchmark
from austins_algorithm import checkpoint_benchmark_verification as verification
from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundles
from austins_algorithm import snapshot_calibration_fusion_checkpoint_store as fusion_store
from austins_algorithm import snapshot_calibration_incremental_store as reference_store
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm import snapshot_calibration_study_checkpoint as checkpoint
from austins_algorithm import snapshot_calibration_study_fusion_checkpoint as fusion_checkpoint
from austins_algorithm import snapshot_calibration_study_incremental as incremental
from austins_algorithm.__main__ import main
from austins_algorithm.unknown_count_snapshot_cache import _json_bytes
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
import test_checkpoint_continuation_benchmark as benchmark_fixtures
import test_snapshot_calibration_study_incremental as incremental_fixtures


class CheckpointBenchmarkVerificationTests(unittest.TestCase):
    capture = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.capture
    timeout = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.timeout
    simulated_runtime = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.simulated_runtime
    write_scenario = benchmark_fixtures.CheckpointContinuationBenchmarkTests.write_scenario

    def setUp(self):
        benchmark_fixtures.CheckpointContinuationBenchmarkTests.setUp(self)
        self.constraint = self.directory / self.manifest["constraint"][0]

    def make_record(
        self, name="record", *, layouts=3, fail=None, time_out=None, partial=False,
        publication=None, reference_incomplete=False,
    ):
        document = deepcopy(self.manifest)
        document["study"]["initial_layouts"] = document["study"]["initial_layouts"][:layouts]
        self.write_scenario(document)
        root = self.directory / name
        calls = []
        occurrences = {}
        current_role = None

        def no_sdk(*_args, **_options):
            return None

        with self.simulated_runtime():
            versions = incremental._bundle_software_versions()
            original_reference = study._capture_calibration_layout
            original_fusion = checkpoint._capture_restored_calibration_layout_fusion

            def record_preparations(*, fused=False):
                for _round in range(2):
                    if not fused:
                        comparison.compile_and_verify()
                    for _date in range(2):
                        comparison._prepare_density_snapshot(density_fusion=fused)

            def reference(prepared, layout, **options):
                record_preparations()
                if reference_incomplete and current_role == "reference-resume":
                    return self.timeout(prepared, layout, cancelled=False)
                return original_reference(prepared, layout, **options)

            def fused(prepared, report, programs):
                record_preparations(fused=True)
                if partial:
                    blocked = _marked(self.timeout(prepared, tuple(report["initial_layout"]), cancelled=False))
                    blocked["density_fusion"]["observed"] = {"earlier": {}, "later": {}}
                    return blocked
                return original_fusion(prepared, report, programs)

            def baseline_reference(prepared, layout, **_options):
                record_preparations()
                return self.capture(prepared, layout), layout

            def baseline_fused(prepared, layout):
                record_preparations(fused=True)
                return _marked(self.capture(prepared, layout))

            def controlled_process(command, **options):
                nonlocal current_role
                role = command[command.index("--worker") + 1]
                current_role = role
                occurrences[role] = occurrences.get(role, 0) + 1
                calls.append(role)
                if time_out == (role, occurrences[role]):
                    raise subprocess.TimeoutExpired(command, options["timeout"])
                if fail == (role, occurrences[role]):
                    return subprocess.CompletedProcess(command, 7)
                args = SimpleNamespace(
                    worker=role, scenario=Path(command[command.index("--scenario") + 1]),
                    expected_scenario_sha256=command[command.index("--expected-scenario-sha256") + 1],
                    directory=Path(command[command.index("--directory") + 1]),
                    reference_generation_sha256=(
                        command[command.index("--reference-generation-sha256") + 1]
                        if "--reference-generation-sha256" in command else None
                    ),
                    fusion_generation_sha256=(
                        command[command.index("--fusion-generation-sha256") + 1]
                        if "--fusion-generation-sha256" in command else None
                    ),
                )
                def publication_failed(_path, _raw):
                    raise OSError("controlled checkpoint publication failure")

                with patch.object(sys, "addaudithook"):
                    if publication == "reference" and role == "reference-resume":
                        with patch.object(reference_store, "_publish_bundle", new=publication_failed):
                            benchmark._worker(args)
                    elif publication == "fusion" and role == "fusion":
                        with patch.object(fusion_store, "_publish_bundle", new=publication_failed):
                            benchmark._worker(args)
                    else:
                        benchmark._worker(args)
                return subprocess.CompletedProcess(command, 0)

            memory = {name: None for name in ("working_set_bytes", "peak_working_set_bytes", "private_usage_bytes")}
            memory["reason"] = "Controlled resource fixture"
            worker_subprocess = SimpleNamespace(
                run=controlled_process, TimeoutExpired=subprocess.TimeoutExpired,
                CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with (
                patch.object(comparison, "compile_and_verify", new=no_sdk),
                patch.object(comparison, "_prepare_density_snapshot", new=no_sdk),
                patch.object(comparison, "_prepare_density_snapshot_batch", new=no_sdk),
                patch.object(study, "_capture_calibration_layout", new=reference),
                patch.object(study, "_capture_calibration_layout_reference", new=baseline_reference),
                patch.object(study, "_capture_calibration_layout_fusion", new=baseline_fused),
                patch.object(checkpoint, "_capture_restored_calibration_layout_fusion", new=fused),
                patch.object(bundles, "_bundle_software_versions", return_value=versions),
                patch.object(fusion_checkpoint, "_bundle_software_versions", return_value=versions),
                patch.object(benchmark, "_process_memory", return_value=memory),
                patch.object(benchmark, "subprocess", new=worker_subprocess),
            ):
                result = benchmark.run_benchmark(self.scenario, root)
        return root, result, calls

    def summary(self, root):
        return json.loads((root / "summary.json").read_bytes())

    def seal(self, root, summary):
        self.refresh_storage(root, summary)
        raw = _json_bytes(summary)
        (root / "summary.json").write_bytes(raw)
        return sha256(raw).hexdigest()

    def refresh_storage(self, root, summary):
        files = [path for path in root.rglob("*") if path.is_file() and path != root / "summary.json"]
        summary["storage"] = {
            "checkpoint_store": benchmark._storage(root / "checkpoints"),
            "all_evidence": {
                "files": len(files), "bytes": sum(path.stat().st_size for path in files), "excludes": ["summary.json"],
            },
        }

    def verify(self, root, *, pin=None):
        if pin is None:
            pin = sha256((root / "summary.json").read_bytes()).hexdigest()
        return verification.verify_checkpoint_benchmark(self.constraint, root, expected_summary_sha256=pin)

    def test_worker_control_keeps_host_metadata_subprocesses_separate(self):
        original_run = subprocess.run
        commands = []

        def host_process(command, **_options):
            self.assertEqual(command, ["chronorealm-host-metadata"])
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="fixture processor", stderr="")

        def processor():
            self.assertIs(subprocess.run, host_process)
            return subprocess.run(["chronorealm-host-metadata"], capture_output=True, text=True, check=True).stdout

        with (
            patch.object(subprocess, "run", new=host_process),
            patch.object(benchmark.platform, "processor", new=processor),
            patch.object(benchmark.platform, "platform", return_value="controlled host fixture"),
        ):
            root, summary, roles = self.make_record(layouts=1)

        self.assertIs(subprocess.run, original_run)
        self.assertEqual(summary["status"], "BENCHMARK_COMPLETED", summary.get("error"))
        self.assertEqual(roles, ["baseline", "reference-start", "fusion"])
        self.assertEqual(len(commands), len(summary["stages"]))
        for stage in summary["stages"]:
            self.assertEqual(stage["metrics"]["processor"], "fixture processor")
        self.assertEqual(self.verify(root)["recorded_status"], "BENCHMARK_COMPLETED")

    def test_completed_eight_layout_record_and_portable_copy_verify_without_external_reads(self):
        root, summary, calls = self.make_record(layouts=8)
        self.assertEqual(summary["status"], "BENCHMARK_COMPLETED")
        self.assertEqual(len(calls), 9)
        verified = self.verify(root)
        self.assertEqual(verified["status"], "CHECKPOINT_BENCHMARK_RECORD_VERIFIED")
        self.assertEqual(verified["recorded_status"], "BENCHMARK_COMPLETED")
        self.assertEqual(verified["checks_scope"], "AVAILABLE_RECORDED_EVIDENCE")
        self.assertEqual(verified["checks"]["stage_content_bindings"], "PARSED_CONTENT_MATCHES_SUMMARY")
        self.assertEqual(verified["evidence"]["worker_execution"], "RECORDED_ONLY")
        self.assertFalse(verified["evidence"]["stored_qpy_decoded"])
        self.assertFalse(verified["evidence"]["sdk_runtime_replayed"])
        relocated = self.directory / "relocated"
        shutil.copytree(root, relocated)
        original_open = Path.open
        opened = []

        def local_only(path, *args, **options):
            resolved = path.resolve()
            self.assertTrue(resolved.is_relative_to(relocated) or resolved == self.constraint.resolve(), str(path))
            opened.append(resolved)
            return original_open(path, *args, **options)

        with patch.object(Path, "open", new=local_only):
            copied = self.verify(relocated)
        self.assertEqual(copied["status"], verified["status"])
        self.assertTrue(opened)

    def test_raw_summary_pin_is_checked_before_invalid_json_is_parsed(self):
        root, _summary, _calls = self.make_record()
        (root / "summary.json").write_bytes(b"{invalid JSON")
        with self.assertRaisesRegex(ValueError, "(?i)(SHA-256|digest|pin)"):
            self.verify(root, pin="0" * 64)

    def test_failed_timeout_and_terminal_records_audit_available_evidence(self):
        cases = (
            ("failed", {"fail": ("reference-resume", 1)}, "BENCHMARK_WORKER_FAILED"),
            ("timeout", {"time_out": ("fusion", 2)}, "BENCHMARK_TIMED_OUT"),
            ("terminal", {"partial": True}, "BENCHMARK_INCOMPLETE"),
        )
        for name, options, status in cases:
            with self.subTest(name=name):
                root, summary, _calls = self.make_record(name, **options)
                self.assertEqual(summary["status"], status)
                verified = self.verify(root)
                self.assertEqual(verified["status"], "CHECKPOINT_BENCHMARK_RECORD_VERIFIED")
                self.assertEqual(verified["recorded_status"], status)
                self.assertFalse(verified["recorded_complete"])
                self.assertEqual(verified["reference_generation_sha256"], summary["reference_generation_sha256"])
                self.assertEqual(verified["fusion_generation_sha256"], summary["fusion_generation_sha256"])
                if name == "timeout":
                    self.assertEqual(summary["stages"][-1]["cancellation_status"], "UNKNOWN")
                if name == "terminal":
                    constraint = self.constraint
                    argv = ["austins-algorithm", "checkpoint-benchmark-verify", str(constraint), str(root),
                            "--expected-summary-sha256", sha256((root / "summary.json").read_bytes()).hexdigest()]
                    with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
                        main()
                    self.assertEqual(json.loads(stdout.getvalue())["recorded_status"], status)

    def test_resealed_summary_and_stage_tampering_cannot_forge_valid_progress(self):
        root, original, _calls = self.make_record()
        original_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        cases = (
            "role order", "pin", "progress", "negative counter", "boolean counter", "role cap",
            "zero successful work", "undercount successful work", "aggregate cap", "source drift",
            "memory reason", "memory value", "finite time",
        )
        for case in cases:
            with self.subTest(case=case):
                for path, raw in original_files.items():
                    path.write_bytes(raw)
                summary = deepcopy(original)
                stage = summary["stages"][1]
                if case == "role order":
                    stage["role"] = "fusion"
                elif case == "pin":
                    stage["result"]["generation_sha256"] = "f" * 64
                elif case == "progress":
                    stage["result"]["completed_layouts"] = []
                elif case == "negative counter":
                    stage["metrics"]["counters"]["reference_preparations"] = -1
                elif case == "boolean counter":
                    stage["metrics"]["counters"]["reference_preparations"] = True
                elif case == "role cap":
                    stage["metrics"]["density_preparation_limit"] += 1
                elif case == "zero successful work":
                    stage["metrics"]["counters"]["compiles"] = 0
                elif case == "undercount successful work":
                    stage["metrics"]["counters"]["compiles"] -= 1
                elif case == "aggregate cap":
                    summary["limits"]["maximum_density_preparations"] += 1
                elif case == "source drift":
                    stage["metrics"]["inputs"]["source_files"]["benchmark_example"]["sha256"] = "f" * 64
                elif case == "memory reason":
                    stage["metrics"]["memory"]["before"]["reason"] = None
                elif case == "memory value":
                    stage["metrics"]["memory"]["before"] = {
                        "working_set_bytes": -1, "peak_working_set_bytes": 0, "private_usage_bytes": 0, "reason": None,
                    }
                else:
                    stage["metrics"]["seconds"] = float("inf")
                for name in ("result", "metrics"):
                    (root / stage["directory"] / f"{name}.json").write_text(json.dumps(stage[name]), encoding="utf-8")
                self.refresh_storage(root, summary)
                raw = json.dumps(summary).encode("utf-8")
                (root / "summary.json").write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.verify(root, pin=sha256(raw).hexdigest())

    def test_stage_file_content_must_match_summary_but_whitespace_can_change(self):
        root, summary, _calls = self.make_record()
        stage = summary["stages"][1]
        path = root / stage["directory"] / "metrics.json"
        original = path.read_bytes()
        path.write_bytes(original.replace(b" ", b"\t", 1))
        self.assertEqual(self.verify(root)["status"], "CHECKPOINT_BENCHMARK_RECORD_VERIFIED")
        changed = original.replace(b'"role": "reference-start"', b'"role": "reference-starp"', 1)
        self.assertNotEqual(changed, original)
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "(?i)(match|differs|content|summary)"):
            self.verify(root)

    def test_stage_path_traversal_is_rejected(self):
        root, summary, _calls = self.make_record()
        summary["stages"][1]["directory"] = "../outside"
        self.seal(root, summary)
        with self.assertRaisesRegex(ValueError, "(?i)(path|stage|directory|canonical)"):
            self.verify(root)
    def test_scenario_symlink_is_rejected(self):
        root, _summary, _calls = self.make_record("linked")
        scenario = root / "scenario.json"
        raw = scenario.read_bytes()
        target = self.directory / "outside-scenario.json"
        target.write_bytes(raw)
        scenario.unlink()
        try:
            scenario.symlink_to(target)
        except OSError as error:
            scenario.write_bytes(raw)
            self.skipTest(f"local symlink creation unavailable, error {getattr(error, 'winerror', error.errno)}")
        try:
            with self.assertRaisesRegex(ValueError, "(?i)(regular|link|reparse)"):
                self.verify(root)
        finally:
            scenario.unlink()
            scenario.write_bytes(raw)

    def test_additional_stage_after_recorded_terminal_outcome_is_rejected(self):
        root, summary, _calls = self.make_record(partial=True)
        self.assertEqual(summary["status"], "BENCHMARK_INCOMPLETE")
        previous = summary["stages"][-1]
        self.assertEqual(previous["result"]["status"], "CALIBRATION_STUDY_FUSION_CHECKPOINT_TERMINAL")
        extra = deepcopy(previous)
        extra["directory"] = f"{len(summary['stages']) + 1:02d}-fusion"
        shutil.copytree(root / previous["directory"], root / extra["directory"])
        summary["stages"].append(extra)
        self.seal(root, summary)
        with self.assertRaisesRegex(ValueError, "(?i)(terminal|after|stage|continu|sequence)"):
            self.verify(root)

    def test_reparse_attribute_is_rejected_before_opening_the_target(self):
        root, _summary, _calls = self.make_record()
        scenario = root / "scenario.json"
        lstat = Path.lstat
        open_file = Path.open

        def reported_reparse(path):
            metadata = lstat(path)
            if path == scenario:
                return SimpleNamespace(
                    st_mode=metadata.st_mode, st_size=metadata.st_size,
                    st_file_attributes=getattr(metadata, "st_file_attributes", 0) | 0x400,
                )
            return metadata

        def no_target_read(path, *args, **options):
            self.assertNotEqual(path, scenario)
            return open_file(path, *args, **options)

        with (
            patch.object(Path, "lstat", new=reported_reparse), patch.object(Path, "open", new=no_target_read),
            self.assertRaisesRegex(ValueError, "(?i)(regular|link|reparse)"),
        ):
            self.verify(root)

    def test_saved_chain_and_consistently_resealed_study_plan_corruption_are_rejected(self):
        root, summary, _calls = self.make_record()
        generation = root / "checkpoints" / "fusion-generations" / f"{summary['fusion_generation_sha256']}.json"
        original = generation.read_bytes()
        generation.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "(?i)(SHA-256|digest|pin)"):
            self.verify(root)
        generation.write_bytes(original)
        for name in ("baseline", "continued"):
            path = Path(summary["reports"][name]["path"])
            document = json.loads(path.read_bytes())
            document["planning"]["feasible_count"] += 1
            raw = _json_bytes(document)
            path.write_bytes(raw)
            fingerprint = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
            summary["reports"][name].update(fingerprint)
            stage = next(stage for stage in summary["stages"] if stage["directory"] == path.parent.name)
            stage["result"]["study_output"].update(fingerprint)
            (path.parent / "result.json").write_bytes(_json_bytes(stage["result"]))
        self.seal(root, summary)
        with self.assertRaisesRegex(ValueError, "(?i)(planning|plan|verified|evidence)"):
            self.verify(root)

    def test_wrong_constraint_and_invalid_cli_pin_fail_without_record_changes(self):
        root, _summary, _calls = self.make_record()
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        raw = self.constraint.read_bytes()
        self.constraint.write_bytes(raw + b" ")
        try:
            with self.assertRaisesRegex(ValueError, "(?i)(constraint|SHA-256|pin)"):
                self.verify(root)
        finally:
            self.constraint.write_bytes(raw)
        argv = ["austins-algorithm", "checkpoint-benchmark-verify", str(self.constraint), str(root),
                "--expected-summary-sha256", "0" * 64]
        with (
            patch.object(sys, "argv", argv), redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as invalid,
        ):
            main()
        self.assertEqual(invalid.exception.code, 1)
        self.assertRegex(stderr.getvalue(), "(?i)(SHA-256|digest|pin)")
        self.assertEqual({path: path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)

    def test_failed_publication_and_reference_capture_payloads_are_scientifically_checked(self):
        cases = (
            ("reference-publication", {"publication": "reference"}),
            ("fusion-publication", {"publication": "fusion"}),
            ("reference-incomplete", {"reference_incomplete": True}),
        )
        for name, options in cases:
            with self.subTest(name=name):
                root, summary, _calls = self.make_record(name, **options)
                self.assertEqual(summary["status"], "BENCHMARK_INCOMPLETE")
                self.assertEqual(self.verify(root)["recorded_status"], "BENCHMARK_INCOMPLETE")
                for corruption in ("calibration", "software versions"):
                    with self.subTest(corruption=corruption):
                        altered = deepcopy(summary)
                        stage = altered["stages"][-1]
                        failure = stage["result"]["failure"]
                        capture = failure["attempt"]["capture"] if name == "fusion-publication" else failure["capture"]
                        report = capture["report"]
                        if corruption == "software versions":
                            report["versions"]["qiskit"] = "99"
                        else:
                            first_round = report["rounds"]["0"]
                            first_wire = next(iter(first_round["target_calibrations"]["earlier"]["qubits"]))
                            first_round["target_calibrations"]["earlier"]["qubits"][first_wire]["t1_seconds"] = None
                        capture["report_sha256"] = _digest(report)
                        (root / stage["directory"] / "result.json").write_bytes(_json_bytes(stage["result"]))
                        self.seal(root, altered)
                        with self.assertRaisesRegex(ValueError, "(?i)(failure|capture|calibration|lifetime|evidence)"):
                            self.verify(root)

