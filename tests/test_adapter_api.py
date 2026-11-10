import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.adapter_api import (  # noqa: E402
    AdapterContractError,
    BackendAdapter,
    BackendCapabilities,
    ExecutionResult,
    ExperimentRequest,
    WorkloadArtifact,
    check_compatibility,
)
from austins_algorithm.local_adapter import (  # noqa: E402
    AustinCpuAdapter,
    PYTHON_PREDICATE_FORMAT,
    PythonPredicateWorkload,
)


class FakeAdapter:
    """Offline example: this test adapter never connects to a service."""

    def capabilities(self):
        return BackendCapabilities(
            adapter_id="test.fake",
            adapter_version="1.0",
            supported_formats=("openqasm3",),
            execution_modes=("SIMULATOR",),
            max_width=8,
            supports_shots=True,
            supports_seed=True,
            supports_async_jobs=True,
        )

    def submit(self, request):
        return ExecutionResult(
            status="QUEUED",
            execution_mode="SIMULATOR",
            adapter_id="test.fake",
            adapter_version="1.0",
            job_id="test-job",
            shots_requested=request.shots,
        )

    def get_result(self, job_id):
        return ExecutionResult(
            status="COMPLETED",
            execution_mode="SIMULATOR",
            adapter_id="test.fake",
            adapter_version="1.0",
            job_id=job_id,
            shots_requested=16,
            shots_completed=16,
            counts={"011": 8, "101": 8},
            measurement_order="q2 q1 q0",
            timings_seconds={"execution": 0.01},
            timing_sources={"execution": "CLIENT_MONOTONIC"},
        )

    def cancel(self, job_id):
        return ExecutionResult(
            status="CANCELLED",
            execution_mode="SIMULATOR",
            adapter_id="test.fake",
            adapter_version="1.0",
            job_id=job_id,
        )


class AdapterApiTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = FakeAdapter().capabilities()
        self.request = ExperimentRequest(
            WorkloadArtifact("openqasm3", "OPENQASM 3.0;", logical_width=3),
            requested_execution_mode="SIMULATOR",
            shots=16,
            seed=7,
        )

    def test_fake_adapter_matches_open_contract_without_external_work(self):
        adapter = FakeAdapter()
        self.assertIsInstance(adapter, BackendAdapter)
        queued = adapter.submit(self.request)
        result = adapter.get_result(queued.job_id)
        self.assertEqual(queued.status, "QUEUED")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(sum(result.counts.values()), result.shots_completed)
        self.assertEqual(result.measurement_order, "q2 q1 q0")
        self.assertEqual(result.schema, "ChronoRealmQuantumExecutionResult/1")

    def test_compatible_request_returns_no_warnings(self):
        self.assertEqual(check_compatibility(self.request, self.capabilities), ())

    def test_unreported_limits_stay_unknown_and_are_returned_as_warnings(self):
        capabilities = BackendCapabilities(
            adapter_id="test.unknown-limits",
            adapter_version="0",
            supported_formats=("openqasm3",),
            execution_modes=("UNKNOWN",),
        )
        warnings = check_compatibility(self.request, capabilities)
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all("did not report" in warning for warning in warnings))

    def test_declared_mode_format_width_shots_and_seed_limits_are_enforced(self):
        bad_requests = (
            ExperimentRequest(WorkloadArtifact("other-format", "payload"), "SIMULATOR"),
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload", logical_width=9), "SIMULATOR"),
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload"), "SIMULATOR", shots=1),
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload"), "SIMULATOR", seed=1),
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload"), "QPU"),
        )
        limited = BackendCapabilities(
            adapter_id="test.limited",
            adapter_version="1",
            supported_formats=("openqasm3",),
            execution_modes=("SIMULATOR",),
            max_width=8,
            supports_shots=False,
            supports_seed=False,
        )
        for request in bad_requests:
            with self.subTest(request=request), self.assertRaises(AdapterContractError):
                check_compatibility(request, limited)

    def test_inputs_and_outputs_reject_malformed_values(self):
        with self.assertRaises(AdapterContractError):
            WorkloadArtifact("", "payload")
        with self.assertRaises(AdapterContractError):
            WorkloadArtifact("openqasm3", None)
        with self.assertRaises(AdapterContractError):
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload"), "SIMULATOR", shots=0)
        with self.assertRaises(AdapterContractError):
            ExperimentRequest(WorkloadArtifact("openqasm3", "payload"), "UNKNOWN")
        with self.assertRaises(AdapterContractError):
            ExecutionResult("MAGIC_SUCCESS", "QPU", "test.fake", "1")
        with self.assertRaises(AdapterContractError):
            ExecutionResult(
                "COMPLETED",
                "SIMULATOR",
                "test.fake",
                "1",
                timings_seconds={"queue": 1.0},
            )
        with self.assertRaises(TypeError):
            ExperimentRequest(
                WorkloadArtifact("openqasm3", "payload"),
                requested_execution_mode="SIMULATOR",
                api_key="secret",
            )

    def test_local_adapter_returns_honestly_labeled_cpu_results(self):
        workload = PythonPredicateWorkload(lambda bits: bits.startswith("11"), "starts_with_11")
        request = ExperimentRequest(
            WorkloadArtifact(PYTHON_PREDICATE_FORMAT, workload, logical_width=4),
            requested_execution_mode="SIMULATOR",
            shots=32,
            seed=7,
        )
        report = AustinCpuAdapter().submit(request)
        self.assertEqual(report.status, "COMPLETED")
        self.assertEqual(report.execution_mode, "SIMULATOR")
        self.assertEqual(report.measurement_order, "MSB-first display bitstrings; basis index is the displayed binary integer")
        self.assertEqual(set(report.counts), {"1100", "1101", "1110", "1111"})
        self.assertEqual(report.shots_completed, 32)
        self.assertEqual(report.result_payload["status"], "CLASSICAL_IDEAL_STATEVECTOR_SIMULATION")
        self.assertFalse(report.result_payload["execution"]["quantum_hardware_used"])
        self.assertEqual(report.timing_sources, {"total": "CLIENT_MONOTONIC"})


if __name__ == "__main__":
    unittest.main()
