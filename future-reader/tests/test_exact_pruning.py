"""Signed utility bounds, exact ties, and deterministic study coverage."""

from fractions import Fraction
from random import Random
import unittest

from future_reader.exact_pruning import POLICIES, fraction_reference, rank, run_scale_study


def route(name, chance=10_000, success=10, failure=-1, **fields):
    return {"id": name, "duration": 20, "permitted": True, "calibrated": True, "protected": False,
            "chance_bps": chance, "success": success, "failure": failure, **fields}


def choice(report):
    score = report["score"]
    return report["winners"], None if score is None else Fraction(score["numerator"], score["denominator"])


class ExactPruningTests(unittest.TestCase):
    def test_better_failure_outcome_raises_certified_ceiling(self):
        inputs = [
            route("first"), route("failure-wins", 5000, 1, 500),
            route("below", success=2, failure=3), route("protected", success=-2, failure=-5, protected=True),
        ]
        report = rank(inputs, deadline=20)
        self.assertEqual(choice(report), (["failure-wins"], Fraction(501, 2)))
        self.assertEqual(report["evaluated"], ["first", "failure-wins", "protected"])
        self.assertEqual(report["deferred"], [{
            "id": "below", "upper_numerator": 30_000, "incumbent_numerator": 2_505_000,
            "reason": "STRICTLY_BELOW_INCUMBENT",
        }])
        self.assertEqual(report["protected_eligible"], ["protected"])

    def test_strict_tie_and_hard_constraint_precedence(self):
        inputs = [
            route("incumbent"), route("tie", chance=0, success=-10, failure=10),
            route("too-late", success=1000, duration=21, protected=True),
            route("not-permitted", success=2000, permitted=False, protected=True),
            route("uncalibrated", success=3000, calibrated=False, protected=True),
            route("early", success=8, duration=19),
        ]
        for policy in POLICIES:
            result = rank(inputs, deadline=20, policy=policy)
            self.assertEqual(choice(result), (["incumbent", "tie"], Fraction(10)))
            self.assertEqual(result["protected_eligible"], [])
            self.assertEqual(result["ineligible"], [
                {"id": "too-late", "reasons": ["DEADLINE"]},
                {"id": "not-permitted", "reasons": ["PERMISSION"]},
                {"id": "uncalibrated", "reasons": ["CALIBRATION"]},
            ])
            result = rank(inputs, deadline=20, equality=False, policy=policy)
            self.assertEqual(choice(result), (["early"], Fraction(8)))

    def test_negative_scores_remain_rankable_and_empty_scope_is_explicit(self):
        self.assertEqual(choice(rank([route("only", success=-10, failure=-3)], deadline=21)),
                         (["only"], Fraction(-10)))
        empty = rank([], deadline=21)
        self.assertEqual((empty["status"], empty["winners"], empty["score"]), ("NO_ELIGIBLE_ROUTE", [], None))

    def test_invalid_inputs_and_policy_fail_closed(self):
        cases = [
            [route("duplicate"), route("duplicate")],
            [route("p", chance=10_001)],
            [route("p", chance=True)],
            [route("p", success=0.5)],
            [route("p", permitted=1)],
            [route("p", bound_slack=-1)],
            [route("p", accidental=4)],
            [route("unsafe/name")],
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                rank(case, deadline=20)
        with self.assertRaises(ValueError):
            rank([], deadline=20, equality=1)
        with self.assertRaises(ValueError):
            rank([], deadline=20, policy="p-cutoff")

    def test_order_chaos_matches_independent_fraction_reference(self):
        random = Random(991)
        rows = [
            route(str(index), chance=random.randrange(10_001), success=random.randrange(-100, 100),
                  failure=random.randrange(-100, 100), protected=index % 10 == 0,
                  duration=random.randrange(10, 23), permitted=index % 7 != 0)
            for index in range(128)
        ]
        for equality in (False, True):
            expected = choice(fraction_reference(rows, deadline=20, equality=equality))
            for policy in POLICIES:
                result = rank(rows, deadline=20, equality=equality, policy=policy)
                self.assertEqual(choice(result), expected)
                self.assertTrue(set(result["protected_eligible"]) <= set(result["evaluated"]))
                self.assertEqual(result["counts"]["eligible"], len(result["evaluated"]) + len(result["deferred"]))
                self.assertTrue(all(row["upper_numerator"] < row["incumbent_numerator"] for row in result["deferred"]))

    def test_scale_grid_records_inputs_and_separates_modeled_cost(self):
        study = run_scale_study(trials=1, seed=(1 << 64) - 1)
        self.assertEqual(study, run_scale_study(trials=1, seed=(1 << 64) - 1))
        self.assertEqual({row["cohort"] for row in study["results"]}, {"ordinary", "holdout", "adversarial"})
        self.assertEqual({row["route_count"] for row in study["results"]}, {8, 32, 128, 1024})
        for row in study["results"]:
            self.assertEqual(len(row["workload_sha256"]), 64)
            for sample in row["results"].values():
                self.assertEqual(sample["eligible"] + sample["ineligible"], row["route_count"])
                self.assertEqual(sample["reference_disagreements"], 0)
                self.assertEqual(sample["modeled_exact_work"][-1]["total_units"], sample["exact_evaluations"] * 1024)


if __name__ == "__main__":
    unittest.main()
