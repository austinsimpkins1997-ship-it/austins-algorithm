import json
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.constraint_diffuser_comparison import run_constraint_diffuser_comparison  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402


MODULE = "austins_algorithm.constraint_diffuser_comparison"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))
CONSTRAINT = BooleanConstraint(4, ((1, 2),))
PLACEMENTS = ((0, 1, 2, 3, 4), (4, 3, 2, 1, 0))
SEEDS = (11, 17, 23, 31)


def _completed(shots, hits):
    return {
        "status": "COMPLETED",
        "simulation": {
            "requested_shots": shots,
            "completed_shots": shots,
            "verified_shots": hits,
            "verified_hit_rate": hits / shots,
            "counts": {"0000": shots - hits, "0001": hits},
        },
        "timing_seconds": {},
    }


class ConstraintDiffuserInputTests(unittest.TestCase):
    def test_invalid_or_duplicate_seeds_rejected_before_oracle_build(self):
        for seeds in (
            None, "1234", (11, 17, 23), (11, 17, 23, 11),
            (11, 17, 23, True), (11, 17, 23, -1), (11, 17, 23, 2**32),
        ):
            with self.subTest(seeds=seeds), patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
                with self.assertRaises(ValueError):
                    run_constraint_diffuser_comparison(
                        CONSTRAINT, model="manila", placements=PLACEMENTS, evaluation_seeds=seeds,
                    )
                synthesize.assert_not_called()


@unittest.skipUnless(IBM_TOOLS, "optional IBM model tools are unavailable")
class ConstraintDiffuserComparisonTests(unittest.TestCase):
    def test_compiles_each_arm_once_and_summarizes_only_matched_cells(self):
        from scipy.stats import binomtest

        hits_by_first_wire = {
            0: {11: 2, 17: 27, 23: 4, 31: 29},
            4: {11: 27, 17: 2, 23: 29, 31: 4},
        }

        def sample(program, backend, simulator, *, method, shots, seed, verifier):
            self.assertTrue(verifier("0001"))
            self.assertFalse(verifier("0000"))
            first_wire = program.report["initial_logical_to_target_qubit"][0]
            return _completed(shots, hits_by_first_wire[first_wire][seed])

        with patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler:
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                shots=32, evaluation_seeds=SEEDS,
            )

        self.assertEqual(report["status"], "COMPLETED")
        self.assertNotIn("selected_candidate", report)
        self.assertAlmostEqual(report["confidence_per_variant"], 0.99375)
        self.assertEqual(
            [(cell["placement"], cell["rounds"]) for cell in report["cells"]],
            [(1, 1), (1, 2), (2, 1), (2, 2)],
        )
        self.assertEqual(sampler.call_count, 16)
        self.assertEqual(len({id(call.args[0]) for call in sampler.call_args_list}), 8)
        self.assertTrue(all(call.kwargs["shots"] == 32 for call in sampler.call_args_list))

        for cell in report["cells"]:
            placement = PLACEMENTS[cell["placement"] - 1]
            hits_by_seed = hits_by_first_wire[placement[0]]
            for variant, expected_seeds in (("standard", (11, 23)), ("clean-ancilla", (17, 31))):
                row = cell["variants"][variant]
                self.assertEqual(row["compilation"]["initial_logical_to_target_qubit"],
                                 list(placement))
                self.assertEqual(tuple(batch["seed"] for batch in row["batches"]), expected_seeds)
                self.assertEqual([batch["simulation"]["verified_shots"] for batch in row["batches"]],
                                 [hits_by_seed[seed] for seed in expected_seeds])
                self.assertEqual(row["summary"]["completed_shots"], 64)
                self.assertEqual(row["summary"]["verified_shots"], sum(hits_by_seed[seed] for seed in expected_seeds))
            standard_hits = hits_by_seed[11] + hits_by_seed[23]
            clean_hits = hits_by_seed[17] + hits_by_seed[31]
            standard_ci = binomtest(standard_hits, 64).proportion_ci(confidence_level=0.99375, method="exact")
            clean_ci = binomtest(clean_hits, 64).proportion_ci(confidence_level=0.99375, method="exact")
            difference = cell["difference"]
            self.assertAlmostEqual(difference["estimate"], (clean_hits - standard_hits) / 64)
            self.assertAlmostEqual(difference["interval"][0], clean_ci.low - standard_ci.high)
            self.assertAlmostEqual(difference["interval"][1], clean_ci.high - standard_ci.low)
            self.assertEqual(difference["status"], "IMPROVED" if cell["placement"] == 1 else "DEGRADED")
        json.dumps(report, allow_nan=False)

    def test_unconfirmed_cancellation_stops_remaining_batches(self):
        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": False,
            "simulation": None,
            "timing_seconds": {},
        }
        with patch(f"{MODULE}._sample_snapshot", side_effect=[_completed(2, 1), timeout]) as sampler:
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags", shots=2,
            )

        self.assertEqual(sampler.call_count, 2)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(report["local_cancellation_unconfirmed"])
        self.assertEqual(len(report["cells"]), 4)
        self.assertTrue(all(cell["difference"] is None for cell in report["cells"]))
        batches = [
            batch for cell in report["cells"]
            for variant in cell["variants"].values()
            for batch in variant["batches"]
        ]
        self.assertCountEqual([batch["status"] for batch in batches], ["COMPLETED", timeout["status"]])


if __name__ == "__main__":
    unittest.main()

