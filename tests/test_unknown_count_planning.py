"""SDK-free tests of bounded ideal unknown-count planning."""

from contextlib import redirect_stdout
from fractions import Fraction
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.__main__ import main as cli_main
from austins_algorithm.unknown_count_planning import plan_unknown_count_search


_SMALL_MENUS = (1, 2, 2, 2, 3, 3)
_SMALL_QUERY_CAP = 1
_HIT_BY_ROUND = (Fraction(1, 4), Fraction(1), Fraction(1, 4))
_TERMINALS = (
    "found_probability", "shot_budget_exhaustion_probability", "query_budget_exhaustion_probability",
)


def _enumerate_small_paths(*, affordable):
    """Expand nominal draws, redraws, hits and misses separately using exact fractions."""
    result = dict.fromkeys(
        (*_TERMINALS, "expected_shots", "expected_oracle_queries", "expected_round_adjustments"), Fraction(0),
    )

    def terminal(outcome, mass, shots, remaining, adjustments):
        result[outcome] += mass
        result["expected_shots"] += mass * shots
        result["expected_oracle_queries"] += mass * (_SMALL_QUERY_CAP - remaining)
        result["expected_round_adjustments"] += mass * adjustments

    def visit(shots, remaining, adjustments, mass):
        if not mass:
            return
        if shots == len(_SMALL_MENUS):
            terminal("shot_budget_exhaustion_probability", mass, shots, remaining, adjustments)
            return
        menu = _SMALL_MENUS[shots]
        nominal_mass = mass / menu
        for nominal in range(menu):
            overflow = nominal > remaining
            if overflow and not affordable:
                terminal("query_budget_exhaustion_probability", nominal_mass, shots, remaining, adjustments)
                continue
            executed = range(remaining + 1) if overflow else (nominal,)
            for rounds in executed:
                branch_mass = nominal_mass / len(executed)
                hit = _HIT_BY_ROUND[rounds]
                after = (shots + 1, remaining - rounds, adjustments + overflow)
                terminal("found_probability", branch_mass * hit, *after)
                visit(*after, branch_mass * (1 - hit))

    visit(0, _SMALL_QUERY_CAP, 0, Fraction(1))
    result["expected_verifier_calls"] = result["expected_shots"]
    return result


class UnknownCountPlanningTests(unittest.TestCase):
    def test_policies_match_independent_exact_path_enumeration(self):
        report = plan_unknown_count_search(
            3, 2, max_shots=len(_SMALL_MENUS), max_oracle_queries=_SMALL_QUERY_CAP, max_rounds_per_shot=2,
        )
        self.assertEqual(report["status"], "UNKNOWN_COUNT_PLAN_MODELED")
        self.assertEqual(report["round_choices_by_attempt"], list(_SMALL_MENUS))
        for policy in ("stop", "affordable"):
            with self.subTest(policy=policy):
                expected = _enumerate_small_paths(affordable=policy == "affordable")
                self.assertEqual(sum(expected[field] for field in _TERMINALS), 1)
                actual = report["policies"][policy]
                for field, value in expected.items():
                    self.assertAlmostEqual(actual[field], float(value), places=13, msg=field)

    def test_all_marked_and_empty_have_distinct_terminal_probabilities(self):
        full = plan_unknown_count_search(2, 4, max_shots=2, max_oracle_queries=0)["policies"]
        empty = plan_unknown_count_search(2, 0, max_shots=2, max_oracle_queries=0)["policies"]
        for policy in ("stop", "affordable"):
            with self.subTest(policy=policy):
                self.assertEqual(full[policy]["found_probability"], 1)
                self.assertEqual(full[policy]["expected_shots"], 1)
                self.assertEqual(full[policy]["expected_oracle_queries"], 0)
                self.assertEqual(full[policy]["expected_round_adjustments"], 0)
                self.assertEqual(empty[policy]["found_probability"], 0)
                self.assertEqual(empty[policy]["expected_oracle_queries"], 0)
        self.assertAlmostEqual(empty["stop"]["query_budget_exhaustion_probability"], .5)
        self.assertAlmostEqual(empty["stop"]["shot_budget_exhaustion_probability"], .5)
        self.assertAlmostEqual(empty["stop"]["expected_shots"], 1.5)
        self.assertAlmostEqual(empty["affordable"]["query_budget_exhaustion_probability"], 0)
        self.assertAlmostEqual(empty["affordable"]["shot_budget_exhaustion_probability"], 1)
        self.assertAlmostEqual(empty["affordable"]["expected_shots"], 2)
        self.assertAlmostEqual(empty["affordable"]["expected_round_adjustments"], .5)

    def test_zero_round_cap_matches_truncated_uniform_sampling(self):
        report = plan_unknown_count_search(3, 1, max_shots=4, max_oracle_queries=7, max_rounds_per_shot=0)
        self.assertEqual(report["round_choices_by_attempt"], [1] * 4)
        expected_hit = 1 - Fraction(7, 8) ** 4
        for policy in ("stop", "affordable"):
            with self.subTest(policy=policy):
                row = report["policies"][policy]
                self.assertAlmostEqual(row["found_probability"], float(expected_hit))
                self.assertAlmostEqual(row["expected_shots"], float(8 * expected_hit))
                self.assertEqual(row["expected_oracle_queries"], 0)
                self.assertEqual(row["query_budget_exhaustion_probability"], 0)
                self.assertEqual(row["expected_round_adjustments"], 0)

    def test_invalid_inputs_fail_validation(self):
        for qubits, solutions, limits in (
            (0, 0, {}), (13, 0, {}), (True, 0, {}), (3, -1, {}), (3, 9, {}),
            (3, True, {}), (3, 1.0, {}), (3, 1, {"max_shots": 0}), (3, 1, {"max_shots": 257}),
            (3, 1, {"max_oracle_queries": -1}), (3, 1, {"max_oracle_queries": 4097}),
            (3, 1, {"max_rounds_per_shot": -1}), (3, 1, {"max_rounds_per_shot": 257}),
        ):
            with self.subTest(qubits=qubits, solutions=solutions, limits=limits), self.assertRaises(ValueError):
                plan_unknown_count_search(qubits, solutions, **limits)

    def test_cli_reports_ideal_plan(self):
        stdout = StringIO()
        arguments = [
            "austins_algorithm", "unknown-count-plan", "--qubits", "1", "--solutions", "1",
            "--max-shots", "2", "--max-oracle-queries", "0", "--max-rounds-per-shot", "1",
        ]
        with patch.object(sys, "argv", arguments), redirect_stdout(stdout):
            cli_main()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "UNKNOWN_COUNT_PLAN_MODELED")
        self.assertAlmostEqual(report["policies"]["stop"]["found_probability"], .625)
        self.assertAlmostEqual(report["policies"]["affordable"]["found_probability"], .75)
        self.assertAlmostEqual(report["policies"]["affordable"]["expected_shots"], 1.5)


if __name__ == "__main__":
    unittest.main()
