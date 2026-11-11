import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from qiskit import QuantumCircuit, synthesis
    from qiskit.circuit import Parameter
    from qiskit.quantum_info import Operator, Statevector
except Exception as exc:  # The base package works without the optional framework.
    QuantumCircuit = None
    HAS_CLEAN_ANCILLA_SYNTHESIS = False
    QISKIT_IMPORT_ERROR = repr(exc)
else:
    QISKIT_IMPORT_ERROR = ""
    HAS_CLEAN_ANCILLA_SYNTHESIS = hasattr(synthesis, "synth_mcx_1_clean_kg24")

from austins_algorithm.core import austin_predicate  # noqa: E402
from austins_algorithm.grover_search import (  # noqa: E402
    audit_phase_oracle,
    build_austin_phase_oracle,
    build_grover_search_circuit,
    build_mod4_range_direct_oracle,
    build_mod4_range_oracle,
    mod4_range_predicate,
    run_grover_search,
)


@unittest.skipIf(QuantumCircuit is None, f"optional Qiskit is unavailable: {QISKIT_IMPORT_ERROR}")
class GroverSearchTests(unittest.TestCase):
    def test_small_oracles_match_independent_exhaustive_predicates(self):
        original = audit_phase_oracle(build_austin_phase_oracle(), 3, austin_predicate)
        second = audit_phase_oracle(build_mod4_range_oracle(), 4, mod4_range_predicate)
        self.assertEqual(original, {"states_checked": 8, "marked_count": 2, "marked_states": ["011", "101"]})
        self.assertEqual(second, {"states_checked": 16, "marked_count": 2, "marked_states": ["1001", "1101"]})

    def test_dense_audit_reuses_one_operator_with_global_phase_and_clean_work(self):
        oracle = build_mod4_range_oracle()
        oracle.global_phase += 0.37
        trace = []

        def verifier(bits):
            trace.append(bits)
            return mod4_range_predicate(bits)

        with (
            patch("qiskit.quantum_info.Operator", wraps=Operator) as build,
            patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis,
        ):
            dense = audit_phase_oracle(oracle, 4, verifier)
        build.assert_called_once()
        basis.assert_not_called()
        with patch("austins_algorithm.grover_search.MAX_DENSE_AUDIT_WIDTH", 0):
            reference = audit_phase_oracle(oracle, 4, mod4_range_predicate)
        self.assertEqual(dense, reference)
        self.assertEqual(trace, [format(index, "04b") for index in range(16)])

    def test_wide_sparse_and_custom_oracles_keep_basis_audit(self):
        from qiskit.circuit import Gate

        wide = QuantumCircuit(8)
        sparse = QuantumCircuit(7)
        reset = QuantumCircuit(1)
        reset.reset(0)
        custom_gate = Gate("custom_definition", 1, [])
        custom_gate.definition = reset
        custom = QuantumCircuit(4)
        custom.append(custom_gate, [3])
        for oracle, search_qubits in ((wide, 8), (sparse, 3), (custom, 3)):
            with (
                self.subTest(width=oracle.num_qubits, search_qubits=search_qubits),
                patch("qiskit.quantum_info.Operator", wraps=Operator) as build,
            ):
                result = audit_phase_oracle(oracle, search_qubits, lambda _: False)
            build.assert_not_called()
            self.assertEqual(result, {
                "states_checked": 1 << search_qubits, "marked_count": 0, "marked_states": [],
            })

    def test_operator_construction_rejection_uses_the_basis_reference(self):
        from qiskit.circuit.library import MCXGate

        gate = MCXGate(3)
        definition = QuantumCircuit(4)
        definition.reset(3)
        gate.definition = definition
        oracle = QuantumCircuit(4)
        oracle.append(gate, range(4))
        with patch("qiskit.quantum_info.Operator", wraps=Operator) as build:
            result = audit_phase_oracle(oracle, 3, lambda _: False)
        build.assert_called_once()
        self.assertEqual(result, {"states_checked": 8, "marked_count": 0, "marked_states": []})

    def test_dense_and_basis_paths_use_same_absolute_leakage_and_phase_tolerances(self):
        from math import isnan

        for width_limit in (0, 7):
            for epsilon, accepted in ((0.99e-9, True), (1.01e-9, False), (float("nan"), False)):
                leak = QuantumCircuit(4)
                leak.rx(2 * epsilon, 3)
                phase = QuantumCircuit(4)
                phase.p(epsilon, 0)
                for oracle, error in ((leak, "work bits dirty"), (phase, "phase disagrees")):
                    if isnan(epsilon):
                        error = "work bits dirty"
                    with (
                        self.subTest(width_limit=width_limit, epsilon=epsilon, gate=oracle.data[0].operation.name),
                        patch("austins_algorithm.grover_search.MAX_DENSE_AUDIT_WIDTH", width_limit),
                    ):
                        if accepted:
                            self.assertEqual(audit_phase_oracle(oracle, 3, lambda _: False)["states_checked"], 8)
                        else:
                            with self.assertRaisesRegex(ValueError, error):
                                audit_phase_oracle(oracle, 3, lambda _: False)

    def test_rounding_sensitive_operator_output_is_checked_by_the_basis_reference(self):
        from random import Random

        random = Random(0)
        body = QuantumCircuit(3)
        for _ in range(40):
            first, second = random.sample(range(3), 2)
            gate = random.randrange(4)
            if gate == 0:
                body.h(first)
            elif gate == 1:
                body.cx(first, second)
            elif gate == 2:
                body.rx(random.random() * 6, first)
            else:
                body.rz(random.random() * 6, first)
        oracle = body.compose(body.inverse())
        oracle.rx(1.99999954e-9, 0)

        def verdict():
            try:
                return audit_phase_oracle(oracle, 3, lambda _: False)
            except ValueError as exc:
                return str(exc)

        with patch("austins_algorithm.grover_search.MAX_DENSE_AUDIT_WIDTH", 0):
            reference = verdict()
        with patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis:
            self.assertEqual(verdict(), reference)
        self.assertGreater(basis.call_count, 0)

    def test_rectangular_blocks_preserve_sparse_operand_order_and_global_phase(self):
        import numpy as np
        from qiskit.circuit.library import CCXGate, CXGate, CZGate, MCXGate

        from austins_algorithm.grover_search import _rectangular_oracle_blocks

        oracle = QuantumCircuit(10, global_phase=0.41)
        oracle.h(4)
        oracle.append(CXGate(ctrl_state=0), [4, 8])
        oracle.append(CCXGate(ctrl_state=1), [8, 4, 6])
        oracle.append(MCXGate(3, ctrl_state=2), [6, 8, 2, 9])
        oracle.append(CZGate(ctrl_state=0), [8, 1])
        oracle.x(9)
        buffer_addresses = set()
        with patch("austins_algorithm.grover_search.MAX_RECTANGULAR_AUDIT_ENTRIES", 1024 * 16):
            for block, (columns, scratch) in enumerate(_rectangular_oracle_blocks(oracle, 5)):
                self.assertEqual(columns.shape, (1024, 16))
                buffer_addresses.update((columns.ctypes.data, scratch.ctypes.data))
                for local in range(16):
                    index = 16 * block + local
                    reference = Statevector.from_int(index, dims=(2,) * 10).evolve(oracle).data
                    np.testing.assert_allclose(columns[:, local], reference, atol=1e-12, rtol=0)
        self.assertEqual(block, 1)
        self.assertEqual(len(buffer_addresses), 2)

    def test_contiguous_dense_spans_use_views_without_allocating_packing_arrays(self):
        import numpy as np
        from qiskit.circuit.library import MCXGate

        from austins_algorithm import grover_search

        packing_shape = (2, grover_search.MAX_RECTANGULAR_AUDIT_TILE_BYTES // 32)
        for wires, uses_views in (
            ((0, 1, 2, 3), True), ((2, 3, 4, 5), True), ((6, 7, 8, 9), True), ((5, 4, 3, 2), False),
        ):
            with self.subTest(wires=wires):
                oracle = QuantumCircuit(10, global_phase=0.41)
                oracle.h(3)
                oracle.append(MCXGate(3, ctrl_state=2), wires)
                reference = [
                    Statevector.from_int(index, dims=(2,) * 10).evolve(oracle).data for index in range(32)
                ]
                with (
                    patch("numpy.empty", wraps=np.empty) as allocate,
                    patch.object(
                        grover_search, "_contract_dense_tiles", wraps=grover_search._contract_dense_tiles,
                    ) as packed,
                ):
                    checked = 0
                    for columns, _ in grover_search._rectangular_oracle_blocks(oracle, 5):
                        for offset in range(columns.shape[1]):
                            np.testing.assert_allclose(columns[:, offset], reference[checked], atol=1e-13, rtol=0)
                            checked += 1
                    self.assertEqual(checked, 32)
                self.assertEqual(packed.call_count, int(not uses_views))
                self.assertEqual(
                    sum(call.args[:1] == (packing_shape,) for call in allocate.call_args_list),
                    int(not uses_views),
                )

    def test_dense_tiles_cover_the_last_partial_group_and_respect_the_byte_limit(self):
        import numpy as np
        from qiskit.circuit.library import MCXGate

        from austins_algorithm import grover_search

        oracle = QuantumCircuit(10, global_phase=0.41)
        oracle.h(4)
        oracle.append(MCXGate(3, ctrl_state=2), [6, 8, 2, 9])
        reference = [
            Statevector.from_int(index, dims=(2,) * 10).evolve(oracle).data for index in range(32)
        ]
        one_group = 2 * 16 * 32 * 16
        for limit, expected_tiled in ((0, 0), (one_group - 1, 0), (3 * one_group, 1)):
            with (
                self.subTest(limit=limit),
                patch.object(grover_search, "MAX_RECTANGULAR_AUDIT_TILE_BYTES", limit),
                patch.object(
                    grover_search, "_contract_dense_tiles", wraps=grover_search._contract_dense_tiles,
                ) as tiled,
            ):
                checked = 0
                for columns, _ in grover_search._rectangular_oracle_blocks(oracle, 5):
                    for offset in range(columns.shape[1]):
                        np.testing.assert_allclose(columns[:, offset], reference[checked], atol=1e-13, rtol=0)
                        checked += 1
                self.assertEqual(checked, 32)
                self.assertEqual(tiled.call_count, expected_tiled)
                if expected_tiled:
                    self.assertEqual(tiled.call_args.args[-1].nbytes, limit)

    def test_dense_matrix_retention_respects_the_shared_byte_budget(self):
        import numpy as np

        from austins_algorithm.grover_search import _rectangular_oracle_blocks

        oracle = QuantumCircuit(10)
        oracle.h(4)
        oracle.h(7)
        reference = [
            Statevector.from_int(index, dims=(2,) * 10).evolve(oracle).data for index in range(32)
        ]
        for limit, expected_builds in ((0, 4), (64, 3), (128, 2)):
            with (
                self.subTest(limit=limit),
                patch("austins_algorithm.grover_search.MAX_RECTANGULAR_AUDIT_ENTRIES", 1024 * 16),
                patch("austins_algorithm.grover_search.MAX_RECTANGULAR_AUDIT_MATRIX_BYTES", limit),
                patch("qiskit.quantum_info.Operator", wraps=Operator) as build,
            ):
                checked = 0
                for columns, _ in _rectangular_oracle_blocks(oracle, 5):
                    for local in range(columns.shape[1]):
                        np.testing.assert_allclose(columns[:, local], reference[checked], atol=1e-12, rtol=0)
                        checked += 1
                self.assertEqual(checked, 32)
                self.assertEqual(build.call_count, expected_builds)

    def test_matrix_override_uses_basis_without_freezing_the_first_conversion(self):
        import numpy as np
        from qiskit.circuit.library import MCXGate

        from austins_algorithm.grover_search import _rectangular_oracle_blocks

        swap = np.eye(16, dtype=complex)
        swap[7, 7] = swap[15, 15] = 0
        swap[7, 15] = swap[15, 7] = 1
        changed = np.eye(16, dtype=complex)
        changed[1, 1] = 1j

        for width in (4, 9):
            with self.subTest(width=width):
                gate = MCXGate(3)
                calls = []

                def matrix():
                    calls.append(None)
                    return swap if len(calls) <= 2 else changed

                gate.to_matrix = matrix
                oracle = QuantumCircuit(width)
                for _ in range(2):
                    oracle.append(gate, [0, 1, 2, 3], copy=False)
                self.assertEqual(list(_rectangular_oracle_blocks(oracle, 4)), [])
                self.assertEqual(calls, [])
                with self.assertRaisesRegex(ValueError, "phase disagrees with the verifier for 0001"):
                    audit_phase_oracle(oracle, 4, lambda _: False)
                self.assertEqual(len(calls), 4)

    def test_small_nonzero_mcx_coefficients_keep_near_tolerance_basis_verdicts(self):
        from qiskit.circuit.library import MCXGate, PhaseGate

        repetitions = 64
        for deviation, accepted in ((0.99e-9, True), (1.01e-9, False)):
            gate = MCXGate(3)
            definition = gate.definition.copy()
            index = next(index for index, item in enumerate(definition.data) if item.operation.name == "p")
            item = definition.data[index]
            phase = item.operation.params[0] + deviation / repetitions
            definition.data[index] = item.replace(operation=PhaseGate(phase))
            gate.definition = definition
            oracle = QuantumCircuit(9)
            for _ in range(repetitions):
                oracle.append(gate, [0, 1, 2, 3])

            with (
                self.subTest(deviation=deviation),
                patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis,
            ):
                if accepted:
                    self.assertEqual(audit_phase_oracle(oracle, 4, lambda _: False)["states_checked"], 16)
                else:
                    with self.assertRaisesRegex(ValueError, "phase disagrees with the verifier for 0001"):
                        audit_phase_oracle(oracle, 4, lambda _: False)
                self.assertEqual(basis.call_count, 16 if accepted else 2)

    def test_rectangular_limits_reject_before_allocating_column_or_gate_arrays(self):
        from qiskit.circuit.library import MCXGate

        from austins_algorithm.grover_search import _rectangular_oracle_blocks

        too_wide = QuantumCircuit(9)
        too_wide.mcx(list(range(7)), 7)
        too_much_work = QuantumCircuit(12)
        for _ in range(256):
            too_much_work.h(0)
        definition = QuantumCircuit(4)
        definition.reset(3)
        gate = MCXGate(3)
        gate.definition = definition
        custom = QuantumCircuit(9)
        custom.append(gate, [0, 1, 2, 8])
        for oracle, search_qubits in ((QuantumCircuit(12), 8), (too_wide, 4), (too_much_work, 6), (custom, 4)):
            with (
                self.subTest(width=oracle.num_qubits, search=search_qubits, operations=len(oracle.data)),
                patch("austins_algorithm.grover_search.MAX_RECTANGULAR_AUDIT_BLOCKS", 3),
                patch("numpy.empty") as allocate,
                patch("qiskit.quantum_info.Operator") as build,
            ):
                self.assertEqual(list(_rectangular_oracle_blocks(oracle, search_qubits)), [])
            allocate.assert_not_called()
            build.assert_not_called()

    def test_rectangular_audit_uses_reference_when_work_is_dirty(self):
        oracle = QuantumCircuit(9)
        oracle.cx(0, 8)
        visited = []

        def verifier(bits):
            visited.append(bits)
            return False

        with (
            patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis,
            self.assertRaisesRegex(ValueError, "work bits dirty for 0001"),
        ):
            audit_phase_oracle(oracle, 4, verifier)
        self.assertEqual(visited, ["0000", "0001"])
        self.assertEqual(basis.call_count, 2)

    def test_blocked_audit_preserves_late_work_and_phase_failures(self):
        for gate, message, expected_basis_calls in (
            ("cx", "work bits dirty", 129),
            ("z", "phase disagrees with the verifier", 0),
        ):
            oracle = QuantumCircuit(12, global_phase=0.413)
            if gate == "cx":
                oracle.cx(7, 11)
            else:
                oracle.z(7)
            visited = []

            def verifier(bits):
                visited.append(bits)
                return False

            with (
                self.subTest(gate=gate),
                patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis,
                self.assertRaisesRegex(ValueError, f"{message} for 10000000"),
            ):
                audit_phase_oracle(oracle, 8, verifier)
            self.assertEqual(visited, [format(index, "08b") for index in range(129)])
            self.assertEqual(basis.call_count, expected_basis_calls)

    def test_uncertain_later_block_restarts_with_the_basis_reference(self):
        import numpy as np

        for epsilon, accepted in ((0.99e-9, True), (1.0000000000000003e-9, False)):
            oracle = QuantumCircuit(9)
            oracle.p(epsilon, 3)
            first = np.zeros((512, 8), dtype=np.complex128)
            second = np.zeros_like(first)
            offsets = np.arange(8)
            first[offsets, offsets] = 1 + 1e-16j
            second[8 + offsets, offsets] = np.exp(1j * epsilon)
            blocks = ((first, np.empty_like(first)), (second, np.empty_like(second)))
            visited = []

            def verifier(bits):
                visited.append(bits)
                return False

            with (
                self.subTest(epsilon=epsilon),
                patch("austins_algorithm.grover_search._rectangular_oracle_blocks", return_value=iter(blocks)),
                patch.object(Statevector, "from_int", wraps=Statevector.from_int) as basis,
            ):
                if accepted:
                    self.assertEqual(audit_phase_oracle(oracle, 4, verifier)["states_checked"], 16)
                else:
                    with self.assertRaisesRegex(ValueError, "phase disagrees with the verifier for 1000"):
                        audit_phase_oracle(oracle, 4, verifier)
            checked = 16 if accepted else 9
            self.assertEqual(basis.call_count, checked)
            self.assertEqual(visited, [format(index, "04b") for index in range(checked)])

    def test_invalid_verifier_result_precedes_operator_construction(self):
        with (
            patch("qiskit.quantum_info.Operator", wraps=Operator) as build,
            self.assertRaisesRegex(TypeError, "verifier must return a bool"),
        ):
            audit_phase_oracle(build_austin_phase_oracle(), 3, lambda _: 1)
        build.assert_not_called()

    def test_second_circuit_measures_only_search_register(self):
        circuit = build_grover_search_circuit(build_mod4_range_oracle(), 4, iterations=2)
        self.assertEqual(circuit.num_qubits, 5)
        self.assertEqual(circuit.num_clbits, 4)
        state = Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
        self.assertAlmostEqual(state.probabilities(qargs=[4])[0], 1.0)
        probabilities = state.probabilities_dict(qargs=[0, 1, 2, 3])
        self.assertAlmostEqual(probabilities["1001"] + probabilities["1101"], 0.9453125)

    def test_direct_mod4_oracle_preserves_phase_and_measurement_distribution(self):
        direct = build_mod4_range_direct_oracle()
        reference = build_mod4_range_oracle()
        self.assertEqual(direct.num_qubits, 4)
        self.assertEqual(
            audit_phase_oracle(direct, 4, mod4_range_predicate),
            audit_phase_oracle(reference, 4, mod4_range_predicate),
        )
        data_qubits = [0, 1, 2, 3]
        distributions = []
        for oracle in (direct, reference):
            circuit = build_grover_search_circuit(oracle, 4, iterations=2)
            state = Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
            distributions.append(state.probabilities(qargs=data_qubits))
        for direct_probability, reference_probability in zip(*distributions):
            self.assertAlmostEqual(direct_probability, reference_probability)

    def test_standard_diffuser_remains_the_default(self):
        oracle = build_mod4_range_oracle()
        implicit = build_grover_search_circuit(oracle, 4, iterations=1)
        explicit = build_grover_search_circuit(oracle, 4, iterations=1, diffuser="standard")
        self.assertEqual(implicit, explicit)

    @unittest.skipUnless(HAS_CLEAN_ANCILLA_SYNTHESIS, "clean-ancilla synthesis requires Qiskit 2.1 or later")
    def test_clean_ancilla_diffuser_preserves_each_clean_input_up_to_one_global_phase(self):
        import numpy as np

        from austins_algorithm.constraint_oracle import synthesize_constraint_oracle
        from austins_algorithm.constraints import load_constraint

        fixture = Path(__file__).resolve().parents[1] / "examples" / "field_sensor_kit.json"
        constraint = load_constraint(fixture)
        oracle, _ = synthesize_constraint_oracle(constraint, synthesis="factorized")
        search_qubits = constraint.qubits
        clean_dimension = 1 << search_qubits

        for rounds in (1, 2):
            with self.subTest(rounds=rounds):
                reference = build_grover_search_circuit(oracle, search_qubits, iterations=rounds)
                optimized = build_grover_search_circuit(
                    oracle, search_qubits, iterations=rounds, diffuser="clean-ancilla",
                )
                self.assertNotEqual(optimized, reference)
                self.assertEqual(optimized.num_qubits, oracle.num_qubits)
                self.assertEqual(optimized.num_clbits, search_qubits)
                expected = Operator(reference.remove_final_measurements(inplace=False)).data[:, :clean_dimension]
                actual = Operator(optimized.remove_final_measurements(inplace=False)).data[:, :clean_dimension]

                # One common phase is required across every clean-workspace input.
                phase = np.vdot(expected, actual) / clean_dimension
                self.assertAlmostEqual(abs(phase), 1.0)
                np.testing.assert_allclose(actual, phase * expected, atol=1e-10, rtol=0)
                np.testing.assert_allclose(actual[clean_dimension:], 0.0, atol=1e-10, rtol=0)

    @unittest.skipUnless(HAS_CLEAN_ANCILLA_SYNTHESIS, "clean-ancilla synthesis requires Qiskit 2.1 or later")
    def test_clean_ancilla_diffuser_keeps_seeded_search_and_verifier_behavior(self):
        oracle = build_mod4_range_oracle()
        reference = run_grover_search(oracle, 4, mod4_range_predicate, marked_count=2, seed=7)
        optimized = run_grover_search(
            oracle, 4, mod4_range_predicate, marked_count=2, seed=7, diffuser="clean-ancilla",
        )
        self.assertEqual(optimized["status"], "FOUND")
        self.assertEqual(optimized["attempts"], reference["attempts"])
        self.assertEqual(optimized["oracle_queries"], reference["oracle_queries"])
        self.assertTrue(mod4_range_predicate(optimized["candidate"]))

    def test_clean_ancilla_diffuser_rejects_missing_work_wire_or_too_few_controls(self):
        for oracle, search_qubits in ((build_mod4_range_direct_oracle(), 4), (QuantumCircuit(4), 3)):
            with self.subTest(search_qubits=search_qubits, width=oracle.num_qubits):
                with self.assertRaises(ValueError):
                    build_grover_search_circuit(oracle, search_qubits, iterations=1, diffuser="clean-ancilla")
                with self.assertRaises(ValueError):
                    run_grover_search(oracle, search_qubits, lambda _: True, diffuser="clean-ancilla")

    def test_standard_diffuser_survives_missing_optional_clean_synthesis(self):
        oracle = build_mod4_range_oracle()
        name = "synth_mcx_1_clean_kg24"
        with patch.dict(synthesis.__dict__):
            synthesis.__dict__.pop(name, None)
            circuit = build_grover_search_circuit(oracle, 4, iterations=1)
            self.assertEqual(circuit.num_qubits, 5)
            with self.assertRaises(ValueError):
                build_grover_search_circuit(oracle, 4, iterations=1, diffuser="clean-ancilla")
            with self.assertRaises(ValueError):
                run_grover_search(oracle, 4, mod4_range_predicate, diffuser="clean-ancilla")

    def test_structural_and_semantic_oracle_errors_are_separate(self):
        measured = QuantumCircuit(2, 1)
        measured.measure(0, 0)
        with self.assertRaisesRegex(ValueError, "classical bits"):
            build_grover_search_circuit(measured, 1, iterations=1)
        reset = QuantumCircuit(2)
        reset.reset(1)
        with self.assertRaisesRegex(ValueError, "reversible"):
            build_grover_search_circuit(reset, 1, iterations=1)
        unbound = QuantumCircuit(1)
        unbound.rz(Parameter("angle"), 0)
        with self.assertRaisesRegex(ValueError, "unbound parameters"):
            build_grover_search_circuit(unbound, 1, iterations=1)
        dirty_work = QuantumCircuit(2)
        dirty_work.cx(0, 1)
        with self.assertRaisesRegex(ValueError, "work bits dirty"):
            audit_phase_oracle(dirty_work, 1, lambda bits: bits == "1")
        with self.assertRaisesRegex(ValueError, "phase disagrees"):
            audit_phase_oracle(QuantumCircuit(1), 1, lambda bits: bits == "1")

    def test_known_count_uses_scheduled_rounds_and_reports_oracle_queries(self):
        report = run_grover_search(
            build_mod4_range_oracle(), 4, mod4_range_predicate, marked_count=2, seed=7,
        )
        self.assertEqual(report["strategy"], "KNOWN_MARKED_COUNT")
        self.assertEqual(report["status"], "FOUND")
        self.assertIn(report["candidate"], {"1001", "1101"})
        self.assertEqual(report["basis_order"], "q3 q2 q1 q0")
        self.assertEqual(report["scheduled_rounds"], 2)
        self.assertEqual(report["oracle_queries"], 2 * report["shots_used"])
        self.assertEqual(report["shots_used"], sum(report["counts"].values()))
        self.assertEqual(report["verifier_calls"], report["shots_used"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertFalse(report["provider_job_submitted"])

    def test_unknown_count_is_seeded_and_checks_only_measured_candidates(self):
        verified_calls = []

        def verify(bits):
            verified_calls.append(bits)
            return mod4_range_predicate(bits)

        oracle = build_mod4_range_oracle()
        report = run_grover_search(oracle, 4, verify, seed=7)
        repeat = run_grover_search(oracle, 4, mod4_range_predicate, seed=7)
        self.assertEqual(report, repeat)
        self.assertEqual(report["status"], "FOUND")
        self.assertEqual(report["strategy"], "BBHT_BOUNDED")
        self.assertIsNone(report["marked_count"])
        self.assertIsNone(report["scheduled_rounds"])
        self.assertIn(report["candidate"], {"1001", "1101"})
        self.assertEqual(verified_calls, [attempt["sample"] for attempt in report["attempts"]])
        self.assertEqual(report["oracle_queries"], sum(attempt["rounds"] for attempt in report["attempts"]))

    def test_unknown_count_finds_single_marker(self):
        oracle = QuantumCircuit(3)
        oracle.ccz(0, 1, 2)
        report = run_grover_search(oracle, 3, lambda bits: bits == "111", seed=7)
        self.assertEqual(report["status"], "FOUND")
        self.assertEqual(report["candidate"], "111")

    def test_repeated_known_rounds_reuse_the_measured_circuit_with_fresh_seeds(self):
        from random import Random

        from qiskit.primitives import StatevectorSampler

        from austins_algorithm import grover_search

        original_run = StatevectorSampler.run
        sampled = []
        seeds = []

        def make_sampler(*, seed):
            seeds.append(seed)
            return StatevectorSampler(seed=seed)

        def observe(sampler, pubs, *, shots=None):
            circuit = pubs[0]
            sampled.append((circuit, circuit.copy()))
            return original_run(sampler, pubs, shots=shots)

        with (
            patch("qiskit.primitives.StatevectorSampler", side_effect=make_sampler),
            patch.object(StatevectorSampler, "run", new=observe),
            patch.object(grover_search, "_build_circuit", wraps=grover_search._build_circuit) as build,
        ):
            report = run_grover_search(
                build_austin_phase_oracle(), 3, lambda _: False, marked_count=2, max_shots=8, seed=7,
            )
        build.assert_called_once()
        random = Random(7 ^ 0xA5A5A5A5)
        self.assertEqual(seeds, [random.getrandbits(32) for _ in range(8)])
        self.assertEqual(report["shots_used"], 8)
        self.assertEqual(report["oracle_queries"], 8 * report["scheduled_rounds"])
        for circuit, before in sampled:
            self.assertIs(circuit, sampled[0][0])
            self.assertEqual(circuit, before)
            self.assertEqual(circuit.count_ops()["measure"], 3)

    def test_repeated_unknown_rounds_evict_the_least_recently_used_circuit(self):
        from qiskit.primitives import StatevectorSampler

        from austins_algorithm import grover_search

        original_run = StatevectorSampler.run
        sampled = []

        def observe(sampler, pubs, *, shots=None):
            sampled.append(pubs[0])
            return original_run(sampler, pubs, shots=shots)

        with (
            patch.object(StatevectorSampler, "run", new=observe),
            patch.object(grover_search, "MAX_CACHED_ROUND_CIRCUITS", 2),
            patch.object(grover_search, "_build_circuit", wraps=grover_search._build_circuit) as build,
        ):
            report = run_grover_search(build_austin_phase_oracle(), 3, lambda _: False, max_shots=8, seed=2)
        self.assertEqual([attempt["rounds"] for attempt in report["attempts"]], [0, 0, 0, 1, 0, 2, 2, 1])
        self.assertEqual([call.args[2] for call in build.call_args_list], [0, 1, 2, 1])
        self.assertIs(sampled[0], sampled[4])
        self.assertIs(sampled[5], sampled[6])
        self.assertIsNot(sampled[3], sampled[7])
        self.assertEqual(sampled[3], sampled[7])

    def test_prepared_state_matches_stock_amplitudes_probabilities_and_seeded_draws(self):
        import numpy as np

        from austins_algorithm import grover_search

        oracle = build_mod4_range_oracle()
        oracle.global_phase += 0.37
        original_sample = Statevector.sample_memory
        original_build = grover_search._build_circuit

        def run(method):
            seen = []
            built = []
            capture = grover_search._LastAttempt()

            def build(*args):
                circuit = original_build(*args)
                built.append(circuit)
                return circuit

            def sample(state, shots, qargs=None):
                result = original_sample(state, shots=shots, qargs=qargs)
                seen.append((state, state.data.copy(), state.probabilities(qargs), result.copy()))
                return result

            with (
                patch.object(Statevector, "sample_memory", new=sample),
                patch.object(grover_search, "_build_circuit", new=build),
            ):
                report = run_grover_search(
                    oracle, 4, lambda _: False, marked_count=2, max_shots=8, seed=19,
                    ideal_sampler=method, _last_attempt=capture,
                )
            self.assertEqual(len(built), 1)
            self.assertIs(capture.circuit, built[0])
            self.assertEqual(capture.circuit.count_ops()["measure"], 4)
            return report, seen

        baseline, stock = run("statevector_sampler")
        prepared, reused = run("prepared_statevector")
        self.assertEqual(prepared.pop("ideal_sampler"), {
            "requested": "prepared_statevector", "used": "prepared_statevector",
        })
        self.assertEqual(prepared.pop("simulator"), "Qiskit Statevector (prepared ideal)")
        baseline.pop("simulator")
        self.assertEqual(prepared, baseline)
        for expected, actual in zip(stock, reused, strict=True):
            self.assertIs(actual[0], reused[0][0])
            for expected_values, actual_values in zip(expected[1:], actual[1:], strict=True):
                np.testing.assert_array_equal(expected_values, actual_values)

    def test_probability_cache_keeps_raw_weights_seed_order_and_msb_first_results(self):
        from random import Random
        from types import SimpleNamespace

        import numpy as np

        cases = (
            (build_mod4_range_oracle(), 4, {"marked_count": 2}),
            (QuantumCircuit(12), 12, {"marked_count": 1, "max_rounds_per_shot": 0}),
        )
        for oracle, width, options in cases:
            with self.subTest(width=width):
                stock_weights = []
                original_sample = Statevector.sample_memory

                def observe_stock(state, shots, qargs=None):
                    stock_weights.append(state.probabilities(qargs=qargs))
                    return original_sample(state, shots=shots, qargs=qargs)

                arguments = {"max_shots": 4, "seed": 47, **options}
                with patch.object(Statevector, "sample_memory", new=observe_stock):
                    stock = run_grover_search(oracle, width, lambda _: False, **arguments)
                original_rng = np.random.default_rng
                seeds = []
                weights = []

                def observe_rng(seed=None):
                    generator = original_rng(seed)
                    if seed is None:
                        return generator
                    seeds.append(seed)

                    def choose(population, *, size, p):
                        self.assertEqual(population, 1 << width)
                        self.assertEqual(size, 1)
                        weights.append(p)
                        return generator.choice(population, size=size, p=p)

                    return SimpleNamespace(choice=choose)

                with patch("numpy.random.default_rng", new=observe_rng):
                    prepared = run_grover_search(
                        oracle, width, lambda _: False, **arguments, ideal_sampler="prepared_probabilities",
                    )
                self.assertEqual(prepared.pop("ideal_sampler"), {
                    "requested": "prepared_probabilities", "used": "prepared_probabilities",
                })
                self.assertEqual(prepared.pop("simulator"), "Qiskit Statevector + NumPy (prepared probabilities)")
                stock.pop("simulator")
                self.assertEqual(prepared, stock)
                random = Random(47 ^ 0xA5A5A5A5)
                self.assertEqual(seeds, [random.getrandbits(32) for _ in range(4)])
                for actual, expected in zip(weights, stock_weights, strict=True):
                    self.assertIs(actual, weights[0])
                    self.assertEqual(actual.dtype, expected.dtype)
                    np.testing.assert_array_equal(actual, expected)

    def test_prepared_probability_validation_preserves_precision_and_rejects_invalid_arrays(self):
        import numpy as np

        from austins_algorithm.grover_search import _prepare_ideal_measurement

        oracle = build_austin_phase_oracle()
        circuit = build_grover_search_circuit(oracle, 3, iterations=1)
        near_normalized = np.array([0.25, 0.7501, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        with patch.object(Statevector, "probabilities", return_value=near_normalized):
            self.assertIs(_prepare_ideal_measurement(circuit, [0, 1, 2], use_probabilities=True), near_normalized)
            report = run_grover_search(
                oracle, 3, lambda _: False, marked_count=2, max_shots=1, ideal_sampler="prepared_probabilities",
            )
        self.assertIn(report["attempts"][0]["sample"], {"000", "001"})
        for values, error, message in (
            (np.array([0.5, 0.5]), RuntimeError, "shape or dtype"),
            (np.array([np.nan, 1, 0, 0, 0, 0, 0, 0]), ValueError, "finite and nonnegative"),
            (np.array([-0.1, 1.1, 0, 0, 0, 0, 0, 0]), ValueError, "finite and nonnegative"),
            (np.full(8, 0.25), ValueError, "(?i)probabilities do not sum"),
        ):
            with (
                self.subTest(message=message, values=values),
                patch.object(Statevector, "probabilities", return_value=values),
                self.assertRaisesRegex(error, message),
            ):
                run_grover_search(
                    oracle, 3, lambda _: False, marked_count=2, max_shots=1, ideal_sampler="prepared_probabilities",
                )

    def test_prepared_sampling_falls_back_for_a_replaced_mcx_definition(self):
        from qiskit.circuit.library import MCXGate
        from qiskit.primitives import StatevectorSampler

        gate = MCXGate(3)
        definition = QuantumCircuit(4)
        definition.reset(3)
        gate.definition = definition
        oracle = QuantumCircuit(4)
        oracle.append(gate, range(4))

        for method in ("prepared_statevector", "prepared_probabilities"):
            with (
                self.subTest(method=method),
                patch("qiskit.primitives.StatevectorSampler", wraps=StatevectorSampler) as sampler,
            ):
                report = run_grover_search(
                    oracle, 3, lambda _: False, marked_count=1, max_shots=2, ideal_sampler=method,
                )
            self.assertEqual(sampler.call_count, 2)
            self.assertEqual(report["ideal_sampler"], {"requested": method, "used": "statevector_sampler"})

    def test_prepared_sampling_skips_preparation_before_any_affordable_attempt(self):
        from austins_algorithm import grover_search

        with patch.object(grover_search, "_supports_prepared_sampling") as eligible:
            report = run_grover_search(
                build_mod4_range_oracle(), 4, mod4_range_predicate, marked_count=2,
                max_oracle_queries=0, ideal_sampler="prepared_statevector",
            )
        eligible.assert_not_called()
        self.assertEqual(report["shots_used"], 0)
        self.assertEqual(report["ideal_sampler"], {"requested": "prepared_statevector", "used": None})

    def test_exhaustion_distinguishes_shot_and_query_budgets(self):
        oracle = QuantumCircuit(4)
        empty_audit = audit_phase_oracle(oracle, 4, lambda _: False)
        self.assertEqual(empty_audit["marked_states"], [])
        shot_limit = run_grover_search(
            oracle, 4, lambda _: False, max_shots=3, max_rounds_per_shot=0, seed=7,
        )
        self.assertEqual(shot_limit["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(shot_limit["exhaustion_reason"], "SHOT_BUDGET")
        self.assertIsNone(shot_limit["candidate"])
        self.assertEqual(shot_limit["shots_used"], 3)
        self.assertEqual(shot_limit["oracle_queries"], 0)
        query_limit = run_grover_search(
            oracle, 4, lambda _: False, max_shots=20, max_oracle_queries=0, seed=7,
        )
        self.assertEqual(query_limit["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(query_limit["exhaustion_reason"], "ORACLE_QUERY_BUDGET")
        self.assertIsNone(query_limit["candidate"])
        self.assertEqual(query_limit["oracle_queries"], 0)

    def test_search_checks_the_verifier_even_when_oracle_marks_other_states(self):
        report = run_grover_search(
            build_austin_phase_oracle(), 3, lambda bits: bits == "111", marked_count=2,
            max_shots=4, seed=7,
        )
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertIsNone(report["candidate"])
        self.assertTrue(set(report["counts"]) <= {"011", "101"})
        self.assertEqual(report["shots_used"], 4)
        self.assertTrue(all(not attempt["verified"] for attempt in report["attempts"]))

    def test_caps_and_verifier_contract_fail_before_unbounded_work(self):
        oracle = build_austin_phase_oracle()
        with self.assertRaisesRegex(ValueError, "max_shots"):
            run_grover_search(oracle, 3, austin_predicate, max_shots=257)
        with self.assertRaisesRegex(ValueError, "max_oracle_queries"):
            run_grover_search(oracle, 3, austin_predicate, max_oracle_queries=-1)
        with self.assertRaisesRegex(ValueError, "max_rounds_per_shot"):
            run_grover_search(oracle, 3, austin_predicate, max_rounds_per_shot=257)
        with self.assertRaisesRegex(TypeError, "return a bool"):
            run_grover_search(oracle, 3, lambda _: 1, max_shots=1)


if __name__ == "__main__":
    unittest.main()
