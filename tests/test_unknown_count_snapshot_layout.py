"""SDK-free tests of joint layout and adaptive-budget selection."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm.unknown_count_snapshot_layout import plan_snapshot_layout_budgets
from austins_algorithm.unknown_count_snapshot_profile import export_snapshot_profile_bundle
from test_unknown_count_snapshot_profile import _SUITE, _bundle_digest, _seal, _source_panel


_BITS = ("00", "01", "10", "11")
_KEYS = ("layout", "policy", "max_shots", "max_oracle_queries")
_GATES = "worst_case_expected_compiled_two_qubit_gates"
_SHOTS = "worst_case_expected_shots"


def _cases(panel):
    return panel["observations"][_SUITE]["cases"]


def _set_layout(case, anchor):
    comparison = case["comparison"]
    comparison["anchor_layout"] = list(anchor)
    for entry in comparison["compilations"].values():
        compilation = entry["compilation"]
        compilation["initial_logical_to_target_qubit"] = list(anchor)
        compilation["openqasm"]["export_wire_to_target_qubit"] = sorted(anchor)
        compilation["openqasm"]["classical_bit_to_target_qubit"] = list(anchor)


def _set_distribution(case, rounds, values):
    distribution = dict(zip(_BITS, values, strict=True))
    case["comparison"]["compilations"][str(rounds)]["snapshot_probabilities"] = {
        "outcome_probabilities": distribution,
        "verified_hit_probability": sum(distribution[bits] for bits in case["reference"]["witnesses"]),
    }


def _panels():
    adjacent, routed = _source_panel(), _source_panel()
    for case in _cases(routed).values():
        _set_layout(case, (0, 2))
        case["comparison"]["seeds"] = [seed + 1000 for seed in case["comparison"]["seeds"]]
        one = case["comparison"]["compilations"]["1"]["compilation"]
        one["compiled"]["two_qubit_gates"] = 8
        one["openqasm"]["sha256"] = "d" * 64
    _set_distribution(_cases(adjacent)["1"], 0, (.375, .125, .25, .25))
    _set_distribution(_cases(adjacent)["1"], 1, (.5, .125, .125, .25))
    _set_distribution(_cases(routed)["1"], 0, (.3125, .1875, .25, .25))
    _set_distribution(_cases(routed)["1"], 1, (1, 0, 0, 0))
    for rounds in (0, 1):
        _set_distribution(_cases(adjacent)["2"], rounds, (0, 0, 0, 1))
        _set_distribution(_cases(routed)["2"], rounds, (1, 0, 0, 0))
    return adjacent, routed


def _bundle(panel):
    return export_snapshot_profile_bundle(_seal(panel))


def _plan(bundles, **overrides):
    options = {"target_probability": .7, "shot_caps": iter((1, 2, 3)), "query_caps": iter((0, 1))}
    return plan_snapshot_layout_budgets(bundles, _SUITE, **{**options, **overrides})


class UnknownCountSnapshotLayoutTests(unittest.TestCase):
    def test_rank_placement_budget_pairs_on_declared_cases_only(self):
        bundles = list(map(_bundle, _panels()))
        report = _plan(bundles, expected_bundle_sha256=[bundle["bundle_sha256"] for bundle in bundles])
        self.assertEqual(report, _plan(bundles[::-1]))
        self.assertEqual(report["status"], "SNAPSHOT_LAYOUT_BUDGETS_PLANNED")
        self.assertEqual(set(report["layouts"]), {"0,1", "0,2"})
        self.assertEqual(len(report["candidates"]), 24)
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        for layout, bundle in zip(("0,1", "0,2"), bundles, strict=True):
            evidence = report["layouts"][layout]
            self.assertEqual(evidence["initial_layout"], [int(wire) for wire in layout.split(",")])
            self.assertEqual(evidence["bundle_sha256"], bundle["bundle_sha256"])
            self.assertEqual(evidence["source_observations_sha256"], bundle["source"]["observations_sha256"])
        for row in report["candidates"]:
            self.assertEqual([case["solution_count"] for case in row["hypotheses"]], [1])
        self.assertIn(report["best_attainable"]["layout"], report["layouts"])

        fewest_shots = report["recommendations"]["fewest_expected_shots"]
        self.assertEqual(tuple(fewest_shots[key] for key in _KEYS), ("0,2", "stop", 2, 1))
        self.assertEqual(fewest_shots["minimum_found_probability"], .763671875)
        self.assertEqual(fewest_shots[_SHOTS], 1.6875)
        self.assertEqual(fewest_shots[_GATES], 2.75)

        for name in ("fewest_expected_oracle_queries", "fewest_expected_compiled_two_qubit_gates"):
            recommendation = report["recommendations"][name]
            self.assertEqual(tuple(recommendation[key] for key in _KEYS), ("0,1", "affordable", 3, 0))
            self.assertEqual(recommendation["minimum_found_probability"], .755859375)
            self.assertEqual(recommendation[_SHOTS], 2.015625)
            self.assertEqual(recommendation[_GATES], 0)

    def test_replay_freezes_recommendations_and_matches_fresh_seeds_across_layouts(self):
        bundles = list(map(_bundle, _panels()))
        planned = _plan(bundles)
        report = _plan(bundles, validation_trials=2)
        self.assertEqual(report["recommendations"], planned["recommendations"])
        self.assertEqual(report["candidates"], planned["candidates"])
        self.assertEqual(report["coverage"]["matched_holdout_counts"], [2, 3])

        validation = report["validation"]
        self.assertEqual(validation["status"], "MATCHED_CASES_REPLAYED")
        self.assertEqual(validation["trials_per_arm"], 2)
        selected = {
            name: tuple(row[key] for key in _KEYS)
            for name, row in planned["recommendations"].items()
        }
        replayed = {
            name: (layout, budget["policy"], budget["max_shots"], budget["max_oracle_queries"])
            for budget in validation["budgets"] for name, layout in budget["selected_layouts"].items()
        }
        self.assertEqual(replayed, selected)
        budget_ids = {budget["id"] for budget in validation["budgets"]}
        self.assertEqual(len(budget_ids), 2)
        self.assertAlmostEqual(validation["confidence"]["per_arm"], 1 - .05 / 12)

        seen = {
            seed for bundle in bundles for case in bundle["suites"][_SUITE]["cases"].values()
            for seed in case["comparison"]["seeds"]
        }
        self.assertEqual(set(validation["cases"]), {"1", "2", "3"})
        for count, case in validation["cases"].items():
            self.assertEqual(case["role"], "DECLARED" if count == "1" else "HOLDOUT")
            self.assertEqual(len(set(case["seeds"])), 2)
            self.assertTrue(seen.isdisjoint(case["seeds"]))
            seen.update(case["seeds"])
            self.assertEqual(set(case["plans"]), budget_ids)
            for layout, bundle in zip(("0,1", "0,2"), bundles, strict=True):
                compilations = bundle["suites"][_SUITE]["cases"][count]["comparison"]["compilations"]
                self.assertEqual(case["round_openqasm_sha256"][layout], [
                    compilations[rounds]["compilation"]["openqasm"]["sha256"] for rounds in ("0", "1")
                ])
            for plan in case["plans"].values():
                self.assertEqual(set(plan), {"0,1", "0,2"})
                for arm in plan.values():
                    self.assertEqual(arm["observed"]["trials"], 2)
                if count == "2":
                    self.assertEqual(plan["0,1"]["observed"]["witnesses"], 0)
                    self.assertEqual(plan["0,2"]["observed"]["witnesses"], 2)

        collision = deepcopy(bundles[1])
        collision["suites"][_SUITE]["cases"]["2"]["comparison"]["seeds"] = validation["cases"]["1"]["seeds"][:1]
        collision["bundle_sha256"] = _bundle_digest(collision)
        with self.assertRaisesRegex(ValueError, r"(?i)overlap"):
            _plan([bundles[0], collision], validation_trials=2)

    def test_missing_holdout_and_missing_declared_round_have_different_coverage(self):
        adjacent, routed = _panels()
        left, right = map(_bundle, (adjacent, routed))
        complete = _plan([left, right])
        missing_case = deepcopy(routed)
        _cases(missing_case).pop("2")
        missing_case["case_statuses"] = {"COMPLETED": 2}
        partial = _plan([left, _bundle(missing_case)], validation_trials=2)
        self.assertEqual(partial["recommendations"], complete["recommendations"])
        self.assertEqual(partial["candidate_count"], complete["candidate_count"])
        self.assertEqual(partial["layouts"]["0,2"]["coverage"]["gaps"]["2"]["status"], "CASE_MISSING")
        self.assertEqual(partial["coverage"]["omitted_holdout_counts"], [2])
        self.assertEqual(partial["validation"]["status"], "PARTIAL_MATCHED_COVERAGE_REPLAYED")
        self.assertEqual(set(partial["validation"]["cases"]), {"1", "3"})

        missing_round = deepcopy(routed)
        comparison = _cases(missing_round)["1"]["comparison"]
        comparison["rounds_to_prepare"] = [0]
        comparison["compilations"].pop("1")
        missing_round["observations"][_SUITE]["plans"].pop("z_first")
        for case in _cases(missing_round).values():
            case["comparison"]["plans"].pop("z_first")
        bounded = _plan([left, _bundle(missing_round)], validation_trials=2)
        blocked = bounded["layouts"]["0,2"]
        self.assertEqual(blocked["status"], "UNAVAILABLE")
        self.assertEqual(blocked["candidate_count"], 0)
        self.assertEqual(blocked["coverage"]["unmatched_declared_counts"], [1])
        self.assertEqual(blocked["coverage"]["gaps"]["1"]["status"], "INSUFFICIENT_ROUND_PROFILES")
        self.assertEqual({row["layout"] for row in bounded["candidates"]}, {"0,1"})
        for case in bounded["validation"]["cases"].values():
            self.assertTrue(all(set(plan) == {"0,1"} for plan in case["plans"].values()))

    def test_digest_pins_and_ordered_anchors(self):
        adjacent, routed = _panels()
        left, right = map(_bundle, (adjacent, routed))
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            _plan([left, right], expected_bundle_sha256=[left["bundle_sha256"], "f" * 64])
        stale = deepcopy(right)
        stale["suites"][_SUITE]["cases"]["1"]["comparison"]["seeds"] = [102]
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            _plan([left, stale])
        with self.assertRaisesRegex(ValueError, r"(?i)layout|anchor|placement"):
            _plan([left, deepcopy(left)])

        _set_layout(_cases(routed)["2"], (1, 2))
        with self.assertRaisesRegex(ValueError, r"(?i)layout|anchor|placement"):
            _plan([left, _bundle(routed)])

        reversed_wires = deepcopy(adjacent)
        for case in _cases(reversed_wires).values():
            _set_layout(case, (1, 0))
        report = _plan([_bundle(reversed_wires), left])
        self.assertEqual(set(report["layouts"]), {"0,1", "1,0"})

    def test_mixed_calibration_software_compiler_and_case_identities_are_rejected(self):
        adjacent, routed = _panels()
        left = _bundle(adjacent)
        for field, diagnostic in (
            ("calibration", "calibration|snapshot"), ("software", "software|version"),
            ("compiler", "compiler|transpiler|seed"), ("constraint", "constraint|reference"),
        ):
            with self.subTest(field=field):
                panel = deepcopy(routed)
                if field == "compiler":
                    panel["configuration"]["seed_transpiler"] = 11
                if field == "constraint":
                    _cases(panel)["2"]["reference"]["constraint_sha256"] = "c" * 64
                for case in _cases(panel).values():
                    comparison = case["comparison"]
                    if field == "calibration":
                        comparison["snapshot"]["configuration_properties_sha256"] = "b" * 64
                    if field == "software":
                        comparison["versions"]["qiskit"] = "2.6.3"
                    if field == "compiler":
                        comparison["seed_transpiler"] = 11
                        for entry in comparison["compilations"].values():
                            entry["compilation"]["seed_transpiler"] = 11
                with self.assertRaisesRegex(ValueError, rf"(?i){diagnostic}"):
                    _plan([left, _bundle(panel)])


if __name__ == "__main__":
    unittest.main()
