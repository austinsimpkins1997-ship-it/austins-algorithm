"""Validate the finite timing receipt, independence check and minimality witnesses."""

from copy import deepcopy
import json
import unittest

from future_reader.codec import digest
from future_reader.exact_grid import _event_choice, run_grid, verify_grid
from future_reader.studies import EVENTS, _latency_grid


class ExactGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receipt = run_grid()

    def test_original_weighted_grid_and_all_policy_counts_are_preserved(self):
        report = self.receipt
        self.assertEqual(report["scope"]["enumerated_slots"], 20_000)
        self.assertEqual(report["scope"]["distinct_event_arrival_commitment_tuples"], 3_476)
        self.assertEqual(report["scope"]["original_total_policy_evaluations"], 10_000_000)
        self.assertFalse(report["scope"]["repeated_slots_are_independent_trials"])
        self.assertEqual(report["verification"]["weighted_case_policy_comparisons"], 140_000)
        self.assertEqual(report["verification"]["unique_within_stratum_case_policy_comparisons"], 60_200)
        policies = report["policies"]
        for name, cap, correct, waited in (
            ("original-1-median", 118, 9_523, 2_161_128),
            ("original-2-median", 236, 18_395, 3_009_260),
            ("original-16/5-median", 377, 19_790, 3_532_965),
            ("one-tick-below-minimum", 413, 19_995, 3_591_870),
            ("minimum-uniform", 414, 20_000, 3_592_535),
            ("original-5-median", 590, 20_000, 3_593_195),
            ("minimum-known-stratum", None, 20_000, 3_590_380),
        ):
            row = policies[name]
            self.assertEqual(row["uniform_cap_ticks"], cap)
            self.assertEqual(row["exact_choice_fraction"], {"numerator": correct, "denominator": 20_000})
            self.assertEqual(row["mean_wait_ticks"], {"numerator": waited, "denominator": 20_000})

    def test_minimality_witnesses_reference_real_slots(self):
        report = self.receipt
        strata = {(drift, clock): rows for drift, clock, rows in _latency_grid()}
        globally = report["global_minimum"]
        self.assertEqual(globally["cap_ticks"], 414)
        self.assertEqual(len(globally["minimality_witnesses"]), 5)
        for stratum in report["strata"]:
            clock = stratum["clock_uncertainty_ticks"]
            cap = stratum["minimum_cap_ticks"]
            self.assertEqual(cap, 414 - clock)
            self.assertTrue(stratum["minimality_witnesses"])
            for row in stratum["minimality_witnesses"]:
                case = strata[row["drift_ticks"], clock][row["slot"]]
                self.assertEqual((EVENTS[case[0]], case[1], case[2]),
                                 (row["check"], row["arrival_tick"], row["latest_commit_tick"]))
                self.assertEqual(_event_choice(*case, cap - 1)[0], row["previous_choice"])
                self.assertEqual(_event_choice(*case, cap)[0], row["reference_choice"])
                self.assertNotEqual(row["previous_choice"], row["reference_choice"])

    def test_reveal_wins_both_ties_and_late_observation_remains_censored(self):
        self.assertEqual(_event_choice(0, 10, 10, 10), ("USE_PREREQUISITE", True, 10))
        self.assertEqual(_event_choice(2, 0, 0, 0), ("USE_FALLBACK", True, 0))
        self.assertEqual(_event_choice(1, 10, 10, 9), ("COMMIT", False, 9))
        self.assertEqual(_event_choice(3, 11, 10), ("COMMIT", False, 10))
        for row in ((4, 10, 11), (0, 2, -2), (2, 10, 10, True)):
            with self.assertRaises(ValueError):
                _event_choice(*row)

    def test_digest_replay_rejects_resealed_changes_and_unexpected_fields(self):
        run = json.loads(json.dumps(self.receipt))
        self.assertTrue(verify_grid(run))
        self.assertEqual(run.pop("sha256"), digest(run))
        altered = deepcopy(self.receipt)
        altered["global_minimum"]["cap_ticks"] -= 1
        altered["sha256"] = digest({key: value for key, value in altered.items() if key != "sha256"})
        self.assertFalse(verify_grid(altered))
        altered = deepcopy(self.receipt)
        altered["authorization"] = "APPROVED"
        self.assertFalse(verify_grid(altered))


if __name__ == "__main__":
    unittest.main()
