import random
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.classical_constraint import reduce_constraint_units  # noqa: E402
from austins_algorithm.constraint_factorization import analyze_constraint_factorization  # noqa: E402
from austins_algorithm.constraint_oracle import synthesize_constraint_oracle  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint, load_constraint  # noqa: E402
from austins_algorithm.grover_search import audit_phase_oracle  # noqa: E402


def marked_states(constraint):
    states = (format(value, f"0{constraint.qubits}b") for value in range(1 << constraint.qubits))
    return [bits for bits in states if constraint.matches(bits)]


class UnitReductionTests(unittest.TestCase):
    def assert_equivalent(self, original, reduced):
        self.assertEqual(reduced.constraint.qubits, original.qubits)
        self.assertEqual(marked_states(reduced.constraint), marked_states(original))
        self.assertEqual(tuple(sorted(reduced.forced_literals, key=abs)), reduced.forced_literals)
        self.assertEqual(len(set(map(abs, reduced.forced_literals))), len(reduced.forced_literals))
        for literal in reduced.forced_literals:
            self.assertIn((literal,), reduced.constraint.clauses)

    def test_propagated_signs_and_residual_clause_are_retained(self):
        clauses = ((1,), (-1, -2), (2, 3), (2, 4, 5), (-3, 4, 5), (4, -4))
        original = BooleanConstraint(5, clauses)
        digest = original.digest()
        reduced = reduce_constraint_units(original)
        self.assertFalse(reduced.contradiction)
        self.assertEqual(reduced.forced_literals, (1, -2, 3))
        self.assertEqual(set(reduced.constraint.clauses), {(1,), (-2,), (3,), (4, 5)})
        self.assertEqual(original.clauses, clauses)
        self.assertEqual(original.digest(), digest)
        self.assert_equivalent(original, reduced)

    def test_root_reduction_does_not_promote_values_from_a_branch(self):
        for clauses in (((1, 2),), ((1, 2), (1, -2), (-1, 2), (-1, -2))):
            with self.subTest(clauses=clauses):
                original = BooleanConstraint(3, clauses)
                reduced = reduce_constraint_units(original)
                self.assertFalse(reduced.contradiction)
                self.assertEqual(reduced.forced_literals, ())
                self.assert_equivalent(original, reduced)

    def test_empty_formula_empty_clause_and_duplicate_literals(self):
        for clauses, expected, contradiction in (
            ((), (), False),
            (((),), ((),), True),
            (((1,), (-1,)), ((),), True),
            (((1,), (-1, 2), (-2, -3), (3,)), ((),), True),
            (((1, -1), (2, 2), (2,)), ((2,),), False),
        ):
            with self.subTest(clauses=clauses):
                original = BooleanConstraint(3, clauses)
                reduced = reduce_constraint_units(original)
                self.assertEqual(reduced.contradiction, contradiction)
                self.assertEqual(reduced.constraint.clauses, expected)
                if contradiction:
                    self.assertEqual(reduced.forced_literals, ())
                self.assert_equivalent(original, reduced)

    def test_seeded_reductions_are_equivalent_at_every_supported_width(self):
        rng = random.Random(4839)
        for width in range(1, 9):
            literals = tuple(range(-width, 0)) + tuple(range(1, width + 1))
            for case in range(48):
                clauses = tuple(
                    tuple(rng.choices(literals, k=rng.randrange(1, 6)))
                    for _ in range(rng.randrange(13))
                )
                with self.subTest(width=width, case=case):
                    original = BooleanConstraint(width, clauses)
                    self.assert_equivalent(original, reduce_constraint_units(original))


