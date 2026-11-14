import importlib.util
import math
import unittest

from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.grover_search import audit_phase_oracle, build_grover_search_circuit
from austins_algorithm.optimization import preview
from austins_algorithm.weighted_oracle import build_weighted_phase_oracle


@unittest.skipUnless(importlib.util.find_spec("qiskit"), "Qiskit extra is required")
class WeightedOracleTests(unittest.TestCase):
    def test_threshold_phase_and_grover_distribution_match_independent_reference(self):
        from qiskit.quantum_info import Statevector

        problem = {"name": "Choice", "variables": [{"name": "A", "cost": 5}, {"name": "B", "cost": 1}],
                   "clauses": [[1, 2]]}
        constraint = BooleanConstraint(2, problem["clauses"])
        oracle, work = build_weighted_phase_oracle(problem, incumbent=5)
        audit = audit_phase_oracle(oracle, 2, lambda bits: constraint.matches(bits) and bits == "10")
        self.assertEqual(audit["marked_states"], ["10"])
        self.assertEqual(work["marked_assignments"], 1)
        grover = build_grover_search_circuit(oracle, 2, iterations=1).remove_final_measurements(inplace=False)
        expected = preview(problem, incumbent=5, rounds=1)["frames"][-1]["probabilities"]
        actual = Statevector.from_instruction(grover).probabilities()
        self.assertTrue(all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(actual, expected)))

    def test_all_empty_and_partially_marked_cubes(self):
        for clauses, bound, expected in (([], None, 4), ([[]], None, 0), ([], 0, 0), ([[1]], None, 2)):
            with self.subTest(clauses=clauses, bound=bound):
                problem = {"name": "Choice", "variables": [{"name": "A", "cost": 1}, {"name": "B", "cost": 1}],
                           "clauses": clauses}
                oracle, work = build_weighted_phase_oracle(problem, incumbent=bound)
                constraint = BooleanConstraint(2, clauses)
                audit = audit_phase_oracle(
                    oracle, 2, lambda bits: constraint.matches(bits) and (bound is None or bits.count("1") < bound))
                self.assertEqual(work["marked_assignments"], expected)
                self.assertEqual(audit["marked_count"], expected)
