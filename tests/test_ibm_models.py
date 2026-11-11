import json
import sys
import unittest
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.core import austin_predicate  # noqa: E402
from austins_algorithm.grover_search import mod4_range_predicate  # noqa: E402
from austins_algorithm.ibm_models import (  # noqa: E402
    run_ibm_snapshot_comparison,
    run_mod4_round_sweep,
    run_mod4_snapshot_comparison,
)


IBM_MODEL_DEPENDENCIES = all(
    find_spec(module) is not None
    for module in ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import")
)


class SnapshotInputTests(unittest.TestCase):
    def test_round_sweep_rejects_reused_seeds_and_out_of_bounds_shots(self):
        with self.assertRaisesRegex(ValueError, "seeds must differ"):
            run_mod4_round_sweep(calibration_seed=19, validation_seed=19)
        with self.assertRaisesRegex(ValueError, "round sweep shots"):
            run_mod4_round_sweep(shots=1025)

    def test_rejects_unknown_model_and_invalid_limits_before_optional_imports(self):
        for values in (
            {"model": "fake_manila"}, {"shots": 0}, {"shots": 4097},
            {"seed": -1}, {"seed": 2**32}, {"shots": True},
        ):
            for runner in (run_ibm_snapshot_comparison, run_mod4_snapshot_comparison):
                with self.subTest(values=values, runner=runner.__name__), self.assertRaises(ValueError):
                    runner(**values)


