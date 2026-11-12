"""Check bounded adaptive search on a local IBM calibration snapshot."""

from collections import Counter
from importlib.util import find_spec
from math import fsum
from pathlib import Path
from runpy import run_path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.unknown_count_planning import plan_unknown_count_search
from austins_algorithm.unknown_count_snapshot import compare_unknown_count_snapshot


_STOP = {"policy": "stop", "max_shots": 4, "max_oracle_queries": 1, "max_rounds_per_shot": 1}
_AFFORDABLE = {**_STOP, "policy": "affordable"}
_OPTIONAL_MODULES = ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import")


class UnknownCountSnapshotTests(unittest.TestCase):
    def test_invalid_inputs_are_rejected_before_optional_sdk_imports(self):
        valid = {
            "phase_oracle": object(), "search_qubits": 2, "verifier": lambda bits: bits == "11",
            "plans": {"stop": _STOP}, "seeds": (7, 29),
        }
        overrides = (
            {"search_qubits": 0}, {"model": "torino"}, {"seed_transpiler": 1 << 32},
            {"seeds": ()}, {"seeds": (7, 7)}, {"seeds": (True,)}, {"seeds": (1 << 32,)},
            {"seeds": tuple(range(129))}, {"plans": {}}, {"plans": {str(index): _STOP for index in range(5)}},
            {"plans": {" ": _STOP}},
            {"plans": {"stop": {"policy": "stop"}}},
            {"plans": {"stop": {**_STOP, "unrecognized": 1}}},
            {"plans": {"stop": {**_STOP, "policy": "known"}}},
            {"plans": {"stop": {**_STOP, "max_shots": 33}}},
            {"plans": {"stop": {**_STOP, "max_oracle_queries": 65}}},
            {"plans": {"stop": {**_STOP, "max_rounds_per_shot": -1}}},
        )
        with patch.dict(sys.modules, {name: None for name in _OPTIONAL_MODULES}):
            for changes in overrides:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    compare_unknown_count_snapshot(**{**valid, **changes})
            with self.assertRaisesRegex(TypeError, "verifier must be callable"):
                compare_unknown_count_snapshot(**{**valid, "verifier": None})

    def test_local_timeout_stops_the_remaining_benchmark_cases(self):
        benchmark = run_path(str(ROOT / "examples" / "benchmark_unknown_count.py"))["run_selection_snapshot_benchmark"]
        timeout = Mock(return_value=(
            {"comparison": {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": False}},
            {"wall": 30.0},
        ))
        with patch.dict(benchmark.__globals__, {"_snapshot_holdout_case": timeout, "version": lambda _: "test"}):
            report = benchmark(1)
        timeout.assert_called_once()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["case_statuses"], {"LOCAL_RESULT_WAIT_TIMED_OUT": 1, "NOT_RUN_AFTER_LOCAL_TIMEOUT": 10})
        omitted = report["observations"]["work_tradeoff"]["cases"]["3"]["comparison"]
        self.assertEqual(omitted["status"], "NOT_RUN_AFTER_LOCAL_TIMEOUT")
        self.assertEqual(len(omitted["seeds"]), 1)
        self.assertIsNone(report["timing_seconds"]["work_tradeoff"]["3"])

    @unittest.skipUnless(all(find_spec(name) is not None for name in _OPTIONAL_MODULES), "optional IBM tools unavailable")
    def test_local_snapshot_compares_matched_plans_without_a_provider_account(self):
        from qiskit import QuantumCircuit
        from qiskit_ibm_runtime import QiskitRuntimeService

        from austins_algorithm.compilation import compile_and_verify

        oracle = QuantumCircuit(2)
        oracle.cz(0, 1)
        seeds = (7, 29)
        plans = {"stop": _STOP, "affordable": _AFFORDABLE}
        with (
            patch.object(QiskitRuntimeService, "__init__", side_effect=AssertionError("provider account requested")) as service,
            patch.object(QiskitRuntimeService, "saved_accounts", side_effect=AssertionError("saved accounts requested")) as accounts,
            patch("austins_algorithm.unknown_count_snapshot.compile_and_verify", wraps=compile_and_verify) as compile_round,
        ):
            result = compare_unknown_count_snapshot(
                oracle, 2, lambda bits: bits == "11", plans=plans, seeds=seeds, model="jakarta", seed_transpiler=11,
            )
        service.assert_not_called()
        accounts.assert_not_called()
        self.assertEqual(compile_round.call_count, 2)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["execution_mode"], "SIMULATOR")
        self.assertFalse(result["provider_job_submitted"])
        self.assertFalse(result["quantum_hardware_used"])
        self.assertEqual(result["snapshot"]["model"], "jakarta")
        self.assertEqual(result["seed_transpiler"], 11)
        self.assertEqual(result["seeds"], list(seeds))
        self.assertEqual(result["audit"], {"states_checked": 4, "marked_count": 1, "marked_states": ["11"]})
        for field, limit in {"plans": 4, "trials_per_plan": 128, "shots_per_trial": 32, "oracle_queries_per_trial": 64}.items():
            self.assertEqual(result["limits"][field], limit)
        self.assertEqual(result["rounds_to_prepare"], [0, 1])
        self.assertEqual(set(result["compilations"]), {"0", "1"})
        gate_costs = {}
        noisy_hits = {}
        for rounds, entry in result["compilations"].items():
            with self.subTest(rounds=rounds):
                compilation = entry["compilation"]
                self.assertEqual(compilation["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
                self.assertEqual(compilation["initial_logical_to_target_qubit"], result["anchor_layout"])
                gate_costs[rounds] = compilation["compiled"]["two_qubit_gates"]
                snapshot = entry["snapshot_probabilities"]
                weights = snapshot["outcome_probabilities"]
                self.assertEqual(set(weights), {"00", "01", "10", "11"})
                self.assertTrue(all(0 <= value <= 1 for value in weights.values()))
                self.assertAlmostEqual(fsum(weights.values()), 1, places=12)
                self.assertAlmostEqual(snapshot["verified_hit_probability"], weights["11"], places=12)
                noisy_hits[int(rounds)] = weights["11"]

        self.assertEqual(set(result["plans"]), set(plans))
        for name, budget in plans.items():
            with self.subTest(plan=name):
                comparison = result["plans"][name]
                self.assertEqual(comparison["budget"], budget)
                lanes = comparison["lanes"]
                self.assertEqual(set(lanes), {"ideal", "snapshot"})
                expected_ideal = plan_unknown_count_search(
                    2, 1, **{key: value for key, value in budget.items() if key != "policy"},
                )["policies"][budget["policy"]]
                for field, value in expected_ideal.items():
                    self.assertAlmostEqual(lanes["ideal"]["modeled"][field], value, places=12)
                self.assertAlmostEqual(
                    lanes["snapshot"]["modeled"]["found_probability"],
                    self._enumerated_noisy_hit_probability(noisy_hits, budget),
                    places=12,
                )
                for lane in lanes.values():
                    self.assertEqual([trial["seed"] for trial in lane["trials"]], list(seeds))
                    for trial in lane["trials"]:
                        self._check_trial(trial, budget)
                    self._check_observed(lane, gate_costs)
                paired = Counter()
                for ideal, snapshot in zip(lanes["ideal"]["trials"], lanes["snapshot"]["trials"], strict=True):
                    outcome = ["found" if trial["status"] == "FOUND" else "miss" for trial in (ideal, snapshot)]
                    paired["_".join(outcome)] += 1
                self.assertEqual(comparison["paired_witness_outcomes"], dict(paired))

    @unittest.skipUnless(all(find_spec(name) is not None for name in _OPTIONAL_MODULES), "optional IBM tools unavailable")
    def test_oversize_oracle_reports_target_width_before_audit_or_compile(self):
        from qiskit import QuantumCircuit

        with (
            patch("austins_algorithm.unknown_count_snapshot.audit_phase_oracle") as audit,
            patch("austins_algorithm.unknown_count_snapshot.compile_and_verify") as compile_one,
        ):
            result = compare_unknown_count_snapshot(
                QuantumCircuit(8), 2, lambda _: False, plans={"stop": _STOP}, seeds=(7,), model="jakarta",
            )
        self.assertEqual(result["status"], "INSUFFICIENT_TARGET_WIDTH")
        self.assertEqual(result["circuit_qubits"], 8)
        self.assertEqual(result["compilations"], {})
        self.assertEqual(result["plans"], {})
        audit.assert_not_called()
        compile_one.assert_not_called()

    def _check_trial(self, trial, budget):
        self.assertIn(trial["status"], {"FOUND", "BUDGET_EXHAUSTED"})
        self.assertEqual(trial["shots_used"], len(trial["attempts"]))
        self.assertEqual(trial["verifier_calls"], trial["shots_used"])
        self.assertLessEqual(trial["shots_used"], budget["max_shots"])
        self.assertEqual(trial["oracle_queries"], sum(row["rounds"] for row in trial["attempts"]))
        self.assertLessEqual(trial["oracle_queries"], budget["max_oracle_queries"])
        for row in trial["attempts"]:
            self.assertLessEqual(row["rounds"], budget["max_rounds_per_shot"])
            self.assertEqual(row["verified"], row["sample"] == "11")
        self.assertEqual(trial["candidate"], "11" if trial["status"] == "FOUND" else None)

    def _check_observed(self, lane, gate_costs):
        observed, trials = lane["observed"], lane["trials"]
        witnesses = sum(trial["status"] == "FOUND" for trial in trials)
        self.assertEqual(observed["trials"], len(trials))
        self.assertEqual(observed["witnesses"], witnesses)
        self.assertEqual(observed["found_rate"], witnesses / len(trials))
        for field, total in (("shots", "shots_used"), ("oracle_queries", "oracle_queries"), ("verifier_calls", "verifier_calls")):
            self.assertEqual(observed[field], sum(trial[total] for trial in trials))
        for field, reason in (
            ("shot_budget_exhaustion_rate", "SHOT_BUDGET"),
            ("query_budget_exhaustion_rate", "ORACLE_QUERY_BUDGET"),
        ):
            self.assertEqual(observed[field], sum(trial["exhaustion_reason"] == reason for trial in trials) / len(trials))
        self.assertEqual(observed["round_adjustments"], sum(len(trial.get("round_adjustments", ())) for trial in trials))
        counts = Counter(str(row["rounds"]) for trial in trials for row in trial["attempts"])
        self.assertEqual(observed["shots_by_round"], dict(counts))
        self.assertEqual(observed["compiled_two_qubit_gates"], sum(count * gate_costs[rounds] for rounds, count in counts.items()))

    def _enumerated_noisy_hit_probability(self, probabilities, budget):
        def visit(attempt, remaining):
            if attempt == budget["max_shots"]:
                return 0.0
            choices = (0,) if attempt == 0 else (0, 1)
            branches = []
            for nominal in choices:
                if nominal > remaining and budget["policy"] == "stop":
                    continue
                # An overflow in this one-query fixture leaves only round zero.
                rounds = min(nominal, remaining)
                hit = probabilities[rounds]
                branches.append(hit + (1 - hit) * visit(attempt + 1, remaining - rounds))
            return fsum(branches) / len(choices)

        return visit(0, budget["max_oracle_queries"])


if __name__ == "__main__":
    unittest.main()
