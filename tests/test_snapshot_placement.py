import json
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.snapshot_placement import (  # noqa: E402
    _alternate_placement,
    _evaluate_trials,
    run_mod4_placement_check,
)


IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))


class PlacementPolicyTests(unittest.TestCase):
    def test_alternate_is_a_single_move_preserving_connectivity(self):
        edges = [(0, 1), (1, 2), (2, 3), (3, 4)]
        self.assertEqual(_alternate_placement([2, 4, 1, 3], edges), [2, 0, 1, 3])
        self.assertIsNone(_alternate_placement([0, 1, 2, 3], edges[:3]))

    def test_requires_a_finite_floor_and_distinct_bounded_seeds(self):
        with self.assertRaisesRegex(ValueError, "minimum_hit_rate"):
            run_mod4_placement_check(minimum_hit_rate=float("nan"))
        with self.assertRaisesRegex(ValueError, "seeds must differ"):
            run_mod4_placement_check(minimum_hit_rate=0.4, evaluation_seeds=(29, 29))
        with self.assertRaisesRegex(ValueError, "placement check shots"):
            run_mod4_placement_check(minimum_hit_rate=0.4, shots=1025)

    def test_incomplete_trials_cannot_pass(self):
        result = _evaluate_trials([], expected_batches=2, confidence=0.99, floor=0.4)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIsNone(result["interval"])

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_exact_interval_can_pass_fail_or_remain_inconclusive(self):
        for hits, status in ((90, "MEETS_TARGET"), (10, "BELOW_TARGET"), (40, "INCONCLUSIVE")):
            with self.subTest(hits=hits):
                trial = {"status": "COMPLETED", "simulation": {"verified_shots": hits, "completed_shots": 100}}
                result = _evaluate_trials([trial, trial], expected_batches=2, confidence=0.99, floor=0.4)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["completed_shots"], 200)


@unittest.skipUnless(IBM_TOOLS, "optional IBM model tools are unavailable")
class PlacementSimulationTests(unittest.TestCase):
    def test_two_placements_two_round_choices_and_seeded_batches(self):
        result = run_mod4_placement_check(model="manila", minimum_hit_rate=0.4, shots=64)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(result["provider_job_submitted"])
        self.assertEqual(result["predeclared_cells"], 4)
        self.assertAlmostEqual(result["interval_confidence_per_cell"], 0.9875)
        model = result["models"]["manila"]
        a, b = model["placements"]
        self.assertEqual(len(set(a["initial_logical_to_target_qubit"]) & set(b["initial_logical_to_target_qubit"])), 3)
        for placement in (a, b):
            for record in placement["rounds"].values():
                self.assertEqual(record["compilation"]["initial_logical_to_target_qubit"],
                                 placement["initial_logical_to_target_qubit"])
                self.assertEqual([trial["seed"] for trial in record["trials"]], [29, 43])
                self.assertEqual(record["evaluation"]["completed_shots"], 128)
        json.dumps(result, allow_nan=False)

    def test_uncancelled_timeout_stops_remaining_batches_and_models(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": False, "simulation": None}
        with patch("austins_algorithm.snapshot_placement._sample_snapshot", return_value=timeout) as sample:
            result = run_mod4_placement_check(minimum_hit_rate=0.4, shots=2)
        sample.assert_called_once()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["predeclared_cells"], 12)
        self.assertEqual(result["models_not_run"], ["jakarta", "torino"])
        self.assertIsNone(result["models"]["manila"]["selected_rounds"])


if __name__ == "__main__":
    unittest.main()