@unittest.skipUnless(IBM_MODEL_DEPENDENCIES, "optional IBM model tools are unavailable")
class SnapshotComparisonTests(unittest.TestCase):
    def test_all_models_preserve_the_ideal_circuit_and_verify_noisy_shots(self):
        report = run_ibm_snapshot_comparison(shots=64)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(report["classical"]["solutions"], ["011", "101"])
        self.assertEqual(report["ideal"]["verified_shots"], 64)
        self.assertEqual(set(report["models"]), {"manila", "jakarta", "torino"})
        for model, expected_method in (
            ("manila", "density_matrix"), ("jakarta", "density_matrix"), ("torino", "matrix_product_state"),
        ):
            with self.subTest(model=model):
                details = report["models"][model]
                self.assertEqual(details["status"], "COMPLETED")
                self.assertIsNotNone(datetime.fromisoformat(details["snapshot"]["last_update_date"]).tzinfo)
                self.assertFalse(details["provider_job_submitted"])
                self.assertFalse(details["quantum_hardware_used"])
                compilation = details["compilation"]
                self.assertEqual(compilation["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
                self.assertGreater(compilation["compiled"]["two_qubit_gates"], 0)
                simulation = details["simulation"]
                self.assertEqual(simulation["method"], expected_method)
                self.assertIn("measure", simulation["noise_instructions"])
                self.assertLessEqual(simulation["simulated_qubits"], report["limits"]["active_qubits"])
                self.assertEqual(simulation["simulated_qubits"], len(details["active_target_qubits"]))
                self.assertEqual(simulation["completed_shots"], 64)
                self.assertEqual(
                    simulation["verified_shots"],
                    sum(count for bits, count in simulation["counts"].items() if austin_predicate(bits)),
                )
                lo, hi = simulation["verified_hit_rate_wilson_95"]
                self.assertLessEqual(lo, simulation["verified_hit_rate"])
                self.assertLessEqual(simulation["verified_hit_rate"], hi)
        self.assertGreater(report["models"]["torino"]["compilation"]["target"]["qubits"], 7)
        self.assertEqual(report["models"]["torino"]["simulation"]["max_bond_dimension"], 8)
        json.dumps(report, allow_nan=False)

    def test_active_wire_cap_keeps_compilation_and_skips_simulation(self):
        with patch("austins_algorithm.ibm_models.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 2):
            report = run_ibm_snapshot_comparison(model="manila", shots=2)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(list(report["models"]), ["manila"])
        self.assertEqual(report["models"]["manila"]["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        self.assertIsNone(report["models"]["manila"]["simulation"])

    def test_mod4_compares_both_implementations_under_one_snapshot(self):
        report = run_mod4_snapshot_comparison(model="jakarta", shots=64)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(report["grover_rounds"], 2)
        self.assertEqual(report["oracle_audits"]["direct"], report["oracle_audits"]["work-qubit"])
        self.assertEqual(report["oracle_audits"]["direct"]["marked_states"], ["1001", "1101"])
        model = report["models"]["jakarta"]
        reference, direct = (model["variants"][name] for name in ("work-qubit", "direct"))
        self.assertEqual(reference["snapshot"], direct["snapshot"])
        self.assertLess(model["ideal_roundtrip_total_variation_distance"], 1e-10)
        self.assertEqual(reference["compilation"]["input"]["qubits"], 5)
        self.assertEqual(direct["compilation"]["input"]["qubits"], 4)
        for result in (reference, direct):
            sample = result["simulation"]
            self.assertEqual(sample["completed_shots"], 64)
            self.assertEqual(
                sample["verified_shots"],
                sum(count for bits, count in sample["counts"].items() if mod4_range_predicate(bits)),
            )
        self.assertAlmostEqual(
            model["direct_minus_work_qubit_hit_rate"],
            direct["simulation"]["verified_hit_rate"] - reference["simulation"]["verified_hit_rate"],
        )
        json.dumps(report, allow_nan=False)

    def test_mod4_partial_comparison_omits_hit_rate_change(self):
        with patch("austins_algorithm.ibm_models.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 4):
            report = run_mod4_snapshot_comparison(model="manila", shots=2)
        self.assertEqual(report["status"], "PARTIAL")
        model = report["models"]["manila"]
        self.assertEqual(model["variants"]["work-qubit"]["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        self.assertEqual(model["variants"]["direct"]["status"], "COMPLETED")
        self.assertIsNone(model["direct_minus_work_qubit_hit_rate"])

    def test_round_sweep_holds_placement_and_uses_fresh_validation_shots(self):
        report = run_mod4_round_sweep(model="jakarta", shots=64)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["ideal_rounds"], 2)
        self.assertEqual(report["round_candidates"], [0, 1, 2, 3])
        self.assertFalse(report["provider_job_submitted"])
        result = report["models"]["jakarta"]
        self.assertEqual(set(result["calibration"]), {0, 1, 2, 3})
        self.assertEqual(set(result["validation"]), {result["selected_rounds"], 2})
        for compiled in result["compilations"].values():
            self.assertEqual(
                compiled["initial_logical_to_target_qubit"], result["initial_logical_to_target_qubit"]
            )
        for stage in ("calibration", "validation"):
            for trial in result[stage].values():
                self.assertEqual(trial["simulation"]["completed_shots"], 64)
        json.dumps(report, allow_nan=False)

    def test_round_selection_does_not_change_after_validation(self):
        def sample(hits):
            return {"status": "COMPLETED", "simulation": {"verified_shots": hits, "verified_hit_rate": hits / 64}}

        with patch("austins_algorithm.ibm_models._sample_snapshot", side_effect=map(sample, (10, 40, 40, 20, 1, 50))) as run:
            report = run_mod4_round_sweep(model="manila", shots=64, calibration_seed=11, validation_seed=23)
        model = report["models"]["manila"]
        self.assertEqual(model["selected_rounds"], 1)
        self.assertLess(model["validation_selected_minus_ideal_hit_rate"], 0)
        calls = run.call_args_list
        self.assertEqual([call.kwargs["seed"] for call in calls], [11, 11, 11, 11, 23, 23])
        self.assertIs(calls[1].args[0], calls[4].args[0])
        self.assertIs(calls[2].args[0], calls[5].args[0])

    def test_round_sweep_does_not_launch_more_work_after_uncancelled_timeout(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None, "local_job_cancelled": False}
        with patch("austins_algorithm.ibm_models._sample_snapshot", return_value=timeout) as run:
            report = run_mod4_round_sweep(shots=2)
        run.assert_called_once()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["models_not_run"], ["jakarta", "torino"])
        model = report["models"]["manila"]
        self.assertIsNone(model["selected_rounds"])
        self.assertIsNone(model["validation_selected_minus_ideal_hit_rate"])


if __name__ == "__main__":
    unittest.main()
