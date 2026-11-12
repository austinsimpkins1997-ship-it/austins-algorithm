"""Exercise the distinction between numerical fusion drift and budget decisions."""

from copy import deepcopy
from importlib.metadata import version
from importlib.util import find_spec
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_fusion import compare_snapshot_calibration_fusion_budgets
from austins_algorithm.snapshot_calibration_planning import plan_snapshot_calibration_budgets
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_compilation_bundle import _test_directory
from test_snapshot_calibration_compilation_cache import _SDK_MODULES, _pinned
from test_snapshot_calibration_planning import _CONSTRAINT, _report


_FUSION = {"fusion_enable": True, "fusion_threshold": 5, "fusion_max_qubit": 2}
_BUDGET = {"target_probability": .5, "shot_caps": [1], "query_caps": [0]}


def _marked(report):
    report["density_fusion"] = {
        "requested": dict(_FUSION),
        "observed": {
            "earlier": {key: None for key in report["rounds"]},
            "later": {key: {"enabled": True, "applied": False, "threshold": 5, "max_fused_qubits": 2}
                      for key in report["rounds"]},
        },
    }
    return report


def _round_zero(hit=.625, layout=(0, 1)):
    return _report(layout, hits=((hit, .75),))


def _compare(reference, fused, *, probability_tolerance=1e-12, **options):
    return compare_snapshot_calibration_fusion_budgets(
        reference, fused, _CONSTRAINT, probability_tolerance=probability_tolerance, **{**_BUDGET, **options},
    )


