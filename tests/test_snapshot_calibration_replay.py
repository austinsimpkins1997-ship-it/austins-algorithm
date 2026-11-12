"""SDK-free checks for paired replay of frozen dated-snapshot budgets."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from random import Random
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import snapshot_calibration_replay
from austins_algorithm.__main__ import main
from austins_algorithm.constraints import BooleanConstraint
from test_snapshot_calibration_planning import _plan, _report


_WITNESS_11 = BooleanConstraint(2, ((1,), (2,)))
_PLAN_KEYS = ("layout", "policy", "max_shots", "max_oracle_queries")
_SEED_START = 0x23800000


def _last_witness_report(*args, **options):
    def relabel(distribution):
        return {**distribution, "00": distribution["11"], "11": distribution["00"]}

    report = _report(*args, **options)
    report["constraint_sha256"] = _WITNESS_11.digest()
    report["audit"]["marked_states"] = ["11"]
    for row in report["rounds"].values():
        for lane in ("earlier", "later"):
            row[lane]["outcome_probabilities"] = relabel(row[lane]["outcome_probabilities"])
        row["right_minus_left_outcome_probabilities"] = relabel(row["right_minus_left_outcome_probabilities"])
        verification = row["compilation"]["verification"]
        for key in ("original_probabilities", "compiled_probabilities", "roundtrip_probabilities"):
            verification[key] = relabel(verification[key])
    return report


def _identity(row):
    return tuple(row[key] for key in _PLAN_KEYS)


class SnapshotCalibrationReplayTests(unittest.TestCase):
    def test_replay_freezes_deduplicated_recommendations_without_changing_forecast(self):
        reports = [_report(), _report((0, 2), hits=((.375, .625), (1, .875)))]
        source = deepcopy(reports)
        forecast = _plan(reports)
        self.assertEqual(forecast, _plan(reports, validation_trials=0))
        self.assertNotIn("validation", forecast)
        report = _plan(reports, validation_trials=6)
        self.assertEqual(report, _plan(reports[::-1], validation_trials=6))
        self.assertEqual(reports, source)
        self.assertEqual({key: value for key, value in report.items() if key != "validation"}, forecast)

        selected = {}
        for group, ranking in (("shared", forecast), *forecast["lane_selection"].items()):
            for objective, row in ranking["recommendations"].items():
                if row is not None:
                    selected.setdefault(_identity(row), set()).add(f"{group}.{objective}")
        validation = report["validation"]
        self.assertEqual(validation["status"], "COMPLETED")
        self.assertEqual(validation["trials_per_arm"], 6)
        self.assertEqual(validation["seeds"], list(range(_SEED_START, _SEED_START + 6)))
        plans = validation["plans"]
        self.assertEqual(len(plans), 3)
        self.assertEqual({_identity(row): set(row["selected_for"]) for row in plans}, selected)
        self.assertEqual(len({row["id"] for row in plans}), len(plans))
        candidates = {_identity(row): row for row in forecast["candidates"]}
        for row in plans:
            candidate = candidates[_identity(row)]
            self.assertEqual(row["max_rounds_per_shot"], forecast["inputs"]["max_rounds_per_shot"])
            self.assertEqual(sum(row["paired_witness_outcomes"].values()), 6)
            for lane in ("earlier", "later"):
                arm = row[lane]
                self.assertEqual(arm["observed"]["trials"], 6)
                self.assertEqual(arm["modeled"], {
                    key: candidate[lane][key] for key in arm["modeled"]
                })
                self.assertRegex(arm["traces_sha256"], r"^[0-9a-f]{64}$")

    def test_replay_verifies_named_witness_and_preserves_paired_outcomes(self):
        trials = 8
        source = _last_witness_report(hits=((1, 0),))
        report = _plan([source], constraint=_WITNESS_11, shot_caps=[1], query_caps=[0], validation_trials=trials)
        validation = report["validation"]
        self.assertEqual(len(validation["plans"]), 1)
        plan = validation["plans"][0]
        self.assertEqual(set(plan["selected_for"]), {
            "shared.best_attainable", "later.best_attainable", "earlier.fewest_expected_shots",
            "earlier.fewest_expected_oracle_queries", "earlier.fewest_expected_compiled_two_qubit_gates",
        })
        self.assertEqual(plan["paired_witness_outcomes"], {
            "both_found": 0, "earlier_only": trials, "later_only": 0, "neither_found": 0,
        })
        for lane, expected in (("earlier", trials), ("later", 0)):
            observed = plan[lane]["observed"]
            self.assertEqual(observed["witnesses"], expected)
            self.assertEqual(observed["trials"], trials)
            self.assertEqual(observed["shots"], trials)
            self.assertEqual(observed["oracle_queries"], 0)
            self.assertEqual(observed["nominal_compiled_two_qubit_gates"], 0)
        confidence = validation["confidence"]
        self.assertEqual(confidence["method"], "CLOPPER_PEARSON_BONFERRONI")
        self.assertEqual(confidence["family"], .95)
        self.assertEqual(confidence["arms"], 3)
        self.assertEqual(confidence["per_arm"], 1 - (1 - .95) / 3)
        alpha = (1 - confidence["per_arm"]) / 2
        self.assertAlmostEqual(plan["earlier"]["interval"]["lower"], alpha**(1 / trials))
        self.assertEqual(plan["earlier"]["interval"]["upper"], 1)
        self.assertEqual(plan["later"]["interval"]["lower"], 0)
        self.assertAlmostEqual(plan["later"]["interval"]["upper"], 1 - alpha**(1 / trials))

    def test_identical_lanes_reuse_trial_draws_and_have_identical_traces(self):
        source = _last_witness_report(hits=((.5, .5), (.75, .75)))
        report = _plan(
            [source], constraint=_WITNESS_11, shot_caps=[2], query_caps=[1], validation_trials=16,
        )
        for plan in report["validation"]["plans"]:
            self.assertEqual(plan["earlier"], plan["later"])
            paired = plan["paired_witness_outcomes"]
            hits = plan["earlier"]["observed"]["witnesses"]
            self.assertEqual(paired, {
                "both_found": hits, "earlier_only": 0, "later_only": 0, "neither_found": 16 - hits,
            })

    def test_classical_baselines_use_common_distinct_prefixes_and_actual_witness(self):
        sources = [
            _last_witness_report(hits=((1, .25), (1, 1))),
            _last_witness_report((0, 2), hits=((.5, .5), (.5, .5))),
        ]
        trials = 24
        report = _plan(sources, constraint=_WITNESS_11, validation_trials=trials)
        validation = report["validation"]
        baseline = validation["classical_distinct_uniform"]
        self.assertEqual(set(baseline), {"1", "2"})
        self.assertEqual({plan["max_shots"] for plan in validation["plans"]}, {1, 2})
        self.assertEqual(validation["confidence"]["arms"], 2 * len(validation["plans"]) + 2)
        expected_work = trials * (2 * sum(plan["max_shots"] for plan in validation["plans"]) + 2)
        self.assertEqual(validation["maximum_possible_attempts"], expected_work)
        orders = [Random(seed ^ 0x5A5A5A5A).sample(range(4), 2) for seed in validation["seeds"]]
        for cap in (1, 2):
            with self.subTest(cap=cap):
                prefixes = [order[:cap] for order in orders]
                hits = sum(3 in prefix for prefix in prefixes)
                checks = sum(prefix.index(3) + 1 if 3 in prefix else cap for prefix in prefixes)
                row = baseline[str(cap)]
                self.assertEqual(row["effective_candidate_cap"], cap)
                self.assertEqual(row["modeled"], report["classical_distinct_uniform"][str(cap)])
                self.assertEqual(row["observed"], {
                    "witnesses": hits, "trials": trials, "found_rate": hits / trials, "verifier_calls": checks,
                })
                self.assertRegex(row["traces_sha256"], r"^[0-9a-f]{64}$")

    def test_combined_attempt_cap_rejects_before_any_replay_draws(self):
        source = _last_witness_report(hits=((1, 0),))
        budgets = {"constraint": _WITNESS_11, "shot_caps": [1], "query_caps": [0]}
        with patch.object(snapshot_calibration_replay, "MAX_VALIDATION_ATTEMPTS", 11):
            allowed = _plan([source], **budgets, validation_trials=3)
            self.assertEqual(allowed["validation"]["maximum_possible_attempts"], 9)
            with (
                patch.object(snapshot_calibration_replay, "_validate_arm") as replay,
                patch.object(snapshot_calibration_replay, "_replay_classical") as classical,
                self.assertRaisesRegex(ValueError, "calibration replay exceeds 11 possible attempts"),
            ):
                _plan([source], **budgets, validation_trials=4)
            replay.assert_not_called()
            classical.assert_not_called()

    def test_validation_bounds_and_unavailable_coverage(self):
        short = _report(hits=((.5, .25),))
        for invalid in (True, -1, 129):
            with self.subTest(trials=invalid), self.assertRaisesRegex(ValueError, "validation_trials"):
                _plan([short], validation_trials=invalid)
        result = _plan([short], validation_trials=4)
        self.assertEqual(result["status"], "SNAPSHOT_CALIBRATION_BUDGET_PLANNING_UNAVAILABLE")
        self.assertEqual(result["validation"]["status"], "UNAVAILABLE")
        self.assertFalse(result["validation"]["plans"])

    def test_cli_opt_in_routes_validation_trials_to_the_planner(self):
        build = (PACKAGE / ".build").resolve()
        if not build.is_relative_to(PACKAGE):
            raise RuntimeError("calibration replay test output must stay in the package")
        build.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=build, prefix="calibration-replay-test-") as directory:
            constraint = Path(directory) / "constraint.json"
            constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[1],[2]]}', encoding="utf-8")
            saved = Path(directory) / "report.json"
            source = _last_witness_report(hits=((1, 0),))
            saved.write_text(json.dumps(source), encoding="utf-8")
            argv = [
                "austins-algorithm", "snapshot-calibration-budget-plan", str(constraint), str(saved),
                "--target-probability", ".7", "--shot-caps", "1", "--query-caps", "0", "--validate-trials", "4",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
                main()
            observed = json.loads(stdout.getvalue())["validation"]
        expected = _plan(
            [source], constraint=_WITNESS_11, shot_caps=[1], query_caps=[0], validation_trials=4,
        )["validation"]
        self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
