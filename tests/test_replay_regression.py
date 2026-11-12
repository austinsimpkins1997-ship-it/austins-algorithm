"""Exercise named replay preflight, execution boundaries and evidence."""

import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.constraint_replay import _replay_sampling_shot_bound  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint, _constraint_document  # noqa: E402
from austins_algorithm.diffuser_receipt import _read_verified_diffuser_receipt  # noqa: E402
from austins_algorithm.diffuser_receipt_comparison import (  # noqa: E402
    _compare_verified_diffuser_bundles,
    compare_diffuser_receipts,
)
from austins_algorithm.placement_receipt import verify_placement_receipt  # noqa: E402
from austins_algorithm.replay_receipt import _write_replay_receipt, verify_replay_receipt  # noqa: E402
from austins_algorithm.replay_regression import _run_worker, run_replay_regression  # noqa: E402


WORKER = "austins_algorithm.replay_regression._run_worker"
FORMULA = BooleanConstraint(2, ((1, 2), (-1, -2)))
WIRE_MAP = {
    "sha256": "1" * 64, "initial_logical_to_target_qubit": [0, 1],
    "export_wire_to_target_qubit": [0, 1], "classical_bit_to_target_qubit": [0, 1],
}
REQUEST = {
    "schema_version": 1, "formula": _constraint_document(FORMULA), "formula_sha256": FORMULA.digest(),
    "snapshot": {
        "backend_name": "fake_manila", "backend_version": "0.0.0",
        "last_update_date": None, "configuration_properties_sha256": "2" * 64,
    },
    "sampling": {
        "model": "manila", "initial_layout": [0, 1], "rounds": 2, "seed": 7, "max_active_qubits": 2,
        "shots": 8, "evaluation_seeds": [11, 17, 23, 31], "simulation_method": "prepared_density_matrix",
        "prefer_native_qasm": False, "density_fusion": False,
    },
    "witness": {
        "target_probability": 0.8, "max_shots": 16, "validation_seeds": [41, 43],
        "stop_on_futility": False, "tolerated_misses": 0,
    },
    "exports": {"direct": WIRE_MAP, "complement": WIRE_MAP},
}
REQUEST_BYTES = b'\xef\xbb\xbf' + json.dumps(REQUEST, indent=2).encode("utf-8")
REPORT = {
    "experiment": "CONSTRAINT_PHASE_COVER_COMPARISON", "execution_mode": "SIMULATOR",
    "provider_job_submitted": False, "quantum_hardware_used": False, "status": "COMPLETED",
    "snapshot": {"status": "COMPLETED"}, "witness_budget": {"status": "PARTIAL_VALIDATION"},
}
OPENQASM = "OPENQASM 3.0;\n"
DIFFUSER_SMOKE_ROOT = PACKAGE_ROOT / "examples" / "diffuser-smoke-regression"


class ReplayRegressionTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = TemporaryDirectory(prefix="replay-regression-", dir=build)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = self.root / "suite.json"
        self.output = self.root / "results"
        self.first = self.root / "baseline-first"
        self.second = self.root / "baseline-second"
        for path in (self.first, self.second):
            path.mkdir()
            self._save_bundle(path)

    @staticmethod
    def _save_bundle(path, *, report=REPORT):
        _write_replay_receipt(path, request=REQUEST_BYTES, report=report, openqasm=OPENQASM)

    def _write_manifest(self, *, total=128, first_limit=64, second_id="second"):
        self.manifest.write_text(json.dumps({
            "schema_version": 1, "max_total_sampling_shots": total,
            "cases": [
                {"id": "first", "baseline": self.first.name, "max_sampling_shots": first_limit, "timeout_seconds": 2},
                {"id": second_id, "baseline": self.second.name, "max_sampling_shots": 64, "timeout_seconds": 3},
            ],
        }), encoding="utf-8")

    def _run(self):
        return run_replay_regression(self.manifest, output_directory=self.output)

    def _mock_worker(self, request, directory, log, timeout, *, expected_sha256):
        self.assertEqual(request, directory.parent / "request.json")
        self.assertEqual(request.read_bytes(), REQUEST_BYTES)
        self.assertEqual(expected_sha256, sha256(REQUEST_BYTES).hexdigest())
        self.assertEqual(log, directory.parent / "stderr.log")
        directory.mkdir()
        self._save_bundle(directory)
        return 0

    def _check_smoke_requirements(self, receipt):
        requirements = PACKAGE_ROOT / "examples" / "smoke-regression" / "requirements.txt"
        pins = dict(
            line.split("==", 1)
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(pins, {name: version for name, version in receipt["software_versions"].items()
                                if name not in ("python", "austins_algorithm")})

    def test_shipped_smoke_fixture_matches_its_manifest_and_receipt(self):
        root = PACKAGE_ROOT / "examples" / "smoke-regression"
        manifest = json.loads((root / "manifest.json").read_bytes())
        case, = manifest["cases"]
        bundle = root / case["baseline"]
        receipt = verify_replay_receipt(bundle)
        raw = (bundle / "request.json").read_bytes()
        self.assertEqual(json.loads(raw)["formula"], json.loads((root / "constraint.json").read_bytes()))
        self.assertEqual(_replay_sampling_shot_bound(raw), case["max_sampling_shots"])
        self.assertEqual(case["max_sampling_shots"], manifest["max_total_sampling_shots"])
        self._check_smoke_requirements(receipt)

    def test_shipped_placement_smoke_fixture_matches_its_receipt_and_pins(self):
        bundle = PACKAGE_ROOT / "examples" / "placement-smoke-regression" / "baseline"
        result = verify_placement_receipt(bundle)
        self.assertEqual(result["placement_status"], "COMPLETED")
        self.assertEqual(result["verified_exports"], 4)
        self.assertEqual(result["completed_shots"], 32)
        self.assertFalse(result["local_cancellation_unconfirmed"])
        request = json.loads((bundle / "request.json").read_bytes())
        self.assertEqual(request["model"], "manila")
        self.assertIs(request["prefer_native_qasm"], False)
        self.assertEqual(request["shots"], 4)
        self.assertEqual(request["evaluation_seeds"], [29, 43])
        self.assertEqual(request["transpiler_seed"], 7)
        self._check_smoke_requirements(verify_replay_receipt(bundle))

    def _check_shipped_diffuser_smoke_fixture(self, manifest_name):
        root = DIFFUSER_SMOKE_ROOT
        manifest = json.loads((root / manifest_name).read_bytes())
        case, = manifest["cases"]
        bundle = root / case["baseline"]
        receipt, report, raw_request = _read_verified_diffuser_receipt(bundle)
        request = json.loads(raw_request)
        self.assertEqual(request["formula"], json.loads((root / "constraint.json").read_bytes()))
        self.assertEqual(request["sampling"]["model"], "manila")
        workload = receipt["workload"]
        self.assertEqual(workload["execution_status"], "COMPLETED")
        self.assertEqual(workload["planned_batches"], 16)
        self.assertEqual(workload["completed_batches"], 16)
        self.assertEqual(workload["planned_shots"], 112)
        self.assertEqual(workload["completed_shots"], 112)
        self.assertEqual(workload["completed_nominal_two_qubit_gate_executions"], 11088)
        self.assertEqual(workload["planned_shots"], case["max_sampling_shots"])
        self.assertEqual(case["max_sampling_shots"], manifest["max_total_sampling_shots"])
        self._check_smoke_requirements(receipt)
        return case, request, report

    def test_shipped_diffuser_smoke_fixture_matches_its_manifest_and_receipt(self):
        _, request, _ = self._check_shipped_diffuser_smoke_fixture("manifest.json")
        self.assertEqual(request["schema_version"], 1)
        self.assertNotIn("prefer_native_qasm", request["sampling"])

    def test_shipped_native_diffuser_smoke_fixture_preserves_measurement_evidence(self):
        _, stable_request, stable_report = self._check_shipped_diffuser_smoke_fixture("manifest.json")
        case, native_request, native_report = self._check_shipped_diffuser_smoke_fixture("manifest-native.json")
        self.assertEqual(case["id"], "manila-diffusers-native")
        self.assertEqual(case["baseline"], "baseline-native")
        self.assertEqual(case["timeout_seconds"], 60)
        self.assertEqual(native_request["schema_version"], 2)
        self.assertEqual(native_request["sampling"], {**stable_request["sampling"], "prefer_native_qasm": True})
        self.assertEqual(native_request["witness"], stable_request["witness"])
        native_evidence = native_request["expected"]
        stable_evidence = stable_request["expected"]
        self.assertNotEqual(native_evidence["circuits_sha256"], stable_evidence["circuits_sha256"])
        self.assertEqual(
            {**native_evidence, "circuits_sha256": stable_evidence["circuits_sha256"]},
            stable_evidence,
        )

        importer = "qiskit.qasm3.loads_experimental"
        for stable_cell, native_cell in zip(stable_report["cells"], native_report["cells"], strict=True):
            for diffuser, native in native_cell["variants"].items():
                stable = stable_cell["variants"][diffuser]
                with self.subTest(placement=native_cell["placement"], rounds=native_cell["rounds"], diffuser=diffuser):
                    stable_compilation = stable["compilation"]
                    native_compilation = native["compilation"]
                    self.assertEqual(stable_compilation["openqasm"]["importer"], "qiskit.qasm3.loads")
                    self.assertEqual(
                        native_compilation["openqasm"],
                        {**stable_compilation["openqasm"], "importer": importer, "preferred_importer": importer},
                    )
                    self.assertEqual(native_compilation["compiled"], stable_compilation["compiled"])
                    for stable_batch, native_batch in zip(
                        stable["modeled_witness_validation"]["batches"],
                        native["modeled_witness_validation"]["batches"],
                        strict=True,
                    ):
                        self.assertEqual(native_batch["seed"], stable_batch["seed"])
                        self.assertEqual(native_batch["simulation"]["counts"], stable_batch["simulation"]["counts"])

    def test_runs_named_cases_once_and_preserves_partial_execution_status(self):
        self._write_manifest()
        with patch(WORKER, side_effect=self._mock_worker) as worker:
            result = self._run()
        self.assertEqual(result["status"], "REGRESSION_MATCHED")
        self.assertEqual(result["planned_max_sampling_shots"], 128)
        self.assertEqual(result["max_worker_seconds"], 5)
        self.assertEqual([row["id"] for row in result["cases"]], ["first", "second"])
        self.assertEqual([call.args[-1] for call in worker.call_args_list], [2, 3])
        for row in result["cases"]:
            self.assertEqual(row["status"], "MATCHED")
            self.assertEqual(row["replay_execution"]["witness_status"], "PARTIAL_VALIDATION")
            self.assertEqual(row["baseline_execution"], row["replay_execution"])
        self.assertEqual(json.loads((self.output / "summary.json").read_bytes()), result)
        self.assertEqual((self.output / "manifest.json").read_bytes(), self.manifest.read_bytes())

    def test_enforces_per_case_cap_before_launching_any_worker(self):
        self._write_manifest(first_limit=63)
        with patch(WORKER) as worker:
            result = self._run()
        worker.assert_not_called()
        self.assertEqual(result["preflight_error"], "CASE_PREFLIGHT_FAILED")
        self.assertEqual([row["status"] for row in result["cases"]],
                         ["CASE_SHOT_LIMIT_EXCEEDED", "NOT_RUN_PREFLIGHT_FAILED"])

    def test_enforces_aggregate_cap_before_launching_any_worker(self):
        self._write_manifest(total=127)
        with patch(WORKER) as worker:
            result = self._run()
        worker.assert_not_called()
        self.assertEqual(result["preflight_error"], "TOTAL_SHOT_LIMIT_EXCEEDED")
        self.assertEqual(result["planned_max_sampling_shots"], 128)
        self.assertEqual({row["status"] for row in result["cases"]}, {"NOT_RUN_PREFLIGHT_FAILED"})

    def test_corrupt_baseline_stops_entire_suite_before_launch(self):
        self._write_manifest()
        (self.second / "report.json").write_bytes(b"{}")
        with patch(WORKER) as worker:
            result = self._run()
        worker.assert_not_called()
        self.assertEqual([row["status"] for row in result["cases"]], ["NOT_RUN_PREFLIGHT_FAILED", "INVALID_BASELINE"])
        self.assertIn("receipt size mismatch", result["cases"][1]["error"])
        self.assertIsNone(result["planned_max_sampling_shots"])

    def test_timeout_and_worker_failure_remain_distinct(self):
        self._write_manifest()
        with patch(WORKER, side_effect=[subprocess.TimeoutExpired("worker", 2), 7]) as worker:
            result = self._run()
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(result["status"], "REGRESSION_FAILED")
        self.assertEqual([row["status"] for row in result["cases"]], ["TIMED_OUT", "REPLAY_FAILED"])
        self.assertEqual(result["cases"][1]["worker_exit_code"], 7)

    def test_rejects_baseline_replacement_during_run(self):
        self._write_manifest()

        def run(request, directory, log, timeout, *, expected_sha256):
            if directory.parent.name == "01":
                path = self.first / "receipt.json"
                receipt = json.loads(path.read_bytes())
                receipt["software_versions"]["python"] = "changed"
                path.write_text(json.dumps(receipt), encoding="utf-8")
            return self._mock_worker(request, directory, log, timeout, expected_sha256=expected_sha256)

        with patch(WORKER, side_effect=run):
            result = self._run()
        self.assertEqual([row["status"] for row in result["cases"]], ["COMPARISON_FAILED", "MATCHED"])
        self.assertIn("changed after preflight", result["cases"][0]["error"])

    def test_reports_result_drift_without_treating_it_as_worker_failure(self):
        self._write_manifest()

        def run(request, directory, log, timeout, *, expected_sha256):
            directory.mkdir()
            self._save_bundle(directory, report={**REPORT, "witness_budget": {"status": "COMPLETED"}})
            return 0

        with patch(WORKER, side_effect=run):
            result = self._run()
        self.assertEqual({row["status"] for row in result["cases"]}, {"DIFFERED"})
        self.assertEqual(result["cases"][0]["comparison"]["sections"]["report"]["first_difference"],
                         {"path": "/witness_budget/status", "kind": "VALUE"})

    def test_only_opted_in_regression_accepts_probability_roundoff(self):
        self._write_manifest()
        for baseline in (self.first, self.second):
            other = baseline / "receipt.json"
            raw = json.dumps({**REPORT, "cross_cover_total_variation_distance": 0.0}).encode("utf-8")
            (baseline / "report.json").write_bytes(raw)
            receipt = json.loads(other.read_bytes())
            receipt["artifacts"]["report.json"] = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
            other.write_text(json.dumps(receipt), encoding="utf-8")

        def run(request, directory, log, timeout, *, expected_sha256):
            self.assertEqual(sha256(request.read_bytes()).hexdigest(), expected_sha256)
            directory.mkdir()
            self._save_bundle(directory, report={**REPORT, "cross_cover_total_variation_distance": 7e-16})
            return 0

        with patch(WORKER, side_effect=run) as worker:
            strict = self._run()
            tolerant = run_replay_regression(
                self.manifest, output_directory=self.root / "roundoff-results", probability_roundoff=True,
            )
        self.assertEqual(worker.call_count, 4)
        self.assertEqual(strict["status"], "REGRESSION_FAILED")
        self.assertNotIn("probability_roundoff", strict)
        self.assertEqual(tolerant["status"], "REGRESSION_MATCHED")
        self.assertIs(tolerant["probability_roundoff"], True)
        self.assertEqual(tolerant["planned_max_sampling_shots"], strict["planned_max_sampling_shots"])
        for row in tolerant["cases"]:
            comparison = row["comparison"]["sections"]
            self.assertTrue(comparison["request"]["exact_bytes_match"])
            self.assertTrue(comparison["selected_qasm"]["matches"])
            self.assertEqual(comparison["report"]["probability_roundoff"]["accepted_count"], 1)

    def test_child_rejects_changed_staged_request_before_replay(self):
        staged, receipt, log = (self.root / name for name in ("staged.json", "receipt", "stderr.log"))
        staged.write_bytes(REQUEST_BYTES + b" ")
        code = _run_worker(staged, receipt, log, 5, expected_sha256=sha256(REQUEST_BYTES).hexdigest())
        self.assertNotEqual(code, 0)
        self.assertFalse(receipt.exists())
        self.assertIn(b"staged request differs", log.read_bytes())

    def test_passes_explicit_worker_arguments_after_request_pin(self):
        staged, receipt, log = (self.root / name for name in ("staged.json", "receipt", "stderr.log"))
        staged.write_bytes(REQUEST_BYTES)
        pin = sha256(REQUEST_BYTES).hexdigest()
        extra = ("baseline directory", "f" * 64)
        with patch("austins_algorithm.replay_regression.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0)) as worker:
            code = _run_worker(staged, receipt, log, 5, expected_sha256=pin, worker_arguments=extra)
        self.assertEqual(code, 0)
        self.assertEqual(worker.call_args.args[0][-4:], [str(receipt), pin, *extra])

    def test_large_staged_request_does_not_block_worker_timeout(self):
        staged, receipt, log = (self.root / name for name in ("large.json", "receipt", "stderr.log"))
        staged.write_bytes(b" " * (64 * 1024))
        entry = "austins_algorithm.replay_regression._WORKER_ENTRY"
        started = monotonic()
        with patch(entry, "import time; time.sleep(10)"):
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_worker(staged, receipt, log, 1, expected_sha256="0" * 64)
        self.assertLess(monotonic() - started, 8)

    def test_rejects_duplicate_names_and_existing_output(self):
        self._write_manifest(second_id="FIRST")
        with patch(WORKER) as worker:
            with self.assertRaisesRegex(ValueError, "duplicate regression case id"):
                self._run()
            self.assertFalse(self.output.exists())
            self._write_manifest()
            self.output.mkdir()
            with self.assertRaises(FileExistsError):
                self._run()
            worker.assert_not_called()


class DiffuserTimingComparisonTests(unittest.TestCase):
    stable = DIFFUSER_SMOKE_ROOT / "baseline"
    native = DIFFUSER_SMOKE_ROOT / "baseline-native"
    zero_seconds = {
        "compile_export_verify": 0.0,
        "transpile": 0.0,
        "openqasm_export": 0.0,
        "openqasm_import": 0.0,
        "openqasm_verify": 0.0,
    }

    def test_diffuser_timings_are_opt_in_and_legacy_values_are_unavailable(self):
        from austins_algorithm.workflows_cli import main

        default = compare_diffuser_receipts(self.stable, self.stable)
        self.assertNotIn("compilation_timing", default)
        self.assertTrue(all("compilation_timing" not in row for row in default["variants"]))
        output = StringIO()
        with redirect_stdout(output):
            main(["diffuser-receipt-compare", str(self.stable), str(self.stable), "--timings"])
        compared = json.loads(output.getvalue())
        timing = compared["compilation_timing"]
        self.assertEqual(compared["status"], default["status"])
        self.assertEqual(compared["alignment"], default["alignment"])
        self.assertEqual(timing["availability"], "NO_PAIRED_TIMINGS")
        self.assertEqual(timing["paired_variants"], 0)
        self.assertIsNone(timing["delta_seconds"])
        for row in compared["variants"]:
            timing = row["compilation_timing"]
            self.assertEqual(timing["context_changes"], [])
            self.assertEqual(timing["left"]["availability"], "NOT_RECORDED")
            self.assertEqual(timing["right"]["availability"], "NOT_RECORDED")
            self.assertIsNone(timing["left"]["seconds"])
            self.assertIsNone(timing["delta_seconds"])

    def test_diffuser_timing_sums_use_paired_valid_observations_and_keep_zero(self):
        left = _read_verified_diffuser_receipt(self.stable)
        right = deepcopy(left)
        left_variants = left[1]["cells"][0]["variants"]
        right_variants = right[1]["cells"][0]["variants"]
        for variant in left_variants.values():
            variant["compilation_timing_seconds"] = dict(self.zero_seconds)
        right_variants["standard"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "compile_export_verify": 2.0, "openqasm_import": 1.0,
        }
        right_variants["clean-ancilla"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "compile_export_verify": 1.0, "transpile": 0.5,
        }
        right[1]["cells"][1]["variants"]["standard"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "openqasm_import": True,
        }
        right[1]["cells"][1]["variants"]["clean-ancilla"]["compilation_timing_seconds"] = None
        right[1]["cells"][2]["variants"]["standard"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "compile_export_verify": 10**400,
        }
        right[1]["cells"][2]["variants"]["clean-ancilla"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "openqasm_import": float("inf"),
        }
        right[1]["cells"][3]["variants"]["standard"]["compilation_timing_seconds"] = {
            **self.zero_seconds, "compile_export_verify": 1.0, "transpile": 2.0,
        }
        compared = _compare_verified_diffuser_bundles(left, right, include_timings=True)
        self.assertEqual(compared["status"], "DIFFUSER_RECEIPTS_MATCH")
        self.assertEqual(compared["alignment"]["matching_completed_batches"], 16)
        totals = compared["compilation_timing"]
        expected = {**self.zero_seconds, "compile_export_verify": 3.0, "transpile": 0.5, "openqasm_import": 1.0}
        self.assertEqual(totals["availability"], "PAIRED")
        self.assertEqual(totals["paired_variants"], 2)
        self.assertEqual(totals["matching_recorded_conditions"], 2)
        self.assertEqual(totals["different_recorded_conditions"], 0)
        self.assertEqual(totals["left_seconds"], self.zero_seconds)
        self.assertEqual(totals["right_seconds"], expected)
        self.assertEqual(totals["delta_seconds"], expected)
        by_variant = {(row["rounds"], row["diffuser"], tuple(row["physical_placement"])): row["compilation_timing"]
                      for row in compared["variants"]}
        measured = by_variant[(1, "standard", tuple(left[1]["placements"][0]))]
        self.assertEqual(measured["left"]["availability"], "RECORDED")
        self.assertEqual(measured["left"]["seconds"], self.zero_seconds)
        self.assertEqual(measured["delta_seconds"]["openqasm_import"], 1.0)
        invalid = [timing for timing in by_variant.values() if timing["right"]["availability"] == "INVALID"]
        self.assertEqual(len(invalid), 5)
        self.assertTrue(all(timing["delta_seconds"] is None for timing in invalid))

    def test_diffuser_timing_keeps_finite_rows_when_aggregate_is_out_of_range(self):
        left = _read_verified_diffuser_receipt(self.stable)
        right = deepcopy(left)
        for bundle in (left, right):
            for variant in bundle[1]["cells"][0]["variants"].values():
                variant["compilation_timing_seconds"] = {**self.zero_seconds, "compile_export_verify": 1e308}
        compared = _compare_verified_diffuser_bundles(left, right, include_timings=True)
        self.assertEqual(compared["status"], "DIFFUSER_RECEIPTS_MATCH")
        totals = compared["compilation_timing"]
        self.assertEqual(totals["availability"], "OUT_OF_RANGE")
        self.assertEqual(totals["paired_variants"], 2)
        for field in ("left_seconds", "right_seconds", "delta_seconds"):
            self.assertIsNone(totals[field])
        rows = [row["compilation_timing"] for row in compared["variants"]
                if row["compilation_timing"]["delta_seconds"] is not None]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["delta_seconds"] == self.zero_seconds for row in rows))

    def test_diffuser_timing_context_identifies_importer_without_different_export(self):
        left = _read_verified_diffuser_receipt(self.stable)
        right = _read_verified_diffuser_receipt(self.native)
        for bundle in (left, right):
            bundle[1]["cells"][0]["variants"]["standard"]["compilation_timing_seconds"] = dict(self.zero_seconds)
        compared = _compare_verified_diffuser_bundles(left, right, include_timings=True)
        self.assertEqual(compared["status"], "DIFFUSER_RECEIPTS_DIFFER")
        self.assertEqual(compared["alignment"]["completed_batches_in_different_conditions"], 16)
        totals = compared["compilation_timing"]
        self.assertEqual(totals["paired_variants"], 1)
        self.assertEqual(totals["matching_recorded_conditions"], 0)
        self.assertEqual(totals["different_recorded_conditions"], 1)
        measured = next(row for row in compared["variants"] if row["compilation_timing"]["delta_seconds"] is not None)
        timing = measured["compilation_timing"]
        self.assertIn("compiled_circuit", measured["context_changes"])
        self.assertCountEqual(timing["context_changes"], ("importer", "preferred_importer"))
        self.assertEqual(timing["left"]["importer"], "qiskit.qasm3.loads")
        self.assertIsNone(timing["left"]["preferred_importer"])
        self.assertEqual(timing["right"]["importer"], "qiskit.qasm3.loads_experimental")
        self.assertEqual(timing["right"]["preferred_importer"], "qiskit.qasm3.loads_experimental")

    def test_diffuser_timing_does_not_pair_variants_from_different_layouts(self):
        left = _read_verified_diffuser_receipt(self.stable)
        right = deepcopy(left)
        for bundle in (left, right):
            bundle[1]["cells"][0]["variants"]["standard"]["compilation_timing_seconds"] = dict(self.zero_seconds)
        right[1]["placements"][0] = [0, 1, 3, 2, 4]
        compared = _compare_verified_diffuser_bundles(left, right, include_timings=True)
        self.assertEqual(compared["alignment"]["shared_variants"], 4)
        self.assertEqual(compared["compilation_timing"]["availability"], "NO_PAIRED_TIMINGS")
        missing_layouts = [row["compilation_timing"] for row in compared["variants"]
                           if row["left"] is None or row["right"] is None]
        self.assertEqual(len(missing_layouts), 8)
        for timing in missing_layouts:
            self.assertIn("VARIANT_NOT_PRESENT", (timing["left"]["availability"], timing["right"]["availability"]))
            self.assertIsNone(timing["context_changes"])
            self.assertIsNone(timing["delta_seconds"])


if __name__ == "__main__":
    unittest.main()

