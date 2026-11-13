import json
import math
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from qiskit.primitives import StatevectorSampler
except Exception as exc:
    StatevectorSampler = None
    QISKIT_IMPORT_ERROR = repr(exc)
else:
    QISKIT_IMPORT_ERROR = ""

from austins_algorithm import device_experiment
from austins_algorithm.device_experiment import (
    IBMHardwareRun,
    austin_classical_baseline,
    run_austin_device_comparison,
    summarize_austin_counts,
    summarize_search_counts,
)


class DeviceInputsTests(unittest.TestCase):
    def test_invalid_physical_inputs_fail_before_access(self):
        for invalid_seconds in (False, 0, 10801):
            with self.subTest(max_execution_seconds=invalid_seconds), self.assertRaises(ValueError):
                IBMHardwareRun("ibm_example", "saved-account", "instance", invalid_seconds)
        with self.assertRaises(ValueError):
            IBMHardwareRun("ibm_example", "saved-account", "auto", 30)
        hardware = IBMHardwareRun("ibm_example", "saved-account", "instance", 30)
        with patch.object(device_experiment, "_load_ibm_runtime") as load, self.assertRaises(ValueError):
            run_austin_device_comparison(shots=4097, hardware=hardware)
        load.assert_not_called()
        with patch.object(device_experiment, "_load_ibm_runtime") as load, self.assertRaises(ValueError):
            run_austin_device_comparison(qpu_preflight_only=True)
        load.assert_not_called()

    def test_preflight_cli_requires_explicit_target_fields(self):
        from austins_algorithm.workflows_cli import main

        for extra, message in (([], "requires --backend"), (["--ibm-model", "manila"], "cannot be combined")):
            with (
                self.subTest(extra=extra),
                patch.object(device_experiment, "run_austin_device_comparison") as run,
                redirect_stderr(StringIO()) as errors,
                self.assertRaises(SystemExit) as exit_info,
            ):
                main(["compare", "--qpu-preflight", *extra])
            self.assertEqual(exit_info.exception.code, 1)
            self.assertIn(message, errors.getvalue())
            run.assert_not_called()

    def test_count_verification_uses_observed_shots(self):
        result = summarize_austin_counts({"011": 2, "001": 1}, requested_shots=4)
        self.assertEqual(result["requested_shots"], 4)
        self.assertEqual(result["completed_shots"], 3)
        self.assertEqual(result["verified_candidates"], ["011"])
        self.assertEqual(result["verified_shots"], 2)
        self.assertAlmostEqual(result["verified_hit_rate"], 2 / 3)
        for counts in ({"001": 5}, {"1": 2}, {"001": True}, {"011": -1}, {}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                summarize_austin_counts(counts, requested_shots=4)

    def test_count_verification_orders_candidates_and_detaches_validated_counts(self):
        original = {"101": 2, "010": 1, "011": 3}
        counts = MappingProxyType(original)
        seen = []

        def verify(bits):
            seen.append(bits)
            original["011"] = -1
            return bits != "010"

        report = summarize_search_counts(counts, requested_shots=8, width=3, verifier=verify)
        self.assertEqual(seen, ["010", "011", "101"])
        self.assertEqual(list(report["counts"].items()), [("010", 1), ("011", 3), ("101", 2)])
        self.assertEqual(report["verified_candidates"], ["011", "101"])
        self.assertEqual(report["verified_shots"], 5)
        self.assertEqual(report["verified_hit_rate"], 5 / 6)

    def test_invalid_count_maps_fail_before_verifier_calls(self):
        class NonbinaryIterator(str):
            def __iter__(self):
                return iter("0x1")

        cases = (
            ({"011": 2, "0x1": 1}, "measurement"),
            ({"011": 2, 123: 1}, "measurement"),
            ({NonbinaryIterator("011"): 2}, "measurement"),
            ({"011": 2, "101": True}, "count"),
            ({"011": 2, "101": 2}, "completed shots"),
        )
        for counts, message in cases:
            with (
                self.subTest(counts=counts),
                patch.object(device_experiment, "austin_predicate") as verify,
                self.assertRaisesRegex(ValueError, message),
            ):
                summarize_search_counts(counts, requested_shots=3, width=3, verifier=verify)
            verify.assert_not_called()

    def test_direct_classical_solution_matches_all_eight_inputs(self):
        result = austin_classical_baseline()
        complete = [
            f"{n:03b}" for n in range(8) if device_experiment.austin_predicate(f"{n:03b}")
        ]
        self.assertEqual(result["solutions"], complete)
        self.assertEqual(result["predicate_verifications"], 2)
        self.assertGreaterEqual(result["wall_seconds"], 0)


@unittest.skipIf(StatevectorSampler is None, f"Qiskit optional extra unavailable: {QISKIT_IMPORT_ERROR}")
class LocalExperimentTests(unittest.TestCase):
    def test_synthetic_comparison_matches_theoretical_endpoints(self):
        with patch.object(device_experiment, "_load_ibm_runtime") as load:
            ideal = run_austin_device_comparison(shots=32, seed=7, noise_probability=0)
            mixed = run_austin_device_comparison(shots=32, seed=7, noise_probability=1)
        load.assert_not_called()
        self.assertEqual(ideal["hardware"]["status"], "NOT_REQUESTED")
        self.assertEqual(ideal["ideal"]["verified_shots"], 32)
        self.assertEqual(ideal["noisy"]["verified_shots"], 32)
        self.assertEqual(ideal["noisy"]["calibration"], "NONE_SYNTHETIC")
        self.assertAlmostEqual(ideal["noisy"]["model_hit_probability"], 1.0)
        self.assertAlmostEqual(mixed["noisy"]["model_hit_probability"], 0.25)
        self.assertEqual(mixed["noisy"]["completed_shots"], 32)
        self.assertTrue(
            all(math.isclose(p, 0.125) for p in mixed["noisy"]["model_probabilities"].values())
        )


@unittest.skipIf(StatevectorSampler is None, f"Qiskit optional extra unavailable: {QISKIT_IMPORT_ERROR}")
class HardwareBoundaryTests(unittest.TestCase):
    def setUp(self):
        class Backend:
            name = "ibm_example"
            is_mock = False
            simulator = False
            local = False
            operational = True

            def status(self):
                return SimpleNamespace(operational=self.operational)

        self.backend_type = Backend
        self.backend = Backend()
        self.hardware = IBMHardwareRun("ibm_example", "saved-account", "instance", 30, 5)
        self.calls = {}
        self.result_error = None
        self.submit_error = None
        self.bad_budget = False
        self.account_missing = False
        self.account_file_present = True
        self.compile_error = False

    def _fake_runtime(self):
        test = self

        class Job:
            def job_id(self):
                return "test-job"

            def result(self, *, timeout):
                test.calls["timeout"] = timeout
                if test.result_error:
                    raise test.result_error
                answer = SimpleNamespace(get_counts=lambda: {"011": 2, "001": 1})
                return [SimpleNamespace(data=SimpleNamespace(answer=answer))]

            def backend(self, *, timeout):
                return test.backend

            def metrics(self):
                return {
                    "usage": {"status": "completed", "qpu_charge_time_seconds": 1.25},
                    "timestamps": {
                        "created": "2026-11-11T12:00:00Z",
                        "running": "2026-11-11T12:00:04Z",
                        "finished": "2026-11-11T12:00:06Z",
                    },
                }

        class Service:
            is_local = False

            def __init__(self, **kwargs):
                test.calls["service"] = kwargs
                if test.account_missing:
                    raise AccountError("account details")

            def backend(self, name, *, instance):
                test.calls["backend"] = (name, instance)
                return test.backend

        class Sampler:
            def __init__(self, *, mode, options):
                test.calls["sampler"] = (mode, options)
                self.options = SimpleNamespace(
                    max_execution_time=999 if test.bad_budget else options["max_execution_time"]
                )

            def run(self, circuits, *, shots, dry_run):
                test.calls["submit"] = (len(circuits), shots, dry_run)
                test.calls["submitted_circuit"] = circuits[0]
                if test.submit_error:
                    raise test.submit_error
                return Job()

        class WaitError(Exception):
            pass

        class LimitError(Exception):
            pass

        class JobError(Exception):
            pass

        class StateError(Exception):
            pass

        class AccountError(Exception):
            pass

        return Service, self.backend_type, Sampler, WaitError, LimitError, JobError, StateError, AccountError

    def _run(self, *, preflight_only=False):
        fake = self._fake_runtime()

        def compile_for_device(circuit, **kwargs):
            self.calls["compile"] = (circuit.num_qubits, kwargs)
            if self.compile_error:
                raise ValueError("provider details")
            self.calls["checked_circuit"] = circuit.copy()
            return SimpleNamespace(circuit=self.calls["checked_circuit"], timing_seconds={"transpile": 0.0},
                                   report={"status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION"})

        with (
            patch.object(device_experiment, "_load_ibm_runtime", return_value=fake),
            patch.object(device_experiment, "_ibm_account_file",
                         return_value=Mock(is_file=Mock(return_value=self.account_file_present))),
            patch.object(device_experiment, "compile_and_verify", side_effect=compile_for_device),
        ):
            return run_austin_device_comparison(
                shots=4, hardware=self.hardware, qpu_preflight_only=preflight_only,
            )["hardware"]

    def test_one_bounded_submission_reports_measured_partial_results(self):
        result = self._run()
        self.assertEqual(self.calls["service"]["name"], "saved-account")
        self.assertEqual(self.calls["backend"], ("ibm_example", "instance"))
        self.assertEqual(self.calls["compile"], (
            3, {"backend": self.backend, "optimization_level": 1, "seed": 7}
        ))
        self.assertEqual(self.calls["sampler"][1]["max_execution_time"], 30)
        self.assertEqual(self.calls["submit"], (1, 4, False))
        self.assertIs(self.calls["submitted_circuit"], self.calls["checked_circuit"])
        self.assertEqual(self.calls["timeout"], 5)
        self.assertEqual(result["status"], "PARTIAL_RESULTS")
        self.assertEqual(result["actual_backend"], "ibm_example")
        self.assertEqual(result["job_id"], "test-job")
        self.assertTrue(result["provider_job_submitted"])
        self.assertTrue(result["quantum_hardware_used"])
        self.assertEqual(result["completed_shots"], 3)
        self.assertEqual(result["verified_candidates"], ["011"])
        self.assertEqual(result["provider_reported_qpu_seconds"], 1.25)
        self.assertEqual(result["provider_reported_queue_seconds"], 4)
        self.assertEqual(result["provider_reported_running_seconds"], 2)
        self.assertIsNone(result["provider_reported_monetary_cost"])

    def test_simulator_and_unconfirmed_budget_are_rejected_before_submission(self):
        self.backend.simulator = True
        self.assertEqual(self._run()["status"], "NON_QPU_OR_WRONG_BACKEND")
        self.backend.simulator = False
        self.bad_budget = True
        self.assertEqual(self._run()["status"], "QPU_BUDGET_NOT_CONFIGURED")
        self.assertNotIn("submit", self.calls)

    def test_preflight_verifies_target_then_returns_before_sampler(self):
        result = self._run(preflight_only=True)
        self.assertEqual(result["status"], "TARGET_COMPILED_AND_VERIFIED")
        self.assertEqual(result["verified_target"], "ibm_example")
        self.assertEqual(result["compilation"]["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
        self.assertFalse(result["provider_job_submitted"])
        self.assertFalse(result["quantum_hardware_used"])
        self.assertNotIn("sampler", self.calls)
        self.assertNotIn("submit", self.calls)

    def test_missing_account_and_nonoperational_target_fail_before_compilation(self):
        self.account_file_present = False
        self.assertEqual(self._run(preflight_only=True)["status"], "IBM_SAVED_PROFILE_UNAVAILABLE")
        self.assertNotIn("service", self.calls)
        self.account_file_present = True
        self.account_missing = True
        result = self._run(preflight_only=True)
        self.assertEqual(result["status"], "IBM_SAVED_PROFILE_UNAVAILABLE")
        self.assertNotIn("account details", str(result))
        self.assertNotIn("backend", self.calls)
        self.account_missing = False
        self.backend.operational = False
        result = self._run()
        self.assertEqual(result["status"], "BACKEND_NOT_OPERATIONAL")
        self.assertNotIn("compile", self.calls)
        self.assertNotIn("submit", self.calls)

    def test_failed_circuit_verification_holds_submission(self):
        self.compile_error = True
        result = self._run()
        self.assertEqual(result["status"], "PROVIDER_STAGE_FAILED")
        self.assertEqual(result["failed_stage"], "compile_and_verify")
        self.assertFalse(result["provider_job_submitted"])
        self.assertNotIn("provider details", str(result))
        self.assertNotIn("sampler", self.calls)
        self.assertNotIn("submit", self.calls)

    def test_cli_reports_missing_profile_with_error_exit(self):
        from austins_algorithm.workflows_cli import main

        self.account_missing = True
        command = ["compare", "--qpu-preflight", "--shots", "4", "--backend", "ibm_example", "--account",
                   "saved-account", "--instance", "instance", "--max-execution-seconds", "30"]
        with (
            patch.object(device_experiment, "_load_ibm_runtime", return_value=self._fake_runtime()),
            patch.object(device_experiment, "_ibm_account_file", return_value=Mock(is_file=Mock(return_value=True))),
            redirect_stdout(StringIO()) as output,
            self.assertRaises(SystemExit) as exit_info,
        ):
            main(command)
        self.assertEqual(exit_info.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["hardware"]["status"], "IBM_SAVED_PROFILE_UNAVAILABLE")
        self.assertNotIn("submit", self.calls)

    def test_wait_timeout_preserves_id_and_unknown_execution(self):
        self.result_error = TimeoutError()
        result = self._run()
        self.assertEqual(result["status"], "WAIT_TIMED_OUT")
        self.assertEqual(result["job_id"], "test-job")
        self.assertTrue(result["provider_job_submitted"])
        self.assertIsNone(result["quantum_hardware_used"])

    def test_ambiguous_submission_has_no_invented_job_state_or_details(self):
        self.submit_error = RuntimeError("provider details")
        result = self._run()
        self.assertEqual(result["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(result["failed_stage"], "submit")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIsNone(result["job_id"])
        self.assertIsNone(result["provider_job_submitted"])
        self.assertNotIn("provider details", str(result))


if __name__ == "__main__":
    unittest.main()
