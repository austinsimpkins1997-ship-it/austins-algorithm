"""SDK-free checks for target-based unknown-count budget selection."""

from contextlib import redirect_stdout
from io import StringIO
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.__main__ import main as cli_main
from austins_algorithm.unknown_count_planning import plan_unknown_count_search, select_unknown_count_budgets


_WORK_FIELDS = ("worst_case_expected_shots", "worst_case_expected_oracle_queries")


def _candidate(report, policy, shots, queries):
    return next(
        row for row in report["candidates"]
        if (row["policy"], row["max_shots"], row["max_oracle_queries"]) == (policy, shots, queries)
    )


class UnknownCountSelectionTests(unittest.TestCase):
    def test_each_grid_candidate_matches_independent_planner_calls(self):
        counts = (1, 3, 4)
        shot_caps = (1, 3, 6)
        query_caps = (0, 1, 3)
        target = .5
        report = select_unknown_count_budgets(
            3, counts, target_probability=target, shot_caps=shot_caps, query_caps=query_caps,
            max_rounds_per_shot=2,
        )
        self.assertEqual(report["candidate_count"], 2 * len(shot_caps) * len(query_caps))
        self.assertEqual(len(report["candidates"]), report["candidate_count"])
        for shots in shot_caps:
            for queries in query_caps:
                plans = [
                    plan_unknown_count_search(
                        3, count, max_shots=shots, max_oracle_queries=queries, max_rounds_per_shot=2,
                    )["policies"]
                    for count in counts
                ]
                for policy in ("stop", "affordable"):
                    with self.subTest(shots=shots, queries=queries, policy=policy):
                        row = _candidate(report, policy, shots, queries)
                        hypothesis_rows = row["hypotheses"]
                        self.assertEqual([item["solution_count"] for item in hypothesis_rows], list(counts))
                        expected = [plan[policy] for plan in plans]
                        for item, metrics in zip(hypothesis_rows, expected, strict=True):
                            for key, value in metrics.items():
                                self.assertAlmostEqual(item[key], value, places=12, msg=key)
                        minimum_hit = min(metrics["found_probability"] for metrics in expected)
                        maximum_miss = max(
                            metrics["shot_budget_exhaustion_probability"] + metrics["query_budget_exhaustion_probability"]
                            for metrics in expected
                        )
                        self.assertAlmostEqual(row["minimum_found_probability"], minimum_hit, places=12)
                        self.assertAlmostEqual(row["maximum_failure_probability"], maximum_miss, places=12)
                        self.assertAlmostEqual(row[_WORK_FIELDS[0]], max(item["expected_shots"] for item in expected))
                        self.assertAlmostEqual(row[_WORK_FIELDS[1]], max(item["expected_oracle_queries"] for item in expected))
                        self.assertEqual(row["meets_target"], minimum_hit >= target)

        feasible = [row for row in report["candidates"] if row["meets_target"]]
        self.assertEqual(report["feasible_count"], len(feasible))
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_MET")
        work_points = {tuple(row[field] for field in _WORK_FIELDS) for row in feasible}
        nondominated = {
            point for point in work_points
            if not any(other != point and all(a <= b for a, b in zip(other, point)) for other in work_points)
        }
        frontier = {tuple(row[field] for field in _WORK_FIELDS) for row in report["feasible_work_frontier"]}
        self.assertEqual(frontier, nondominated)
        for objective, field in zip(("fewest_expected_shots", "fewest_expected_oracle_queries"), _WORK_FIELDS):
            recommended = report["recommendations"][objective]
            self.assertTrue(recommended["meets_target"])
            self.assertEqual(recommended[field], min(row[field] for row in feasible))

    def test_affordable_policy_can_meet_target_only_at_smaller_query_cap(self):
        report = select_unknown_count_budgets(
            2, [3, 3], target_probability=.9, shot_caps=[2, 1, 2], query_caps=[1, 0, 1],
        )
        self.assertEqual(report["inputs"]["solution_counts"], [3])
        self.assertEqual(report["inputs"]["shot_caps"], [1, 2])
        self.assertEqual(report["inputs"]["query_caps"], [0, 1])
        self.assertEqual(report["candidate_count"], 8)
        self.assertEqual(report["feasible_count"], 1)
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_MET")
        zero_queries = _candidate(report, "affordable", 2, 0)
        one_query = _candidate(report, "affordable", 2, 1)
        self.assertAlmostEqual(zero_queries["minimum_found_probability"], .9375)
        self.assertAlmostEqual(one_query["minimum_found_probability"], .84375)
        self.assertTrue(zero_queries["meets_target"])
        self.assertFalse(one_query["meets_target"])
        self.assertEqual(report["best_attainable"]["minimum_found_probability"], .9375)
        for recommendation in report["recommendations"].values():
            self.assertEqual((recommendation["policy"], recommendation["max_shots"], recommendation["max_oracle_queries"]),
                             ("affordable", 2, 0))

    def test_larger_solution_hypothesis_can_be_the_limiting_case(self):
        report = select_unknown_count_budgets(
            3, [4, 3], target_probability=.753, shot_caps=[2], query_caps=[0, 1],
        )
        row = _candidate(report, "affordable", 2, 1)
        self.assertAlmostEqual(row["hypotheses"][0]["found_probability"], .755859375)
        self.assertAlmostEqual(row["hypotheses"][1]["found_probability"], .75)
        self.assertAlmostEqual(row["minimum_found_probability"], .75)
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_INFEASIBLE")
        self.assertEqual(report["feasible_count"], 0)
        self.assertEqual(report["feasible_work_frontier"], [])
        self.assertEqual(report["recommendations"], {"fewest_expected_shots": None, "fewest_expected_oracle_queries": None})

    def test_target_one_preserves_positive_failure_when_hit_rounds_to_one(self):
        report = select_unknown_count_budgets(
            6, [63], target_probability=1.0, shot_caps=[12], query_caps=[0],
        )
        row = _candidate(report, "affordable", 12, 0)
        self.assertEqual(row["minimum_found_probability"], 1.0)
        self.assertGreater(row["maximum_failure_probability"], 0.0)
        self.assertFalse(row["meets_target"])
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_INFEASIBLE")

        tiny_failure = select_unknown_count_budgets(
            12, [4095], target_probability=1.0, shot_caps=[256], query_caps=[0],
        )
        self.assertFalse(_candidate(tiny_failure, "affordable", 256, 0)["meets_target"])

        full = select_unknown_count_budgets(
            6, [64], target_probability=1.0, shot_caps=[1], query_caps=[0],
        )
        self.assertEqual(full["status"], "UNKNOWN_COUNT_TARGET_MET")
        self.assertEqual(full["feasible_count"], 2)
        self.assertEqual(full["recommendations"]["fewest_expected_shots"]["worst_case_expected_shots"], 1.0)

    def test_cli_distinguishes_shot_and_query_recommendations(self):
        stdout = StringIO()
        arguments = [
            "austins_algorithm", "unknown-count-select", "--qubits", "2", "--solutions", "1",
            "--target-probability", "0.9", "--shot-caps", *map(str, range(1, 11)), "--query-caps", "0", "1", "2", "3",
        ]
        with patch.object(sys, "argv", arguments), redirect_stdout(stdout):
            cli_main()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_MET")
        self.assertEqual(len(report["feasible_work_frontier"]), 2)
        for objective, caps, work in (
            ("fewest_expected_shots", ("stop", 4, 1), (2.13671875, .568359375)),
            ("fewest_expected_oracle_queries", ("affordable", 9, 0), (3.6996612548828125, 0)),
        ):
            with self.subTest(objective=objective):
                row = report["recommendations"][objective]
                self.assertEqual((row["policy"], row["max_shots"], row["max_oracle_queries"]), caps)
                for field, expected in zip(_WORK_FIELDS, work):
                    self.assertAlmostEqual(row[field], expected, places=12)

    def test_invalid_inputs_are_rejected(self):
        valid = {"search_qubits": 3, "solution_counts": [1], "target_probability": .5, "shot_caps": [1], "query_caps": [0]}
        for overrides in (
            {"search_qubits": 0}, {"solution_counts": []}, {"solution_counts": [9]}, {"solution_counts": [True]},
            {"target_probability": True}, {"target_probability": 0}, {"target_probability": 1.01},
            {"target_probability": math.nan}, {"shot_caps": []}, {"shot_caps": [0]}, {"shot_caps": [257]},
            {"query_caps": []}, {"query_caps": [-1]}, {"query_caps": [4097]}, {"max_rounds_per_shot": 257},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                select_unknown_count_budgets(**{**valid, **overrides})

    def test_profile_cap_includes_both_policies(self):
        report = select_unknown_count_budgets(
            1, [1], target_probability=.6, shot_caps=[1], query_caps=range(4096),
        )
        self.assertEqual(report["candidate_count"], 8192)
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_INFEASIBLE")
        with self.assertRaisesRegex(ValueError, "8192"):
            select_unknown_count_budgets(
                1, [1], target_probability=.6, shot_caps=[1], query_caps=range(4097),
            )


if __name__ == "__main__":
    unittest.main()
