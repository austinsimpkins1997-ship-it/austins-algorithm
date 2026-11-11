"""Check compile-aware selection between two exact terminal phase covers."""

from __future__ import annotations

import hashlib
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.compilation import MEASUREMENT_TOLERANCE, line_target  # noqa: E402
from austins_algorithm.constraint_cover_comparison import compare_constraint_phase_covers  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint, load_constraint  # noqa: E402

try:
    from qiskit import qasm3
except ImportError:
    QASM_AVAILABLE = False
else:
    QASM_AVAILABLE = hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import") is not None


@unittest.skipUnless(QASM_AVAILABLE, "optional Qiskit OpenQASM tools are unavailable")
class ConstraintCoverComparisonTests(unittest.TestCase):
    def test_interlock_compares_both_audited_programs_on_one_target(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "site_interlock.json")
        comparison = compare_constraint_phase_covers(constraint, target=line_target(10))
        report = comparison.report
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(set(report["candidates"]), {"direct", "complement"})
        self.assertLessEqual(report["cross_cover_total_variation_distance"], MEASUREMENT_TOLERANCE)
        for candidate in report["candidates"].values():
            self.assertEqual(candidate["active_qubits"], 6)
            self.assertTrue(candidate["eligible"])
            self.assertEqual(candidate["oracle_audit"]["states_checked"], 64)
            self.assertEqual(candidate["oracle_audit"]["marked_count"], 6)
            self.assertEqual(candidate["compilation"]["target"]["qubits"], 10)
        candidates = report["candidates"]
        self.assertEqual(candidates["direct"]["oracle_audit"], candidates["complement"]["oracle_audit"])
        winner = candidates[report["selected_cover"]]["compilation"]
        self.assertEqual(
            winner["compiled"]["two_qubit_gates"],
            min(candidate["compilation"]["compiled"]["two_qubit_gates"] for candidate in candidates.values()),
        )
        self.assertEqual(comparison.selected.report, winner)
        self.assertEqual(hashlib.sha256(comparison.selected.openqasm.encode("utf-8")).hexdigest(),
                         winner["openqasm"]["sha256"])

    def test_sensor_kit_forces_the_complement_even_when_heuristic_would_keep_direct(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        comparison = compare_constraint_phase_covers(constraint, target=line_target(12))
        report = comparison.report
        direct, complement = (report["candidates"][cover] for cover in ("direct", "complement"))
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(direct["oracle_audit"], complement["oracle_audit"])
        self.assertEqual(direct["oracle_audit"]["marked_count"], 9)
        self.assertNotEqual(direct["compilation"]["input"]["operations"],
                            complement["compilation"]["input"]["operations"])
        self.assertLessEqual(report["cross_cover_total_variation_distance"], MEASUREMENT_TOLERANCE)

    def test_active_wire_limit_leaves_no_eligible_selection(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "site_interlock.json")
        comparison = compare_constraint_phase_covers(constraint, target=line_target(10), max_active_qubits=5)
        self.assertEqual(comparison.report["status"], "NO_COVER_FITS")
        self.assertIsNone(comparison.report["selected_cover"])
        self.assertIsNone(comparison.selected)
        for candidate in comparison.report["candidates"].values():
            self.assertFalse(candidate["eligible"])
            self.assertGreater(candidate["active_qubits"], 5)

    def test_no_terminal_component_compiles_direct_once(self):
        constraint = BooleanConstraint(2, ((1,),))
        comparison = compare_constraint_phase_covers(constraint, target=line_target(3))
        self.assertEqual(comparison.report["status"], "DIRECT_ONLY")
        self.assertEqual(comparison.report["selected_cover"], "direct")
        self.assertEqual(set(comparison.report["candidates"]), {"direct"})
        self.assertIsNone(comparison.report["cross_cover_total_variation_distance"])
        self.assertEqual(comparison.selected.report, comparison.report["candidates"]["direct"]["compilation"])

    def test_fixed_initial_layout_is_shared_by_both_covers(self):
        constraint = BooleanConstraint(2, ((1, 2), (-1, -2)))
        comparison = compare_constraint_phase_covers(constraint, target=line_target(5), initial_layout=(4, 3))
        self.assertEqual(comparison.report["status"], "COMPLETED")
        for candidate in comparison.report["candidates"].values():
            self.assertEqual(candidate["compilation"]["initial_logical_to_target_qubit"], [4, 3])

    def test_invalid_comparison_limits_are_rejected(self):
        constraint = BooleanConstraint(2, ((1, 2),))
        target = line_target(3)
        with self.assertRaises(ValueError):
            compare_constraint_phase_covers(constraint)
        for kwargs in ({"rounds": -1}, {"seed": -1}, {"max_active_qubits": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                compare_constraint_phase_covers(constraint, target=target, **kwargs)


if __name__ == "__main__":
    unittest.main()

