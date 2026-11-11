import json
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.constraint_snapshot_sweep import run_constraint_snapshot_sweep  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402


MODULE = "austins_algorithm.constraint_snapshot_sweep"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import",
))
CONSTRAINT = BooleanConstraint(2, ((1,), (2,)))
PLACEMENTS = ((0, 1), (2, 3))


class ConstraintSnapshotInputTests(unittest.TestCase):
    def test_rejects_reused_shot_seeds_before_oracle_construction(self):
        with patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
            with self.assertRaises(ValueError):
                run_constraint_snapshot_sweep(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, calibration_seed=11, validation_seed=11,
                )
        synthesize.assert_not_called()

    def test_rejects_invalid_placement_structure_before_oracle_construction(self):
        for placements in (
            ((0, 1),),
            ((0, 1), (0, 1)),
            ((0, 1), (1, 1)),
            ((0, 1), (2, True)),
            ((0, 1), (2, -1)),
        ):
            with self.subTest(placements=placements), patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
                with self.assertRaises(ValueError):
                    run_constraint_snapshot_sweep(CONSTRAINT, model="manila", placements=placements)
                synthesize.assert_not_called()


@unittest.skipUnless(IBM_TOOLS, "optional IBM model tools are unavailable")
class ConstraintSnapshotSweepTests(unittest.TestCase):
    def test_manila_compares_all_candidates_under_equal_budgets_and_fresh_seeds(self):
        shots = 8
        result = run_constraint_snapshot_sweep(
            CONSTRAINT, model="manila", placements=PLACEMENTS, shots=shots, calibration_seed=11, validation_seed=23,
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(result["provider_job_submitted"])
        self.assertFalse(result["quantum_hardware_used"])

        candidates = result["candidates"]
        self.assertEqual(
            [(candidate["placement"], candidate["rounds"]) for candidate in candidates],
            [(placement, rounds) for placement in (1, 2) for rounds in (0, 1, 2)],
        )
        for candidate in candidates:
            with self.subTest(placement=candidate["placement"], rounds=candidate["rounds"]):
                self.assertEqual(
                    candidate["compilation"]["initial_logical_to_target_qubit"],
                    list(PLACEMENTS[candidate["placement"] - 1]),
                )
                self.assertAlmostEqual(candidate["ideal_verified_hit_rate"], (0.25, 1.0, 0.25)[candidate["rounds"]])
                for stage, seed in (("calibration", 11), ("validation", 23)):
                    sample = candidate[stage]
                    self.assertEqual(sample["seed"], seed)
                    self.assertEqual(sample["status"], "COMPLETED")
                    counts = sample["simulation"]
                    self.assertEqual(counts["requested_shots"], shots)
                    self.assertEqual(counts["completed_shots"], shots)
                    self.assertEqual(counts["verified_shots"], counts["counts"].get("11", 0))

        expected = min(candidates, key=lambda candidate: (
            -candidate["calibration"]["simulation"]["verified_shots"],
            candidate["compilation"]["compiled"]["two_qubit_gates"],
            candidate["compilation"]["compiled"]["depth"],
            candidate["rounds"],
            candidate["placement"],
        ))
        self.assertEqual(result["selected_candidate"], {
            "placement": expected["placement"],
            "rounds": expected["rounds"],
        })
        json.dumps(result, allow_nan=False)

    def test_calibration_selects_before_validation_and_uses_compiled_cost_for_ties(self):
        trials = iter((1, 4, 1, 4, 0, 0, 1, 4, 1, 0, 0, 0))

        def simulate(compiled, backend, simulator, *, method, shots, seed, verifier):
            hits = next(trials)
            return {
                "status": "COMPLETED",
                "simulation": {
                    "requested_shots": shots,
                    "completed_shots": shots,
                    "verified_shots": hits,
                    "verified_hit_rate": hits / shots,
                    "counts": {"00": shots - hits, "11": hits},
                },
                "timing_seconds": {},
            }

        with patch(f"{MODULE}._sample_snapshot", side_effect=simulate) as sample:
            result = run_constraint_snapshot_sweep(
                CONSTRAINT, model="manila", placements=PLACEMENTS, shots=4, calibration_seed=13, validation_seed=29,
            )
        self.assertEqual([call.kwargs["seed"] for call in sample.call_args_list], [13] * 6 + [29] * 6)
        for calibration, validation in zip(sample.call_args_list[:6], sample.call_args_list[6:]):
            self.assertIs(calibration.args[0], validation.args[0])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["selected_candidate"], {"placement": 2, "rounds": 0})
        self.assertEqual(result["candidates"][3]["validation"]["simulation"]["verified_shots"], 0)

    def test_proven_empty_constraint_stops_before_loading_a_snapshot(self):
        constraint = BooleanConstraint(2, ((1,), (-1,)))
        with patch(f"{MODULE}._snapshot_specs") as snapshot_specs:
            result = run_constraint_snapshot_sweep(constraint, model="manila", placements=PLACEMENTS, shots=2)
        snapshot_specs.assert_not_called()
        self.assertEqual(result["status"], "PROVEN_EMPTY")
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["selected_candidate"])

    def test_width_limit_keeps_compilation_evidence_and_skips_all_sampling(self):
        with (
            patch(f"{MODULE}.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 1),
            patch(f"{MODULE}._sample_snapshot") as sample,
        ):
            result = run_constraint_snapshot_sweep(CONSTRAINT, model="manila", placements=PLACEMENTS, shots=2)
        sample.assert_not_called()
        self.assertEqual(result["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        self.assertEqual(len(result["candidates"]), 6)
        for candidate in result["candidates"]:
            self.assertGreater(len(candidate["active_target_qubits"]), 1)
            self.assertIsNone(candidate["calibration"])
            self.assertIsNone(candidate["validation"])

    def test_unconfirmed_cancellation_stops_remaining_calibration_and_validation(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None, "local_job_cancelled": False}
        with patch(f"{MODULE}._sample_snapshot", return_value=timeout) as sample:
            result = run_constraint_snapshot_sweep(CONSTRAINT, model="manila", placements=PLACEMENTS, shots=2)
        sample.assert_called_once()
        self.assertEqual(result["status"], "PARTIAL_CALIBRATION")
        self.assertTrue(result["local_cancellation_unconfirmed"])
        self.assertIsNone(result["selected_candidate"])
        self.assertEqual(result["candidates"][0]["calibration"]["status"], timeout["status"])
        self.assertTrue(all(candidate["calibration"] is None for candidate in result["candidates"][1:]))
        self.assertTrue(all(candidate["validation"] is None for candidate in result["candidates"]))


if __name__ == "__main__":
    unittest.main()
