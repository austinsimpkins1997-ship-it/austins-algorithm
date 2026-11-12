"""Verify retained diffuser benchmark artifacts with the standard library."""

import json
import sys
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.constraint_diffuser_comparison import _diffuser_replay_digest  # noqa: E402
from austins_algorithm.diffuser_benchmark import run_diffuser_benchmark  # noqa: E402
from austins_algorithm.diffuser_benchmark_comparison import compare_diffuser_benchmarks  # noqa: E402
from austins_algorithm.diffuser_benchmark_verification import verify_diffuser_benchmark  # noqa: E402

FIXTURES = PACKAGE_ROOT / "examples" / "diffuser-smoke-regression"
BASELINES = {"left": "baseline", "right": "baseline-native"}
BENCHMARK_LAUNCH = "austins_algorithm.diffuser_benchmark._launch_trial"
CIRCUIT_TIMES = {
    "compile_export_verify": 0.5,
    "transpile": 0.04,
    "openqasm_export": 0.03,
    "openqasm_import": 0.10,
    "openqasm_verify": 0.02,
}


def _write_json(path, document):
    path.write_bytes((json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _checked_report(baseline):
    report = json.loads((FIXTURES / baseline / "report.json").read_bytes())
    report["status"] = "DIFFUSER_REPLAY_VERIFIED"
    report["result_kind"] = "MODELED_PROBABILITIES_WITH_WITNESS_PLAN"
    report["limits"].update(shots=0, max_sampling_batches=0)
    report["wall_seconds"] = 4.5
    plan = report.pop("modeled_witness_validation")
    for key in ("completed_batches", "completed_shots", "completed_nominal_two_qubit_gate_executions"):
        plan.pop(key)
    report["modeled_witness_plan"] = {**plan, "status": "PLANNED", "local_sampling_performed": False}
    report["timing_seconds"].update(sampling=0.0, **{phase: seconds * 8 for phase, seconds in CIRCUIT_TIMES.items()})
    for cell in report["cells"]:
        for variant in cell["variants"].values():
            validation = variant.pop("modeled_witness_validation")
            variant["modeled_witness_plan"] = {
                "status": "PLANNED",
                **{key: validation[key] for key in ("planned_batches", "planned_shots")},
            }
            variant["compilation_timing_seconds"] = CIRCUIT_TIMES.copy()
    return report


class DiffuserBenchmarkVerificationTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="benchmark-verification-test-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = {}
        for side, baseline in BASELINES.items():
            path = self.root / f"{side}.json"
            path.write_bytes((FIXTURES / baseline / "request.json").read_bytes())
            self.sources[side] = path

    def _retain_benchmark(
        self, *, name="recorded", timeout=False, legacy=False, pairs=2,
        durations=(5.0, 5.5, 6.5, 6.0), arm_order=("left", "right"),
    ):
        output = self.root / name
        durations = iter(durations)
        arm_sources = dict(zip(BASELINES, arm_order, strict=True))
        timed_out = False

        def launch(staged, directory, timeout_seconds, *, expected_sha256):
            nonlocal timed_out
            side = staged.name.split("-", maxsplit=1)[0]
            path = directory / "report.json"
            log = directory / "stderr.log"
            self.assertEqual(sha256(staged.read_bytes()).hexdigest(), expected_sha256)
            if timeout and side == "right":
                path.write_bytes(b"")
                log.write_bytes(b"worker interrupted")
                timed_out = True
                raise TimeoutExpired(["worker"], timeout_seconds)
            report = _checked_report(BASELINES[arm_sources[side]])
            report["witness_replay"]["replay_request"] = str(staged)
            report["witness_replay"]["content_sha256"] = json.loads(staged.read_bytes())["content_sha256"]
            _write_json(path, report)
            log.write_bytes(b"")
            return 0, next(durations)

        software = json.loads((FIXTURES / "baseline" / "receipt.json").read_bytes())["software_versions"]
        with (
            patch(BENCHMARK_LAUNCH, side_effect=launch),
            patch("austins_algorithm.diffuser_benchmark._software_versions", return_value=software),
        ):
            summary = run_diffuser_benchmark(
                self.sources[arm_sources["left"]], self.sources[arm_sources["right"]],
                output_directory=output, pairs=pairs,
            )
        self.assertEqual(timed_out, timeout)
        self.assertEqual(summary["status"], "BENCHMARK_FAILED" if timeout else "BENCHMARK_COMPLETED")
        self.assertEqual(summary["schema_version"], 2)
        if legacy:
            summary["schema_version"] = 1
            for trial in summary["trials"]:
                del trial["artifacts"]
            _write_json(output / "summary.json", summary)
        return output, summary

    @staticmethod
    def _trial_artifact(output, summary, name):
        return output / summary["trials"][0]["artifacts_directory"] / name

    def test_verifies_completed_legacy_and_current_benchmarks_after_relocation(self):
        retained = [
            self._retain_benchmark(name="current", legacy=False),
            self._retain_benchmark(name="legacy", legacy=True),
        ]
        for path in self.sources.values():
            path.unlink()
        for original, summary in retained:
            with self.subTest(schema=summary["schema_version"]):
                moved = self.root / f"relocated-{original.name}"
                self.assertTrue(original.resolve().is_relative_to(self.root))
                self.assertTrue(moved.resolve().is_relative_to(self.root))
                original.rename(moved)
                verified = verify_diffuser_benchmark(moved)
                self.assertEqual(verified["status"], "BENCHMARK_EVIDENCE_VERIFIED")
                self.assertEqual(verified["benchmark_status"], "BENCHMARK_COMPLETED")
                self.assertEqual(verified["artifact_integrity"], (
                    "CHECKSUMS_VERIFIED" if summary["schema_version"] == 2 else "TRIAL_CHECKSUMS_UNRECORDED"
                ))
                self.assertEqual(verified["verified_trials"], 4)
                self.assertEqual(verified["timing_summary"], summary["timing_summary"])
                self.assertEqual(
                    verified["timing_summary"]["right_minus_left"]["process_wall"],
                    {"min": 0.5, "median": 0.5, "max": 0.5},
                )
                self.assertEqual(verified["summary_sha256"], sha256((moved / "summary.json").read_bytes()).hexdigest())

    def test_verifies_timeout_as_failed_benchmark_without_timing_summary(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                output, summary = self._retain_benchmark(name=f"timeout-{legacy}", timeout=True, legacy=legacy)
                verified = verify_diffuser_benchmark(output)
                self.assertEqual(verified["status"], "BENCHMARK_EVIDENCE_VERIFIED")
                self.assertEqual(verified["benchmark_status"], "BENCHMARK_FAILED")
                self.assertEqual(verified["artifact_integrity"], (
                    "TRIAL_CHECKSUMS_UNRECORDED" if legacy else "CHECKSUMS_VERIFIED"
                ))
                self.assertEqual(verified["verified_trials"], 1)
                self.assertIsNone(verified["timing_summary"])
                if not legacy:
                    log = output / summary["trials"][-1]["artifacts_directory"] / "stderr.log"
                    log.write_bytes(log.read_bytes() + b" changed")
                    with self.assertRaises(ValueError):
                        verify_diffuser_benchmark(output)

    def test_compares_complete_benchmarks_with_unequal_pair_counts(self):
        baseline, baseline_summary = self._retain_benchmark(
            name="four-pairs", legacy=True, pairs=4,
            durations=(5.0, 7.0, 10.0, 6.0, 8.0, 9.0, 12.0, 11.0),
        )
        candidate, candidate_summary = self._retain_benchmark(name="two-pairs")
        compared = compare_diffuser_benchmarks(baseline, candidate)

        self.assertEqual(compared["status"], "BENCHMARKS_COMPARED")
        self.assertEqual(compared["timing_context"], "MATCHING_RECORDED_CONDITIONS")
        self.assertTrue(compared["compiled_output_matches"])
        self.assertEqual(compared["environment_changes"], [])
        self.assertEqual(compared["protocol_changes"], [])
        self.assertEqual(compared["baseline"]["artifact_integrity"], "TRIAL_CHECKSUMS_UNRECORDED")
        self.assertEqual(compared["candidate"]["artifact_integrity"], "CHECKSUMS_VERIFIED")
        for run, pairs in (("baseline", 4), ("candidate", 2)):
            self.assertEqual(compared[run]["requested_pairs"], pairs)
            self.assertEqual(compared[run]["verified_pairs"], pairs)
            self.assertEqual(compared[run]["verified_trials"], pairs * 2)
            for side in ("left", "right"):
                self.assertEqual(compared["arms"][side][run]["verified_trials"], pairs)
        for side, delta in (("left", -1.5), ("right", -3.5)):
            arm = compared["arms"][side]
            self.assertTrue(arm["request_content_matches"])
            self.assertTrue(arm["request_circuit_digest_matches"])
            self.assertEqual(arm["workload_changes"], [])
            self.assertEqual(arm["importer_changes"], [])
            self.assertEqual(arm["baseline"]["timing_seconds"], baseline_summary["timing_summary"][side])
            self.assertEqual(arm["candidate"]["timing_seconds"], candidate_summary["timing_summary"][side])
            self.assertEqual(arm["median_delta_seconds"]["process_wall"], delta)
        effect = compared["paired_effect"]
        self.assertEqual(effect["baseline"], baseline_summary["timing_summary"]["right_minus_left"])
        self.assertEqual(effect["candidate"], candidate_summary["timing_summary"]["right_minus_left"])
        self.assertEqual(effect["median_delta_seconds"]["process_wall"], -1.0)

    def test_compares_changed_recorded_environment_and_timeout(self):
        baseline, original = self._retain_benchmark(name="before")
        candidate, changed = self._retain_benchmark(name="after")
        changed["environment"]["logical_cpu_count"] = (original["environment"]["logical_cpu_count"] or 0) + 1
        changed["environment"]["software_versions"]["python"] = "3.12.1"
        changed["timeout_seconds"] += 1
        _write_json(candidate / "summary.json", changed)
        compared = compare_diffuser_benchmarks(baseline, candidate)

        self.assertEqual(compared["status"], "BENCHMARKS_COMPARED")
        self.assertEqual(compared["timing_context"], "RECORDED_CONDITIONS_DIFFER")
        self.assertEqual(compared["environment_changes"], [
            {
                "field": "logical_cpu_count",
                "baseline": original["environment"]["logical_cpu_count"],
                "candidate": changed["environment"]["logical_cpu_count"],
            },
            {
                "field": "software_versions.python",
                "baseline": original["environment"]["software_versions"]["python"],
                "candidate": "3.12.1",
            },
        ])
        self.assertEqual(compared["protocol_changes"], ["timeout_seconds"])
        self.assertEqual(compared["baseline"]["environment"], original["environment"])
        self.assertEqual(compared["candidate"]["environment"], changed["environment"])
        self.assertEqual(compared["paired_effect"]["median_delta_seconds"]["process_wall"], 0.0)

    def test_compares_reversed_importer_arms_by_their_recorded_side(self):
        baseline, _ = self._retain_benchmark(name="original-arms")
        candidate, _ = self._retain_benchmark(name="reversed-arms", arm_order=("right", "left"))
        compared = compare_diffuser_benchmarks(baseline, candidate)

        self.assertEqual(compared["status"], "BENCHMARKS_COMPARED")
        self.assertEqual(compared["timing_context"], "RECORDED_CONDITIONS_DIFFER")
        self.assertTrue(compared["compiled_output_matches"])
        for side, preference in (("left", False), ("right", True)):
            arm = compared["arms"][side]
            self.assertFalse(arm["request_content_matches"])
            self.assertFalse(arm["request_circuit_digest_matches"])
            self.assertEqual(arm["workload_changes"], [])
            self.assertCountEqual(arm["importer_changes"], ["prefer_native_qasm", "actual_importers_per_trial"])
            for run, preferred in (("baseline", preference), ("candidate", not preference)):
                self.assertIs(arm[run]["prefer_native_qasm"], preferred)
                importer = "qiskit.qasm3.loads_experimental" if preferred else "qiskit.qasm3.loads"
                self.assertEqual(arm[run]["actual_importers_per_trial"], {importer: 8})

    def test_compares_changed_witness_settings_with_same_compiled_output(self):
        baseline, _ = self._retain_benchmark(name="original-workload")
        for path in self.sources.values():
            request = json.loads(path.read_bytes())
            del request["content_sha256"]
            request["witness"]["witness_stop_on_futility"] = True
            request["content_sha256"] = _diffuser_replay_digest(request)
            _write_json(path, request)
        candidate, _ = self._retain_benchmark(name="changed-workload")

        compared = compare_diffuser_benchmarks(baseline, candidate)
        self.assertEqual(compared["status"], "BENCHMARKS_COMPARED")
        self.assertEqual(compared["timing_context"], "RECORDED_CONDITIONS_DIFFER")
        self.assertTrue(compared["compiled_output_matches"])
        for arm in compared["arms"].values():
            self.assertEqual(arm["workload_changes"], ["witness"])
            self.assertEqual(arm["importer_changes"], [])
            self.assertFalse(arm["request_content_matches"])
            self.assertEqual(arm["median_delta_seconds"]["process_wall"], 0.0)
        self.assertTrue(compare_diffuser_benchmarks(baseline, baseline)["summary_bytes_match"])

    def test_incomplete_comparison_keeps_coverage_and_completed_run_statistics(self):
        baseline, summary = self._retain_benchmark(name="complete")
        candidate, _ = self._retain_benchmark(name="timed-out", timeout=True)
        compared = compare_diffuser_benchmarks(baseline, candidate)

        self.assertEqual(compared["status"], "BENCHMARKS_INCOMPLETE")
        self.assertEqual(compared["timing_context"], "INCOMPLETE")
        self.assertIsNone(compared["compiled_output_matches"])
        self.assertEqual(compared["candidate"]["terminal_trial_status"], "TIMED_OUT")
        self.assertEqual(compared["candidate"]["verified_trials"], 1)
        self.assertEqual(compared["candidate"]["verified_pairs"], 0)
        for side, verified_count in (("left", 1), ("right", 0)):
            arm = compared["arms"][side]
            self.assertEqual(arm["baseline"]["timing_seconds"], summary["timing_summary"][side])
            self.assertEqual(arm["candidate"]["verified_trials"], verified_count)
            self.assertIsNone(arm["candidate"]["timing_seconds"])
            self.assertIsNone(arm["median_delta_seconds"])
        self.assertIsNone(compared["arms"]["right"]["candidate"]["actual_importers_per_trial"])
        self.assertEqual(compared["paired_effect"]["baseline"], summary["timing_summary"]["right_minus_left"])
        self.assertIsNone(compared["paired_effect"]["candidate"])
        self.assertIsNone(compared["paired_effect"]["median_delta_seconds"])

    def test_comparison_rejects_tampered_candidate_evidence(self):
        baseline, _ = self._retain_benchmark(name="baseline")
        candidate, summary = self._retain_benchmark(name="candidate")
        path = self._trial_artifact(candidate, summary, "stderr.log")
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            compare_diffuser_benchmarks(baseline, candidate)

    def test_rejects_changed_staged_request_bytes(self):
        output, _ = self._retain_benchmark()
        path = output / "left-request.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            verify_diffuser_benchmark(output)

    def test_rejects_changed_trial_schedule_or_noncanonical_trial_path(self):
        output, summary = self._retain_benchmark()
        changed_order = deepcopy(summary)
        changed_order["trials"][0], changed_order["trials"][1] = changed_order["trials"][1], changed_order["trials"][0]
        changed_path = deepcopy(summary)
        changed_path["trials"][0]["artifacts_directory"] = "../outside"
        for changed in (changed_order, changed_path):
            with self.subTest(trial=changed["trials"][0]):
                _write_json(output / "summary.json", changed)
                with self.assertRaises(ValueError):
                    verify_diffuser_benchmark(output)

    def test_rejects_changed_summary_statistic_or_impossible_process_wall(self):
        output, summary = self._retain_benchmark()
        changed_stat = deepcopy(summary)
        changed_stat["timing_summary"]["right_minus_left"]["process_wall"]["median"] += 0.1
        changed_wall = deepcopy(summary)
        changed_wall["trials"][0]["seconds"]["process_wall"] = 1.0
        changed_wall["timing_summary"]["left"]["process_wall"] = {"min": 1.0, "median": 3.5, "max": 6.0}
        changed_wall["timing_summary"]["right_minus_left"]["process_wall"] = {"min": 0.5, "median": 2.5, "max": 4.5}
        for changed in (changed_stat, changed_wall):
            with self.subTest(wall=changed["trials"][0]["seconds"]["process_wall"]):
                _write_json(output / "summary.json", changed)
                with self.assertRaises(ValueError):
                    verify_diffuser_benchmark(output)

    def test_recomputes_legacy_report_circuit_and_plan_evidence(self):
        output, summary = self._retain_benchmark(legacy=True)
        path = self._trial_artifact(output, summary, "report.json")
        report = json.loads(path.read_bytes())
        changed_circuit = deepcopy(report)
        changed_circuit["cells"][0]["variants"]["standard"]["compilation"]["openqasm"]["sha256"] = "0" * 64
        changed_plan = deepcopy(report)
        changed_plan["modeled_witness_plan"]["seed_schedule"]["standard"][0] += 1
        for label, changed in (("circuit", changed_circuit), ("plan", changed_plan)):
            with self.subTest(changed=label):
                _write_json(path, changed)
                with self.assertRaises(ValueError):
                    verify_diffuser_benchmark(output)

    def test_rejects_changed_report_or_stderr_bytes_with_trial_checksums(self):
        output, summary = self._retain_benchmark()
        for filename in ("report.json", "stderr.log"):
            with self.subTest(artifact=filename):
                path = self._trial_artifact(output, summary, filename)
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    verify_diffuser_benchmark(output)
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
