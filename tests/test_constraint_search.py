import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import mock_open, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm import grover_search  # noqa: E402
from austins_algorithm.constraint_factorization import analyze_constraint_factorization  # noqa: E402
from austins_algorithm.constraint_oracle import (  # noqa: E402
    build_constraint_phase_oracle,
    synthesize_constraint_oracle,
)
from austins_algorithm.compilation import compile_and_verify, line_target  # noqa: E402
from austins_algorithm.constraint_search import (  # noqa: E402
    PreparedConstraintLineSearch,
    classical_first_match,
    run_constraint_search,
)
from austins_algorithm.constraints import BooleanConstraint, MAX_CONSTRAINT_BYTES, load_constraint  # noqa: E402
from austins_algorithm.grover_search import _LastAttempt, audit_phase_oracle, run_grover_search  # noqa: E402

try:
    from qiskit import qasm3
except Exception as exc:
    QISKIT_AVAILABLE = False
    QISKIT_REASON = repr(exc)
else:
    QISKIT_AVAILABLE = True
    QISKIT_REASON = ""


@unittest.skipUnless(QISKIT_AVAILABLE, "optional Qiskit unavailable")
class PreparedConstraintLineSearchTests(unittest.TestCase):
    @unittest.skipUnless(
        QISKIT_AVAILABLE and (hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import")),
        "optional OpenQASM importer unavailable",
    )
    def test_compiles_once_and_replays_known_search(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        with patch("austins_algorithm.constraint_search.compile_and_verify", wraps=compile_and_verify) as compiler:
            plan = PreparedConstraintLineSearch(constraint, synthesis="unit-factorized", compiler_seed=11)
            for seed, query_cap in ((7, 8), (19, 8), (23, 0)):
                with self.subTest(seed=seed, query_cap=query_cap):
                    attempt = _LastAttempt()
                    baseline = run_constraint_search(
                        constraint, strategy="known", seed=seed, synthesis="unit-factorized",
                        max_shots=4, max_oracle_queries=query_cap, ideal_sampler="prepared_probabilities",
                        _last_attempt=attempt,
                    )["search"]
                    replay = plan.run(seed=seed, max_shots=4, max_oracle_queries=query_cap)
                    self.assertEqual(replay.pop("constraint_sha256"), constraint.digest())
                    self.assertEqual(
                        replay.pop("prepared_compilation_sha256"), plan.compilation_report["openqasm"]["sha256"],
                    )
                    self.assertEqual(replay, baseline)
                    if attempt.circuit is not None:
                        fresh = compile_and_verify(attempt.circuit, target=line_target(7, seed=11), seed=11)
                        self.assertEqual(plan.openqasm, fresh.openqasm)
                        self.assertEqual(plan.compilation_report, fresh.report)
                    else:
                        self.assertEqual(replay["status"], "BUDGET_EXHAUSTED")
            compiler.assert_called_once()

        report = plan.compilation_report
        report["openqasm"]["classical_bit_to_target_qubit"].clear()
        self.assertEqual(len(plan.compilation_report["openqasm"]["classical_bit_to_target_qubit"]), 6)

    @unittest.skipUnless(
        QISKIT_AVAILABLE and (hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import")),
        "optional OpenQASM importer unavailable",
    )
    def test_plan_prepares_the_compiled_input_once_across_concurrent_runs(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        seeds = (0, 1, 7, 19, 41, 2**32 - 1)
        oracle, _ = synthesize_constraint_oracle(constraint, synthesis="unit-factorized")
        baseline = [
            run_grover_search(
                oracle, constraint.qubits, constraint.matches, marked_count=9, seed=seed,
                max_shots=4, max_oracle_queries=8, ideal_sampler="prepared_probabilities",
            )
            for seed in seeds
        ]
        with patch("austins_algorithm.constraint_search.compile_and_verify", wraps=compile_and_verify) as compiler:
            plan = PreparedConstraintLineSearch(constraint, synthesis="unit-factorized")

        with patch.object(grover_search, "_prepare_ideal_measurement", side_effect=RuntimeError("evolution failed")):
            with self.assertRaisesRegex(RuntimeError, "evolution failed"):
                plan.run()

        captured = []
        original_prepare = grover_search._prepare_ideal_measurement

        def capture(circuit, measured_qubits, *, use_probabilities):
            weights = original_prepare(circuit, measured_qubits, use_probabilities=use_probabilities)
            captured.append((circuit, weights))
            return weights

        with patch.object(grover_search, "_prepare_ideal_measurement", side_effect=capture) as prepare:
            exhausted = plan.run(max_oracle_queries=0)
            self.assertEqual(exhausted["shots_used"], 0)
            self.assertIsNone(exhausted["ideal_sampler"]["used"])
            with self.assertRaises(ValueError):
                plan.run(seed=-1)
            plan.run(ideal_sampler="statevector_sampler")
            prepare.assert_not_called()

            def run(seed):
                result = plan.run(seed=seed, max_shots=4, max_oracle_queries=8)
                del result["constraint_sha256"], result["prepared_compilation_sha256"]
                return result

            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(run, seeds)), baseline)
            prepare.assert_called_once()
            circuit, weights = captured[0]
            self.assertIs(circuit, compiler.call_args.args[0])
            self.assertFalse(weights.flags.writeable)

            override = plan.run(ideal_sampler="prepared_statevector")
            self.assertEqual(override["ideal_sampler"]["used"], "prepared_statevector")
            self.assertEqual(run(seeds[0]), baseline[0])
            self.assertEqual(prepare.call_count, 2)

    @unittest.skipUnless(
        QISKIT_AVAILABLE and (hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import")),
        "optional OpenQASM importer unavailable",
    )
    def test_zero_round_plan_preserves_probability_eligibility_fallback(self):
        constraint = BooleanConstraint(2, ())
        for bytes_allowed, expected_sampler, preparations in (
            (128, "prepared_probabilities", 1),
            (0, "statevector_sampler", 0),
        ):
            with self.subTest(expected_sampler=expected_sampler):
                with (
                    patch.object(grover_search, "MAX_PREPARED_PROBABILITY_BYTES", bytes_allowed),
                    patch.object(
                        grover_search, "_prepare_ideal_measurement",
                        wraps=grover_search._prepare_ideal_measurement,
                    ) as prepare,
                ):
                    plan = PreparedConstraintLineSearch(constraint)
                    for seed in (5, 13):
                        report = plan.run(seed=seed, max_shots=1, max_oracle_queries=0)
                        self.assertEqual(report["status"], "FOUND")
                        self.assertEqual(report["scheduled_rounds"], 0)
                        self.assertEqual(report["oracle_queries"], 0)
                        self.assertEqual(report["shots_used"], 1)
                        self.assertEqual(report["ideal_sampler"]["used"], expected_sampler)
                        self.assertTrue(constraint.matches(report["candidate"]))
                    self.assertEqual(prepare.call_count, preparations)

    def test_proven_empty_formula_does_not_compile(self):
        constraint = BooleanConstraint(1, ((1,), (-1,)))
        with patch("austins_algorithm.constraint_search.compile_and_verify") as compiler:
            with self.assertRaisesRegex(ValueError, "proved the formula empty"):
                PreparedConstraintLineSearch(constraint)
        compiler.assert_not_called()


class ConstraintInputTests(unittest.TestCase):
    def test_signs_and_display_order_match_classical_assignments(self):
        constraint = BooleanConstraint(3, ((1,), (-3,)))
        self.assertEqual([bits for bits in ("001", "011", "100", "101", "110") if constraint.matches(bits)],
                         ["001", "011"])
        self.assertFalse(constraint.matches("xx1"))
        self.assertFalse(constraint.matches("01"))
        self.assertEqual(classical_first_match(constraint)["assignments_checked"], 2)

    def test_schema_rejects_invalid_versions_fields_literals_and_size(self):
        for data in (
            b'{"schema_version":true,"qubits":2,"clauses":[]}',
            b'{"schema_version":1,"qubits":2,"qubits":3,"clauses":[]}',
            b'{"schema_version":1,"qubits":2,"clauses":[[0]]}',
            b'{"schema_version":1,"qubits":2,"clauses":[[true]]}',
            b'{"schema_version":1,"qubits":2,"clauses":[[3]]}',
            b'{"schema_version":1,"qubits":2,"clauses":[],"extra":0}',
            b" " * (MAX_CONSTRAINT_BYTES + 1),
        ):
            with self.subTest(data=data[:120]), patch(
                "austins_algorithm.constraints.Path.open", mock_open(read_data=data)
            ), self.assertRaises(ValueError):
                load_constraint("formula.json")

    def test_factorization_counts_disjoint_components_and_free_variables(self):
        constraint = BooleanConstraint(5, ((1, 2), (-1, -2), (3,), (4, -4)))
        plan = analyze_constraint_factorization(constraint)
        self.assertEqual(plan.status, "ELIGIBLE")
        self.assertEqual(tuple(component.variables for component in plan.components), ((0, 1), (2,)))
        self.assertEqual(plan.unconstrained_variables, (3, 4))
        self.assertEqual(plan.assignments_checked, 6)
        self.assertEqual(plan.marked_count, 8)

    def test_digest_is_stable_across_whitespace_and_key_order(self):
        first = b'{"schema_version":1,"qubits":2,"clauses":[[1,-2]]}'
        second = b'{ "clauses": [[1, -2]], "qubits": 2, "schema_version": 1 }'
        with patch("austins_algorithm.constraints.Path.open", mock_open(read_data=first)):
            a = load_constraint("a.json")
        with patch("austins_algorithm.constraints.Path.open", mock_open(read_data=second)):
            b = load_constraint("b.json")
        self.assertEqual(a.digest(), b.digest())
        self.assertNotEqual(a.digest(), BooleanConstraint(2, ((-1, -2),)).digest())


@unittest.skipUnless(QISKIT_AVAILABLE, f"optional Qiskit is unavailable: {QISKIT_REASON}")
class ConstraintOracleTests(unittest.TestCase):
    def test_tautologies_duplicates_and_constants_preserve_phase_and_workspace(self):
        for clauses, expected in (
            ((), ["00", "01", "10", "11"]),
            (((),), []),
            (((1,), (-1,)), []),
            (((1, -1), (2, 2), (2,)), ["10", "11"]),
            (((1, -2), (-1, 2), (1, -2)), ["00", "11"]),
            (((1, 2), (-1, 2), (1, -2), (-1, -2)), []),
        ):
            with self.subTest(clauses=clauses):
                constraint = BooleanConstraint(2, clauses)
                oracle = build_constraint_phase_oracle(constraint)
                audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
                self.assertEqual(audit["marked_states"], expected)

    def test_work_wire_limit_rejects_circuit_before_execution(self):
        constraint = BooleanConstraint(8, ((1, 2), (1, 3), (1, 4), (1, 5), (1, 6)))
        with self.assertRaisesRegex(ValueError, "12"):
            build_constraint_phase_oracle(constraint)

    def test_empty_proof_and_sampling_exhaustion_are_distinct(self):
        with patch("austins_algorithm.constraint_search.run_grover_search") as sample:
            empty = run_constraint_search(BooleanConstraint(2, ((1,), (-1,))))
        sample.assert_not_called()
        self.assertEqual(empty["status"], "PROVEN_EMPTY")
        self.assertEqual(empty["exhaustive_oracle_audit"]["states_checked"], 4)
        self.assertIsNone(empty["search"])
        exhausted = run_constraint_search(
            BooleanConstraint(3, ((1,), (2,), (3,))), strategy="known", max_oracle_queries=0,
        )
        self.assertEqual(exhausted["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(exhausted["exhaustive_oracle_audit"]["marked_states"], ["111"])

    def test_cli_compile_and_snapshot_use_the_last_completed_sampler_circuit(self):
        from types import SimpleNamespace

        from qiskit.primitives import StatevectorSampler

        from austins_algorithm import grover_search
        from austins_algorithm.workflows_cli import _constraint

        original_run = StatevectorSampler.run
        for budget, rounds, status in (
            (1, [0, 0, 0, 1], "FOUND"),
            (0, [0, 0, 0], "BUDGET_EXHAUSTED"),
        ):
            sampled = []

            def observe(sampler, pubs, *, shots=None):
                sampled.append(pubs[0])
                return original_run(sampler, pubs, shots=shots)

            with (
                self.subTest(budget=budget),
                patch.object(StatevectorSampler, "run", new=observe),
                patch("austins_algorithm.constraint_search.synthesize_constraint_oracle",
                      wraps=synthesize_constraint_oracle) as synthesize,
                patch("austins_algorithm.constraint_oracle.synthesize_constraint_oracle",
                      side_effect=AssertionError("the CLI must not synthesize again")),
                patch.object(grover_search, "_build_circuit", wraps=grover_search._build_circuit) as build,
                patch.object(grover_search, "_prepare_diffuser", wraps=grover_search._prepare_diffuser) as diffuser,
                patch("austins_algorithm.compilation.compile_and_verify",
                      return_value=SimpleNamespace(report={"status": "COMPILED"}, openqasm="")) as compile_one,
                patch("austins_algorithm.ibm_models.run_constraint_snapshot",
                      return_value={"status": "COMPLETED"}) as snapshot,
            ):
                report = _constraint(self._constraint_cli_args(
                    formula=PACKAGE_ROOT / "examples" / "field_sensor_kit.json", max_oracle_queries=budget,
                ))
            self.assertEqual(report["status"], status)
            self.assertEqual(report["oracle"]["synthesis_used"], "factorized")
            self.assertEqual([row["rounds"] for row in report["search"]["attempts"]], rounds)
            self.assertEqual(len(sampled), len(rounds))
            self.assertEqual(build.call_count, len(set(rounds)))
            synthesize.assert_called_once()
            diffuser.assert_called_once()
            compile_one.assert_called_once()
            snapshot.assert_called_once()
            self.assertIs(compile_one.call_args.args[0], sampled[-1])
            self.assertIs(snapshot.call_args.args[0], sampled[-1])
            for result in (report["compilation"], report["snapshot"]):
                self.assertEqual((result["attempt"], result["rounds"]), (len(rounds), rounds[-1]))
            compilation = report["compilation"]
            if budget == 0:
                self.assertEqual(compilation["attempt_build_seconds"], 0.0)
            self.assertEqual(
                compilation["build_compile_export_verify_seconds"],
                compilation["attempt_build_seconds"] + compilation["compile_export_verify_seconds"],
            )

    def test_cli_with_no_attempt_skips_compilation_snapshot_and_output(self):
        from unittest.mock import Mock

        from austins_algorithm.workflows_cli import _constraint

        for formula, status in (
            (BooleanConstraint(3, ((),)), "PROVEN_EMPTY"),
            (BooleanConstraint(3, ((1,), (2,), (3,))), "BUDGET_EXHAUSTED"),
        ):
            output = Mock(spec=Path)
            with (
                self.subTest(status=status),
                patch("austins_algorithm.constraints.load_constraint", return_value=formula),
                patch("austins_algorithm.grover_search._build_circuit") as build,
                patch("austins_algorithm.compilation.compile_and_verify") as compile_one,
                patch("austins_algorithm.ibm_models.run_constraint_snapshot") as snapshot,
            ):
                report = _constraint(self._constraint_cli_args(
                    strategy="known", max_oracle_queries=0, qasm_output=output,
                ))
            self.assertEqual(report["status"], status)
            self.assertEqual(report["compilation"], {"status": "NOT_RUN_NO_ATTEMPT"})
            self.assertEqual(report["snapshot"], {"status": "NOT_RUN_NO_ATTEMPT"})
            build.assert_not_called()
            compile_one.assert_not_called()
            snapshot.assert_not_called()
            output.open.assert_not_called()

    @staticmethod
    def _constraint_cli_args(**overrides):
        from argparse import Namespace

        options = {
            "formula": Path("formula.json"), "strategy": "unknown", "max_shots": 6,
            "max_oracle_queries": 256, "seed": 2, "synthesis": "auto", "diffuser": "standard",
            "classical_method": "scan", "compile": True, "line_width": None,
            "ibm_model": "manila", "snapshot_shots": 16, "qasm_output": None, "ideal_sampler": "statevector_sampler",
        }
        return Namespace(**{**options, **overrides})

    def test_field_kit_factorized_and_clause_flag_circuits_match_on_all_assignments(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        factored, report = synthesize_constraint_oracle(constraint)
        reference, _ = synthesize_constraint_oracle(constraint, synthesis="clause-flags")

        self.assertEqual((factored.num_qubits, reference.num_qubits), (7, 12))
        self.assertEqual(report["synthesis_used"], "factorized")
        self.assertEqual(report["factorization"]["local_assignments_checked"], 16)
        self.assertEqual(report["factorization"]["cubes_per_component"], [2, 2])
        factored_audit = audit_phase_oracle(factored, constraint.qubits, constraint.matches)
        reference_audit = audit_phase_oracle(reference, constraint.qubits, constraint.matches)
        self.assertEqual(factored_audit["states_checked"], 64)
        self.assertEqual(factored_audit["marked_states"], reference_audit["marked_states"])
        self.assertEqual(len(factored_audit["marked_states"]), 9)

    def test_auto_keeps_clause_flags_when_a_connected_component_exceeds_the_limit(self):
        constraint = BooleanConstraint(5, ((1, 2), (2, 3), (3, 4), (4, 5)))
        oracle, report = synthesize_constraint_oracle(constraint)
        self.assertEqual(oracle.num_qubits, 9)
        self.assertEqual(report["synthesis_used"], "clause-flags")
        self.assertEqual(report["factorization"]["status"], "COMPONENT_WIDTH_LIMIT")
        with self.assertRaisesRegex(ValueError, "COMPONENT_WIDTH_LIMIT"):
            synthesize_constraint_oracle(constraint, synthesis="factorized")

    def test_explicit_factorization_handles_constant_and_single_component_formulas(self):
        for clauses, expected in (
            ((), ["00", "01", "10", "11"]),
            (((1,), (-1,)), []),
            (((1, -2), (-1, 2)), ["00", "11"]),
        ):
            with self.subTest(clauses=clauses):
                constraint = BooleanConstraint(2, clauses)
                oracle, report = synthesize_constraint_oracle(constraint, synthesis="factorized")
                self.assertEqual(report["synthesis_used"], "factorized")
                self.assertEqual(oracle.num_qubits, 2)
                audit = audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
                self.assertEqual(audit["marked_states"], expected)

    def test_field_kit_known_and_unknown_strategies_share_the_same_oracle_audit(self):
        constraint = load_constraint(PACKAGE_ROOT / "examples" / "field_sensor_kit.json")
        expected = ["001010", "011001", "011010", "100110", "101010", "110101", "110110", "111001", "111010"]
        for strategy, supplied_count in (("known", 9), ("unknown", None)):
            with self.subTest(strategy=strategy):
                result = run_constraint_search(constraint, strategy=strategy)
                self.assertEqual(result["status"], "FOUND")
                self.assertEqual(result["exhaustive_oracle_audit"]["states_checked"], 64)
                self.assertEqual(result["exhaustive_oracle_audit"]["marked_states"], expected)
                self.assertEqual(result["search"]["marked_count"], supplied_count)
                self.assertIn(result["search"]["candidate"], expected)
                self.assertFalse(result["provider_job_submitted"])


if __name__ == "__main__":
    unittest.main()