@unittest.skipUnless(find_spec("qiskit") is not None, "optional Qiskit is unavailable")
class UnitReducedOracleTests(unittest.TestCase):
    def test_site_interlock_reduces_width_and_preserves_all_basis_phases(self):
        original = load_constraint(PACKAGE_ROOT / "examples" / "site_interlock.json")
        reference, _ = synthesize_constraint_oracle(original, synthesis="clause-flags")
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-reduced")
        reduced = reduce_constraint_units(original)
        self.assertEqual((reference.num_qubits, oracle.num_qubits), (10, 7))
        self.assertEqual(report["synthesis_used"], "unit-reduced")
        self.assertEqual(report["unit_reduction"]["forced_literals"], list(reduced.forced_literals))
        self.assertFalse(report["unit_reduction"]["contradiction"])
        self.assertEqual(report["unit_reduction"]["residual_clause_count"], 1)
        self.assertEqual(report["unit_reduction"]["retained_clause_count"], len(reduced.constraint.clauses))
        audited = audit_phase_oracle(oracle, original.qubits, original.matches)
        self.assertEqual(audited["states_checked"], 64)
        self.assertEqual(audited["marked_states"], marked_states(original))
        self.assertEqual(audited, audit_phase_oracle(reference, original.qubits, original.matches))

    def test_implied_contradiction_has_an_identity_oracle(self):
        original = BooleanConstraint(3, ((1,), (-1, 2), (-2, -3), (3,)))
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-reduced")
        self.assertEqual(oracle.num_qubits, original.qubits)
        self.assertEqual(oracle.size(), 0)
        self.assertTrue(report["unit_reduction"]["contradiction"])
        self.assertEqual(report["unit_reduction"]["forced_literals"], [])
        self.assertIsNone(report["unit_reduction"]["residual_clause_count"])
        self.assertEqual(report["unit_reduction"]["retained_clause_count"], 1)
        self.assertEqual(audit_phase_oracle(oracle, original.qubits, original.matches)["marked_states"], [])

    def test_unit_factorization_splits_the_connected_site_interlock(self):
        original = load_constraint(PACKAGE_ROOT / "examples" / "site_interlock.json")
        with self.assertRaisesRegex(ValueError, "COMPONENT_WIDTH_LIMIT"):
            synthesize_constraint_oracle(original, synthesis="factorized")
        reference, _ = synthesize_constraint_oracle(original, synthesis="unit-reduced")
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized")
        self.assertEqual((reference.num_qubits, oracle.num_qubits), (7, 6))
        self.assertEqual(report["synthesis_requested"], "unit-factorized")
        self.assertEqual(report["synthesis_used"], "unit-factorized")
        self.assertEqual(report["unit_reduction"]["forced_literals"], [1, 2, -3])
        self.assertEqual(report["unit_reduction"]["residual_clause_count"], 1)
        self.assertEqual(report["factorization"]["status"], "ELIGIBLE")
        self.assertEqual(report["factorization"]["component_variables"], [[0], [1], [2], [3, 4]])
        audited = audit_phase_oracle(oracle, original.qubits, original.matches)
        self.assertEqual(audited["states_checked"], 64)
        self.assertEqual(audited["marked_states"], marked_states(original))
        self.assertEqual(audited, audit_phase_oracle(reference, original.qubits, original.matches))

    def test_unit_factorization_rejects_an_oversized_residual_component(self):
        original = BooleanConstraint(6, ((1,), (2, 3, 4, 5, 6)))
        with self.assertRaisesRegex(ValueError, "COMPONENT_WIDTH_LIMIT"):
            synthesize_constraint_oracle(original, synthesis="unit-factorized")

    def test_unit_factorization_distinguishes_the_two_contradiction_proofs(self):
        root_conflict = BooleanConstraint(3, ((1,), (-1, 2), (-2, -3), (3,)))
        component_conflict = BooleanConstraint(3, ((3,), (1, 2), (1, -2), (-1, 2), (-1, -2)))
        for original, contradiction, forced in ((root_conflict, True, []), (component_conflict, False, [3])):
            with self.subTest(clauses=original.clauses):
                oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized")
                self.assertEqual(report["synthesis_used"], "unit-factorized")
                self.assertEqual(report["unit_reduction"]["contradiction"], contradiction)
                self.assertEqual(report["unit_reduction"]["forced_literals"], forced)
                self.assertEqual(report["factorization"]["status"], "PROVEN_EMPTY")
                self.assertEqual(oracle.num_qubits, original.qubits)
                self.assertEqual(oracle.size(), 0)
                audit = audit_phase_oracle(oracle, original.qubits, original.matches)
                self.assertEqual(audit["states_checked"], 8)
                self.assertEqual(audit["marked_states"], [])

    def test_unit_factorization_marks_an_all_true_formula(self):
        original = BooleanConstraint(3, ((1, -1),))
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized")
        self.assertEqual(report["unit_reduction"]["forced_literals"], [])
        self.assertEqual(report["factorization"]["status"], "ALWAYS_TRUE")
        self.assertEqual(oracle.num_qubits, original.qubits)
        self.assertEqual(audit_phase_oracle(oracle, original.qubits, original.matches)["marked_states"], marked_states(original))



