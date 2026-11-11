import json
import os
import random
import subprocess
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.classical_constraint import classical_first_match  # noqa: E402
from austins_algorithm.constraint_search import classical_first_match as original_first_match  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402


class ClassicalConstraintTests(unittest.TestCase):
    def test_scan_default_remains_available_at_original_import_path(self):
        constraint = BooleanConstraint(3, ((1,), (-3,)))
        for search in (classical_first_match, original_first_match):
            with self.subTest(search=search):
                result = search(constraint)
                self.assertEqual(result["method"], "classical_clause_scan_first_match")
                self.assertEqual(result["candidate"], "001")
                self.assertEqual(result["assignments_checked"], 2)
                self.assertGreaterEqual(result["wall_seconds"], 0)

    def test_propagation_handles_branch_order_and_cnf_edge_cases(self):
        cases = (
            (3, (), "000"),
            (3, ((),), None),
            (3, ((1, 3),), "001"),
            (3, ((3,), (-3, 2), (-2, 1)), "111"),
            (3, ((3, 2), (3, -2), (-3, 1)), "101"),
            (2, ((1,), (-1,)), None),
            (2, ((1, 2), (1, -2), (-1, 2), (-1, -2)), None),
            (3, ((1, -1), (2, 2, 2), (2,)), "010"),
        )
        for width, clauses, expected in cases:
            with self.subTest(width=width, clauses=clauses):
                constraint = BooleanConstraint(width, clauses)
                digest = constraint.digest()
                result = classical_first_match(constraint, method="propagation")
                self.assertEqual(result["method"], "classical_unit_propagation_first_match")
                self.assertEqual(result["candidate"], expected)
                self.assertEqual(constraint.clauses, clauses)
                self.assertEqual(constraint.digest(), digest)
                if expected is None:
                    self.assertGreater(result["conflicts"], 0)
                else:
                    self.assertTrue(constraint.matches(result["candidate"]))

    def test_all_forced_assignment_prunes_the_scan_range(self):
        constraint = BooleanConstraint(8, tuple((literal,) for literal in range(1, 9)))
        scan = classical_first_match(constraint, method="scan")
        propagation = classical_first_match(constraint, method="propagation")
        self.assertEqual(scan["candidate"], "11111111")
        self.assertEqual(propagation["candidate"], scan["candidate"])
        self.assertEqual(scan["assignments_checked"], 256)
        self.assertEqual(propagation["forced_assignments"], 8)
        self.assertEqual(propagation["conflicts"], 0)
        self.assertLess(propagation["nodes_visited"], scan["assignments_checked"])

    def test_seeded_formulas_match_scan_across_all_supported_widths(self):
        randomizer = random.Random(874)
        for width in range(1, 9):
            literals = tuple(range(-width, 0)) + tuple(range(1, width + 1))
            for case in range(40):
                minimum_length = 0 if case % 4 == 0 else 1
                clauses = tuple(
                    tuple(randomizer.choices(literals, k=randomizer.randint(minimum_length, 2 * width)))
                    for _ in range(randomizer.randrange(13))
                )
                with self.subTest(width=width, case=case):
                    constraint = BooleanConstraint(width, clauses)
                    scan = classical_first_match(constraint, method="scan")
                    propagation = classical_first_match(constraint, method="propagation")
                    self.assertEqual(propagation["candidate"], scan["candidate"])

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            classical_first_match(BooleanConstraint(1, ()), method="unknown")

    def test_cli_uses_classical_solver_with_site_packages_disabled(self):
        environment = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT / "src"))
        for flags, method in (
            ([], "classical_unit_propagation_first_match"),
            (["--method", "scan"], "classical_clause_scan_first_match"),
        ):
            with self.subTest(flags=flags):
                process = subprocess.run(
                    [sys.executable, "-S", "-B", "-m", "austins_algorithm", "constraint-classical",
                     "examples/field_sensor_kit.json", *flags],
                    cwd=PACKAGE_ROOT, env=environment, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                report = json.loads(process.stdout)
                self.assertEqual(report["status"], "FOUND")
                self.assertEqual(report["method"], method)
                self.assertEqual(report["candidate"], "001010")


if __name__ == "__main__":
    unittest.main()

