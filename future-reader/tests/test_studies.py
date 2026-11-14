"""Check the synthetic workloads without repeating the full measured run."""

from fractions import Fraction
from random import Random
import unittest

from future_reader.studies import _candidate, _candidates, _ranking, _step, run_study


class StudiesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_study(repetitions=1, seed=41, pruning_trials=100)

    def test_horizon_grid_and_counter_consistency(self):
        timing = self.report["timing"]
        self.assertTrue(timing["analytic_match"])
        self.assertEqual(timing["total_policy_evaluations"], 80_000)
        self.assertEqual(timing["steps_per_horizon"], 20_000)
        self.assertEqual(timing["distinct_input_slots"], 20_000)
        self.assertEqual(timing["independent_trial_count"], "NOT_APPLICABLE")
        horizons = timing["horizons"]
        self.assertEqual([row["matched_full_choice_fraction"] for row in horizons], [0.47615, 0.91975, 0.9895, 1.0])
        for row in horizons:
            disagreements = row["missed_feasible_opportunity"] + row["consequential_state_not_seen"]
            self.assertEqual(row["steps"] - row["matched_full_choice"], disagreements)
        self.assertEqual(self.report["supplied"]["status"], "UNRESOLVED")

    def test_lookahead_censors_checks_only_after_latest_start(self):
        self.assertEqual(_step(0, 10, 10, 10), (True, False, False, 0, True, 10))
        self.assertEqual(_step(0, 11, 10, 20), (True, False, False, 0, False, 10))
        self.assertEqual(_step(3, 10, 10, 9), (False, False, True, 50, False, 9))
        self.assertEqual(_step(2, 10, 11, 9), (False, True, False, 5, False, 9))

    def test_guarded_choice_matches_independent_fraction_oracle(self):
        rng = Random(32017)
        for index in range(100):
            for adversarial in (True, False):
                rows = _candidates(rng, index, adversarial)
                utility = {index: Fraction(chance * reward + (10_000 - chance) * loss, 10_000)
                           for index, (eligible, _, chance, reward, loss, _) in enumerate(rows) if eligible}
                best = max(utility.values(), default=None)
                expected = [index for index, score in utility.items() if score == best]
                value, winners, _, protected_lost, _ = _ranking(rows, "guarded")
                self.assertEqual(winners, expected)
                self.assertEqual(None if value is None else Fraction(value, 10_000), best)
                self.assertEqual(protected_lost, 0)

    def test_hard_constraint_and_tie_survive_ranking(self):
        rows = [
            _candidate(20, True, True, 10_000, 25, -1),
            _candidate(21, True, True, 10_000, 50_000, -1),
            _candidate(20, True, True, 10_000, 25, -1),
            _candidate(20, False, True, 10_000, 50_000, -1),
        ]
        self.assertEqual(_ranking(rows, "guarded")[:2], (250_000, [0, 2]))
        rare = _candidate(20, True, True, 150, 5_000, -3, protected=True)
        self.assertEqual(_ranking([rows[0], rare], "guarded")[1], [1])
        self.assertEqual(_ranking([rows[0], rare], "probability_only")[1:4], ([0], 1, 1))

    def test_observed_adversarial_guardrails_and_resource_limits(self):
        values = {(row["cohort"], row["policy"]): row for row in self.report["pruning"]["results"]}
        for cohort in ("ordinary", "adversarial"):
            self.assertEqual(values[cohort, "guarded"]["altered_decisions"], 0)
            self.assertEqual(values[cohort, "guarded"]["protected_paths_lost"], 0)
        self.assertGreater(values["adversarial", "probability_only"]["altered_decisions"], 0)
        with self.assertRaises(ValueError):
            run_study(repetitions=501)


if __name__ == "__main__":
    unittest.main()
