"""Check conservative shot planning and independent witness-budget validation."""

from __future__ import annotations

import math
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.compilation import compile_and_verify  # noqa: E402
from austins_algorithm.constraint_cover_comparison import run_constraint_cover_snapshot  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402
from austins_algorithm.witness_budget import (  # noqa: E402
    WitnessBudgetRequest,
    plan_witness_budget,
    summarize_witness_batches,
)


MODULE = "austins_algorithm.constraint_cover_comparison"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))
CONSTRAINT = BooleanConstraint(2, ((1, 2), (-1, -2)))
LAYOUT = (0, 1)
CALIBRATION_SEEDS = (11, 17, 23, 31)
VALIDATION_SEEDS = (41, 43)


class WitnessBudgetPlanningTests(unittest.TestCase):
    def test_returns_the_smallest_feasible_integer_budget(self):
        exact = plan_witness_budget(0.5, target_probability=0.75, max_shots=4)
        higher = plan_witness_budget(0.5, target_probability=math.nextafter(0.75, 1), max_shots=4)
        self.assertEqual(exact["status"], "PLANNED")
        self.assertEqual(exact["shots_required"], 2)
        self.assertAlmostEqual(exact["success_probability_at_budget"], 0.75)
        self.assertAlmostEqual(exact["success_probability_at_cap"], 0.9375)
        self.assertEqual(higher["shots_required"], 3)

        result = plan_witness_budget(0.2, target_probability=0.95, max_shots=64)
        self.assertEqual(result["shots_required"], 14)
        self.assertGreaterEqual(result["success_probability_at_budget"], 0.95)
        self.assertLess(1 - 0.8 ** (result["shots_required"] - 1), 0.95)

    def test_reports_an_exactly_attainable_probability_without_rounding_below_it(self):
        result = plan_witness_budget(0.25, target_probability=0.4375, max_shots=32)
        self.assertEqual(result["shots_required"], 2)
        self.assertEqual(result["success_probability_at_budget"], 0.4375)
        higher = plan_witness_budget(0.25, target_probability=math.nextafter(0.4375, 1), max_shots=32)
        self.assertEqual(higher["shots_required"], 3)

    def test_preserves_the_probability_bound_when_cap_is_insufficient(self):
        result = plan_witness_budget(0.2, target_probability=0.95, max_shots=13)
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertIsNone(result["shots_required"])
        self.assertIsNone(result["success_probability_at_budget"])
        self.assertAlmostEqual(result["success_probability_at_cap"], 1 - 0.8 ** 13)

        tiny = plan_witness_budget(math.nextafter(0, 1), target_probability=0.95, max_shots=256)
        self.assertEqual(tiny["status"], "BUDGET_EXCEEDED")
        self.assertGreater(tiny["success_probability_at_cap"], 0)
        self.assertIsNone(tiny["shots_required"])

    def test_distinguishes_certain_zero_and_missing_bounds(self):
        certain = plan_witness_budget(1, target_probability=0.999, max_shots=10)
        self.assertEqual(certain["status"], "PLANNED")
        self.assertEqual(certain["shots_required"], 1)
        self.assertEqual(certain["success_probability_at_budget"], 1)
        self.assertEqual(certain["success_probability_at_cap"], 1)

        zero = plan_witness_budget(0, target_probability=0.95, max_shots=10)
        self.assertEqual(zero["status"], "NO_POSITIVE_LOWER_BOUND")
        self.assertIsNone(zero["shots_required"])
        self.assertIsNone(zero["success_probability_at_budget"])
        self.assertEqual(zero["success_probability_at_cap"], 0)

        unknown = plan_witness_budget(None, target_probability=0.95, max_shots=10)
        self.assertEqual(unknown["status"], "UNASSESSED")
        self.assertIsNone(unknown["shots_required"])
        self.assertIsNone(unknown["success_probability_at_budget"])
        self.assertIsNone(unknown["success_probability_at_cap"])

    def test_invalid_probability_and_cap_inputs_are_rejected(self):
        for lower, target, cap in (
            (False, 0.95, 8),
            (-0.1, 0.95, 8),
            (1.01, 0.95, 8),
            (math.nan, 0.95, 8),
            (math.inf, 0.95, 8),
            (0.5, True, 8),
            (0.5, 0, 8),
            (0.5, 1, 8),
            (0.5, math.nan, 8),
            (0.5, math.inf, 8),
            (0.5, 0.95, 0),
            (0.5, 0.95, True),
            (0.5, 0.95, 1.5),
        ):
            with self.subTest(lower=lower, target=target, cap=cap), self.assertRaises(ValueError):
                plan_witness_budget(lower, target_probability=target, max_shots=cap)



