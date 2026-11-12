import json
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.snapshot_placement import (  # noqa: E402
    PreparedMod4PlacementCheck,
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

    def test_requires_boolean_native_importer_preference_before_loading_snapshots(self):
        with (
            patch("austins_algorithm.snapshot_placement._snapshot_specs") as load,
            self.assertRaisesRegex(ValueError, "prefer_native_qasm must be a boolean"),
        ):
            run_mod4_placement_check(minimum_hit_rate=0.4, prefer_native_qasm=1)
        load.assert_not_called()

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
        from qiskit import qasm3

        from austins_algorithm import compilation

        with (
            patch("austins_algorithm.snapshot_placement._transpile_initial_layout",
                  wraps=compilation._transpile_initial_layout) as discover,
            patch("austins_algorithm.snapshot_placement.compile_and_verify",
                  wraps=compilation.compile_and_verify) as compiler,
            patch.object(qasm3, "dumps", wraps=qasm3.dumps) as export,
            patch("austins_algorithm.compilation._import_openqasm",
                  wraps=compilation._import_openqasm) as import_qasm,
        ):
            result = run_mod4_placement_check(
                model="manila", minimum_hit_rate=0.4, shots=64, prefer_native_qasm=True,
            )
        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["seed"], 7)
        self.assertEqual(compiler.call_count, 4)
        self.assertEqual(export.call_count, 4)
        self.assertEqual(import_qasm.call_count, 4)
        self.assertTrue(all(call.kwargs["initial_layout"] is not None for call in compiler.call_args_list))
        self.assertTrue(all(call.kwargs["prefer_native_qasm"] is True for call in compiler.call_args_list))
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
                self.assertEqual(record["compilation"]["openqasm"]["preferred_importer"],
                                 "qiskit.qasm3.loads_experimental")
                self.assertEqual([trial["seed"] for trial in record["trials"]], [29, 43])
                self.assertEqual(record["evaluation"]["completed_shots"], 128)
        json.dumps(result, allow_nan=False)

    def test_prepared_plan_reuses_verified_circuits_with_fresh_simulators_and_detached_reports(self):
        from austins_algorithm import snapshot_placement as placement

        def stable_report(value):
            if isinstance(value, dict):
                return {
                    key: stable_report(item) for key, item in value.items()
                    if key not in {"wall_seconds", "timing_seconds", "aer_experiment_seconds"}
                }
            if isinstance(value, list):
                return [stable_report(item) for item in value]
            return value

        request = {"minimum_hit_rate": 0.4, "shots": 16}
        expected = stable_report(run_mod4_placement_check(model="manila", prefer_native_qasm=True, **request))
        with (
            patch.object(placement, "_transpile_initial_layout", wraps=placement._transpile_initial_layout) as discover,
            patch.object(placement, "compile_and_verify", wraps=placement.compile_and_verify) as compile_circuit,
            patch.object(placement, "_snapshot_simulator", wraps=placement._snapshot_simulator) as setup,
            patch.object(placement, "_sample_snapshot", wraps=placement._sample_snapshot) as sample,
        ):
            plan = PreparedMod4PlacementCheck(model="manila", prefer_native_qasm=True)
            with self.assertRaisesRegex(ValueError, "seeds must differ"):
                plan.run(evaluation_seeds=(29, 29), **request)
            discover.assert_not_called()
            compile_circuit.assert_not_called()
            setup.assert_not_called()

            first = plan.run(**request)
            self.assertEqual(stable_report(first), expected)
            first["oracle_audit"]["marked_states"].clear()
            baseline = first["models"]["manila"]["placements"][0]
            baseline["initial_logical_to_target_qubit"].clear()
            baseline["rounds"][1]["compilation"]["openqasm"].clear()

            second = plan.run(**request)

        discover.assert_called_once()
        self.assertEqual(compile_circuit.call_count, 4)
        self.assertEqual(setup.call_count, 2)
        self.assertEqual(sample.call_count, 16)
        first_simulator = sample.call_args_list[0].args[2]
        second_simulator = sample.call_args_list[8].args[2]
        self.assertIsNot(first_simulator, second_simulator)
        for calls, simulator in ((sample.call_args_list[:8], first_simulator),
                                 (sample.call_args_list[8:], second_simulator)):
            self.assertTrue(all(call.args[2] is simulator for call in calls))
        self.assertEqual(stable_report(second), expected)

    def test_prepared_plan_refuses_reuse_after_uncancelled_timeout(self):
        from austins_algorithm import snapshot_placement as placement

        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": False, "simulation": None}
        with (
            patch.object(placement, "_transpile_initial_layout", wraps=placement._transpile_initial_layout) as discover,
            patch.object(placement, "compile_and_verify", wraps=placement.compile_and_verify) as compile_circuit,
            patch.object(placement, "_snapshot_simulator") as setup,
            patch.object(placement, "_sample_snapshot", return_value=timeout) as sample,
        ):
            plan = PreparedMod4PlacementCheck(prefer_native_qasm=True)
            report = plan.run(minimum_hit_rate=0.4, shots=2)
            self.assertEqual(report["status"], "PARTIAL")
            self.assertTrue(report["local_cancellation_unconfirmed"])
            self.assertEqual(report["models_not_run"], ["jakarta", "torino"])
            report["local_cancellation_unconfirmed"] = False
            with self.assertRaises(RuntimeError):
                plan.run(minimum_hit_rate=0.4, shots=2)

        discover.assert_called_once()
        compile_circuit.assert_called_once()
        setup.assert_called_once()
        sample.assert_called_once()

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
