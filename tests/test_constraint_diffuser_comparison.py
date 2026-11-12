import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from importlib.util import find_spec
from io import StringIO
from math import comb, fsum, isfinite, nextafter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.constraint_diffuser_comparison import (  # noqa: E402
    _plan_modeled_witness,
    _search_modeled_witness_schedules,
    plan_diffuser_witness_schedule,
    run_constraint_diffuser_comparison,
)
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402
from austins_algorithm.workflows_cli import main as workflow_main  # noqa: E402


MODULE = "austins_algorithm.constraint_diffuser_comparison"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))
CONSTRAINT = BooleanConstraint(4, ((1, 2),))
PLACEMENTS = ((0, 1, 2, 3, 4), (4, 3, 2, 1, 0))
SEEDS = (11, 17, 23, 31)
COMPILATION_TIMING_PHASES = ("compile_export_verify", "transpile", "openqasm_export", "openqasm_import", "openqasm_verify")


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


def _completed_witness_sample(*args, shots, **kwargs):
    from austins_algorithm.device_experiment import summarize_search_counts

    counts = summarize_search_counts(
        {"0001": shots}, requested_shots=shots, width=CONSTRAINT.qubits, verifier=CONSTRAINT.matches,
    )
    return {"status": "COMPLETED", "simulation": {"execution_mode": "SIMULATOR", "method": "density_matrix", **counts}}


