"""SDK-free comparisons of compact snapshot profiles and shared budgets."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm.unknown_count_snapshot_profile import export_snapshot_profile_bundle
from austins_algorithm.unknown_count_snapshot_profile_comparison import compare_snapshot_profile_bundles
from test_unknown_count_snapshot_profile import _SUITE, _bundle_digest, _cli_bytes, _encode, _seal, _source_panel


_GATES = "worst_case_expected_compiled_two_qubit_gates"
_BITS = ("00", "01", "10", "11")
_BUDGET_KEYS = ("policy", "max_shots", "max_oracle_queries")


def _case(panel, count):
    return panel["observations"][_SUITE]["cases"][str(count)]


def _distribution(panel, count, rounds, values):
    case = _case(panel, count)
    probabilities = dict(zip(_BITS, values, strict=True))
    case["comparison"]["compilations"][str(rounds)]["snapshot_probabilities"] = {
        "outcome_probabilities": probabilities,
        "verified_hit_probability": sum(probabilities[bits] for bits in case["reference"]["witnesses"]),
    }


def _different_panels():
    left, right = _source_panel(), _source_panel()
    _distribution(right, 1, 0, (.125, .25, .25, .375))
    compiled = _case(right, 1)["comparison"]["compilations"]["1"]["compilation"]
    compiled["compiled"]["two_qubit_gates"] = 8
    compiled["openqasm"]["sha256"] = "d" * 64
    for rounds in (0, 1):
        _distribution(left, 2, rounds, (0, 0, 0, 1))
        _distribution(right, 2, rounds, (1, 0, 0, 0))
    for case in right["observations"][_SUITE]["cases"].values():
        comparison = case["comparison"]
        comparison["snapshot"]["configuration_properties_sha256"] = "b" * 64
        comparison["seeds"] = [seed + 1000 for seed in comparison["seeds"]]
    return _seal(left), _seal(right)


def _block(panel, count):
    _case(panel, count)["comparison"].update(
        status="INSUFFICIENT_TARGET_WIDTH", circuit_qubits=8,
        audit=None, anchor_layout=None, compilations={}, plans={},
    )
    panel.update(status="COMPLETED_WITH_BLOCKED_CASES", case_statuses={"COMPLETED": 2, "INSUFFICIENT_TARGET_WIDTH": 1})
    return _seal(panel)


def _compare(left, right, **overrides):
    options = {"target_probability": .4, "shot_caps": [1, 2], "query_caps": [0, 1]}
    return compare_snapshot_profile_bundles(left, right, _SUITE, **{**options, **overrides})


def _candidate(report, policy, shots, queries):
    return next(row for row in report["candidates"]
                if (row["policy"], row["max_shots"], row["max_oracle_queries"]) == (policy, shots, queries))


class UnknownCountSnapshotProfileComparisonTests(unittest.TestCase):
    def test_identical_profiles_agree_across_grid_and_shared_recommendations(self):
        bundle = export_snapshot_profile_bundle(_source_panel())
        report = _compare(
            bundle, deepcopy(bundle), expected_left_bundle_sha256=bundle["bundle_sha256"],
            expected_right_bundle_sha256=bundle["bundle_sha256"],
        )
        self.assertEqual(report["status"], "SNAPSHOT_PROFILE_BUNDLES_COMPARED")
        self.assertEqual(len(report["candidates"]), 8)
        self.assertEqual(report["feasibility"], {"both": 3, "left_only": 0, "right_only": 0, "neither": 5})
        for row in report["candidates"]:
            self.assertEqual(row["left"]["minimum_found_probability"], row["right"]["minimum_found_probability"])
            self.assertEqual(row["shared"]["minimum_found_probability"], row["left"]["minimum_found_probability"])
            self.assertEqual(row["shared"][_GATES], row["left"][_GATES])
        self.assertEqual(report["recommendations"]["left"], report["recommendations"]["right"])

    def test_different_profiles_preserve_shared_feasibility_and_paired_work(self):
        left_panel, right_panel = _different_panels()
        left, right = map(export_snapshot_profile_bundle, (left_panel, right_panel))
        report = _compare(left, right, validation_trials=2)
        self.assertEqual(report["status"], "SNAPSHOT_PROFILE_BUNDLES_COMPARED")
        self.assertEqual(report["feasibility"], {"both": 2, "left_only": 1, "right_only": 0, "neither": 5})
        self.assertEqual(report["coverage"]["matched_counts"], [1, 2, 3])
        self.assertEqual(report["coverage"]["matched_holdout_counts"], [2, 3])
        zero_queries = _candidate(report, "affordable", 2, 0)
        self.assertTrue(zero_queries["left"]["meets_target"])
        self.assertFalse(zero_queries["right"]["meets_target"])
        self.assertFalse(zero_queries["shared"]["meets_target"])
        one_query = _candidate(report, "affordable", 2, 1)
        self.assertTrue(one_query["shared"]["meets_target"])
        self.assertAlmostEqual(one_query["left"]["minimum_found_probability"], .625)
        self.assertAlmostEqual(one_query["right"]["minimum_found_probability"], .5078125)
        self.assertAlmostEqual(one_query["shared"]["minimum_found_probability"], .5078125)
        self.assertAlmostEqual(one_query["shared"][_GATES], 3.5)
        recommendation = report["recommendations"]["shared"]["fewest_expected_compiled_two_qubit_gates"]
        self.assertEqual((recommendation["max_shots"], recommendation["max_oracle_queries"]), (2, 1))

        rounds = report["round_comparisons"]["1"]["rounds"]
        self.assertTrue(report["recorded_changes"]["calibration_digest_changed"])
        self.assertTrue(rounds["0"]["recorded_compiled_fields_match"])
        self.assertAlmostEqual(rounds["0"]["right_minus_left_hit_probability"], -.125)
        self.assertAlmostEqual(rounds["0"]["outcome_total_variation_distance"], .125)
        self.assertFalse(rounds["1"]["recorded_compiled_fields_match"])
        self.assertEqual(rounds["1"]["left"]["compiled_two_qubit_gates"], 4)
        self.assertEqual(rounds["1"]["right"]["compiled_two_qubit_gates"], 8)
        self.assertEqual(rounds["1"]["outcome_total_variation_distance"], 0)
        self._check_paired_validation(report, left, right)

    def _check_paired_validation(self, report, left, right):
        validation = report["validation"]
        self.assertEqual(validation["status"], "MATCHED_CASES_REPLAYED")
        self.assertEqual(validation["trials_per_arm"], 2)
        expected_budgets = {
            tuple(row[key] for key in _BUDGET_KEYS)
            for group in report["recommendations"].values() for row in group.values() if row is not None
        }
        budgets = {budget["id"]: budget for budget in validation["budgets"]}
        self.assertEqual({tuple(row[key] for key in _BUDGET_KEYS) for row in budgets.values()}, expected_budgets)
        self.assertAlmostEqual(validation["confidence"]["per_arm"], 1 - .05 / (6 * len(budgets)))
        seen = {
            seed for bundle in (left, right) for case in bundle["suites"][_SUITE]["cases"].values()
            for seed in case["comparison"]["seeds"]
        }
        self.assertEqual(set(validation["cases"]), {"1", "2", "3"})
        for count, case in validation["cases"].items():
            self.assertEqual(len(set(case["seeds"])), 2)
            self.assertTrue(seen.isdisjoint(case["seeds"]))
            seen.update(case["seeds"])
            self.assertEqual(set(case["plans"]), set(budgets))
            for plan in case["plans"].values():
                paired = plan["paired_witness_outcomes"]
                self.assertEqual(sum(paired.values()), 2)
                delta = plan["right"]["observed"]["found_rate"] - plan["left"]["observed"]["found_rate"]
                self.assertEqual(plan["right_minus_left_found_rate"], delta)
                if count == "2":
                    self.assertEqual(case["role"], "HOLDOUT")
                    self.assertEqual(paired, {
                        "both_found": 0, "left_only_found": 0, "right_only_found": 2, "neither_found": 0,
                    })
                    self.assertEqual(delta, 1)

        collision = deepcopy(right)
        collision_case = collision["suites"][_SUITE]["cases"]["2"]["comparison"]
        collision_case["seeds"] = validation["cases"]["1"]["seeds"][:1]
        collision["bundle_sha256"] = _bundle_digest(collision)
        with self.assertRaisesRegex(ValueError, "overlap"):
            _compare(left, collision, validation_trials=2)

    def test_bad_pins_and_different_constraint_identity_are_rejected(self):
        panel = _source_panel()
        left = export_snapshot_profile_bundle(panel)
        for side in ("left", "right"):
            with self.subTest(side=side), self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
                _compare(left, deepcopy(left), **{f"expected_{side}_bundle_sha256": "f" * 64})

        stale = deepcopy(left)
        round_one = stale["suites"][_SUITE]["cases"]["1"]["comparison"]["compilations"]["1"]
        round_one["compilation"]["compiled"]["two_qubit_gates"] = 8
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            _compare(left, stale)

        _case(panel, 1)["reference"]["constraint_sha256"] = "c" * 64
        different = export_snapshot_profile_bundle(_seal(panel))
        with self.assertRaisesRegex(ValueError, r"(?i)constraint"):
            _compare(left, different)

    def test_declared_round_gap_and_width_block_cannot_look_like_shared_feasibility(self):
        panel = _source_panel()
        left = export_snapshot_profile_bundle(panel)
        shorter = deepcopy(panel)
        comparison = _case(shorter, 1)["comparison"]
        comparison["compilations"].pop("1")
        comparison["rounds_to_prepare"] = [0]
        suite = shorter["observations"][_SUITE]
        suite["plans"].pop("z_first")
        for case in suite["cases"].values():
            case["comparison"]["plans"].pop("z_first")
        right = export_snapshot_profile_bundle(_seal(shorter))
        self.assertEqual(_compare(left, right, query_caps=[0])["status"], "SNAPSHOT_PROFILE_BUNDLES_COMPARED")
        missing = _compare(left, right)
        self.assertEqual(missing["status"], "SNAPSHOT_PROFILE_COMPARISON_UNAVAILABLE")
        self.assertEqual(missing["coverage"]["unmatched_declared_counts"], [1])
        self.assertEqual(missing["coverage"]["gaps"]["1"]["right"]["status"], "INSUFFICIENT_ROUND_PROFILES")
        self.assertEqual(missing["candidates"], [])
        self.assertIsNone(missing["recommendations"])

        unavailable = _compare(left, export_snapshot_profile_bundle(_block(deepcopy(panel), 1)))
        self.assertEqual(unavailable["status"], "SNAPSHOT_PROFILE_COMPARISON_UNAVAILABLE")
        self.assertEqual(unavailable["coverage"]["gaps"]["1"]["right"]["status"], "INSUFFICIENT_TARGET_WIDTH")
        self.assertEqual(unavailable["candidates"], [])

        missing_holdout = deepcopy(panel)
        missing_holdout["observations"][_SUITE]["cases"].pop("2")
        missing_holdout["case_statuses"] = {"COMPLETED": 2}
        partial = _compare(left, export_snapshot_profile_bundle(_seal(missing_holdout)), validation_trials=2)
        self.assertEqual(partial["status"], "SNAPSHOT_PROFILE_BUNDLES_COMPARED")
        self.assertEqual(partial["coverage"]["matched_holdout_counts"], [3])
        self.assertEqual(partial["coverage"]["gaps"]["2"]["right"]["status"], "CASE_MISSING")
        self.assertEqual(partial["validation"]["status"], "PARTIAL_MATCHED_COVERAGE_REPLAYED")
        self.assertEqual(set(partial["validation"]["cases"]), {"1", "3"})

    def test_cli_reports_each_file_digest(self):
        left, right = map(export_snapshot_profile_bundle, _different_panels())
        left_bytes, right_bytes = map(_encode, (left, right))
        build = (TESTS.parent / ".build").resolve()
        if not build.is_relative_to(TESTS.parent):
            raise RuntimeError("snapshot comparison test output must stay in the package")
        build.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=build, prefix="snapshot-profile-comparison-test-") as directory:
            left_path, right_path = Path(directory) / "left.json", Path(directory) / "right.json"
            left_path.write_bytes(left_bytes)
            right_path.write_bytes(right_bytes)
            report = json.loads(_cli_bytes(
                "unknown-count-snapshot-profile-compare", left_path, right_path, "--suite", _SUITE,
                "--target-probability", ".4", "--shot-caps", "1", "2", "--query-caps", "0", "1",
                "--expected-left-bundle-sha256", left["bundle_sha256"],
                "--expected-right-bundle-sha256", right["bundle_sha256"],
            ))
        self.assertEqual(report["status"], "SNAPSHOT_PROFILE_BUNDLES_COMPARED")
        self.assertEqual(report["artifacts"]["left"]["file_sha256"], sha256(left_bytes).hexdigest())
        self.assertEqual(report["artifacts"]["right"]["file_sha256"], sha256(right_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
