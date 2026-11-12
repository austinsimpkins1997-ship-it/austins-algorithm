"""SDK-free checks for snapshot sensitivity grids and fixed-plan tolerance."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import unknown_count_snapshot_layout
from test_unknown_count_snapshot_layout import _KEYS, _bundle, _cases, _panels, _plan, _set_distribution
from test_unknown_count_snapshot_profile import _SUITE, _cli_bytes, _encode
from test_unknown_count_snapshot_sensitivity import _extreme_panels


_FOUND = "minimum_found_probability"
_FAILURE = "maximum_failure_probability"


def _signature(row):
    return tuple(row[key] for key in _KEYS)


class UnknownCountSnapshotSensitivityGridTests(unittest.TestCase):
    def test_grid_canonicalizes_radii_and_keeps_each_single_radius_ranking(self):
        bundles = list(map(_bundle, _panels()))
        radii = (0, .0625, .125)
        supplied = iter((.125, 0, .125, .0625))
        report = _plan(bundles, probability_radii=supplied, validation_trials=2)
        grid = report["sensitivity_grid"]
        self.assertEqual(grid["radii"], list(radii))
        self.assertEqual([row["probability_radius"] for row in grid["results"]], list(radii))
        self.assertNotIn("sensitivity_grid", _plan(bundles))

        for radius, row in zip(radii, grid["results"], strict=True):
            with self.subTest(radius=radius):
                single = _plan(bundles, probability_radius=radius)
                self.assertNotIn("sensitivity_grid", single)
                self.assertEqual(row["classification_counts"], single["sensitivity"]["classification_counts"])
                self.assertEqual(row["recommendations"], single["recommendations"])
                self.assertEqual(row["best_attainable"], single["best_attainable"])

        largest = _plan(bundles, probability_radius=radii[-1], validation_trials=2)
        for key in ("candidates", "recommendations", "best_attainable", "sensitivity", "feasible_count"):
            self.assertEqual(report[key], largest[key])
        validation = report["validation"]
        self.assertEqual(validation["probability_basis"], "NOMINAL_SAVED_DISTRIBUTIONS")
        self.assertEqual(validation["probability_radius"], radii[-1])
        self.assertEqual(validation["budgets"], largest["validation"]["budgets"])
        self.assertEqual(validation["cases"], largest["validation"]["cases"])

    def test_one_shot_tolerances_bracket_exact_dyadic_breakpoints(self):
        bundles = list(map(_bundle, _panels()))
        report = _plan(bundles, target_probability=.25, shot_caps=[1], query_caps=[0], probability_radii=[0, .25])
        grid = report["sensitivity_grid"]
        brackets = grid["fixed_plan_tolerances"]
        self.assertEqual(grid["maximum_bracket_width"], 2**-30)
        self.assertEqual(len(brackets), 4)
        self.assertEqual({_signature(row) for row in brackets}, {_signature(row) for row in report["candidates"]})
        self.assertEqual(report["feasible_count"], 0)
        nominal_hits = {"0,1": .375, "0,2": .3125}
        for row in brackets:
            with self.subTest(plan=_signature(row)):
                self.assertEqual(row["status"], "BRACKETED")
                lower, upper = row["lower"], row["upper"]
                self.assertTrue(lower["meets_target"])
                self.assertFalse(upper["meets_target"])
                hit = nominal_hits[row["layout"]]
                exact_radius = hit - .25
                self.assertLessEqual(lower["probability_radius"], exact_radius)
                self.assertGreater(upper["probability_radius"], exact_radius)
                self.assertLessEqual(upper["probability_radius"] - lower["probability_radius"], 2**-30)
                for endpoint in (lower, upper):
                    self.assertEqual(endpoint[_FOUND], hit - endpoint["probability_radius"])
                    self.assertEqual(endpoint[_FAILURE], 1 - endpoint[_FOUND])

    def test_empty_and_all_marked_tolerances_keep_distinct_terminal_states(self):
        for count, status in ((0, "NOMINAL_TARGET_MISSED"), (4, "FULL_RADIUS_DOMAIN")):
            with self.subTest(solution_count=count):
                bundles = list(map(_bundle, _extreme_panels(count)))
                report = _plan(bundles, target_probability=1, shot_caps=[1], query_caps=[0], probability_radii=[1, 0])
                rows = report["sensitivity_grid"]["fixed_plan_tolerances"]
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertEqual(row["status"], status)
                    if count:
                        self.assertEqual(row["lower"], {
                            "probability_radius": 1, _FOUND: 1, _FAILURE: 0, "meets_target": True,
                        })
                        self.assertIsNone(row["upper"])
                    else:
                        self.assertIsNone(row["lower"])
                        self.assertEqual(row["upper"], {
                            "probability_radius": 0, _FOUND: 0, _FAILURE: 1, "meets_target": False,
                        })

    def test_target_one_rejects_rounded_hit_with_positive_miss_mass(self):
        miss = 2**-54
        panels = _panels()
        for panel in panels:
            _set_distribution(_cases(panel)["1"], 0, (1.0, miss, 0.0, 0.0))
        report = _plan(
            list(map(_bundle, panels)), target_probability=1, shot_caps=[1], query_caps=[0],
            probability_radii=[0, 1],
        )
        grid = report["sensitivity_grid"]
        self.assertIsNone(grid["results"][0]["recommendations"]["fewest_expected_shots"])
        for row in grid["fixed_plan_tolerances"]:
            with self.subTest(plan=_signature(row)):
                self.assertEqual(row["status"], "NOMINAL_TARGET_MISSED")
                self.assertIsNone(row["lower"])
                self.assertEqual(row["upper"], {
                    "probability_radius": 0, _FOUND: 1, _FAILURE: miss, "meets_target": False,
                })

    def test_grid_rejects_aggregate_transition_work_before_running_recurrence(self):
        bundles = list(map(_bundle, _panels()))
        budgets = {"shot_caps": [1], "query_caps": [0]}
        with patch.object(unknown_count_snapshot_layout, "MAX_SELECTION_TRANSITIONS", 12):
            single = _plan(bundles, **budgets, probability_radius=.125)
            self.assertGreater(single["candidate_count"], 0)
            with (
                patch.object(unknown_count_snapshot_layout, "_layout_grid") as recurrence,
                self.assertRaisesRegex(ValueError, "layout grid exceeds 12 modeled transition updates"),
            ):
                _plan(bundles, **budgets, probability_radii=[0, .125])
            recurrence.assert_not_called()

    def test_grid_validates_supplied_count_and_excludes_single_radius(self):
        bundles = list(map(_bundle, _panels()))

        def too_many_duplicates():
            yield from (0 for _ in range(17))
            raise AssertionError("the planner read beyond the supplied-radii limit")

        for values in ([], [-.01], [1.01], [float("nan")], [True], ".125", too_many_duplicates()):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, r"(?i)radi"):
                _plan(bundles, probability_radii=values)
        with self.assertRaisesRegex(ValueError, r"(?i)radi"):
            _plan(bundles, probability_radius=0, probability_radii=[0])

        # The supplied-entry cap permits exactly sixteen duplicates.
        report = _plan(bundles, probability_radii=[0] * 16, shot_caps=[1], query_caps=[0])
        self.assertEqual(report["sensitivity_grid"]["radii"], [0])

    def test_cli_accepts_grid_and_rejects_combined_radius_options(self):
        bundles = list(map(_bundle, _panels()))
        build = (TESTS.parent / ".build").resolve()
        if not build.is_relative_to(TESTS.parent):
            raise RuntimeError("snapshot sensitivity grid test output must stay in the package")
        build.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=build, prefix="snapshot-sensitivity-grid-test-") as directory:
            paths = [Path(directory) / f"profile-{index}.json" for index in range(len(bundles))]
            for path, bundle in zip(paths, bundles, strict=True):
                path.write_bytes(_encode(bundle))
            arguments = (
                "unknown-count-snapshot-layout-plan", *paths, "--suite", _SUITE, "--target-probability", ".25",
                "--shot-caps", "1", "--query-caps", "0", "--probability-radii", ".25", "0", ".25",
            )
            report = json.loads(_cli_bytes(*arguments))
            self.assertEqual(report["sensitivity_grid"]["radii"], [0, .25])
            self.assertEqual(report["sensitivity"]["probability_radius"], .25)
            with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as rejected:
                _cli_bytes(*arguments, "--probability-radius", "0")
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertIn("--probability-radius", stderr.getvalue())
            self.assertIn("--probability-radii", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
