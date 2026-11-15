import json
import math
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm import optimization
from austins_algorithm.core import theoretical_success_probability


def weighted_problem():
    return {
        "name": "Choose a link",
        "variables": [{"name": "Primary", "cost": 5}, {"name": "Backup", "cost": 1}],
        "clauses": [[1, 2]],
    }


class OptimizationTests(unittest.TestCase):
    def test_exact_weighted_minimum_ties_and_infeasibility(self):
        problem = weighted_problem()
        optimum = optimization.exact_minimum(problem)
        self.assertEqual(optimum, {
            "status": "OPTIMUM", "bits": "10", "cost": 1, "ties": ["10"],
            "feasible_count": 3, "assignments_checked": 4,
        })
        problem["variables"][0]["cost"] = 1
        self.assertEqual(optimization.exact_minimum(problem)["ties"], ["01", "10"])
        problem["clauses"] = [[1], [-1]]
        self.assertEqual(optimization.exact_minimum(problem), {
            "status": "INFEASIBLE", "bits": None, "cost": None, "ties": [],
            "feasible_count": 0, "assignments_checked": 4,
        })

    def test_validation_rejects_wrong_schema_malformed_literals_and_budgets(self):
        for mutate in (
            lambda problem: problem.update(extra=5),
            lambda problem: problem.update(name="  "),
            lambda problem: problem.update(clauses=[[0]]),
            lambda problem: problem.update(clauses=[[True]]),
            lambda problem: problem.update(clauses=[[3]]),
            lambda problem: problem["variables"][0].update(cost=True),
            lambda problem: problem["variables"][0].update(cost=-1),
            lambda problem: problem["variables"][0].update(cost=optimization.MAX_WEIGHT + 1),
            lambda problem: problem["variables"][0].update(name=" backup "),
            lambda problem: problem.update(variables=[]),
        ):
            problem = weighted_problem()
            mutate(problem)
            with self.assertRaises(ValueError):
                optimization.problem_from_document(problem)
        for settings in (
            {"seed": -1}, {"max_shots": 0}, {"max_queries": True},
            {"max_rounds": optimization.MAX_ROUNDS + 1}, {"prove": 1},
        ):
            with self.assertRaises(ValueError):
                optimization.SearchSession(weighted_problem(), **settings)

    def test_oracle_and_diffusion_frames_have_expected_amplitudes(self):
        preview = optimization.preview(weighted_problem(), incumbent=5, rounds=2)
        self.assertEqual([row["bits"] for row in preview["states"] if row["marked"]], ["10"])
        self.assertEqual([(row["stage"], row["round"]) for row in preview["frames"]], [
            ("prepare", 0), ("oracle", 1), ("diffusion", 1), ("oracle", 2), ("diffusion", 2),
        ])
        self.assertEqual(preview["frames"][0]["amplitudes"], [0.5, 0.5, 0.5, 0.5])
        self.assertEqual(preview["frames"][1]["amplitudes"], [0.5, 0.5, -0.5, 0.5])
        self.assertEqual(preview["frames"][2]["amplitudes"], [0.0, 0.0, 1.0, 0.0])
        for frame in preview["frames"]:
            self.assertAlmostEqual(math.fsum(frame["probabilities"]), 1.0)
            round_index = frame["round"] - (frame["stage"] == "oracle")
            self.assertAlmostEqual(frame["marked_probability"], theoretical_success_probability(2, 1, round_index))

    def test_empty_tautological_and_repeated_clauses_match_reference(self):
        problem = weighted_problem()
        for clauses in ([], [[]], [[1, -1]], [[-1, -1, 2], [-2]], [[1], [-1]]):
            with self.subTest(clauses=clauses):
                problem["clauses"] = clauses
                trace = optimization.preview(problem, rounds=3)
                exact = optimization.exact_minimum(problem)
                self.assertEqual(trace["marked_count"], exact["feasible_count"])
                for frame in trace["frames"]:
                    self.assertAlmostEqual(math.fsum(frame["probabilities"]), 1.0)
                    self.assertAlmostEqual(
                        frame["marked_probability"],
                        theoretical_success_probability(
                            2, exact["feasible_count"], frame["round"] - (frame["stage"] == "oracle"),
                        ),
                    )

    def test_sampled_candidate_checked_independently_before_spending(self):
        session = optimization.SearchSession(weighted_problem(), prove=False)
        fabricated = session.preview(0)
        fabricated["states"][0].update(marked=True, feasible=True, cost=0)
        fabricated["frames"][0]["probabilities"] = [1.0, 0.0, 0.0, 0.0]
        with patch.object(session, "preview", return_value=fabricated):
            with self.assertRaisesRegex(RuntimeError, "verifier disagree"):
                session.measure(0)
        self.assertIsNone(session.best)
        self.assertEqual((session.shots_used, session.queries_used), (0, 0))

    def test_query_limit_preserves_zero_round_shots(self):
        session = optimization.SearchSession(
            weighted_problem(), seed=7, max_shots=2, max_queries=1, max_rounds=4, prove=False,
        )
        session.measure(1)
        with self.assertRaisesRegex(ValueError, "remaining query budget"):
            session.measure(1)
        self.assertEqual((session.shots_used, session.queries_used), (1, 1))
        self.assertEqual(session.preview(4)["rounds"], 4)
        session.measure(0)
        report = session.report()
        self.assertEqual((report["status"], report["termination"]), ("BUDGET_EXHAUSTED", "SHOT_LIMIT"))
        self.assertEqual(report["shots_used"], 2)
        self.assertEqual(report["queries_used"], 1)
        self.assertIsNone(report["reference"])
        with self.assertRaisesRegex(ValueError, "finished"):
            session.measure(0)

    def test_exact_reference_never_replaces_sampled_best(self):
        session = optimization.SearchSession(weighted_problem(), max_shots=1)
        self.assertIsNone(session.best)
        self.assertEqual(session.report()["reference"]["bits"], "10")
        # With one round and three marked states, diffusion amplifies the unmarked state.
        event = session.measure(1)
        self.assertEqual(event["bits"], "00")
        self.assertFalse(event["accepted"])
        self.assertIsNone(session.best)
        self.assertEqual(session.status, "BUDGET_EXHAUSTED")
        problem = weighted_problem()
        problem["clauses"] = [[1], [-1]]
        proof = optimization.SearchSession(problem).run()
        self.assertEqual(proof["status"], "INFEASIBLE")
        self.assertEqual(proof["shots_used"], 0)
        self.assertIsNone(proof["best"])
        self.assertEqual(
            optimization.SearchSession(problem, max_shots=1, prove=False).run()["status"],
            "BUDGET_EXHAUSTED",
        )

    def test_proven_optimum_stops_only_after_a_sample_matches_the_reference(self):
        problem = weighted_problem()
        problem["clauses"] = [[2], [-1]]
        session = optimization.SearchSession(problem, max_shots=4)
        self.assertEqual(session.status, "RUNNING")
        self.assertIsNone(session.best)
        event = session.measure(1)
        self.assertEqual(event["incumbent_after"], {"bits": "10", "cost": 1})
        self.assertEqual((session.status, session.termination), ("OPTIMUM", "PROVEN_OPTIMUM"))
        self.assertEqual(session.shots_remaining, 3)
        self.assertEqual(session.preview(1)["marked_count"], 0)
        with self.assertRaisesRegex(ValueError, "finished"):
            session.measure(1)

    def test_shot_replay_restores_automatic_continuation(self):
        settings = {"seed": 81, "max_shots": 12, "max_queries": 8, "prove": False}
        session = optimization.SearchSession(weighted_problem(), **settings)
        session.auto_step()
        session.measure(1)
        session.auto_step()
        partial = json.loads(json.dumps(session.report(), allow_nan=False))
        replay = optimization.SearchSession(partial["problem"], **partial["settings"])
        for event in partial["events"]:
            self.assertEqual(replay.measure(event["rounds"]), event)
        self.assertEqual(replay.report(), partial)
        self.assertEqual(replay.run(), session.run())
        self.assertLessEqual(replay.queries_used, settings["max_queries"])
        accepted = [event["cost"] for event in replay.report()["events"] if event["accepted"]]
        self.assertTrue(all(earlier > later for earlier, later in zip(accepted, accepted[1:])))

    def test_reports_and_callback_payloads_do_not_mutate_session(self):
        problem = weighted_problem()
        original = deepcopy(problem)
        session = optimization.SearchSession(problem, prove=False)
        problem["variables"][0]["cost"] = 0
        report = session.run(
            should_stop=lambda: session.shots_used >= 2,
            on_event=lambda event: event["trace"]["states"].clear(),
        )
        self.assertEqual(report["problem"], original)
        self.assertEqual(report["status"], "RUNNING")
        self.assertEqual(len(report["events"]), 2)
        self.assertEqual(len(report["events"][0]["trace"]["states"]), 4)
        report["events"].clear()
        self.assertEqual(session.shots_used, 2)


if __name__ == "__main__":
    unittest.main()
