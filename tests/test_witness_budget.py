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
    check_witness_schedule,
    plan_witness_budget,
    plan_witness_schedule,
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


class WitnessScheduleInputTests(unittest.TestCase):
    def test_preflight_and_runtime_reject_the_same_invalid_seed_lists(self):
        for evaluation, validation, message in (
            ((11, 17, 23), VALIDATION_SEEDS, "four evaluation seeds"),
            ((11, 17, 23, 23), VALIDATION_SEEDS, "evaluation seeds must be distinct"),
            ((11, 17, 23, True), VALIDATION_SEEDS, "seed must be an integer"),
            ((11, 17, 23, 2**32), VALIDATION_SEEDS, "seed must be an integer"),
            (CALIBRATION_SEEDS, (41,), "even number of seeds"),
            (CALIBRATION_SEEDS, tuple(range(1000, 1258)), "even number of seeds"),
            (CALIBRATION_SEEDS, (41, 43, 47, 41), "validation seeds must be distinct"),
            (CALIBRATION_SEEDS, (41, -1), "validation seed must be an integer"),
            (CALIBRATION_SEEDS, (41, True), "validation seed must be an integer"),
            (CALIBRATION_SEEDS, (41, 2**32), "validation seed must be an integer"),
            (CALIBRATION_SEEDS, (41, 43, 47, 31), "differ from all calibration seeds"),
        ):
            with (
                self.subTest(evaluation=evaluation, validation=validation),
                patch("austins_algorithm.witness_budget.plan_witness_schedule") as plan,
                patch(f"{MODULE}._snapshot_specs") as specs,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    check_witness_schedule(
                        target_probability=0.95, evaluation_seeds=evaluation, validation_seeds=validation,
                    )
                with self.assertRaisesRegex(ValueError, message):
                    run_constraint_cover_snapshot(
                        CONSTRAINT, model="manila", initial_layout=LAYOUT, evaluation_seeds=evaluation,
                        witness_budget=WitnessBudgetRequest(0.95, validation_seeds=validation),
                    )
                plan.assert_not_called()
                specs.assert_not_called()

    def test_preflight_rejects_overlap_with_default_calibration_seeds(self):
        with self.assertRaisesRegex(ValueError, "differ from all calibration seeds"):
            check_witness_schedule(target_probability=0.95, validation_seeds=(1001, 181))

    def test_cli_requires_a_proposal_when_calibration_seeds_are_supplied(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from austins_algorithm.workflows_cli import main

        error = StringIO()
        with (
            patch("austins_algorithm.witness_budget.plan_witness_schedule") as plan,
            redirect_stderr(error),
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(["witness-schedule", "--target-probability", "0.95", "--evaluation-seeds", "1", "2", "3", "4"])
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("--evaluation-seeds requires --witness-seed-schedule", error.getvalue())
        plan.assert_not_called()

    def test_rejects_invalid_sizing_inputs_before_evaluating_bounds(self):
        for override in (
            {"target_probability": True}, {"target_probability": math.nan}, {"target_probability": math.inf},
            {"target_probability": 0}, {"target_probability": 1}, {"target_probability": "0.95"},
            {"tolerated_misses": False}, {"tolerated_misses": -1}, {"tolerated_misses": 129},
            {"tolerated_misses": 1.5}, {"max_batches_per_cover": True}, {"max_batches_per_cover": 0},
            {"max_batches_per_cover": 129}, {"max_batches_per_cover": 2.5},
        ):
            with (
                self.subTest(override=override),
                patch("austins_algorithm.witness_budget._witness_lower_bound") as bound,
                self.assertRaises(ValueError),
            ):
                plan_witness_schedule(**{"target_probability": 0.95, **override})
            bound.assert_not_called()


@unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
class WitnessSchedulePlanningTests(unittest.TestCase):
    def test_proposed_schedule_reports_minimum_shortfall_and_caps(self):
        for n, misses, cap, status, extra in (
            (71, 0, 128, "SCHEDULE_TOO_SHORT", 2),
            (72, 0, 128, "SCHEDULE_FEASIBLE", 0),
            (73, 0, 72, "SCHEDULE_EXCEEDS_CAP", None),
            (109, 1, 128, "SCHEDULE_TOO_SHORT", 2),
            (110, 1, 128, "SCHEDULE_FEASIBLE", 0),
            (128, 2, 128, "BATCH_CAP_EXCEEDED", None),
        ):
            with self.subTest(n=n, misses=misses, cap=cap):
                seeds = range(1001, 1001 + 2 * n)
                report = check_witness_schedule(
                    target_probability=0.95, validation_seeds=seeds, tolerated_misses=misses,
                    max_batches_per_cover=cap,
                )
                proposal = report.pop("proposed_schedule")
                self.assertEqual(report, plan_witness_schedule(
                    target_probability=0.95, tolerated_misses=misses, max_batches_per_cover=cap,
                ))
                self.assertEqual(proposal["status"], status)
                self.assertEqual(proposal["validation_batches_per_cover"], n)
                self.assertEqual(proposal["additional_validation_seeds"], extra)
                self.assertEqual(proposal["validation_seed_schedule"], {
                    "direct": list(seeds[::2]), "complement": list(seeds[1::2]),
                })
                batches = [_completed(8, 0)] * misses + [_completed(8, 1)] * (n - misses)
                expected = summarize_witness_batches(batches, planned_batches=n, target_probability=0.95)
                self.assertEqual(
                    proposal["lower_success_probability_at_proposed_size"], expected["lower_success_probability"],
                )

    def test_cli_preflight_preserves_interleaved_order_and_inclusive_seed_limits(self):
        import json
        from contextlib import redirect_stdout
        from io import StringIO

        from austins_algorithm.__main__ import main

        output = StringIO()
        with (
            patch.object(sys, "argv", [
                "austins-algorithm", "witness-schedule", "--target-probability", "0.1",
                "--witness-seed-schedule", "0", "4294967295", "8", "2",
            ]),
            patch(f"{MODULE}._snapshot_specs") as specs,
            patch(f"{MODULE}.compile_and_verify") as compile_circuit,
            patch(f"{MODULE}._sample_snapshot") as sample,
            redirect_stdout(output),
        ):
            main()
        specs.assert_not_called()
        compile_circuit.assert_not_called()
        sample.assert_not_called()
        proposal = json.loads(output.getvalue())["proposed_schedule"]
        self.assertEqual(proposal["status"], "SCHEDULE_FEASIBLE")
        self.assertEqual(proposal["evaluation_seeds"], {"direct": [167, 179], "complement": [173, 181]})
        self.assertEqual(proposal["validation_seed_schedule"], {"direct": [0, 8], "complement": [2**32 - 1, 2]})

    def test_minimum_matches_full_schedule_and_the_preceding_batch_count(self):
        for misses, minimum in ((0, 72), (1, 110)):
            with self.subTest(misses=misses):
                plan = plan_witness_schedule(target_probability=0.95, tolerated_misses=misses)
                self.assertEqual(plan["status"], "PLANNED")
                self.assertEqual(plan["minimum_batches_per_cover"], minimum)
                self.assertEqual(plan["minimum_validation_seeds"], 2 * minimum)

                batches = [_completed(8, 0)] * misses + [_completed(8, 1)] * (minimum - misses)
                completed = summarize_witness_batches(batches, planned_batches=minimum, target_probability=0.95)
                preceding = summarize_witness_batches(
                    batches[:-1], planned_batches=minimum - 1, target_probability=0.95,
                )
                self.assertEqual(completed["status"], "TARGET_SUPPORTED")
                self.assertEqual(preceding["status"], "UNRESOLVED")
                self.assertEqual(plan["lower_success_probability_at_minimum"], completed["lower_success_probability"])

                at_cap = plan_witness_schedule(
                    target_probability=0.95, tolerated_misses=misses, max_batches_per_cover=minimum,
                )
                self.assertEqual(at_cap["lower_success_probability_at_cap"], completed["lower_success_probability"])
                below = plan_witness_schedule(
                    target_probability=0.95, tolerated_misses=misses, max_batches_per_cover=minimum - 1,
                )
                self.assertEqual(below["status"], "BATCH_CAP_EXCEEDED")
                self.assertIsNone(below["minimum_batches_per_cover"])
                self.assertEqual(below["lower_success_probability_at_cap"], preceding["lower_success_probability"])

    def test_cap_report_distinguishes_impossible_sizes_from_feasible_counts(self):
        for misses, cap, expected_bound in ((2, 128, 0.94469740998), (8, 8, 0.0), (128, 1, 0.0)):
            with self.subTest(misses=misses, cap=cap):
                plan = plan_witness_schedule(
                    target_probability=0.95, tolerated_misses=misses, max_batches_per_cover=cap,
                )
                self.assertEqual(plan["status"], "BATCH_CAP_EXCEEDED")
                self.assertAlmostEqual(plan["lower_success_probability_at_cap"], expected_bound)
                self.assertIsNone(plan["minimum_batches_per_cover"])
                self.assertIsNone(plan["minimum_validation_seeds"])
                self.assertIsNone(plan["lower_success_probability_at_minimum"])

    def test_exact_equality_and_single_batch_edge(self):
        single = plan_witness_schedule(target_probability=0.01, max_batches_per_cover=1)
        self.assertEqual(single["minimum_batches_per_cover"], 1)
        self.assertEqual(single["minimum_validation_seeds"], 2)
        cutoff = single["lower_success_probability_at_cap"]
        equality = plan_witness_schedule(target_probability=cutoff, max_batches_per_cover=1)
        self.assertEqual(equality["status"], "PLANNED")
        above = plan_witness_schedule(target_probability=math.nextafter(cutoff, 1.0), max_batches_per_cover=1)
        self.assertEqual(above["status"], "BATCH_CAP_EXCEEDED")

    def test_package_cli_reports_sizing_before_compiling_or_sampling(self):
        import json
        from contextlib import redirect_stdout
        from io import StringIO

        from austins_algorithm.__main__ import main

        output = StringIO()
        with (
            patch.object(sys, "argv", [
                "austins-algorithm", "witness-schedule", "--target-probability", "0.95",
                "--tolerated-misses", "1", "--max-batches-per-cover", "110",
            ]),
            patch(f"{MODULE}._snapshot_specs") as specs,
            patch(f"{MODULE}.compile_and_verify") as compile_circuit,
            patch(f"{MODULE}._sample_snapshot") as sample,
            redirect_stdout(output),
        ):
            main()
        specs.assert_not_called()
        compile_circuit.assert_not_called()
        sample.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "PLANNED")
        self.assertEqual(report["minimum_validation_seeds"], 220)
        self.assertNotIn("proposed_schedule", report)


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

    def test_futility_is_opt_in_and_requires_a_boolean(self):
        self.assertFalse(WitnessBudgetRequest(0.95).stop_on_futility)
        with self.assertRaisesRegex(TypeError, "stop_on_futility"):
            WitnessBudgetRequest(0.95, stop_on_futility=1)

    def test_cli_futility_requires_witness_probability_and_snapshot_shots(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from austins_algorithm.workflows_cli import main

        for extra, message in (
            ([], "--witness-stop-on-futility requires --witness-probability"),
            (["--witness-probability", "0.95"], "--witness-probability requires --snapshot-shots"),
        ):
            error = StringIO()
            with (
                self.subTest(extra=extra),
                patch("austins_algorithm.constraints.load_constraint") as load,
                redirect_stderr(error),
                self.assertRaises(SystemExit) as exit_status,
            ):
                main([
                    "constraint-cover", "missing.json", "--ibm-model", "manila", "--initial-layout", "0", "1",
                    "--witness-stop-on-futility", *extra,
                ])
            load.assert_not_called()
            self.assertEqual(exit_status.exception.code, 1)
            self.assertIn(message, error.getvalue())

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

    def test_futility_stops_only_the_cover_that_cannot_reach_the_target(self):
        seeds = tuple(range(1001, 1145))

        def sample(*args, shots, seed, **kwargs):
            return _completed(shots, 12 if seed in CALIBRATION_SEEDS else int(seed != 1003))

        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            report = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32,
                evaluation_seeds=CALIBRATION_SEEDS,
                witness_budget=WitnessBudgetRequest(
                    0.95, max_shots=64, validation_seeds=seeds, stop_on_futility=True,
                ),
            ).report

        witness = report["witness_budget"]
        direct, complement = (witness["candidates"][name] for name in ("direct", "complement"))
        self.assertEqual(report["snapshot"]["status"], "COMPLETED")
        self.assertEqual(witness["status"], "STOPPED_FOR_FUTILITY")
        self.assertTrue(witness["stop_on_futility"])
        self.assertEqual(witness["validation_seed_schedule"]["direct"], list(seeds[::2]))
        self.assertEqual([call.kwargs["seed"] for call in sampler.call_args_list[4:]], [
            1001, 1003, *seeds[1::2],
        ])
        stop = direct["futility_stop"]
        self.assertEqual(stop["skipped_seeds"], list(seeds[4::2]))
        self.assertEqual(stop["observed_failed_batches"], 1)
        self.assertEqual(stop["best_case_successful_batches"], 71)
        self.assertAlmostEqual(stop["best_case_lower_success_probability"], 0.9250286891)
        self.assertEqual(direct["verified_witness"], "01")
        reliability = direct["batch_reliability"]
        self.assertEqual(reliability["status"], "STOPPED_FOR_FUTILITY")
        self.assertIsNone(reliability["lower_success_probability"])
        self.assertEqual(reliability["completed_batches"], 2)
        self.assertEqual(reliability["planned_batches"], 72)
        self.assertEqual(reliability["completed_shots"], 2 * direct["budget"]["shots_required"])
        self.assertEqual(complement["batch_reliability"]["status"], "TARGET_SUPPORTED")
        self.assertNotIn("futility_stop", complement)

    def test_futility_keeps_the_first_result_on_an_impossible_schedule(self):
        seeds = (41, 43, 47, 53)
        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", side_effect=lambda *args, shots, **kwargs: _completed(shots, 1))
            as sample,
        ):
            witness = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=4,
                witness_budget=WitnessBudgetRequest(0.95, validation_seeds=seeds, stop_on_futility=True),
            ).report["witness_budget"]

        self.assertEqual([call.kwargs["seed"] for call in sample.call_args_list[4:]], [41, 43])
        self.assertEqual(witness["status"], "STOPPED_FOR_FUTILITY")
        for row in witness["candidates"].values():
            self.assertEqual(row["verified_witness"], "01")
            self.assertEqual(row["futility_stop"]["observed_failed_batches"], 0)
            self.assertEqual(row["futility_stop"]["best_case_successful_batches"], 2)
            self.assertIsNone(row["batch_reliability"]["lower_success_probability"])

    def test_futility_keeps_equality_and_the_last_batch(self):
        seeds = tuple(range(1001, 1145))
        equality = summarize_witness_batches(
            [_completed(8, 1)] * 72, planned_batches=72, target_probability=0.95,
        )["lower_success_probability"]
        for target, miss, expected in (
            (equality, None, "TARGET_SUPPORTED"),
            (0.95, seeds[-2], "UNRESOLVED"),
        ):
            def sample(*args, shots, seed, **kwargs):
                return _completed(shots, 12 if seed in CALIBRATION_SEEDS else int(seed != miss))

            with (
                self.subTest(target=target, miss=miss),
                patch(f"{MODULE}._snapshot_simulator"),
                patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
            ):
                witness = run_constraint_cover_snapshot(
                    CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32,
                    evaluation_seeds=CALIBRATION_SEEDS,
                    witness_budget=WitnessBudgetRequest(target, validation_seeds=seeds, stop_on_futility=True),
                ).report["witness_budget"]
                self.assertEqual(sampler.call_count, 148)
                self.assertEqual(witness["status"], "COMPLETED")
                self.assertNotIn("futility_stop", witness["candidates"]["direct"])
                self.assertEqual(witness["candidates"]["direct"]["batch_reliability"]["status"], expected)

    def test_confirmed_timeout_preserves_partial_status_and_the_cover_schedule(self):
        seeds = (41, 43, 47, 53, 59, 61)
        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": True,
            "simulation": None,
            "timing_seconds": {},
        }

        def sample(*args, shots, seed, **kwargs):
            return timeout if seed == 41 else _completed(shots, 1)

        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            witness = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=4,
                witness_budget=WitnessBudgetRequest(0.95, validation_seeds=seeds, stop_on_futility=True),
            ).report["witness_budget"]

        self.assertEqual([call.kwargs["seed"] for call in sampler.call_args_list[4:]], [41, 47, 59, 43])
        self.assertEqual(witness["status"], "PARTIAL_VALIDATION")
        direct, complement = (witness["candidates"][name] for name in ("direct", "complement"))
        self.assertNotIn("futility_stop", direct)
        self.assertEqual(direct["batch_reliability"]["status"], "INCOMPLETE")
        self.assertEqual(direct["batch_reliability"]["completed_batches"], 2)
        self.assertEqual(complement["batch_reliability"]["status"], "STOPPED_FOR_FUTILITY")

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
                    stop_on_futility=True,
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