class SnapshotCalibrationFusionInputTests(unittest.TestCase):
    def test_one_ulp_drift_can_change_feasibility_within_tolerance(self):
        target = .625
        reference = _round_zero(target)
        fused = _marked(_round_zero(math.nextafter(target, -math.inf)))
        report = _compare([reference], [fused], target_probability=target)
        check = report["fusion_check"]
        self.assertEqual(report["status"], "SNAPSHOT_CALIBRATION_FUSION_COMPARED")
        self.assertEqual(check["status"], "DECISIONS_CHANGED")
        self.assertTrue(check["within_probability_tolerance"])
        self.assertFalse(check["decisions_match"])
        self.assertFalse(check["fused_plan_used_for_selection"])
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(
            [(row["policy"], row["reference"], row["fused"]) for row in check["candidate_target_status_changes"]],
            [("stop", "MEETS_BOTH", "LATER_ONLY"), ("affordable", "MEETS_BOTH", "LATER_ONLY")],
        )
        self.assertGreater(check["maximum_total_variation_distance"], 0.0)
        self.assertEqual(
            report["reference_plan"],
            plan_snapshot_calibration_budgets([reference], _CONSTRAINT, **{**_BUDGET, "target_probability": target}),
        )
        capture = check["layouts"]["0,1"]
        self.assertEqual(capture["reference_report_sha256"], _digest(reference))
        self.assertEqual(capture["fused_report_sha256"], _digest(fused))
        self.assertIsNone(capture["density_fusion"]["observed"]["earlier"]["0"])

    def test_rank_change_is_detected_when_all_candidate_classes_match(self):
        up, down = (math.nextafter(.625, direction) for direction in (math.inf, -math.inf))
        second = _round_zero(layout=(0, 2))
        report = _compare([_round_zero(up), second], [_marked(deepcopy(second)), _marked(_round_zero(down))])
        check = report["fusion_check"]
        self.assertEqual(check["status"], "DECISIONS_CHANGED")
        self.assertTrue(check["within_probability_tolerance"])
        self.assertFalse(check["decisions_match"])
        self.assertEqual(check["candidate_target_status_changes"], [])
        self.assertIn({
            "scope": "shared", "objective": "fewest_expected_shots",
            "reference": {"layout": "0,1", "policy": "stop", "max_shots": 1, "max_oracle_queries": 0},
            "fused": {"layout": "0,2", "policy": "stop", "max_shots": 1, "max_oracle_queries": 0},
        }, check["selection_changes"])

    def test_total_variation_tolerance_is_inclusive_and_independent_of_decisions(self):
        reference, fused = _round_zero(.75), _marked(_round_zero())
        for tolerance, status in ((.125, "MATCHED"), (math.nextafter(.125, -math.inf), "TOLERANCE_EXCEEDED")):
            with self.subTest(tolerance=tolerance):
                check = _compare([reference], [fused], probability_tolerance=tolerance)["fusion_check"]
                self.assertEqual(check["maximum_total_variation_distance"], .125)
                self.assertEqual(check["maximum_absolute_outcome_probability_difference"], .125)
                self.assertEqual(check["maximum_absolute_hit_probability_difference"], .125)
                self.assertEqual(check["maximum_absolute_miss_probability_difference"], .125)
                self.assertEqual(check["status"], status)
                self.assertIs(check["within_probability_tolerance"], status == "MATCHED")
                self.assertTrue(check["decisions_match"])
                self.assertEqual(check["selection_changes"], [])
        for tolerance in (-1, math.nan, math.inf, True):
            with self.subTest(invalid_tolerance=tolerance), self.assertRaisesRegex(ValueError, "probability_tolerance"):
                _compare([reference], [fused], probability_tolerance=tolerance)

    def test_insufficient_round_profiles_do_not_claim_decision_parity(self):
        reference = _round_zero()
        check = _compare([reference], [_marked(deepcopy(reference))], shot_caps=[2], query_caps=[1])["fusion_check"]
        self.assertEqual(check["status"], "INSUFFICIENT_ROUND_PROFILES")
        self.assertTrue(check["within_probability_tolerance"])
        self.assertIsNone(check["decisions_match"])
        self.assertIsNone(check["candidate_target_status_changes"])
        self.assertIsNone(check["selection_changes"])

    def test_rejects_structural_changes_and_malformed_fusion_observations(self):
        changes = (
            (("rounds", "0", "compilation", "openqasm", "sha256"), "a" * 64, "identical round 0 compilation"),
            (("rounds", "0", "target_calibrations", "earlier", "qubits", "0", "t1_seconds"),
             .002, "identical round 0 target_calibrations"),
            (("rounds", "0", "readout_assignment_matrices", "earlier", "0"),
             [[.9, .1], [.1, .9]], "identical round 0 readout_assignment_matrices"),
            (("density_fusion", "requested", "fusion_threshold"), 6, "supported density fusion options"),
            (("density_fusion", "observed", "earlier"), {}, "cover every saved round"),
            (("density_fusion", "observed", "later", "0"), {"applied": 1}, "invalid observed density fusion"),
        )
        for path, value, error in changes:
            with self.subTest(path=path):
                fused = _marked(_round_zero())
                row = fused
                for key in path[:-1]:
                    row = row[key]
                row[path[-1]] = value
                with self.assertRaisesRegex(ValueError, error):
                    _compare([_round_zero()], [fused])
        fused = _marked(_round_zero())
        fused["batch_density"] = True
        with self.assertRaisesRegex(ValueError, "same batch_density policy"):
            _compare([_round_zero()], [fused])

    def test_fused_reports_cannot_be_used_as_the_authoritative_plan(self):
        fused = _marked(_round_zero())
        with self.assertRaisesRegex(ValueError, "fusion-tagged"):
            plan_snapshot_calibration_budgets([fused], _CONSTRAINT, **_BUDGET)
        with self.assertRaisesRegex(ValueError, "fusion-tagged"):
            _compare([fused], [fused])
        with self.assertRaisesRegex(ValueError, "density fusion provenance"):
            _compare([_round_zero()], [_round_zero()])


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationFusionRuntimeTests(unittest.TestCase):
    def test_batched_fusion_retains_each_rounds_serial_observation(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = source / FakeJakartaV2.conf_filename
        properties = source / FakeJakartaV2.props_filename
        later = _pinned(configuration, properties)
        earlier_properties = json.loads(properties.read_bytes())
        earlier_properties["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier_path = directory / "earlier.json"
        earlier_path.write_text(json.dumps(earlier_properties), encoding="utf-8")
        earlier = _pinned(configuration, earlier_path)
        options = {
            "initial_layout": (0, 1, 2, 3, 5, 4), "max_rounds": 2,
            "compilation_cache": SnapshotCalibrationCompilationCache(), "prefer_native_qasm": True,
            "density_fusion": True,
        }
        constraint = BooleanConstraint(5, ((1, 2), (-1,)))
        serial = comparison.compare_snapshot_calibrations(constraint, earlier, later, **options)
        with patch.object(comparison, "_prepare_density_snapshot_batch",
                          wraps=comparison._prepare_density_snapshot_batch) as prepare:
            batched = comparison.compare_snapshot_calibrations(constraint, earlier, later, batch_density=True, **options)
        self.assertEqual(serial["status"], "COMPLETED")
        self.assertEqual(batched.pop("batch_density"), True)
        self.assertEqual(batched, serial)
        self.assertEqual([len(call.args[0]) for call in prepare.call_args_list], [2, 2])
        self.assertTrue(all(call.kwargs["density_fusion"] is True for call in prepare.call_args_list))
        marker = serial["density_fusion"]
        self.assertEqual(marker["requested"], _FUSION)
        for lane in marker["observed"].values():
            self.assertEqual(set(lane), {"0", "1", "2"})
            if version("qiskit-aer") == "0.18.2":
                self.assertEqual([lane[str(rounds)]["applied"] for rounds in range(3)], [False, True, True])
