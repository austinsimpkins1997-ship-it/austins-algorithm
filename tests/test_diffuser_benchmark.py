"""Exercise local diffuser compiler benchmarking without the quantum SDK."""

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
from austins_algorithm.diffuser_benchmark import _verify_trial_report, run_diffuser_benchmark  # noqa: E402

MODULE = "austins_algorithm.diffuser_benchmark"
FIXTURES = PACKAGE_ROOT / "examples" / "diffuser-smoke-regression"
TRIAL_SIDES = ["left", "right", "right", "left", "left", "right", "right", "left"]


class DiffuserBenchmarkTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="diffuser-benchmark-test-", dir=build)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.left = root / "left.json"
        self.right = root / "right.json"
        self.output = root / "results"
        self.original_requests = {
            "left": (FIXTURES / "baseline" / "request.json").read_bytes(),
            "right": (FIXTURES / "baseline-native" / "request.json").read_bytes(),
        }
        for side, path in (("left", self.left), ("right", self.right)):
            path.write_bytes(self.original_requests[side])

    def run_benchmark(self, *, pairs=4, timeout=30):
        return run_diffuser_benchmark(
            self.left, self.right, output_directory=self.output,
            pairs=pairs, timeout_seconds=timeout,
        )

    @staticmethod
    def verified_report(path, document, process_wall):
        importer = "qiskit.qasm3.loads"
        if document["sampling"].get("prefer_native_qasm"):
            importer = "qiskit.qasm3.loads_experimental"
        raw = b"{}"
        path.write_bytes(raw)
        path.with_name("stderr.log").write_bytes(b"")
        return (
            {
                "process_wall": process_wall,
                "compile_export_verify": process_wall / 2,
                "transpile": 0.01,
                "openqasm_export": 0.01,
                "openqasm_import": 0.02,
                "openqasm_verify": 0.01,
            },
            {importer: 8},
            [{"physical_placement": placement, "rounds": rounds, "diffuser": diffuser}
             for placement in document["sampling"]["placements"]
             for rounds in (1, 2) for diffuser in ("standard", "clean-ancilla")],
            {"bytes": len(raw), "sha256": sha256(raw).hexdigest()},
        )

    def test_alternates_order_and_keeps_checked_request_bytes(self):
        durations = [20.0, 13.0, 2.0, 15.0, 9.0, 3.0, 10.0, 8.0]
        launches = []

        def launch(staged, trial_directory, timeout, *, expected_sha256):
            index = len(launches)
            side = TRIAL_SIDES[index]
            raw = self.original_requests[side]
            self.assertEqual(staged, self.output / f"{side}-request.json")
            self.assertEqual(staged.read_bytes(), raw)
            self.assertEqual(expected_sha256, sha256(raw).hexdigest())
            self.assertEqual(timeout, 30)
            self.assertTrue(trial_directory.is_relative_to(self.output))
            launches.append(side)
            if index == 0:
                self.left.write_bytes(b"changed after preflight")
                self.right.write_bytes(b"changed after preflight")
            return 0, durations[index]

        with (
            patch(f"{MODULE}._launch_trial", side_effect=launch),
            patch(f"{MODULE}._verify_trial_report", side_effect=self.verified_report) as verify,
        ):
            result = self.run_benchmark()

        self.assertEqual(launches, TRIAL_SIDES)
        self.assertEqual(verify.call_count, 8)
        self.assertEqual(result["status"], "BENCHMARK_COMPLETED")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["trials"][0]["artifacts"]["stderr.log"], {
            "bytes": 0, "sha256": sha256(b"").hexdigest(),
        })
        self.assertEqual([trial["side"] for trial in result["trials"]], TRIAL_SIDES)
        self.assertEqual([trial["pair"] for trial in result["trials"]], [1, 1, 2, 2, 3, 3, 4, 4])
        self.assertTrue(all(trial["status"] == "VERIFIED" for trial in result["trials"]))
        self.assertEqual([trial["seconds"]["process_wall"] for trial in result["trials"]], durations)
        stats = result["timing_summary"]
        self.assertEqual(stats["left"]["process_wall"], {"min": 8.0, "median": 12.0, "max": 20.0})
        self.assertEqual(stats["right"]["process_wall"], {"min": 2.0, "median": 6.5, "max": 13.0})
        self.assertEqual(stats["right_minus_left"]["process_wall"], {"min": -13.0, "median": -6.5, "max": 2.0})
        self.assertEqual(json.loads((self.output / "summary.json").read_bytes())["timing_summary"], stats)
        for side, raw in self.original_requests.items():
            self.assertEqual((self.output / f"{side}-request.json").read_bytes(), raw)

    def test_preflight_rejects_different_compiler_inputs_and_plan_before_launch(self):
        for section, field, value in (
            ("sampling", "transpiler_seed", 13),
            ("witness", "witness_validation_power", 0.5),
            ("expected", "plan_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                right = json.loads(self.original_requests["right"])
                right[section][field] = value
                right.pop("content_sha256")
                right["content_sha256"] = _diffuser_replay_digest(right)
                self.right.write_text(json.dumps(right), encoding="utf-8")
                with (
                    patch(f"{MODULE}._launch_trial") as launch,
                    self.assertRaisesRegex(ValueError, "diffuser benchmark requests differ"),
                ):
                    self.run_benchmark()
                launch.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_timeout_stops_further_trials_and_preserves_verified_trial(self):
        with (
            patch(f"{MODULE}._launch_trial", side_effect=[(0, 0.25), TimeoutExpired(["worker"], 5)]) as launch,
            patch(f"{MODULE}._verify_trial_report", side_effect=self.verified_report) as verify,
        ):
            result = self.run_benchmark(timeout=5)

        self.assertEqual(launch.call_count, 2)
        verify.assert_called_once()
        self.assertEqual(result["status"], "BENCHMARK_FAILED")
        self.assertEqual(
            [(row["side"], row["status"]) for row in result["trials"]],
            [("left", "VERIFIED"), ("right", "TIMED_OUT")],
        )
        self.assertIsNone(result["timing_summary"])
        self.assertEqual(json.loads((self.output / "summary.json").read_bytes())["status"], "BENCHMARK_FAILED")

    def test_trial_report_recomputes_checked_evidence_and_validates_timing(self):
        document = json.loads(self.original_requests["left"])
        report = json.loads((FIXTURES / "baseline" / "report.json").read_bytes())
        report["status"] = "DIFFUSER_REPLAY_VERIFIED"
        report["result_kind"] = "MODELED_PROBABILITIES_WITH_WITNESS_PLAN"
        report["limits"].update(shots=0, max_sampling_batches=0)
        report["wall_seconds"] = 4.5
        plan = report.pop("modeled_witness_validation")
        for field in ("completed_batches", "completed_shots", "completed_nominal_two_qubit_gate_executions"):
            plan.pop(field)
        report["modeled_witness_plan"] = {**plan, "status": "PLANNED", "local_sampling_performed": False}
        per_circuit = {
            "compile_export_verify": 0.5,
            "transpile": 0.04,
            "openqasm_export": 0.03,
            "openqasm_import": 0.10,
            "openqasm_verify": 0.02,
        }
        report["timing_seconds"].update(sampling=0.0, **{phase: value * 8 for phase, value in per_circuit.items()})
        for cell in report["cells"]:
            for record in cell["variants"].values():
                validation = record.pop("modeled_witness_validation")
                record["modeled_witness_plan"] = {
                    "status": "PLANNED",
                    **{field: validation[field] for field in ("planned_batches", "planned_shots")},
                }
                record["compilation_timing_seconds"] = per_circuit.copy()

        path = self.left.with_name("trial-report.json")
        path.write_text(json.dumps(report), encoding="utf-8")
        seconds, importers, circuits, fingerprint = _verify_trial_report(path, document, 5.0)
        self.assertEqual(fingerprint, {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(seconds["process_wall"], 5.0)
        self.assertEqual(seconds["compile_export_verify"], 4.0)
        self.assertEqual(importers, {"qiskit.qasm3.loads": 8})
        self.assertEqual(len(circuits), 8)
        self.assertTrue(all("importer" not in circuit["openqasm"] for circuit in circuits))

        changed_circuit = deepcopy(report)
        changed_circuit["cells"][0]["variants"]["standard"]["compilation"]["openqasm"]["sha256"] = "0" * 64
        changed_plan = deepcopy(report)
        changed_plan["modeled_witness_plan"]["seed_schedule"]["standard"][0] += 1
        changed_timing = deepcopy(report)
        changed_timing["timing_seconds"]["openqasm_import"] += 0.25
        attempted_sampling = deepcopy(report)
        attempted_sampling["modeled_witness_plan"]["local_sampling_performed"] = True
        for changed, error in (
            (changed_circuit, "circuit or plan evidence differs"),
            (changed_plan, "circuit or plan evidence differs"),
            (changed_timing, "compiler totals differ"),
            (attempted_sampling, "did not verify the pinned check-only request"),
        ):
            with self.subTest(error=error):
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    _verify_trial_report(path, document, 5.0)

    def test_mismatched_compiled_identity_suppresses_the_summary(self):
        def verify(path, document, process_wall):
            seconds, importers, identities, fingerprint = self.verified_report(path, document, process_wall)
            if document["sampling"].get("prefer_native_qasm"):
                identities[0]["rounds"] = 3
            return seconds, importers, identities, fingerprint

        with (
            patch(f"{MODULE}._launch_trial", return_value=(0, 0.25)) as launch,
            patch(f"{MODULE}._verify_trial_report", side_effect=verify),
        ):
            result = self.run_benchmark()

        self.assertEqual(launch.call_count, 2)
        self.assertEqual(result["status"], "BENCHMARK_FAILED")
        self.assertIsNone(result["timing_summary"])


if __name__ == "__main__":
    unittest.main()
