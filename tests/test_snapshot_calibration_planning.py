"""SDK-free regressions for planning against matched archived calibrations."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import snapshot_calibration_planning, unknown_count_snapshot_layout
from austins_algorithm.__main__ import main
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_planning import plan_snapshot_calibration_budgets


_CONSTRAINT = BooleanConstraint(2, ((-1,), (-2,)))
_OUTCOMES = ("00", "01", "10", "11")
_SIDE_NAMES = ("earlier", "later")
_FOUND = "minimum_found_probability"
_FAILURE = "maximum_failure_probability"
_GATES = "worst_case_expected_compiled_two_qubit_gates"
_PLAN_ID = ("layout", "max_shots", "max_oracle_queries", "policy")


def _pin(label):
    return sha256(label.encode("utf-8")).hexdigest()


def _distribution(hit, *, miss=None):
    return dict(zip(_OUTCOMES, (hit, 1 - hit if miss is None else miss, 0, 0), strict=True))


def _report(layout=(0, 1), *, hits=((.5, .25), (.75, .875)), first_later_miss=None):
    snapshots = {}
    for side, date in zip(_SIDE_NAMES, ("2021-07-26T11:47:01-04:00", "2024-05-27T14:32:24-04:00"), strict=True):
        snapshots[side] = {
            "backend_name": "ibmq_jakarta", "backend_version": "1.0.0", "qubits": 7,
            "last_update_date": date, "configuration_sha256": _pin(f"{side}:config"),
            "properties_sha256": _pin(f"{side}:properties"),
        }
    rows = {}
    for round_count, (earlier_hit, later_hit) in enumerate(hits):
        gates = 4 * round_count
        operations = {"sx": 2, "measure": 2}
        if gates:
            operations["cx"] = gates
        ideal = _distribution(1) if round_count else dict.fromkeys(_OUTCOMES, .25)
        physical = sorted(layout)
        target_calibration = {
            "qubits": {str(wire): {"t1_seconds": .001, "t2_seconds": .001} for wire in layout},
            "operations": [
                {"operation": operation, "wires": [wire], "error": .01, "duration_seconds": 1e-6}
                for operation in ("measure", "sx") for wire in physical
            ],
        }
        if gates:
            target_calibration["operations"].append({
                "operation": "cx", "wires": list(layout), "error": .01, "duration_seconds": 1e-6,
            })
        earlier = _distribution(earlier_hit)
        later = _distribution(later_hit, miss=first_later_miss if round_count == 0 else None)
        deltas = {bits: later[bits] - earlier[bits] for bits in _OUTCOMES}
        rows[str(round_count)] = {
            "compilation": {
                "status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION",
                "target": {"kind": "BACKEND_V2", "name": "ibmq_jakarta", "qubits": 7,
                           "operations": ["cx", "measure", "sx"]},
                "seed_transpiler": 7, "optimization_level": 1,
                "initial_logical_to_target_qubit": list(layout),
                "input": {"qubits": 2, "depth": 2 + gates, "two_qubit_gates": gates, "operations": operations},
                "compiled": {"qubits": 7, "depth": 2 + gates, "two_qubit_gates": gates, "operations": operations},
                "openqasm": {
                    "representation": "PORTABLE_COMPILED_WIRES", "bytes": 128,
                    "sha256": _pin(f"layout:{layout}:round:{round_count}"), "importer": "qiskit.qasm3.loads",
                    "export_wire_to_target_qubit": physical, "classical_bit_to_target_qubit": list(layout),
                },
                "verification": {
                    "bit_order": "Highest classical bit index first", "tolerance": 1e-10,
                    "original_probabilities": ideal, "compiled_probabilities": ideal, "roundtrip_probabilities": ideal,
                    "compilation_total_variation_distance": 0, "export_total_variation_distance": 0,
                    "roundtrip_total_variation_distance": 0,
                },
                "provider_job_submitted": False, "quantum_hardware_used": False,
            },
            "target_calibrations": {side: deepcopy(target_calibration) for side in _SIDE_NAMES},
            "readout_assignment_matrices": {
                side: {str(wire): [[1, 0], [0, 1]] for wire in layout} for side in _SIDE_NAMES
            },
            "earlier": {"outcome_probabilities": earlier, "input_probability_mass": math.fsum(earlier.values()),
                        "verified_hit_probability": earlier_hit},
            "later": {"outcome_probabilities": later, "input_probability_mass": math.fsum(later.values()),
                      "verified_hit_probability": later_hit},
            "right_minus_left_outcome_probabilities": deltas,
            "right_minus_left_hit_probability": deltas["00"],
            "outcome_total_variation_distance": math.fsum(map(abs, deltas.values())) / 2,
        }
    return {
        "schema_version": 1, "status": "COMPLETED", "execution_mode": "SIMULATOR",
        "comparison": "MATCHED_CIRCUITS_ON_TWO_ARCHIVED_IBM_SNAPSHOTS",
        "noise_method": "AER_COMPACT_STATIC_DENSITY_MATRIX_LOCAL_READOUT", "compile_target": "later",
        "initial_layout": list(layout), "seed_transpiler": 7, "max_rounds": len(rows) - 1,
        "constraint_sha256": _CONSTRAINT.digest(), "search_qubits": 2,
        "audit": {"states_checked": 4, "marked_count": 1, "marked_states": ["00"]},
        "snapshots": snapshots, "configuration_changed": True, "backend_version_changed": False,
        "versions": {"qiskit": "2.6.2", "qiskit-aer": "0.18.2", "qiskit-ibm-runtime": "0.51.0", "numpy": "2.5.4"},
        "limits": {"active_qubits": 7, "aer_state_memory_mb": 128, "aer_threads": 1,
                   "local_result_wait_seconds": 30, "shots": 4096},
        "rounds": rows,
        "maximum_absolute_hit_probability_difference": max(
            abs(row["right_minus_left_hit_probability"]) for row in rows.values()
        ),
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _plan(reports, *, constraint=_CONSTRAINT, **overrides):
    options = {"target_probability": .7, "shot_caps": [1, 2], "query_caps": [0, 1], **overrides}
    return plan_snapshot_calibration_budgets(reports, constraint, **options)


def _candidates(report):
    return {tuple(row[key] for key in _PLAN_ID): row for row in report["candidates"]}


class SnapshotCalibrationPlanningTests(unittest.TestCase):
    def test_joint_selection_uses_worse_snapshot_and_exact_round_cost(self):
        first = _report()
        second = _report((0, 2), hits=((.375, .625), (1, .875)))
        report = _plan([first, second])
        self.assertEqual(report["status"], "SNAPSHOT_CALIBRATION_BUDGETS_PLANNED")
        self.assertEqual(report, _plan([second, first]))
        self.assertEqual(set(report["layouts"]), {"0,1", "0,2"})
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        rows = _candidates(report)
        for policy in ("stop", "affordable"):
            with self.subTest(policy=policy):
                a = rows["0,1", 2, 1, policy]
                self.assertEqual(a["target_status"], "EARLIER_ONLY")
                self.assertTrue(a["earlier"]["meets_target"])
                self.assertFalse(a["later"]["meets_target"])
                self.assertFalse(a["meets_target"])
                self.assertEqual(a["earlier"]["found_probability"], .8125)
                self.assertEqual(a["later"]["found_probability"], .671875)
                self.assertEqual(a[_FOUND], .671875)
                self.assertEqual(a[_FAILURE], .328125)
                self.assertEqual(a[_GATES], 1.5)
                self.assertEqual(a["worst_case_expected_shots"], 1.75)
                self.assertEqual(a["worst_case_expected_oracle_queries"], .375)
                b = rows["0,2", 2, 1, policy]
                self.assertEqual(b["target_status"], "MEETS_BOTH")
                self.assertTrue(b["meets_target"])
                self.assertEqual(b[_FOUND], .8046875)
        self.assertEqual(rows["0,1", 2, 0, "stop"][_FOUND], .34375)
        self.assertEqual(rows["0,1", 2, 0, "affordable"][_FOUND], .4375)
        self.assertEqual(rows["0,2", 1, 0, "stop"]["target_status"], "MEETS_NEITHER")
        self.assertEqual(rows["0,2", 2, 0, "stop"]["target_status"], "LATER_ONLY")
        self.assertEqual(report["feasible_count"], 2)
        self.assertEqual(report["feasible_count"], sum(row["meets_target"] for row in rows.values()))
        recommended = {row["layout"] for row in report["recommendations"].values() if row is not None}
        self.assertEqual(recommended, {"0,2"})
        self.assertEqual(report["classical_distinct_uniform"]["2"], {
            "found_probability": .5, "expected_verifier_calls": 1.75,
        })

    def test_target_one_uses_positive_miss_mass_even_if_hit_rounds_to_one(self):
        miss = 2**-54
        uncertain = _report(hits=((1, 1),), first_later_miss=miss)
        certain = _report((0, 2), hits=((1, 1),))
        report = _plan([uncertain, certain], target_probability=1, shot_caps=[1], query_caps=[0])
        rows = _candidates(report)
        uncertain_plan = rows["0,1", 1, 0, "stop"]
        self.assertEqual(uncertain_plan[_FOUND], 1)
        self.assertEqual(uncertain_plan[_FAILURE], miss)
        self.assertEqual(uncertain_plan["target_status"], "EARLIER_ONLY")
        self.assertFalse(uncertain_plan["meets_target"])
        self.assertEqual(rows["0,2", 1, 0, "stop"]["target_status"], "MEETS_BOTH")

    def test_insufficient_round_coverage_is_reported_without_extrapolation(self):
        short = _report(hits=((.5, .25),))
        unavailable = _plan([short])
        self.assertEqual(unavailable["status"], "SNAPSHOT_CALIBRATION_BUDGET_PLANNING_UNAVAILABLE")
        self.assertEqual(unavailable["candidates"], [])
        self.assertEqual(unavailable["required_rounds"], [0, 1])
        self.assertEqual(unavailable["layouts"]["0,1"]["status"], "INSUFFICIENT_ROUND_PROFILES")
        self.assertEqual(unavailable["layouts"]["0,1"]["saved_rounds"], [0])
        mixed = _plan([short, _report((0, 2))])
        self.assertEqual({row["layout"] for row in mixed["candidates"]}, {"0,2"})
        available = _plan([short], query_caps=[0])
        self.assertEqual(available["status"], "SNAPSHOT_CALIBRATION_BUDGETS_PLANNED")
        self.assertTrue(available["candidates"])

    def test_rejects_audit_and_derived_distribution_tampering(self):
        with self.assertRaisesRegex(ValueError, r"(?i)constraint"):
            _plan([_report()], constraint=BooleanConstraint(2, ((1,), (2,))))
        changes = (
            (("audit", "marked_states"), ["11"], r"(?i)(audit|witness|marked)"),
            (("rounds", "0", "later", "verified_hit_probability"), .5, r"(?i)(probabilit|hit|distribution)"),
            (("rounds", "1", "right_minus_left_hit_probability"), 0, r"(?i)(probabilit|hit|delta)"),
            (("rounds", "0", "outcome_total_variation_distance"), 0, r"(?i)(variation|distribution|delta)"),
        )
        for fields, value, message in changes:
            with self.subTest(fields=fields):
                modified = _report()
                parent = modified
                for field in fields[:-1]:
                    parent = parent[field]
                parent[fields[-1]] = value
                with self.assertRaisesRegex(ValueError, message):
                    _plan([modified])

    def test_rejects_mixed_provenance_and_duplicate_ordered_layouts(self):
        first = _report()
        with self.assertRaisesRegex(ValueError, r"(?i)(duplicate|unique|layout)"):
            _plan([first, _report()])
        for fields, value, message in (
            (("snapshots", "earlier", "properties_sha256"), _pin("other archive"), r"(?i)(snapshot|dated pair)"),
            (("versions", "qiskit"), "2.0.0", r"(?i)(version|software|provenance)"),
            (("rounds", "1", "compilation", "initial_logical_to_target_qubit"), [0, 1], r"(?i)(layout|compil)"),
        ):
            with self.subTest(fields=fields):
                second = _report((0, 2))
                parent = second
                for field in fields[:-1]:
                    parent = parent[field]
                parent[fields[-1]] = value
                with self.assertRaisesRegex(ValueError, message):
                    _plan([first, second])

    def test_transition_limit_counts_both_calibration_lanes_and_all_eligible_layouts(self):
        first, second = _report(), _report((0, 2))
        budgets = {"shot_caps": [2], "query_caps": [1]}
        with patch.object(unknown_count_snapshot_layout, "MAX_SELECTION_TRANSITIONS", 6):
            self.assertEqual(_plan([first], **budgets)["candidate_count"], 2)
            with (
                patch.object(snapshot_calibration_planning, "_selection_grid") as recurrence,
                self.assertRaisesRegex(ValueError, "exceeds 6 modeled transition updates"),
            ):
                _plan([first, second], **budgets)
            recurrence.assert_not_called()

    def test_cli_checks_pinned_report_bytes(self):
        build = (PACKAGE / ".build").resolve()
        if not build.is_relative_to(PACKAGE):
            raise RuntimeError("calibration planning test output must stay in the package")
        build.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=build, prefix="calibration-planning-test-") as directory:
            constraint = Path(directory) / "constraint.json"
            constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
            saved = Path(directory) / "report.json"
            raw = json.dumps(_report()).encode("utf-8")
            saved.write_bytes(raw)
            argv = [
                "austins-algorithm", "snapshot-calibration-budget-plan", str(constraint), str(saved),
                "--target-probability", ".7", "--shot-caps", "2", "--query-caps", "1",
                "--expected-report-sha256", sha256(raw).hexdigest(),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
                main()
            self.assertEqual(json.loads(stdout.getvalue())["status"], "SNAPSHOT_CALIBRATION_BUDGETS_PLANNED")
            saved.write_bytes(raw + b"\n")
            with (
                patch.object(sys, "argv", argv), redirect_stderr(StringIO()) as stderr,
                self.assertRaises(SystemExit) as rejected,
            ):
                main()
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertRegex(stderr.getvalue(), r"(?i)(sha-?256|digest)")


if __name__ == "__main__":
    unittest.main()