class ConstraintDiffuserInputTests(unittest.TestCase):
    def test_invalid_or_duplicate_seeds_rejected_before_oracle_build(self):
        for seeds in (
            None, "1234", (11, 17, 23), (11, 17, 23, 11),
            (11, 17, 23, True), (11, 17, 23, -1), (11, 17, 23, 2**32),
            (11, 17, 23, 31, 37), tuple(range(18)),
        ):
            with self.subTest(seeds=seeds), patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
                with self.assertRaises(ValueError):
                    run_constraint_diffuser_comparison(
                        CONSTRAINT, model="manila", placements=PLACEMENTS, evaluation_seeds=seeds,
                    )
                synthesize.assert_not_called()


    def test_diffuser_schedule_rejects_invalid_seeds_before_statistics(self):
        for seeds in ((41,), (41, 43, 41, 47), (41, True), (41, 2**32), range(258)):
            with (
                self.subTest(seeds=seeds),
                patch(f"{MODULE}._minimum_witness_schedule") as size,
                self.assertRaises(ValueError),
            ):
                plan_diffuser_witness_schedule(target_probability=0.95, validation_seeds=seeds)
            size.assert_not_called()

    def test_diffuser_schedule_cli_rejects_cover_calibration_options(self):
        with (
            patch(f"{MODULE}.plan_diffuser_witness_schedule") as plan,
            redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as error,
        ):
            workflow_main([
                "witness-schedule", "--comparison", "diffuser", "--target-probability", "0.95",
                "--evaluation-seeds", "11", "17", "23", "31",
            ])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("--evaluation-seeds is only used for the phase-cover comparison", stderr.getvalue())
        plan.assert_not_called()

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_diffuser_preflight_shares_holdout_confidence_and_reports_caps(self):
        for n, misses, cap, minimum, status, extra in (
            (98, 0, 128, 99, "SCHEDULE_TOO_SHORT", 2),
            (99, 0, 128, 99, "SCHEDULE_FEASIBLE", 0),
            (128, 1, 128, None, "BATCH_CAP_EXCEEDED", None),
            (100, 1, 98, None, "SCHEDULE_EXCEEDS_CAP", None),
        ):
            with self.subTest(n=n, misses=misses, cap=cap):
                seeds = (0, 2**32 - 1, *range(1001, 1001 + 2 * n - 2))
                plan = plan_diffuser_witness_schedule(
                    target_probability=0.95, tolerated_misses=misses, max_batches_per_variant=cap,
                    validation_seeds=seeds,
                )
                self.assertEqual(plan["family_variant_count"], 8)
                self.assertEqual(plan["batch_family_confidence"], 0.95)
                self.assertEqual(plan["confidence_per_variant"], 0.99375)
                self.assertEqual(plan["minimum_batches_per_variant"], minimum)
                self.assertEqual(plan["minimum_validation_seeds"], None if minimum is None else 2 * minimum)
                self.assertEqual(plan["max_sampling_batches"], 8 * cap)
                self.assertFalse(plan["local_sampling_performed"])
                proposal = plan["proposed_schedule"]
                self.assertEqual(proposal["status"], status)
                self.assertEqual(proposal["additional_validation_seeds"], extra)
                self.assertEqual(proposal["sampling_batch_ceiling"], 8 * n)
                self.assertEqual(proposal["validation_seed_schedule"], {
                    "standard": list(seeds[::2]), "clean-ancilla": list(seeds[1::2]),
                })
                if misses == 0:
                    self.assertAlmostEqual(proposal["lower_success_probability_at_proposed_size"], 0.00625 ** (1 / n))

    def test_validation_power_rejects_invalid_options_before_statistics(self):
        for options in (
            {"minimum_validation_power": 0.8},
            {"assumed_batch_success_probability": True},
            {"assumed_batch_success_probability": float("nan")},
            {"assumed_batch_success_probability": -0.01},
            {"assumed_batch_success_probability": 1.01},
            {"assumed_batch_success_probability": 0.9, "minimum_validation_power": 0.0},
            {"assumed_batch_success_probability": 0.9, "minimum_validation_power": 1.01},
        ):
            with (
                self.subTest(options=options),
                patch(f"{MODULE}._minimum_witness_schedule") as size,
                self.assertRaises(ValueError),
            ):
                plan_diffuser_witness_schedule(target_probability=0.95, **options)
            size.assert_not_called()
        with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as error:
            workflow_main([
                "witness-schedule", "--target-probability", "0.95", "--assumed-batch-success-probability", "0.99",
            ])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("validation power options require --comparison diffuser", stderr.getvalue())

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_validation_power_search_can_find_a_smaller_schedule_when_cap_misses_power(self):
        report = plan_diffuser_witness_schedule(
            target_probability=0.95, validation_seeds=range(196), assumed_batch_success_probability=0.99,
            minimum_validation_power=0.35,
        )
        power = report["validation_power"]
        self.assertEqual(power["status"], "MINIMUM_POWER_MET")
        self.assertEqual(power["at_sizing_minimum"], power["best_within_cap"])
        self.assertEqual(power["at_sizing_minimum"], power["smallest_meeting_minimum_power"])
        self.assertEqual(power["best_within_cap"]["required_successful_batches"], 99)
        self.assertEqual(power["best_within_cap"]["validation_seed_count"], 198)
        self.assertAlmostEqual(power["best_within_cap"]["probability_of_target_support"], 0.99**99)
        self.assertTrue(power["best_within_cap"]["meets_minimum_validation_power"])
        self.assertEqual(power["at_cap"]["required_successful_batches"], 128)
        self.assertAlmostEqual(power["at_cap"]["probability_of_target_support"], 0.99**128)
        self.assertFalse(power["at_cap"]["meets_minimum_validation_power"])
        self.assertIsNone(power["at_proposed_schedule"]["required_successful_batches"])
        self.assertEqual(power["at_proposed_schedule"]["probability_of_target_support"], 0.0)

        infeasible = plan_diffuser_witness_schedule(
            target_probability=0.95, tolerated_misses=1, assumed_batch_success_probability=1.0,
        )["validation_power"]
        self.assertEqual(infeasible["status"], "NO_ELIGIBLE_SCHEDULE")
        self.assertIsNone(infeasible["best_within_cap"])
        self.assertEqual(infeasible["at_cap"]["maximum_failed_batches"], 0)
        self.assertEqual(infeasible["at_cap"]["probability_of_target_support"], 1.0)

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_validation_power_respects_runtime_threshold_and_exact_probability_edges(self):
        from austins_algorithm.witness_budget import _witness_lower_bound

        target = _witness_lower_bound(16, 20, confidence_level=0.99375)
        for threshold, successes in ((target, 16), (nextafter(target, 1), 17)):
            with self.subTest(threshold=threshold):
                report = plan_diffuser_witness_schedule(
                    target_probability=threshold, max_batches_per_variant=20, assumed_batch_success_probability=0.9,
                    minimum_validation_power=1.0,
                )
                power = report["validation_power"]
                self.assertEqual(power["status"], "MINIMUM_POWER_UNREACHABLE")
                self.assertEqual(power["at_cap"]["required_successful_batches"], successes)
                self.assertAlmostEqual(
                    power["at_cap"]["probability_of_target_support"],
                    fsum(comb(20, hits) * 0.9**hits * 0.1**(20 - hits) for hits in range(successes, 21)),
                )
        for assumed in (0.0, nextafter(1.0, 0.0), 1.0):
            with self.subTest(assumed=assumed):
                power = plan_diffuser_witness_schedule(
                    target_probability=target, max_batches_per_variant=20, assumed_batch_success_probability=assumed,
                    minimum_validation_power=1.0,
                )["validation_power"]
                self.assertEqual(power["at_cap"]["meets_minimum_validation_power"], assumed == 1.0)
                self.assertEqual(power["at_cap"]["probability_of_target_support"], 0.0 if assumed == 0.0 else 1.0)

    @unittest.skipUnless(find_spec("scipy") is not None, "optional SciPy is unavailable")
    def test_joint_schedule_search_keeps_shot_and_gate_optima_separate(self):
        for cap, max_batches, feasible, status in (
            (64, 7, 2, "PLANNED"),
            (6, 7, 1, "NO_FULLY_FEASIBLE_SCHEDULE"),
            (64, 3, 0, "NO_ELIGIBLE_SCHEDULE"),
        ):
            with self.subTest(cap=cap, max_batches=max_batches):
                search = {
                    "max_batches_per_variant": max_batches, "confidence_per_variant": 0.99375,
                    "required_variant_count": 2, "candidates": [],
                    "lowest_shot_cost_batches_per_variant": None, "lowest_nominal_gate_cost_batches_per_variant": None,
                }
                variants = {
                    method: {
                        "modeled_witness_budget": _plan_modeled_witness(
                            rate, target_probability=0.2, max_shots=cap, two_qubit_gates=gates,
                        ),
                        "compilation": {"compiled": {"two_qubit_gates": gates}},
                    }
                    for method, rate, gates in (("standard", 0.04, 10), ("clean-ancilla", 0.2, 1))
                }
                report = {
                    "modeled_witness_schedule_search": search,
                    "modeled_witness_target": {"target_probability": 0.2, "minimum_validation_power": 0.4},
                    "cells": [{"placement": 1, "rounds": 1, "variants": variants}],
                }
                _search_modeled_witness_schedules(report)
                self.assertEqual(search["status"], status)
                self.assertEqual(search["max_feasible_variants"], feasible)
                if status != "PLANNED":
                    self.assertIsNone(search["lowest_shot_cost_batches_per_variant"])
                    self.assertIsNone(search["lowest_nominal_gate_cost_batches_per_variant"])
                    continue
                self.assertEqual(search["minimum_eligible_batches_per_variant"], 4)
                self.assertEqual(search["lowest_shot_cost_batches_per_variant"], 4)
                self.assertEqual(search["lowest_nominal_gate_cost_batches_per_variant"], 7)
                self.assertEqual(
                    [(row["batches_per_variant"], row["planned_shots"], row["planned_nominal_two_qubit_gate_executions"])
                     for row in search["candidates"]],
                    [(4, 188, 1592), (5, 265, 2245), (6, 210, 1776), (7, 189, 1575)],
                )

    def test_diffuser_regression_caps_the_full_plan_even_for_a_partial_baseline(self):
        from austins_algorithm.diffuser_regression import run_diffuser_regression

        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        verified = {
            "artifacts": {},
            "workload": {"execution_status": "PARTIAL", "planned_shots": 16, "completed_shots": 1},
        }
        with (
            TemporaryDirectory(dir=build) as directory,
            patch("austins_algorithm.diffuser_regression._read_verified_diffuser_receipt",
                  return_value=(verified, {}, b"request")) as verify,
            patch("austins_algorithm.diffuser_regression._run_worker") as worker,
        ):
            output = Path(directory) / "run"
            baseline = Path(directory) / "baseline"
            result = run_diffuser_regression(baseline, output_directory=output, max_sampling_shots=15, timeout_seconds=5)
            self.assertEqual(result["status"], "REGRESSION_PREFLIGHT_FAILED")
            self.assertEqual(result["reason"], "SHOT_LIMIT_EXCEEDED")
            self.assertEqual(result["planned_workload"]["planned_shots"], 16)
            self.assertEqual(list(output.iterdir()), [output / "summary.json"])
            self.assertEqual(json.loads((output / "summary.json").read_bytes()), result)
            with self.assertRaises(FileExistsError):
                run_diffuser_regression(baseline, output_directory=output, max_sampling_shots=16, timeout_seconds=5)
            verify.assert_called_once()
            worker.assert_not_called()

    def test_diffuser_suite_checks_every_budget_before_starting_a_worker(self):
        from austins_algorithm.diffuser_regression import run_diffuser_regression_suite

        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        for suite_cap, second_cap, invalid, reason, second_reason, total in (
            (31, 20, False, "TOTAL_SHOT_LIMIT_EXCEEDED", "TOTAL_SHOT_LIMIT_EXCEEDED", 32),
            (32, 19, False, "CASE_PREFLIGHT_FAILED", "SHOT_LIMIT_EXCEEDED", 32),
            (32, 20, True, "CASE_PREFLIGHT_FAILED", "INVALID_BASELINE", None),
        ):
            with self.subTest(suite_cap=suite_cap, second_cap=second_cap, invalid=invalid), TemporaryDirectory(dir=build) as directory:
                root = Path(directory)
                manifest = root / "manifest.json"
                manifest.write_text(json.dumps({
                    "schema_version": 1, "max_total_sampling_shots": suite_cap,
                    "cases": [
                        {"id": "first", "baseline": "first", "max_sampling_shots": 12, "timeout_seconds": 3},
                        {"id": "second", "baseline": "second", "max_sampling_shots": second_cap, "timeout_seconds": 4},
                    ],
                }), encoding="utf-8")
                first = ({"artifacts": {}, "workload": {"planned_shots": 12}}, {}, b"first")
                second = ({"artifacts": {}, "workload": {"planned_shots": 20}}, {}, b"second")
                with (
                    patch("austins_algorithm.diffuser_regression._read_verified_diffuser_receipt",
                          side_effect=[first, ValueError("bad baseline") if invalid else second]) as verify,
                    patch("austins_algorithm.diffuser_regression._run_worker") as worker,
                ):
                    output = root / "run"
                    result = run_diffuser_regression_suite(manifest, output_directory=output)
                    worker.assert_not_called()
                self.assertEqual([call.args[0] for call in verify.call_args_list],
                                 [root.resolve() / "first", root.resolve() / "second"])
                self.assertEqual(result["status"], "REGRESSION_PREFLIGHT_FAILED")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["planned_max_sampling_shots"], total)
                self.assertEqual(result["verified_replays"], 0)
                self.assertEqual(result["cases"][0]["status"], "NOT_RUN_PREFLIGHT_FAILED")
                self.assertEqual(result["cases"][1]["reason"], second_reason)
                self.assertEqual({item.name for item in output.iterdir()}, {"manifest.json", "summary.json"})
                self.assertEqual((output / "manifest.json").read_bytes(), manifest.read_bytes())

    def test_invalid_snapshot_method_is_rejected_before_oracle_build(self):
        with patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
            with self.assertRaisesRegex(ValueError, "simulation_method"):
                run_constraint_diffuser_comparison(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, simulation_method="invalid",
                )
            synthesize.assert_not_called()

    def test_modeled_budget_uses_exact_threshold_and_reports_unavailable_costs(self):
        plan = _plan_modeled_witness(0.5, target_probability=0.75, max_shots=2, two_qubit_gates=3)
        self.assertEqual(plan["modeled_hit_probability"], 0.5)
        self.assertEqual(plan["shots_required"], 2)
        self.assertEqual(plan["success_probability_at_budget"], 0.75)
        self.assertEqual(plan["nominal_two_qubit_gate_executions"], 6)
        self.assertNotIn("lower_hit_rate", plan)

        above = _plan_modeled_witness(0.5, target_probability=nextafter(0.75, 1), max_shots=2, two_qubit_gates=3)
        self.assertEqual(above["status"], "BUDGET_EXCEEDED")
        self.assertIsNone(above["nominal_two_qubit_gate_executions"])
        self.assertEqual(above["success_probability_at_cap"], 0.75)

        zero = _plan_modeled_witness(0.0, target_probability=0.75, max_shots=2, two_qubit_gates=3)
        self.assertEqual(zero["status"], "ZERO_MODELED_HIT_PROBABILITY")
        self.assertEqual(zero["success_probability_at_cap"], 0.0)
        self.assertIsNone(zero["shots_required"])
        self.assertIsNone(zero["nominal_two_qubit_gate_executions"])

        certain = _plan_modeled_witness(1.0, target_probability=0.75, max_shots=2, two_qubit_gates=0)
        self.assertEqual(certain["status"], "PLANNED")
        self.assertEqual(certain["shots_required"], 1)
        self.assertEqual(certain["nominal_two_qubit_gate_executions"], 0)

    def test_power_budget_compares_exact_probabilities_and_keeps_batch_target(self):
        def plan(rate, power, cap=256, target=0.05, required=2):
            return _plan_modeled_witness(
                rate, target_probability=target, max_shots=cap, two_qubit_gates=3, validation_batches=2,
                required_successes=required, minimum_validation_power=power,
            )

        self.assertEqual(plan(0.5, 0.5625)["shots_required"], 2)
        self.assertEqual(plan(0.5, nextafter(0.5625, 1))["shots_required"], 3)
        self.assertEqual(plan(0.2, 0.12960000000000002)["shots_required"], 3)
        capped = plan(0.2, 0.12960000000000002, cap=2)
        self.assertEqual(capped["status"], "VALIDATION_POWER_CAP_EXCEEDED")
        self.assertFalse(capped["meets_minimum_validation_power_at_cap"])
        self.assertIsNone(capped["shots_required"])
        self.assertIsNone(capped["validation_power_at_budget"])
        self.assertEqual(plan(0.5, 0.1, target=0.95)["shots_required"], 5)
        self.assertEqual(plan(0.5, 0.1, cap=1, target=0.95)["status"], "BUDGET_EXCEEDED")
        self.assertEqual(plan(0, 0.1)["status"], "ZERO_MODELED_HIT_PROBABILITY")
        self.assertEqual(plan(1, 1)["shots_required"], 1)
        rounded = plan(0.5, 1)
        self.assertEqual(rounded["validation_power_at_cap"], 1.0)
        self.assertFalse(rounded["meets_minimum_validation_power_at_cap"])
        short = plan(1, 0.5, required=None)
        self.assertEqual(short["status"], "VALIDATION_SCHEDULE_TOO_SHORT")
        self.assertEqual(short["validation_power_at_cap"], 0.0)

    def test_invalid_modeled_budget_flags_are_rejected_before_oracle_build(self):
        formula = str(Path(__file__).resolve().parents[1] / "examples/field_sensor_kit.json")
        base = [
            "constraint-sweep", formula, "--ibm-model", "manila",
            "--placement", "0", "1", "2", "3", "4", "--placement", "4", "3", "2", "1", "0",
        ]
        modeled = ["--compare-diffusers", "--snapshot-method", "modeled_probabilities"]
        for options, message in (
            (["--witness-probability", "0.95"], "witness budget flags require"),
            (["--witness-stop-on-futility"], "witness budget flags require"),
            (["--witness-plan-only"], "witness budget flags require"),
            (["--save-replay-request", "unused.json"], "witness budget flags require"),
            ([*modeled, "--save-replay-request", "unused.json", "--witness-search-max-batches", "7"],
             "--save-replay-request requires a modeled diffuser sweep with a fixed seed schedule"),
            (["--witness-search-max-batches", "7"], "witness budget flags require"),
            ([*modeled, "--witness-search-max-batches", "129"], "witness_search_max_batches"),
            ([*modeled, "--witness-search-max-batches", "7"], "requires witness_probability and witness_validation_power"),
            ([*modeled, "--witness-search-max-batches", "7", "--witness-seed-schedule", "41", "43"],
             "cannot be used with witness_seed_schedule"),
            (["--witness-validation-power", "0.8"], "witness budget flags require"),
            ([*modeled, "--witness-validation-power", "0.8"], "witness_validation_power requires witness_seed_schedule"),
            ([*modeled, "--witness-validation-power", "nan", "--witness-seed-schedule", "41", "43"],
             "minimum_validation_power must be greater than zero"),
            ([*modeled, "--witness-plan-only"], "witness_plan_only requires witness_seed_schedule"),
            ([*modeled, "--witness-plan-only", "--witness-seed-schedule", "41", "43"], "requires witness_probability"),
            ([*modeled, "--witness-plan-only", "--witness-stop-on-futility", "--witness-seed-schedule", "41", "43",
              "--witness-probability", "0.95"], "cannot be used with witness_stop_on_futility"),
            ([*modeled, "--witness-probability", "0.95", "--witness-stop-on-futility"], "requires witness_seed_schedule"),
            (["--compare-diffusers", "--witness-probability", "0.95"], "witness budgets require"),
            ([*modeled, "--witness-max-shots", "8"], "requires witness_probability"),
            ([*modeled, "--witness-seed-schedule", "41", "43"], "requires witness_probability"),
            ([*modeled, "--witness-probability", "0.95", "--witness-seed-schedule", "41", "41"], "must be distinct"),
            ([*modeled, "--witness-probability", "0.95", "--witness-seed-schedule", "41", "43", "47"],
             "an even number of seeds"),
            ([*modeled, "--witness-probability", "nan"], "strictly between"),
            ([*modeled, "--witness-probability", "0.95", "--witness-max-shots", "257"], "max_shots"),
        ):
            with self.subTest(options=options), patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize:
                with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as error:
                    workflow_main([*base, *options])
                self.assertEqual(error.exception.code, 1)
                self.assertIn(message, stderr.getvalue())
                synthesize.assert_not_called()

    def test_modeled_cli_rejects_explicit_sampling_options(self):
        formula = str(Path(__file__).resolve().parents[1] / "examples/field_sensor_kit.json")
        arguments = [
            "constraint-sweep", formula, "--compare-diffusers", "--ibm-model", "manila",
            "--placement", "0", "1", "2", "3", "4", "--placement", "4", "3", "2", "1", "0",
            "--snapshot-method", "modeled_probabilities",
        ]
        with patch(f"{MODULE}.run_constraint_diffuser_comparison") as run:
            for option in (
                ["--shots", "64"], ["--evaluation-seeds", *map(str, SEEDS)],
                ["--evaluation-seed-schedule", *map(str, SEEDS)], ["--validation-seed", "19"],
            ):
                with self.subTest(option=option), redirect_stderr(StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        workflow_main([*arguments, *option])
                    self.assertEqual(error.exception.code, 1)
                    self.assertIn("uses no shot or sampling seed options", stderr.getvalue())
            run.assert_not_called()

    def test_cli_keeps_four_seed_option_and_accepts_alternating_schedule(self):
        formula = str(Path(__file__).resolve().parents[1] / "examples/field_sensor_kit.json")
        arguments = [
            "--compare-diffusers", "--ibm-model", "manila",
            "--placement", "0", "1", "2", "3", "4", "--placement", "4", "3", "2", "1", "0",
        ]
        with patch(f"{MODULE}.run_constraint_diffuser_comparison", return_value={}) as run:
            with redirect_stdout(StringIO()):
                workflow_main(["constraint-sweep", "--evaluation-seeds", *map(str, SEEDS), formula, *arguments])
                schedule = (*SEEDS, 37, 41, 43, 47)
                workflow_main([
                    "constraint-sweep", formula, *arguments, "--evaluation-seed-schedule", *map(str, schedule),
                    "--snapshot-method", "prepared_density_matrix",
                ])
            self.assertEqual([call.kwargs["evaluation_seeds"] for call in run.call_args_list],
                             [list(SEEDS), list(schedule)])
            self.assertEqual([call.kwargs["simulation_method"] for call in run.call_args_list],
                             [None, "prepared_density_matrix"])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                workflow_main([
                    "constraint-sweep", formula, *arguments,
                    "--evaluation-seeds", *map(str, SEEDS), "--evaluation-seed-schedule", *map(str, schedule),
                ])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(run.call_count, 2)


    def test_native_qasm_cli_forwards_preference_to_comparison_and_save(self):
        formula = str(Path(__file__).resolve().parents[1] / "examples/field_sensor_kit.json")
        arguments = [
            "constraint-sweep", formula, "--compare-diffusers", "--native-qasm", "--ibm-model", "manila",
            "--placement", "0", "1", "2", "3", "4", "--placement", "4", "3", "2", "1", "0",
            "--snapshot-method", "modeled_probabilities",
        ]
        with (
            patch(f"{MODULE}.run_constraint_diffuser_comparison", return_value={}) as run,
            patch("austins_algorithm.diffuser_replay.save_diffuser_replay", return_value={}) as save,
            redirect_stdout(StringIO()),
        ):
            workflow_main(arguments)
            workflow_main([*arguments, "--save-replay-request", "unused.json"])
        self.assertIs(run.call_args.kwargs["prefer_native_qasm"], True)
        self.assertIs(save.call_args.kwargs["prefer_native_qasm"], True)

    def test_native_qasm_rejects_non_boolean_preference_before_oracle_build(self):
        with (
            patch(f"{MODULE}.synthesize_constraint_oracle") as synthesize,
            self.assertRaisesRegex(ValueError, "prefer_native_qasm must be a boolean"),
        ):
            run_constraint_diffuser_comparison(CONSTRAINT, model="manila", placements=PLACEMENTS, prefer_native_qasm=1)
        synthesize.assert_not_called()


@unittest.skipUnless(IBM_TOOLS, "optional IBM model tools are unavailable")
class ConstraintDiffuserComparisonTests(unittest.TestCase):
    def assert_compilation_timings(self, report):
        timings = [
            variant["compilation_timing_seconds"]
            for cell in report["cells"]
            for variant in cell["variants"].values()
        ]
        self.assertEqual(len(timings), 8)
        for timing in timings:
            self.assertEqual(set(timing), set(COMPILATION_TIMING_PHASES))
            for elapsed in timing.values():
                self.assertIsInstance(elapsed, float)
                self.assertTrue(isfinite(elapsed))
                self.assertGreaterEqual(elapsed, 0)
            self.assertLessEqual(
                fsum(timing[phase] for phase in COMPILATION_TIMING_PHASES[1:]),
                timing["compile_export_verify"],
            )
        for phase in COMPILATION_TIMING_PHASES:
            self.assertAlmostEqual(report["timing_seconds"][phase], fsum(timing[phase] for timing in timings))

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
        self.assert_compilation_timings(report)
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

    def test_full_schedule_reuses_each_compilation_and_pools_every_batch(self):
        schedule = tuple(range(16))
        with patch(f"{MODULE}._sample_snapshot", side_effect=lambda *args, **kw: _completed(16, kw["seed"])) as sampler:
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                shots=16, evaluation_seeds=schedule,
            )
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["limits"]["max_sampling_batches"], 64)
        self.assertEqual(sampler.call_count, 64)
        self.assertEqual(len({id(call.args[0]) for call in sampler.call_args_list}), 8)
        for cell in report["cells"]:
            for index, method in enumerate(("standard", "clean-ancilla")):
                variant = cell["variants"][method]
                seeds = schedule[index::2]
                self.assertEqual(tuple(batch["seed"] for batch in variant["batches"]), seeds)
                self.assertEqual(variant["summary"]["completed_shots"], 128)
                self.assertEqual(variant["summary"]["verified_shots"], sum(seeds))
            self.assertAlmostEqual(cell["difference"]["estimate"], 8 / 128)
            self.assertEqual(cell["difference"]["status"], "UNRESOLVED")

    def test_confirmed_timeout_keeps_affected_cell_incomplete_after_two_batches(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": True, "simulation": None}
        schedule = (*SEEDS, 37, 41, 43, 47)
        samples = [_completed(2, 1)] * 32
        samples[2] = timeout
        with patch(f"{MODULE}._sample_snapshot", side_effect=samples) as sampler:
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                shots=2, evaluation_seeds=schedule,
            )
        self.assertEqual(sampler.call_count, 32)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertFalse(report["local_cancellation_unconfirmed"])
        affected, *complete = report["cells"]
        self.assertIsNone(affected["variants"]["standard"]["summary"])
        self.assertEqual(len(affected["variants"]["standard"]["batches"]), 4)
        self.assertIsNone(affected["difference"])
        self.assertTrue(all(cell["difference"] is not None for cell in complete))

    def test_prepared_state_is_built_once_and_reused_for_every_seed(self):
        from austins_algorithm.ibm_models import _compact_snapshot_inputs, _prepare_density_snapshot, _sample_snapshot

        with (
            patch(f"{MODULE}._compact_snapshot_inputs", wraps=_compact_snapshot_inputs) as compact,
            patch(f"{MODULE}._prepare_density_snapshot", wraps=_prepare_density_snapshot) as prepare,
            patch(f"{MODULE}._sample_snapshot", wraps=_sample_snapshot) as sample,
        ):
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                shots=16, evaluation_seeds=(*SEEDS, 37, 41, 43, 47), simulation_method="prepared_density_matrix",
            )
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["snapshot_method"], "prepared_density_matrix")
        self.assertEqual(report["wire_representation"], "compact")
        self.assertEqual(report["limits"]["max_state_preparation_jobs"], 8)
        compact.assert_called_once()
        self.assertEqual(prepare.call_count, 8)
        self.assertEqual(sample.call_count, 32)
        self.assertEqual(len({id(call.kwargs["sampling_circuit"]) for call in sample.call_args_list}), 8)
        for offset in range(0, 32, 4):
            calls = sample.call_args_list[offset:offset + 4]
            self.assertTrue(all(call.args[0] is calls[0].args[0] for call in calls))
            self.assertTrue(all(call.kwargs["sampling_circuit"] is calls[0].kwargs["sampling_circuit"] for call in calls))
        for cell in report["cells"]:
            for variant in cell["variants"].values():
                self.assertEqual(variant["state_preparation"]["status"], "COMPLETED")
                self.assertEqual(variant["summary"]["completed_shots"], 64)
                self.assertTrue(all(batch["simulation"]["noise_instructions"] == ["measure"]
                                    for batch in variant["batches"]))
        self.assertGreater(report["timing_seconds"]["state_preparation"], 0)
        self.assertGreater(report["timing_seconds"]["sampling"], 0)

    def test_modeled_probability_map_uses_classical_bits_and_asymmetric_readout(self):
        import numpy as np
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError

        from austins_algorithm.ibm_models import _prepared_snapshot_probabilities

        circuit = QuantumCircuit(3, 2)
        weights = np.zeros(8)
        weights[1], weights[6] = 0.6, 0.4
        circuit.set_density_matrix(np.diag(weights))
        circuit.measure(0, 1)
        circuit.measure(2, 0)
        noise = NoiseModel()
        noise.add_readout_error(ReadoutError([[0.9, 0.1], [0.2, 0.8]]), [0])
        noise.add_readout_error(ReadoutError([[0.75, 0.25], [0.05, 0.95]]), [2])
        simulator = AerSimulator(method="density_matrix", noise_model=noise)
        result = _prepared_snapshot_probabilities(circuit, simulator, verifier=lambda bits: bits.endswith("1"))
        expected = {"00": 0.108, "01": 0.372, "10": 0.362, "11": 0.158}
        self.assertEqual(list(result["outcome_probabilities"]), list(expected))
        for bits, value in expected.items():
            self.assertAlmostEqual(result["outcome_probabilities"][bits], value, places=12)
        self.assertAlmostEqual(result["verified_hit_probability"], 0.53, places=12)

        correlated = NoiseModel()
        correlated.add_readout_error(ReadoutError(np.eye(4)[[1, 0, 2, 3]]), [0, 2])
        simulator = AerSimulator(method="density_matrix", noise_model=correlated)
        with self.assertRaisesRegex(ValueError, "single-wire"):
            _prepared_snapshot_probabilities(circuit, simulator, verifier=lambda bits: True)

    def test_modeled_all_hit_probability_stays_within_the_planner_domain(self):
        import numpy as np
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError

        from austins_algorithm.ibm_models import _prepared_snapshot_probabilities

        circuit = QuantumCircuit(2, 2)
        weights = [0.41991283697742265, 0.18878283612429134, 0.21137252205489226, 0.17993180484339383]
        circuit.set_density_matrix(np.diag(weights))
        circuit.measure(0, 1)
        circuit.measure(1, 0)
        noise = NoiseModel()
        noise.add_readout_error(ReadoutError([[0.9, 0.1], [0.2, 0.8]]), [0])
        noise.add_readout_error(ReadoutError([[0.75, 0.25], [0.05, 0.95]]), [1])
        simulator = AerSimulator(method="density_matrix", noise_model=noise)
        result = _prepared_snapshot_probabilities(circuit, simulator, verifier=lambda bits: True)
        probability = result["verified_hit_probability"]
        self.assertLessEqual(probability, 1.0)
        self.assertAlmostEqual(probability, 1.0, places=14)
        plan = _plan_modeled_witness(probability, target_probability=0.95, max_shots=1, two_qubit_gates=4)
        self.assertEqual(plan["shots_required"], 1)

    def test_modeled_comparison_has_eight_gate_saves_and_zero_measurement_batches(self):
        from austins_algorithm.ibm_models import _prepare_density_snapshot

        with (
            patch(f"{MODULE}._prepare_density_snapshot", wraps=_prepare_density_snapshot) as prepare,
            patch(f"{MODULE}._sample_snapshot", side_effect=AssertionError("modeled mode sampled a batch")) as sample,
        ):
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                simulation_method="modeled_probabilities",
            )
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["result_kind"], "MODELED_PROBABILITIES")
        self.assertEqual(report["limits"]["max_sampling_batches"], 0)
        self.assertEqual(report["limits"]["max_state_preparation_jobs"], 8)
        self.assertNotIn("confidence_per_variant", report)
        self.assertNotIn("modeled_witness_target", report)
        self.assertEqual(prepare.call_count, 8)
        sample.assert_not_called()
        for cell in report["cells"]:
            hits = {}
            for diffuser, variant in cell["variants"].items():
                self.assertNotIn("batches", variant)
                probabilities = variant["modeled_output"]["outcome_probabilities"]
                self.assertAlmostEqual(fsum(probabilities.values()), 1.0, places=10)
                hits[diffuser] = fsum(value for bits, value in probabilities.items() if CONSTRAINT.matches(bits))
                self.assertEqual(variant["modeled_output"]["verified_hit_probability"], hits[diffuser])
            self.assertEqual(cell["modeled_hit_rate_difference"], hits["clean-ancilla"] - hits["standard"])
        json.dumps(report, allow_nan=False)

    def test_holdout_confidence_requires_99_all_success_batches_at_the_95_percent_target(self):
        from austins_algorithm.witness_budget import summarize_witness_batches

        for n, status in ((98, "UNRESOLVED"), (99, "TARGET_SUPPORTED")):
            with self.subTest(n=n):
                summary = summarize_witness_batches(
                    [_completed(2, 1)] * n, planned_batches=n, target_probability=0.95, confidence_level=0.99375,
                )
                self.assertEqual(summary["status"], status)
                self.assertAlmostEqual(summary["lower_success_probability"], 0.00625 ** (1 / n))
        with self.assertRaisesRegex(ValueError, "confidence_level"):
            summarize_witness_batches([], planned_batches=1, target_probability=0.95, confidence_level=float("nan"))

    def test_holdout_reuses_prepared_circuits_and_skips_infeasible_budgets(self):
        from austins_algorithm.ibm_models import _prepare_density_snapshot

        rates = (0.75, 0.5, 0.0, 0.5, 0.1, 0.0, 0.75, 0.5)

        def sample(program, backend, simulator, *, shots, seed, method, verifier, sampling_circuit):
            self.assertEqual(prepare.call_count, 8)
            self.assertEqual(method, "density_matrix")
            self.assertTrue(verifier("0001"))
            self.assertFalse(verifier("0000"))
            return _completed(shots, 1)

        with (
            patch(f"{MODULE}._prepare_density_snapshot", wraps=_prepare_density_snapshot) as prepare,
            patch(f"{MODULE}._prepared_snapshot_probabilities",
                  side_effect=[{"verified_hit_probability": rate} for rate in rates]),
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                simulation_method="modeled_probabilities", witness_probability=0.75, witness_max_shots=2,
                witness_seed_schedule=(41, 43, 47, 53),
            )

        self.assertEqual(report["status"], "COMPLETED")
        validation = report["modeled_witness_validation"]
        self.assertEqual(validation["status"], "COMPLETED")
        self.assertEqual(validation["planned_batches"], 10)
        self.assertEqual(validation["completed_batches"], 10)
        self.assertEqual(validation["completed_shots"], 16)
        self.assertEqual(report["limits"]["max_sampling_batches"], 16)
        self.assertEqual(sampler.call_count, 10)
        self.assertEqual([call.kwargs["shots"] for call in sampler.call_args_list], [1, 1, 2, 2, 2, 2, 1, 1, 2, 2])
        self.assertEqual([call.kwargs["seed"] for call in sampler.call_args_list], [41, 47, 43, 53, 43, 53, 41, 47, 43, 53])
        circuits = [call.kwargs["sampling_circuit"] for call in sampler.call_args_list]
        self.assertEqual(len({id(circuit) for circuit in circuits}), 5)
        self.assertTrue(all(circuits[index] is circuits[index + 1] for index in range(0, 10, 2)))
        infeasible = report["cells"][1]["variants"]["standard"]["modeled_witness_validation"]
        self.assertEqual(infeasible["status"], "NO_FEASIBLE_BUDGET")
        self.assertEqual(infeasible["batches"], [])
        observed = report["cells"][1]["variants"]["clean-ancilla"]["modeled_witness_validation"]
        self.assertEqual(observed["successful_batches"], 2)
        self.assertEqual(observed["status"], "UNRESOLVED")

    def test_plan_only_reports_per_variant_power_and_costs_without_sampling(self):
        rates = (0.75, 0.5, 0.0, 0.5, 0.1, 0.0, 0.75, 0.5)
        for target, feasible_count, planned_shots, required in (
            (0.05, 6, 12, 2),
            (0.75, 5, 16, None),
        ):
            with (
                self.subTest(target=target),
                patch(f"{MODULE}._prepared_snapshot_probabilities",
                      side_effect=[{"verified_hit_probability": rate} for rate in rates]),
                patch(f"{MODULE}._sample_snapshot") as sample,
                patch(f"{MODULE}._run_modeled_witness_validation") as validate,
            ):
                report = run_constraint_diffuser_comparison(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                    simulation_method="modeled_probabilities", witness_probability=target, witness_max_shots=2,
                    witness_seed_schedule=(41, 43, 47, 53), witness_plan_only=True,
                )
                sample.assert_not_called()
                validate.assert_not_called()
            self.assertEqual(report["status"], "COMPLETED")
            self.assertEqual(report["result_kind"], "MODELED_PROBABILITIES_WITH_WITNESS_PLAN")
            self.assertEqual(report["limits"]["shots"], 0)
            self.assertEqual(report["limits"]["max_sampling_batches"], 0)
            self.assertNotIn("modeled_witness_validation", report)
            plan = report["modeled_witness_plan"]
            self.assertEqual(plan["status"], "PLANNED")
            self.assertFalse(plan["local_sampling_performed"])
            self.assertEqual(plan["required_successful_batches"], required)
            self.assertEqual(plan["planned_batches"], 2 * feasible_count)
            self.assertEqual(plan["planned_shots"], planned_shots)
            self.assertEqual(plan["seed_schedule"], {"standard": [41, 47], "clean-ancilla": [43, 53]})
            total_gates = 0
            for cell in report["cells"]:
                for variant in cell["variants"].values():
                    row = variant["modeled_witness_plan"]
                    budget = variant["modeled_witness_budget"]
                    if budget["shots_required"] is None:
                        self.assertEqual(row["status"], "NO_FEASIBLE_BUDGET")
                        self.assertEqual(row["planned_shots"], 0)
                        self.assertIsNone(row["probability_of_target_support"])
                    else:
                        self.assertEqual(row["status"], "PLANNED")
                        self.assertEqual(row["planned_shots"], 2 * budget["shots_required"])
                        assumed = budget["success_probability_at_budget"]
                        self.assertEqual(row["assumed_batch_success_probability"], assumed)
                        self.assertAlmostEqual(row["probability_of_target_support"], assumed**2 if required else 0.0)
                    self.assertNotIn("lower_success_probability", row)
                    self.assertNotIn("batches", row)
                    gates = row["planned_shots"] * variant["compilation"]["compiled"]["two_qubit_gates"]
                    self.assertEqual(row["planned_nominal_two_qubit_gate_executions"], gates)
                    total_gates += gates
            self.assertEqual(plan["planned_nominal_two_qubit_gate_executions"], total_gates)

    def test_power_sized_budgets_drive_both_preview_and_sampling(self):
        rates = (0.5, 0.25, 0.0, 1.0) * 2
        for plan_only in (True, False):
            with (
                self.subTest(plan_only=plan_only),
                patch(f"{MODULE}._prepared_snapshot_probabilities",
                      side_effect=[{"verified_hit_probability": rate} for rate in rates]),
                patch(f"{MODULE}._sample_snapshot", side_effect=lambda *args, shots, **kwargs: _completed(shots, 1))
                as sampler,
            ):
                report = run_constraint_diffuser_comparison(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                    simulation_method="modeled_probabilities", witness_probability=0.05, witness_max_shots=2,
                    witness_seed_schedule=(41, 43, 47, 53), witness_plan_only=plan_only, witness_validation_power=0.5625,
                )
            self.assertEqual(report["status"], "COMPLETED")
            self.assertEqual(report["modeled_witness_target"]["minimum_validation_power"], 0.5625)
            key = "modeled_witness_plan" if plan_only else "modeled_witness_validation"
            self.assertEqual(report[key]["required_successful_batches"], 2)
            self.assertEqual(report[key]["planned_batches"], 8)
            self.assertEqual(report[key]["planned_shots"], 12)
            first = report["cells"][0]["variants"]
            self.assertEqual(first["standard"]["modeled_witness_budget"]["shots_required"], 2)
            self.assertEqual(first["clean-ancilla"]["modeled_witness_budget"]["status"], "VALIDATION_POWER_CAP_EXCEEDED")
            if plan_only:
                sampler.assert_not_called()
                self.assertEqual(first["standard"][key]["probability_of_target_support"], 0.5625)
                self.assertIsNone(first["clean-ancilla"][key]["probability_of_target_support"])
            else:
                self.assertEqual(report[key]["completed_shots"], 12)
                self.assertEqual([call.kwargs["shots"] for call in sampler.call_args_list], [2, 2, 1, 1] * 2)

    def test_saved_replay_verifies_the_last_variant_before_first_measurement(self):
        from austins_algorithm.device_experiment import summarize_search_counts
        from austins_algorithm.diffuser_receipt import verify_diffuser_receipt
        from austins_algorithm.diffuser_receipt_comparison import compare_diffuser_receipts
        from austins_algorithm.diffuser_replay import run_diffuser_replay, save_diffuser_replay
        from austins_algorithm.replay_receipt import _write_replay_receipt

        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        with (
            TemporaryDirectory(dir=build) as directory,
            patch(f"{MODULE}._prepared_snapshot_probabilities",
                  return_value={"verified_hit_probability": 0.75}) as probabilities,
            patch(f"{MODULE}._sample_snapshot", side_effect=_completed_witness_sample) as sampler,
        ):
            path = Path(directory) / "request.json"
            saved = save_diffuser_replay(
                path, CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                witness_probability=0.05, witness_max_shots=2, witness_seed_schedule=(41, 43, 47, 53),
                witness_validation_power=0.5, witness_stop_on_futility=True,
            )
            request = json.loads(path.read_bytes())
            self.assertEqual(request["schema_version"], 1)
            self.assertNotIn("prefer_native_qasm", request["sampling"])
            self.assertNotIn("timing_seconds", request)
            self.assertEqual(saved["expected"], request["expected"])
            self.assertEqual(saved["expected"]["planned_batches"], 16)
            self.assertEqual(saved["expected"]["planned_shots"], 16)
            for phase in COMPILATION_TIMING_PHASES:
                elapsed = saved["timing_seconds"][phase]
                self.assertIsInstance(elapsed, float)
                self.assertTrue(isfinite(elapsed))
                self.assertGreaterEqual(elapsed, 0)
            checked = run_diffuser_replay(path, check_only=True)
            self.assertEqual(checked["status"], "DIFFUSER_REPLAY_VERIFIED")
            self.assertEqual(checked["witness_replay_evidence"], request["expected"])
            self.assert_compilation_timings(checked)
            for cell in checked["cells"]:
                for variant in cell["variants"].values():
                    self.assertNotIn("preferred_importer", variant["compilation"]["openqasm"])
            sampler.assert_not_called()

            probabilities.side_effect = [
                {"verified_hit_probability": rate} for rate in [0.75] * 7 + [0.5]
            ]
            with self.assertRaisesRegex(ValueError, "modeled budget, seed schedule, or output differs"):
                run_diffuser_replay(path)
            sampler.assert_not_called()

            probabilities.side_effect = None
            receipt = path.with_name("receipt")
            replay = run_diffuser_replay(path, receipt_directory=receipt)
            self.assertEqual(replay["witness_replay"]["status"], "DIFFUSER_REPLAY_VERIFIED")
            self.assertEqual(replay["witness_replay_evidence"], request["expected"])
            self.assert_compilation_timings(replay)
            self.assertEqual(replay["modeled_witness_validation"]["completed_shots"], 16)
            self.assertTrue(replay["modeled_witness_validation"]["stop_on_futility"])
            self.assertEqual((receipt / "request.json").read_bytes(), path.read_bytes())
            self.assertEqual(verify_diffuser_receipt(receipt)["workload"]["completed_shots"], 16)
            with self.assertRaises(FileExistsError):
                run_diffuser_replay(path, receipt_directory=receipt)
            self.assertEqual(sampler.call_count, 16)

            same = compare_diffuser_receipts(receipt, receipt)
            self.assertEqual(same["status"], "DIFFUSER_RECEIPTS_MATCH")
            self.assertEqual(same["alignment"]["matching_completed_batches"], 16)

            retimed = deepcopy(replay)
            timing = retimed["cells"][0]["variants"]["standard"]["compilation_timing_seconds"]
            for phase in ("compile_export_verify", "openqasm_import"):
                timing[phase] += 1.0
                retimed["timing_seconds"][phase] += 1.0
            retimed["wall_seconds"] += 1.0
            changed_timing = path.with_name("changed-timing")
            changed_timing.mkdir()
            _write_replay_receipt(changed_timing, request=path.read_bytes(), report=retimed, openqasm=None)
            comparison = compare_diffuser_receipts(receipt, changed_timing)
            self.assertEqual(comparison["status"], "DIFFUSER_RECEIPTS_MATCH")
            self.assertTrue(comparison["request_content_matches"])
            self.assertEqual(comparison["exact_artifact_bytes_match"], {"request.json": True, "report.json": False})
            self.assertEqual(comparison["alignment"]["matching_completed_batches"], 16)

            changed_outcome = path.with_name("changed-outcome")
            changed_outcome.mkdir()
            batch = replay["cells"][0]["variants"]["standard"]["modeled_witness_validation"]["batches"][0]
            batch["simulation"].update(summarize_search_counts(
                {"0010": 1}, requested_shots=1, width=CONSTRAINT.qubits, verifier=CONSTRAINT.matches,
            ))
            _write_replay_receipt(changed_outcome, request=path.read_bytes(), report=replay, openqasm=None)
            comparison = compare_diffuser_receipts(receipt, changed_outcome)
            self.assertEqual(comparison["status"], "DIFFUSER_RECEIPTS_DIFFER")
            self.assertEqual(comparison["alignment"]["changed_completed_batches"], 1)
            self.assertEqual(comparison["alignment"]["matching_completed_batches"], 15)
            self.assertEqual(comparison["workload_delta"]["completed_shots"], 0)
            changed_batches = [row for variant in comparison["variants"] for row in variant["batches"]["differences"]]
            self.assertEqual(changed_batches[0]["seed"], 41)
            self.assertEqual(changed_batches[0]["count_deltas"], {"0001": -1, "0010": 1})

            replay["modeled_witness_validation"]["completed_shots"] += 1
            changed = path.with_name("changed-receipt")
            changed.mkdir()
            _write_replay_receipt(changed, request=path.read_bytes(), report=replay, openqasm=None)
            with self.assertRaisesRegex(ValueError, "validation disagrees on completed_shots"):
                verify_diffuser_receipt(changed)

    def test_regression_allows_only_pinned_modeled_probability_roundoff_before_sampling(self):
        from hashlib import sha256
        from io import BytesIO
        from types import SimpleNamespace

        from austins_algorithm.constraint_diffuser_comparison import _diffuser_replay_plan
        from austins_algorithm.diffuser_receipt import _read_verified_diffuser_receipt
        from austins_algorithm.diffuser_receipt_comparison import _compare_verified_diffuser_bundles
        from austins_algorithm.diffuser_regression import _worker_main
        from austins_algorithm.diffuser_replay import run_diffuser_replay, save_diffuser_replay

        def modeled(rate):
            return {"verified_hit_probability": rate, "outcome_probabilities": {"0000": 1 - rate, "0001": rate}}

        def changed_work(report, schedule):
            plan = deepcopy(_diffuser_replay_plan(report, schedule))
            if "witness_replay" not in report:
                plan["variants"][0]["budget"]["nominal_two_qubit_gate_executions"] += 1
                plan["variants"][-1]["budget"]["nominal_two_qubit_gate_executions"] -= 1
            return plan

        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        with (
            TemporaryDirectory(dir=build) as directory,
            patch(f"{MODULE}._prepared_snapshot_probabilities", return_value=modeled(.75)) as probabilities,
            patch(f"{MODULE}._sample_snapshot", side_effect=_completed_witness_sample) as sampler,
        ):
            root = Path(directory)
            request = root / "request.json"
            baseline = root / "baseline"
            save_diffuser_replay(
                request, CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                witness_probability=.05, witness_max_shots=2, witness_seed_schedule=(41, 43, 47, 53),
                witness_validation_power=.5,
            )
            run_diffuser_replay(request, receipt_directory=baseline)
            raw = request.read_bytes()
            raw_request_pin = sha256(raw).hexdigest()
            report_pin = sha256((baseline / "report.json").read_bytes()).hexdigest()
            verified = _read_verified_diffuser_receipt(baseline)
            sampler.reset_mock()

            def worker(destination, pin=report_pin):
                with patch.object(sys, "stdin", SimpleNamespace(buffer=BytesIO(raw))):
                    _worker_main(str(destination), raw_request_pin, str(baseline), pin)

            probabilities.return_value = modeled(.75 + 1e-13)
            with self.assertRaisesRegex(ValueError, "modeled budget, seed schedule, or output differs"):
                run_diffuser_replay(request)
            with self.assertRaisesRegex(ValueError, "preflight report or request"):
                worker(root / "wrong-report-pin", "f" * 64)
            sampler.assert_not_called()

            observed_path = root / "observed"
            worker(observed_path)
            observed = _read_verified_diffuser_receipt(observed_path)
            comparison = _compare_verified_diffuser_bundles(verified, observed, probability_roundoff=True)
            self.assertEqual(comparison["status"], "DIFFUSER_RECEIPTS_MATCH")
            self.assertEqual(comparison["alignment"]["matching_completed_batches"], 16)
            self.assertFalse(comparison["request_content_matches"])
            roundoff = comparison["probability_roundoff"]
            self.assertEqual(roundoff["status"], "MODELED_PROBABILITY_ROUNDOFF_MATCHED")
            self.assertNotEqual(roundoff["baseline_plan_sha256"], roundoff["observed_plan_sha256"])
            self.assertGreater(roundoff["changed_modeled_probability_values"], 0)
            self.assertLessEqual(roundoff["maximum_absolute_difference"], 1e-12)
            self.assertEqual(sampler.call_count, 16)
            self.assertEqual((baseline / "request.json").read_bytes(), raw)
            self.assertEqual(sha256((baseline / "report.json").read_bytes()).hexdigest(), report_pin)

            sampler.reset_mock()
            probabilities.return_value = modeled(.75 + 1e-9)
            with self.assertRaisesRegex(ValueError, "modeled plan differs"):
                worker(root / "material-probability-change")
            sampler.assert_not_called()

            probabilities.return_value = modeled(.75)
            with (
                patch(f"{MODULE}._diffuser_replay_plan", side_effect=changed_work),
                self.assertRaisesRegex(ValueError, "nominal_two_qubit_gate_executions"),
            ):
                worker(root / "changed-variant-work")
            sampler.assert_not_called()

    def test_native_replay_binds_importer_preference_and_actual_importer(self):
        from qiskit import qasm3

        from austins_algorithm.constraint_diffuser_comparison import (
            _diffuser_replay_circuit_digest, _diffuser_replay_digest,
        )
        from austins_algorithm.diffuser_receipt import verify_diffuser_receipt
        from austins_algorithm.diffuser_replay import run_diffuser_replay, save_diffuser_replay
        from austins_algorithm.replay_receipt import _write_replay_receipt

        if not hasattr(qasm3, "loads_experimental"):
            self.skipTest("native OpenQASM importer is unavailable")

        def encode_request(document):
            content = {key: value for key, value in document.items() if key != "content_sha256"}
            document["content_sha256"] = _diffuser_replay_digest(content)
            return json.dumps(document).encode()

        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        with (
            TemporaryDirectory(dir=build) as directory,
            patch(f"{MODULE}._prepared_snapshot_probabilities", return_value={"verified_hit_probability": 0.75}),
            patch(f"{MODULE}._sample_snapshot", side_effect=_completed_witness_sample) as sampler,
        ):
            path = Path(directory) / "native.json"
            save_diffuser_replay(
                path, CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                witness_probability=0.05, witness_max_shots=2, witness_seed_schedule=(41, 43, 47, 53),
                witness_validation_power=0.5, prefer_native_qasm=True,
            )
            sampler.assert_not_called()
            original_request = path.read_bytes()
            request = json.loads(original_request)
            self.assertEqual(request["schema_version"], 2)
            self.assertIs(request["sampling"]["prefer_native_qasm"], True)
            self.assertNotIn("timing_seconds", request)

            receipt = path.with_name("native-receipt")
            replay = run_diffuser_replay(path, receipt_directory=receipt)
            self.assertEqual(replay["witness_replay_evidence"], request["expected"])
            self.assert_compilation_timings(replay)
            self.assertEqual((receipt / "request.json").read_bytes(), original_request)
            self.assertEqual(verify_diffuser_receipt(receipt)["workload"]["completed_shots"], 16)
            native = "qiskit.qasm3.loads_experimental"
            standard = "qiskit.qasm3.loads"
            for cell in replay["cells"]:
                for variant in cell["variants"].values():
                    openqasm = variant["compilation"]["openqasm"]
                    self.assertEqual(openqasm["preferred_importer"], native)
                    self.assertIn(openqasm["importer"], (native, standard))

            sampler.reset_mock()
            changed = path.with_name("changed-request.json")
            request["sampling"]["prefer_native_qasm"] = 1
            changed.write_bytes(encode_request(request))
            with self.assertRaisesRegex(ValueError, "diffuser replay prefer_native_qasm must be a boolean"):
                run_diffuser_replay(changed)

            request["sampling"]["prefer_native_qasm"] = False
            changed.write_bytes(encode_request(request))
            with self.assertRaisesRegex(ValueError, "compiled circuit identity differs"):
                run_diffuser_replay(changed)
            sampler.assert_not_called()

            first = replay["cells"][0]["variants"]["standard"]["compilation"]["openqasm"]
            original_importer = first["importer"]
            first["importer"] = standard if original_importer == native else native
            changed_importer = path.with_name("changed-actual-importer")
            changed_importer.mkdir()
            _write_replay_receipt(changed_importer, request=original_request, report=replay, openqasm=None)
            with self.assertRaisesRegex(ValueError, "recorded circuit digest differs"):
                verify_diffuser_receipt(changed_importer)
            first["importer"] = original_importer

            # Rebind the altered report to a consistent circuit digest to isolate the preference check.
            del first["preferred_importer"]
            request = json.loads(original_request)
            request["expected"]["circuits_sha256"] = _diffuser_replay_circuit_digest(replay["cells"])
            changed_request = encode_request(request)
            replay["witness_replay_evidence"] = request["expected"]
            replay["witness_replay"]["content_sha256"] = request["content_sha256"]
            changed_preference = path.with_name("changed-preferred-importer")
            changed_preference.mkdir()
            _write_replay_receipt(changed_preference, request=changed_request, report=replay, openqasm=None)
            with self.assertRaisesRegex(ValueError, "OpenQASM importer preference differs from the request"):
                verify_diffuser_receipt(changed_preference)

    def test_holdout_timeouts_preserve_evidence_and_stop_on_unconfirmed_cancellation(self):
        for cancelled, attempted, completed in ((True, 16, 15), (False, 2, 1)):
            timeout = {
                "status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": cancelled, "simulation": None,
            }
            samples = [_completed(1, 1), timeout, *([_completed(1, 1)] * 14)]
            with (
                self.subTest(cancelled=cancelled),
                patch(f"{MODULE}._prepared_snapshot_probabilities", return_value={"verified_hit_probability": 0.75}),
                patch(f"{MODULE}._sample_snapshot", side_effect=samples) as sampler,
            ):
                report = run_constraint_diffuser_comparison(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                    simulation_method="modeled_probabilities", witness_probability=0.75,
                    witness_seed_schedule=(41, 43, 47, 53),
                )
                self.assertEqual(report["status"], "PARTIAL")
                validation = report["modeled_witness_validation"]
                self.assertEqual(validation["status"], "PARTIAL_VALIDATION")
                self.assertEqual(validation["completed_batches"], completed)
                self.assertEqual(validation["completed_shots"], completed)
                self.assertEqual(report["local_cancellation_unconfirmed"], not cancelled)
                self.assertEqual(sampler.call_count, attempted)
                first = report["cells"][0]["variants"]["standard"]["modeled_witness_validation"]
                self.assertEqual(first["observed_success_fraction"], 1.0)
                self.assertEqual(first["status"], "INCOMPLETE")
                self.assertIsNone(first["lower_success_probability"])

    def test_holdout_futility_keeps_fixed_denominator_and_timeout_precedence(self):
        from austins_algorithm.witness_budget import _witness_lower_bound

        confidence = 0.99375
        target = _witness_lower_bound(3, 3, confidence_level=confidence)
        for cancelled, attempted, completed, status in (
            (None, 22, 22, "STOPPED_FOR_FUTILITY"),
            (True, 22, 21, "PARTIAL"),
            (False, 2, 1, "PARTIAL"),
        ):
            with self.subTest(cancelled=cancelled):
                first_miss = _completed(1, 0)
                success = _completed(1, 1)
                timeout = {
                    "status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": cancelled, "simulation": None,
                }
                samples = [first_miss, *([success] * 21)]
                if cancelled is not None:
                    samples[1] = timeout
                    samples[2] = first_miss
                with (
                    patch(f"{MODULE}._prepared_snapshot_probabilities",
                          return_value={"verified_hit_probability": 0.75}),
                    patch(f"{MODULE}._sample_snapshot", side_effect=samples) as sampler,
                ):
                    report = run_constraint_diffuser_comparison(
                        CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                        simulation_method="modeled_probabilities", witness_probability=target, witness_max_shots=1,
                        witness_seed_schedule=(41, 43, 47, 53, 59, 61), witness_stop_on_futility=True,
                    )

                self.assertEqual(report["status"], status)
                self.assertEqual(report["local_cancellation_unconfirmed"], cancelled is False)
                self.assertEqual(sampler.call_count, attempted)
                validation = report["modeled_witness_validation"]
                self.assertEqual(validation["status"], status if cancelled is None else "PARTIAL_VALIDATION")
                self.assertEqual(validation["planned_batches"], 24)
                self.assertEqual(validation["completed_batches"], completed)
                self.assertEqual(validation["completed_shots"], completed)
                self.assertEqual(validation["skipped_batches_for_futility"], 2)
                self.assertEqual(validation["skipped_shots_for_futility"], 2)
                first, second = [
                    variant["modeled_witness_validation"] for variant in report["cells"][0]["variants"].values()
                ]
                self.assertEqual(first["status"], "STOPPED_FOR_FUTILITY")
                self.assertIsNone(first["lower_success_probability"])
                self.assertEqual(first["futility_stop"], {
                    "observed_failed_batches": 1,
                    "best_case_successful_batches": 2,
                    "best_case_lower_success_probability": _witness_lower_bound(2, 3, confidence_level=confidence),
                    "skipped_seeds": [47, 59],
                })
                self.assertEqual([batch["seed"] for batch in first["batches"]], [41])
                self.assertNotIn("futility_stop", second)
                if cancelled is None:
                    self.assertEqual(second["status"], "TARGET_SUPPORTED")
                    self.assertEqual(second["lower_success_probability"], target)
                else:
                    self.assertEqual(second["status"], "INCOMPLETE")
                    self.assertIsNone(second["lower_success_probability"])
                self.assertEqual(
                    [batch["seed"] for batch in second["batches"]],
                    [43] if cancelled is False else [43, 53, 61],
                )

    def test_modeled_preparation_timeout_leaves_probabilities_unreported(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": True, "wall_seconds": 0.5}
        for plan_only, key in ((False, "modeled_witness_validation"), (True, "modeled_witness_plan")):
            with (
                self.subTest(plan_only=plan_only),
                patch(f"{MODULE}._prepare_density_snapshot", return_value=(None, timeout)) as prepare,
                patch(f"{MODULE}._sample_snapshot") as sample,
            ):
                report = run_constraint_diffuser_comparison(
                    CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                    simulation_method="modeled_probabilities", witness_probability=0.95, witness_max_shots=9,
                    witness_seed_schedule=(41, 43), witness_plan_only=plan_only,
                )
                prepare.assert_called_once()
                sample.assert_not_called()
            self.assertEqual(report["status"], "STATE_PREPARATION_INCOMPLETE")
            self.assertFalse(report["local_cancellation_unconfirmed"])
            self.assertEqual(report[key]["status"], "NOT_STARTED")
            self.assertIsNone(report[key]["planned_batches"])
            self.assertTrue(all(cell["modeled_hit_rate_difference"] is None for cell in report["cells"]))
            self.assertTrue(all(cell["modeled_witness_cost_comparison"] is None for cell in report["cells"]))
            self.assertTrue(all(variant["modeled_output"] is None and variant["modeled_witness_budget"] is None
                                and variant[key] is None
                                for cell in report["cells"] for variant in cell["variants"].values()))

    def test_preparation_timeout_retains_completed_batches_and_stops(self):
        from austins_algorithm.ibm_models import _prepare_density_snapshot

        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": False,
            "wall_seconds": 0.5,
        }
        attempts = 0

        def prepare_one(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                return None, timeout
            return _prepare_density_snapshot(*args, **kwargs)

        with (
            patch(f"{MODULE}._prepare_density_snapshot", side_effect=prepare_one),
            patch(f"{MODULE}._sample_snapshot", return_value=_completed(2, 1)) as sample,
        ):
            report = run_constraint_diffuser_comparison(
                CONSTRAINT, model="manila", placements=PLACEMENTS, synthesis="clause-flags",
                shots=2, simulation_method="prepared_density_matrix",
            )
        self.assertEqual(attempts, 2)
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(report["status"], "STATE_PREPARATION_INCOMPLETE")
        self.assertTrue(report["local_cancellation_unconfirmed"])
        cell = report["cells"][0]
        self.assertEqual(len(cell["variants"]["standard"]["batches"]), 2)
        self.assertEqual(cell["variants"]["clean-ancilla"]["state_preparation"], timeout)
        self.assertEqual(cell["variants"]["clean-ancilla"]["batches"], [])
        self.assertIsNone(cell["difference"])

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

