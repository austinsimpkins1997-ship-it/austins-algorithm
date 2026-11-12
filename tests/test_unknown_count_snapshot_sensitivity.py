"""SDK-free checks for bounded sensitivity of snapshot layout decisions."""

from pathlib import Path
import sys
import unittest


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm.constraints import BooleanConstraint
from test_unknown_count_snapshot_layout import _BITS, _GATES, _KEYS, _SHOTS, _bundle, _cases, _panels, _plan, _set_distribution
from test_unknown_count_snapshot_profile import _SUITE


_QUERIES = "worst_case_expected_oracle_queries"
_FOUND = "minimum_found_probability"


def _candidate(report, layout, policy, shots, queries):
    return next(row for row in report["candidates"]
                if tuple(row[key] for key in _KEYS) == (layout, policy, shots, queries))


def _unannotated(row):
    return {key: value for key, value in row.items() if key != "sensitivity"}


def _signature(row):
    return tuple(row[key] for key in _KEYS)


def _extreme_panels(count):
    panels = _panels()
    witnesses = list(_BITS[:count])
    constraint = BooleanConstraint(2, ((),) if count == 0 else ())
    for panel in panels:
        suite = panel["observations"][_SUITE]
        suite["selection"]["inputs"]["solution_counts"] = [count]
        case = suite["cases"].pop("1")
        case["reference"].update(solution_count=count, witnesses=witnesses, constraint_sha256=constraint.digest())
        case["comparison"]["audit"].update(marked_count=count, marked_states=witnesses)
        for rounds in (0, 1):
            _set_distribution(case, rounds, (.25, .25, .25, .25))
        suite["cases"][str(count)] = case
    return panels