class ComponentComplementTests(unittest.TestCase):
    def test_opt_in_complement_cubes_partition_each_local_truth_table(self):
        for original in (
            BooleanConstraint(2, ((1, 2),)),
            BooleanConstraint(2, ((1, 2), (-1, -2))),
            BooleanConstraint(4, ((1, 2, 3, 4), (-1, -2, 3, 4), (-3, 4))),
        ):
            with self.subTest(clauses=original.clauses):
                ordinary = analyze_constraint_factorization(original)
                analyzed = analyze_constraint_factorization(original, include_complement=True)
                component, = analyzed.components
                self.assertIsNone(ordinary.components[0].complement_cubes)
                self.assertEqual(component.cubes, ordinary.components[0].cubes)
                self.assertEqual(analyzed.assignments_checked, 1 << original.qubits)
                self.assertEqual(analyzed.marked_count, ordinary.marked_count)
                for value in range(1 << original.qubits):
                    bits = format(value, f"0{original.qubits}b")
                    passes = original.matches(bits)
                    for cubes, expected in ((component.cubes, passes), (component.complement_cubes, not passes)):
                        matching = sum(
                            all(bool(value & (1 << wire)) == bit for wire, bit in cube)
                            for cube in cubes
                        )
                        self.assertEqual(matching, int(expected), bits)

    def test_complement_keeps_the_component_width_limit(self):
        original = BooleanConstraint(5, ((1, 2, 3, 4, 5),))
        analysis = analyze_constraint_factorization(original, include_complement=True)
        self.assertEqual(analysis.status, "COMPONENT_WIDTH_LIMIT")
        self.assertEqual(analysis.assignments_checked, 0)
        self.assertIsNone(analysis.components[0].complement_cubes)


@unittest.skipUnless(find_spec("qiskit") is not None, "optional Qiskit is unavailable")
class ComplementPhaseOracleTests(unittest.TestCase):
    def assert_exact_clean_workspace_phase(self, original, oracle):
        import numpy as np
        from qiskit.quantum_info import Operator

        count = 1 << original.qubits
        observed = Operator(oracle).data[:, :count]
        expected = np.zeros_like(observed)
        for value in range(count):
            bits = format(value, f"0{original.qubits}b")
            expected[value, value] = -1 if original.matches(bits) else 1
        np.testing.assert_allclose(observed, expected, atol=1e-10, rtol=0)

    def test_empty_prefix_uses_exact_global_phase(self):
        original = BooleanConstraint(2, ((1, 2),))
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized-complement")
        self.assertEqual(report["synthesis_used"], "unit-factorized-complement")
        self.assertEqual(report["phase_cover"]["selected"], "complement")
        self.assertEqual(report["phase_cover"]["terminal_component_variables"], [0, 1])
        self.assertEqual(oracle.num_qubits, 2)
        self.assert_exact_clean_workspace_phase(original, oracle)

    def test_site_interlock_keeps_each_forced_literal_as_a_phase_control(self):
        original = load_constraint(PACKAGE_ROOT / "examples" / "site_interlock.json")
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized-complement")
        self.assertEqual(oracle.num_qubits, 6)
        self.assertEqual(report["phase_cover"]["selected"], "complement")
        self.assertEqual(report["phase_cover"]["direct_arities"], [5, 4])
        self.assertEqual(report["phase_cover"]["complement_arities"], [5, 3])
        self.assertEqual(report["unit_reduction"]["forced_literals"], [1, 2, -3])
        self.assert_exact_clean_workspace_phase(original, oracle)

    def test_terminal_complement_respects_and_uncomputes_other_factor_flag(self):
        original = BooleanConstraint(4, ((1, 2), (3, 4)))
        oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized-complement")
        self.assertEqual(report["phase_cover"]["selected"], "complement")
        self.assertEqual(oracle.num_qubits, 5)
        self.assert_exact_clean_workspace_phase(original, oracle)

    def test_equal_cost_and_field_sensor_kit_keep_direct_cover(self):
        tie = BooleanConstraint(2, ((1, 2), (-1, -2)))
        kit = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        for original in (tie, kit):
            with self.subTest(clauses=original.clauses):
                oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized-complement")
                self.assertEqual(report["phase_cover"]["selected"], "direct")
                self.assert_exact_clean_workspace_phase(original, oracle)

    def test_constant_formulas_keep_exact_phase_without_terminal_cover(self):
        for clauses in (((1, -1),), ((1,), (-1,))):
            with self.subTest(clauses=clauses):
                original = BooleanConstraint(2, clauses)
                oracle, report = synthesize_constraint_oracle(original, synthesis="unit-factorized-complement")
                self.assertEqual(report["phase_cover"]["selected"], "not-applicable")
                self.assert_exact_clean_workspace_phase(original, oracle)



if __name__ == "__main__":
    unittest.main()

