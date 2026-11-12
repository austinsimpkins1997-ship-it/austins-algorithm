"""Checks for the fixed unknown-count budget holdout benchmark."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
from runpy import run_path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_EXAMPLE = ROOT / "examples" / "benchmark_unknown_count.py"
_BENCHMARK = run_path(str(_EXAMPLE))
_threshold_constraint = _BENCHMARK["_threshold_constraint"]
run_selection_holdout_benchmark = _BENCHMARK["run_selection_holdout_benchmark"]

_SUITES = {
    "sparse_and_dense": ((1, 49), (0, 2, 16, 32, 50, 64), 1),
    "work_tradeoff": ((1,), (2, 3), 2),
}
_DEVELOPMENT_SEEDS = {7, *range(4101, 4229)}
_BUDGET_KEYS = ("policy", "max_shots", "max_oracle_queries")


class UnknownCountHoldoutTests(unittest.TestCase):
    def test_threshold_formula_matches_unsigned_integer_comparison(self):
        for width in (1, 2, 6):
            for count in range((1 << width) + 1):
                with self.subTest(width=width, count=count):
                    formula = _threshold_constraint(width, count)
                    actual = [
                        value for value in range(1 << width)
                        if formula.matches(format(value, f"0{width}b"))
                    ]
                    self.assertEqual(actual, list(range(count)))

    def test_holdout_rejects_invalid_trial_counts_without_sdk(self):
        for trials in (0, 129, True, 1.0):
            with self.subTest(trials=trials), self.assertRaisesRegex(ValueError, "trials must be an integer"):
                run_selection_holdout_benchmark(trials)

        with (
            patch.object(sys, "argv", [str(_EXAMPLE), "--validate-budget-selection", "--trials", "0"]),
            redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as error,
        ):
            run_path(str(_EXAMPLE), run_name="__main__")
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--trials must be", stderr.getvalue())

    @unittest.skipUnless(find_spec("qiskit") is not None, "optional Qiskit unavailable")
    def test_real_holdout_uses_selected_budgets_and_fresh_seeded_trials(self):
        trial_count = 2
        stdout = StringIO()
        with (
            patch.object(sys, "argv", [str(_EXAMPLE), "--validate-budget-selection", "--trials", str(trial_count)]),
            redirect_stdout(stdout),
        ):
            run_path(str(_EXAMPLE), run_name="__main__")
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["experiment"], "LOCAL_IDEAL_UNKNOWN_COUNT_SELECTION_HOLDOUT")
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        config, observations = report["configuration"], report["observations"]
        semantic = {"configuration": config, "observations": observations}
        encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(report["observations_sha256"], sha256(encoded).hexdigest())
        self.assertEqual(config["trials_per_arm"], trial_count)
        self.assertEqual(config["sampler"], "prepared_probabilities")
        self.assertTrue(config["controller_only"])
        confidence = config["confidence_interval"]
        self.assertEqual(confidence["method"], "CLOPPER_PEARSON_BONFERRONI")
        self.assertEqual(confidence["family_confidence"], .95)
        self.assertEqual(confidence["arms"], 14)
        self.assertAlmostEqual(confidence["per_arm_confidence"], 1 - .05 / 14)

        self.assertEqual(set(observations), set(_SUITES))
        seen = set()
        for name, (declared, held_out, plan_count) in _SUITES.items():
            suite = observations[name]
            selection = suite["selection"]
            self.assertEqual(selection["inputs"]["solution_counts"], list(declared))
            self.assertEqual(len(suite["plans"]), plan_count)
            self.assertEqual(set(suite["cases"]), {str(count) for count in (*declared, *held_out)})
            recommendations = {
                tuple(row[key] for key in _BUDGET_KEYS) for row in selection["recommendations"].values()
            }
            selected = {
                tuple(plan["selected_budget"][key] for key in _BUDGET_KEYS)
                for plan in suite["plans"].values()
            }
            self.assertEqual(selected, recommendations)
            width = selection["inputs"]["search_qubits"]
            for count in (*declared, *held_out):
                with self.subTest(suite=name, count=count):
                    case = suite["cases"][str(count)]
                    self.assertEqual(case["role"], "DECLARED" if count in declared else "HOLDOUT")
                    self.assertEqual(case["reference"]["states_checked"], 1 << width)
                    self.assertEqual(case["reference"]["solution_count"], count)
                    witnesses = [format(value, f"0{width}b") for value in range(count)]
                    self.assertEqual(case["reference"]["witnesses"], witnesses)
                    seeds = case["seeds"]
                    self.assertEqual(len(seeds), trial_count)
                    self.assertEqual(len(set(seeds)), trial_count)
                    self.assertTrue(set(seeds).isdisjoint(_DEVELOPMENT_SEEDS | seen))
                    seen.update(seeds)
                    self.assertEqual(set(case["plans"]), set(suite["plans"]))
                    for plan_name, arm in case["plans"].items():
                        budget = suite["plans"][plan_name]["selected_budget"]
                        self._check_arm(arm, budget, seeds, witnesses, 1 << width, confidence["per_arm_confidence"])

    def _check_arm(self, arm, budget, seeds, witnesses, size, per_arm_confidence):
        self.assertEqual([trial["seed"] for trial in arm["trials"]], seeds)
        hits = 0
        for trial in arm["trials"]:
            self.assertEqual(trial.get("query_budget_policy", "stop"), budget["policy"])
            for cap in ("max_shots", "max_oracle_queries"):
                self.assertEqual(trial[cap], budget[cap])
            self.assertEqual(trial["shots_used"], len(trial["attempts"]))
            self.assertEqual(trial["verifier_calls"], trial["shots_used"])
            self.assertLessEqual(trial["shots_used"], budget["max_shots"])
            self.assertEqual(trial["oracle_queries"], sum(row["rounds"] for row in trial["attempts"]))
            self.assertLessEqual(trial["oracle_queries"], budget["max_oracle_queries"])
            for row in trial["attempts"]:
                self.assertEqual(row["verified"], row["sample"] in witnesses)
            if trial["status"] == "FOUND":
                self.assertIn(trial["candidate"], witnesses)
                hits += 1
            else:
                self.assertEqual(trial["status"], "BUDGET_EXHAUSTED")
                self.assertIsNone(trial["candidate"])

        observed, interval = arm["observed"], arm["interval"]
        self.assertEqual(observed["witnesses"], hits)
        self.assertEqual(observed["trials"], len(seeds))
        self.assertEqual(observed["found_rate"], hits / len(seeds))
        self.assertLessEqual(interval["lower"], observed["found_rate"])
        self.assertGreaterEqual(interval["upper"], observed["found_rate"])
        alpha_root = ((1 - per_arm_confidence) / 2) ** (1 / len(seeds))
        if not witnesses:
            self.assertEqual(arm["modeled"]["found_probability"], 0)
            self.assertEqual(hits, 0)
            self.assertEqual(interval["lower"], 0)
            self.assertAlmostEqual(interval["upper"], 1 - alpha_root)
        elif len(witnesses) == size:
            self.assertEqual(arm["modeled"]["found_probability"], 1)
            self.assertEqual(hits, len(seeds))
            self.assertTrue(all(trial["shots_used"] == 1 and trial["oracle_queries"] == 0 for trial in arm["trials"]))
            self.assertAlmostEqual(interval["lower"], alpha_root)
            self.assertEqual(interval["upper"], 1)


if __name__ == "__main__":
    unittest.main()