class UnknownCountSnapshotSensitivityTests(unittest.TestCase):
    def test_zero_radius_annotates_existing_ranking_without_changing_it(self):
        bundles = list(map(_bundle, _panels()))
        baseline = _plan(bundles, validation_trials=2)
        self.assertNotIn("sensitivity", baseline)
        self.assertTrue(all("sensitivity" not in row for row in baseline["candidates"]))

        report = _plan(bundles, probability_radius=0, validation_trials=2)
        self.assertEqual(report["status"], baseline["status"])
        self.assertEqual(report["feasible_count"], baseline["feasible_count"])
        summary = report["sensitivity"]
        self.assertEqual(summary["probability_radius"], 0)
        self.assertEqual(summary["robust_feasible_count"], baseline["feasible_count"])
        self.assertEqual(summary["possible_feasible_count"], baseline["feasible_count"])
        self.assertEqual(summary["recommendation_changed"], dict.fromkeys(baseline["recommendations"], False))
        self.assertEqual([_unannotated(row) for row in report["candidates"]], baseline["candidates"])
        self.assertEqual({name: _unannotated(row) for name, row in report["recommendations"].items()},
                         baseline["recommendations"])
        self.assertEqual(report["validation"]["budgets"], baseline["validation"]["budgets"])
        self.assertEqual(report["validation"]["cases"], baseline["validation"]["cases"])
        for row in report["candidates"]:
            annotation = row["sensitivity"]
            self.assertEqual(annotation["nominal"][_FOUND], row[_FOUND])
            self.assertEqual(annotation["optimistic"][_FOUND], row[_FOUND])
            expected = "ROBUST_FEASIBLE" if row["meets_target"] else "TARGET_UNREACHABLE_WITHIN_RADIUS"
            self.assertEqual(annotation["classification"], expected)

    def test_radius_changes_selected_layout_and_budget_with_exact_endpoints(self):
        bundles = list(map(_bundle, _panels()))
        baseline = _plan(bundles)
        report = _plan(bundles, probability_radius=.125)
        self.assertEqual(report["candidate_count"], baseline["candidate_count"])
        summary = report["sensitivity"]
        self.assertEqual(summary["nominal_recommendations"], baseline["recommendations"])
        self.assertEqual(summary["nominal_feasible_count"], baseline["feasible_count"])
        self.assertEqual(summary["robust_feasible_count"], 2)
        self.assertEqual(summary["possible_feasible_count"], 11)
        self.assertEqual(summary["classification_counts"], {
            "ROBUST_FEASIBLE": 2, "TARGET_POSSIBLE": 9, "TARGET_UNREACHABLE_WITHIN_RADIUS": 13,
        })
        self.assertEqual(summary["recommendation_changed"], dict.fromkeys(baseline["recommendations"], True))
        for layout, row in report["layouts"].items():
            self.assertEqual(row["sensitivity"]["nominal_recommendations"], baseline["layouts"][layout]["recommendations"])
        self.assertEqual(_signature(baseline["recommendations"]["fewest_expected_compiled_two_qubit_gates"]),
                         ("0,1", "affordable", 3, 0))
        self.assertEqual({_signature(row) for row in report["recommendations"].values()},
                         {("0,2", "stop", 3, 1)})

        free = _candidate(report, "0,1", "affordable", 3, 0)
        self.assertFalse(free["meets_target"])
        self.assertEqual(free[_FOUND], .578125)
        self.assertEqual(free["sensitivity"]["nominal"][_FOUND], .755859375)
        self.assertEqual(free["sensitivity"]["optimistic"][_FOUND], .875)
        self.assertEqual(free["sensitivity"]["classification"], "TARGET_POSSIBLE")

        routed = _candidate(report, "0,2", "stop", 2, 1)
        self.assertEqual(routed[_FOUND], .619140625)
        self.assertEqual(routed["sensitivity"]["nominal"][_FOUND], .763671875)
        self.assertEqual(routed["sensitivity"]["optimistic"][_FOUND], .841796875)
        self.assertEqual((routed[_SHOTS], routed[_QUERIES], routed[_GATES]), (1.8125, .40625, 3.25))
        self.assertEqual(routed["sensitivity"]["classification"], "TARGET_POSSIBLE")
        self.assertEqual(routed["sensitivity"]["hypotheses"], [{
            "solution_count": 1, "lower_found_probability": .619140625,
            "nominal_found_probability": .763671875, "upper_found_probability": .841796875,
        }])

        unreachable = _candidate(report, "0,1", "stop", 1, 0)
        self.assertEqual(unreachable["sensitivity"]["classification"], "TARGET_UNREACHABLE_WITHIN_RADIUS")
        self.assertEqual(unreachable["sensitivity"]["optimistic"][_FOUND], .5)
        robust = _candidate(report, "0,2", "stop", 3, 1)
        self.assertTrue(robust["meets_target"])
        self.assertEqual(robust["sensitivity"]["classification"], "ROBUST_FEASIBLE")
        self.assertEqual(robust[_FOUND], .79925537109375)

    def test_clips_round_probabilities_and_preserves_exact_empty_and_all_marked_cases(self):
        bundles = list(map(_bundle, _panels()))
        clipped = _candidate(_plan(bundles, probability_radius=.75), "0,1", "stop", 1, 0)
        self.assertEqual(clipped[_FOUND], 0)
        self.assertEqual(clipped["sensitivity"]["optimistic"][_FOUND], 1)
        for count, expected, classification in (
            (0, 0, "TARGET_UNREACHABLE_WITHIN_RADIUS"), (4, 1, "ROBUST_FEASIBLE"),
        ):
            with self.subTest(solution_count=count):
                evidence = list(map(_bundle, _extreme_panels(count)))
                report = _plan(evidence, target_probability=1, probability_radius=1)
                self.assertEqual(report["feasible_count"], report["candidate_count"] if count else 0)
                for row in report["candidates"]:
                    self.assertEqual(row[_FOUND], expected)
                    self.assertEqual(row["sensitivity"]["nominal"][_FOUND], expected)
                    self.assertEqual(row["sensitivity"]["optimistic"][_FOUND], expected)
                    self.assertEqual(row["sensitivity"]["classification"], classification)

    def test_validation_replays_the_frozen_robust_budget_under_nominal_weights(self):
        bundles = list(map(_bundle, _panels()))
        baseline = _plan(bundles)
        report = _plan(bundles, probability_radius=.125, validation_trials=2)
        validation = report["validation"]
        self.assertEqual(validation["probability_basis"], "NOMINAL_SAVED_DISTRIBUTIONS")
        self.assertEqual(len(validation["budgets"]), 1)
        budget = validation["budgets"][0]
        self.assertEqual((budget["policy"], budget["max_shots"], budget["max_oracle_queries"]), ("stop", 3, 1))
        self.assertEqual(set(budget["selected_layouts"].values()), {"0,2"})
        self.assertEqual(set(budget["selected_layouts"]), set(report["recommendations"]))
        declared = validation["cases"]["1"]["plans"][budget["id"]]
        for layout, arm in declared.items():
            original = _candidate(baseline, layout, "stop", 3, 1)
            self.assertEqual(arm["modeled"]["found_probability"], original[_FOUND])
            self.assertEqual(arm["observed"]["trials"], 2)
        self.assertNotEqual(declared["0,2"]["modeled"]["found_probability"],
                            report["recommendations"]["fewest_expected_shots"][_FOUND])
        self.assertEqual({count for count, case in validation["cases"].items() if case["role"] == "HOLDOUT"},
                         {"2", "3"})

    def test_invalid_radius_is_rejected(self):
        bundles = list(map(_bundle, _panels()))
        for radius in (-.01, 1.01, float("nan"), float("inf"), True, ".125"):
            with self.subTest(radius=radius), self.assertRaisesRegex(ValueError, r"(?i)radius"):
                _plan(bundles, probability_radius=radius)


if __name__ == "__main__":
    unittest.main()