def _completed(shots: int, hits: int) -> dict:
    return {
        "status": "COMPLETED",
        "simulation": {
            "requested_shots": shots,
            "completed_shots": shots,
            "verified_shots": hits,
            "verified_hit_rate": hits / shots,
            "verified_candidates": ["01"] if hits else [],
            "counts": {"00": shots - hits, "01": hits},
        },
        "timing_seconds": {"sample": 0.01, "verify": 0.001},
    }


class WitnessBatchReliabilityTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_one_sided_bound_distinguishes_all_success_and_a_miss(self):
        for planned, successful, status in (
            (71, 71, "UNRESOLVED"),
            (72, 72, "TARGET_SUPPORTED"),
            (72, 71, "UNRESOLVED"),
        ):
            with self.subTest(planned=planned, successful=successful):
                batches = [
                    _completed(8, 3 if index < successful else 0)
                    for index in range(planned)
                ]
                result = summarize_witness_batches(batches, planned_batches=planned, target_probability=0.95)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["planned_batches"], planned)
                self.assertEqual(result["completed_batches"], planned)
                self.assertEqual(result["successful_batches"], successful)
                self.assertEqual(result["observed_success_fraction"], successful / planned)
                self.assertEqual(result["completed_shots"], 8 * planned)
                if successful == planned:
                    self.assertAlmostEqual(result["lower_success_probability"], 0.025 ** (1 / planned))
                if status == "TARGET_SUPPORTED":
                    self.assertGreaterEqual(result["lower_success_probability"], 0.95)
                else:
                    self.assertLess(result["lower_success_probability"], 0.95)

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_complete_zero_hits_have_a_zero_lower_bound(self):
        result = summarize_witness_batches(
            [_completed(8, 0)] * 4, planned_batches=4, target_probability=0.95,
        )
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["completed_batches"], 4)
        self.assertEqual(result["successful_batches"], 0)
        self.assertEqual(result["observed_success_fraction"], 0)
        self.assertEqual(result["lower_success_probability"], 0)
        self.assertEqual(result["completed_shots"], 32)

    def test_timeout_and_unstarted_batch_leave_reliability_incomplete(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None}
        result = summarize_witness_batches(
            [_completed(8, 3), timeout, _completed(8, 0)],
            planned_batches=4,
            target_probability=0.95,
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["planned_batches"], 4)
        self.assertEqual(result["completed_batches"], 2)
        self.assertEqual(result["successful_batches"], 1)
        self.assertIsNone(result["lower_success_probability"])
        self.assertEqual(result["completed_shots"], 16)


class WitnessBudgetInputTests(unittest.TestCase):
    def test_accepts_a_bounded_even_interleaved_seed_vector(self):
        seeds = list(range(256))
        request = WitnessBudgetRequest(target_probability=0.95, validation_seeds=seeds)
        seeds[0] = 999
        self.assertEqual(request.validation_seeds, tuple(range(256)))

    def test_rejects_malformed_repeated_seed_vectors(self):
        for seeds in (
            (),
            (41,),
            (41, 43, 47),
            (41, 43, 41, 53),
            (41, 43, 47, True),
            (41, 43, 47, 2**32),
            tuple(range(258)),
        ):
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                WitnessBudgetRequest(target_probability=0.95, validation_seeds=seeds)

    def test_later_validation_seed_cannot_reuse_calibration(self):
        with patch(f"{MODULE}._snapshot_specs") as specs:
            with self.assertRaises(ValueError):
                run_constraint_cover_snapshot(
                    CONSTRAINT, model="manila", initial_layout=LAYOUT,
                    evaluation_seeds=CALIBRATION_SEEDS,
                    witness_budget=WitnessBudgetRequest(
                        target_probability=0.95, validation_seeds=(41, 43, 47, 11),
                    ),
                )
        specs.assert_not_called()

    def test_calibration_seed_cannot_be_reused_for_validation(self):
        with patch(f"{MODULE}._snapshot_specs") as specs:
            with self.assertRaises(ValueError):
                run_constraint_cover_snapshot(
                    CONSTRAINT, model="manila", initial_layout=LAYOUT,
                    evaluation_seeds=CALIBRATION_SEEDS,
                    witness_budget=WitnessBudgetRequest(
                        target_probability=0.95, max_shots=64, validation_seeds=(11, 43),
                    ),
                )
        specs.assert_not_called()


@unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
class WitnessBudgetSnapshotTests(unittest.TestCase):
    def test_fresh_validation_uses_the_same_compiled_circuits(self):
        programs = {}
        compiler_backend = None
        simulator = object()

        def compile_one(circuit, **kwargs):
            nonlocal compiler_backend
            if compiler_backend is None:
                compiler_backend = kwargs["backend"]
            self.assertIs(kwargs["backend"], compiler_backend)
            program = compile_and_verify(circuit, **kwargs)
            cover = ("direct", "complement")[len(programs)]
            programs[cover] = program
            return program

        def sample(compiled, backend, sampler, *, method, shots, seed, verifier):
            self.assertIs(backend, compiler_backend)
            self.assertIs(sampler, simulator)
            self.assertTrue(verifier("01"))
            self.assertFalse(verifier("00"))
            if seed in CALIBRATION_SEEDS:
                cover = "direct" if seed in CALIBRATION_SEEDS[::2] else "complement"
                self.assertIs(compiled, programs[cover])
                self.assertEqual(shots, 32)
                return _completed(shots, 12)
            self.assertIn(seed, VALIDATION_SEEDS)
            cover = ("direct", "complement")[VALIDATION_SEEDS.index(seed)]
            self.assertIs(compiled, programs[cover])
            return _completed(shots, 1 if cover == "direct" else 0)

        with (
            patch(f"{MODULE}.compile_and_verify", side_effect=compile_one) as compiler,
            patch(f"{MODULE}._snapshot_simulator", return_value=simulator) as create,
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32,
                evaluation_seeds=CALIBRATION_SEEDS,
                witness_budget=WitnessBudgetRequest(
                    target_probability=0.95, max_shots=64, validation_seeds=VALIDATION_SEEDS,
                ),
            )

        self.assertEqual(compiler.call_count, 2)
        create.assert_called_once()
        self.assertEqual(sampler.call_count, 6)
        self.assertEqual(
            [call.kwargs["seed"] for call in sampler.call_args_list],
            [*CALIBRATION_SEEDS[::2], *CALIBRATION_SEEDS[1::2], *VALIDATION_SEEDS],
        )
        self.assertIs(result.selected, programs[result.report["selected_cover"]])
        snapshot = result.report["snapshot"]
        self.assertEqual(snapshot["status"], "COMPLETED")
        budget = result.report["witness_budget"]
        self.assertEqual(budget["status"], "COMPLETED")
        self.assertEqual(budget["classical_reference"]["candidate"], "01")
        for stage in ("compile_and_audit", "calibration", "holdout_validation", "workflow_wall"):
            self.assertGreaterEqual(budget["timing_seconds"][stage], 0)
        for cover, seed in zip(("direct", "complement"), VALIDATION_SEEDS):
            with self.subTest(cover=cover):
                self.assertIs(result.programs[cover], programs[cover])
                row = budget["candidates"][cover]
                self.assertEqual(row["budget"]["status"], "PLANNED")
                self.assertEqual(row["validation"]["seed"], seed)
                self.assertEqual(row["validation"]["status"], "COMPLETED")
                shots = row["budget"]["shots_required"]
                self.assertEqual(row["validation"]["simulation"]["completed_shots"], shots)
                self.assertEqual(row["verified_witness"], "01" if cover == "direct" else None)
                gates = programs[cover].report["compiled"]["two_qubit_gates"]
                self.assertEqual(row["nominal_two_qubit_gate_executions"], shots * gates)

    def test_repeated_validation_reuses_each_program_and_freezes_its_shots(self):
        validation_seeds = (41, 43, 47, 53, 59, 61)
        cover_seeds = {"direct": validation_seeds[::2], "complement": validation_seeds[1::2]}
        misses = {41, 53}

        def sample(compiled, backend, simulator, *, method, shots, seed, verifier):
            if seed in CALIBRATION_SEEDS:
                return _completed(shots, 12)
            self.assertIn(seed, validation_seeds)
            return _completed(shots, 0 if seed in misses else 1)

        with (
            patch(f"{MODULE}.compile_and_verify", wraps=compile_and_verify) as compiler,
            patch(f"{MODULE}._snapshot_simulator") as create,
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32,
                evaluation_seeds=CALIBRATION_SEEDS,
                witness_budget=WitnessBudgetRequest(
                    target_probability=0.95, max_shots=64, validation_seeds=validation_seeds,
                ),
            )

        self.assertEqual(compiler.call_count, 2)
        create.assert_called_once()
        self.assertEqual(sampler.call_count, 10)
        budget = result.report["witness_budget"]
        self.assertEqual(budget["status"], "COMPLETED")
        self.assertIn("validation_seed_schedule", budget)
        holdout_calls = sampler.call_args_list[4:]
        for cover, seeds in cover_seeds.items():
            with self.subTest(cover=cover):
                row = budget["candidates"][cover]
                shots = row["budget"]["shots_required"]
                batches = row["validation_batches"]
                self.assertEqual(tuple(batch["seed"] for batch in batches), seeds)
                self.assertEqual(row["validation"], batches[0])
                self.assertEqual(row["verified_witness"], "01")
                calls = [call for call in holdout_calls if call.kwargs["seed"] in seeds]
                self.assertEqual(len(calls), 3)
                for call in calls:
                    self.assertIs(call.args[0], result.programs[cover])
                    self.assertEqual(call.kwargs["shots"], shots)
                summary = row["batch_reliability"]
                self.assertEqual(summary["planned_batches"], 3)
                self.assertEqual(summary["completed_batches"], 3)
                self.assertEqual(summary["successful_batches"], 2)
                self.assertEqual(summary["observed_success_fraction"], 2 / 3)
                self.assertEqual(summary["completed_shots"], 3 * shots)
                self.assertEqual(summary["status"], "UNRESOLVED")
                gates = result.programs[cover].report["compiled"]["two_qubit_gates"]
                self.assertEqual(row["completed_nominal_two_qubit_gate_executions"], 3 * shots * gates)

    def test_infeasible_budget_skips_validation(self):
        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", return_value=_completed(16, 1)) as sample,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=16,
                evaluation_seeds=CALIBRATION_SEEDS,
                witness_budget=WitnessBudgetRequest(
                    target_probability=0.95, max_shots=1, validation_seeds=VALIDATION_SEEDS,
                ),
            )
        self.assertEqual(sample.call_count, 4)
        budget = result.report["witness_budget"]
        self.assertEqual(budget["status"], "NO_FEASIBLE_BUDGET")
        for row in budget["candidates"].values():
            self.assertEqual(row["budget"]["status"], "BUDGET_EXCEEDED")
            self.assertIsNone(row["validation"])
            self.assertIsNone(row["verified_witness"])

    def test_unconfirmed_validation_cancellation_stops_other_cover(self):
        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": False,
            "simulation": None,
            "timing_seconds": {},
        }
        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", side_effect=[_completed(32, 12)] * 4 + [timeout]) as sample,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32,
                evaluation_seeds=CALIBRATION_SEEDS,
                witness_budget=WitnessBudgetRequest(
                    target_probability=0.95, max_shots=64, validation_seeds=VALIDATION_SEEDS,
                ),
            )

        self.assertEqual(sample.call_count, 5)
        self.assertEqual(sample.call_args.kwargs["seed"], VALIDATION_SEEDS[0])
        budget = result.report["witness_budget"]
        self.assertEqual(budget["status"], "PARTIAL_VALIDATION")
        direct, complement = (budget["candidates"][cover] for cover in ("direct", "complement"))
        self.assertEqual(direct["validation"]["status"], timeout["status"])
        self.assertIsNone(direct["verified_witness"])
        self.assertIsNone(complement["validation"])
        self.assertIsNone(complement["verified_witness"])


if __name__ == "__main__":
    unittest.main()

