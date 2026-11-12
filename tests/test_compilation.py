import hashlib
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from qiskit import QuantumCircuit, qasm3
    from qiskit.providers.fake_provider import GenericBackendV2
    from qiskit.transpiler import CouplingMap
except ImportError:
    QISKIT_AVAILABLE = False
else:
    QISKIT_AVAILABLE = True

from austins_algorithm.compilation import compile_and_verify, line_target  # noqa: E402
from austins_algorithm.qiskit_adapter import build_austin_circuit  # noqa: E402


@unittest.skipUnless(QISKIT_AVAILABLE, "optional Qiskit tools are unavailable")
class RoundtripDistributionTests(unittest.TestCase):
    def test_roundtrip_checks_width_and_work_before_statevector(self):
        from austins_algorithm.compilation import _roundtrip_distribution

        too_many_gates = QuantumCircuit(1, 1)
        for _ in range(6):
            too_many_gates.x(0)
        too_many_gates.measure(0, 0)
        for circuit, error, message in (
            (object(), RuntimeError, "did not return"),
            (QuantumCircuit(17, 1), RuntimeError, "changed the declared wires"),
            (QuantumCircuit(1, 2), RuntimeError, "changed the declared wires"),
            (too_many_gates, ValueError, "statevector verification work limit"),
        ):
            with (
                self.subTest(circuit=circuit, message=message),
                patch("austins_algorithm.compilation._distribution") as distribution,
                patch("austins_algorithm.compilation.MAX_EXACT_WORK", 8),
                self.assertRaisesRegex(error, message),
            ):
                _roundtrip_distribution(circuit, qubits=1, clbits=1, measured=[0])
            distribution.assert_not_called()

    def test_roundtrip_returns_distribution_in_verified_terminal_order(self):
        from austins_algorithm.compilation import _roundtrip_distribution

        circuit = QuantumCircuit(2, 2)
        circuit.x(0)
        circuit.measure(0, 1)
        circuit.measure(1, 0)
        probabilities = _roundtrip_distribution(circuit, qubits=2, clbits=2, measured=(1, 0))
        self.assertEqual(probabilities, {"10": 1.0})
        with self.assertRaisesRegex(RuntimeError, "changed the declared wires or measurement mapping"):
            _roundtrip_distribution(circuit, qubits=2, clbits=2, measured=[0, 1])


@unittest.skipUnless(
    QISKIT_AVAILABLE
    and (hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import") is not None),
    "optional Qiskit OpenQASM tools are unavailable",
)
class CompilationTests(unittest.TestCase):
    def test_compiler_times_transpile_and_openqasm_phases_separately(self):
        clock = [100.0, 102.0, 105.0, 110.0, 112.0, 120.0, 121.0, 124.0]
        with patch("austins_algorithm.compilation.perf_counter", side_effect=clock):
            program = compile_and_verify(build_austin_circuit(), target=line_target(3))
        self.assertEqual(program.timing_seconds, {
            "compile_export_verify": 24.0,
            "transpile": 3.0,
            "openqasm_export": 2.0,
            "openqasm_import": 8.0,
            "openqasm_verify": 1.0,
        })
        self.assertNotIn("timing_seconds", program.report)
        self.assertNotIn("timing_seconds", program.report["openqasm"])

    def test_line_target_export_preserves_austin_probabilities(self):
        program = compile_and_verify(build_austin_circuit(), target=line_target(3))
        report = program.report
        self.assertEqual(report["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
        self.assertEqual(report["target"]["kind"], "TARGET")
        self.assertEqual(report["compiled"]["depth"], program.circuit.depth())
        self.assertEqual(report["compiled"]["two_qubit_gates"], program.circuit.count_ops()["cx"])
        self.assertGreater(report["compiled"]["two_qubit_gates"], 0)
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertTrue(program.openqasm.startswith("OPENQASM 3.0;"))
        self.assertEqual(
            report["openqasm"]["sha256"],
            hashlib.sha256(program.openqasm.encode("utf-8")).hexdigest(),
        )
        self.assertLessEqual(
            report["verification"]["roundtrip_total_variation_distance"],
            report["verification"]["tolerance"],
        )
        probabilities = report["verification"]["roundtrip_probabilities"]
        self.assertAlmostEqual(probabilities["011"], 0.5)
        self.assertAlmostEqual(probabilities["101"], 0.5)

    def test_backend_preserves_classical_output_order_and_maps_physical_wires(self):
        backend = GenericBackendV2(5, coupling_map=CouplingMap.from_line(5), seed=7, noise_info=False)
        circuit = QuantumCircuit(2, 2)
        circuit.x(0)
        circuit.measure(0, 1)
        circuit.measure(1, 0)
        program = compile_and_verify(circuit, backend=backend)
        self.assertEqual(program.report["target"]["kind"], "BACKEND_V2")
        self.assertEqual(program.report["compiled"]["qubits"], 5)
        self.assertEqual(len(program.report["openqasm"]["export_wire_to_target_qubit"]), 2)
        measurements = {
            program.circuit.find_bit(item.clbits[0]).index: program.circuit.find_bit(item.qubits[0]).index
            for item in program.circuit.data
            if item.operation.name == "measure"
        }
        self.assertEqual(program.report["openqasm"]["classical_bit_to_target_qubit"], [measurements[0], measurements[1]])
        self.assertAlmostEqual(program.report["verification"]["roundtrip_probabilities"]["10"], 1.0)

    def test_fixed_initial_layout_is_recorded_for_backend_and_target(self):
        backend = GenericBackendV2(5, coupling_map=CouplingMap.from_line(5), seed=7, noise_info=False)
        layout = (4, 3, 2)
        for kwargs in ({"backend": backend}, {"target": backend.target}):
            with self.subTest(kind=next(iter(kwargs))):
                program = compile_and_verify(build_austin_circuit(), initial_layout=layout, **kwargs)
                actual = program.circuit.layout.initial_index_layout(filter_ancillas=True)
                self.assertEqual(actual, list(layout))
                self.assertEqual(program.report["initial_logical_to_target_qubit"], actual)
                self.assertEqual(program.report["compiled"]["qubits"], 5)

    def test_invalid_initial_layout_is_rejected(self):
        circuit = build_austin_circuit()
        target = line_target(5)
        for layout in ([0, 1], [0, 0, 1], [-1, 0, 1], [0, 1, 5], [False, 1, 2], ["0", 1, 2], "012", {0, 1, 2}):
            with self.subTest(layout=layout), self.assertRaisesRegex(ValueError, "initial_layout"):
                compile_and_verify(circuit, target=target, initial_layout=layout)

    def test_verification_rejects_nonterminal_measurement(self):
        circuit = QuantumCircuit(1, 1)
        circuit.measure(0, 0)
        circuit.x(0)
        with self.assertRaisesRegex(ValueError, "cannot handle instruction"):
            compile_and_verify(circuit, target=line_target(1))

    def test_verification_detects_changed_export_distribution(self):
        circuit = QuantumCircuit(1, 1)
        circuit.x(0)
        circuit.measure(0, 0)
        changed = QuantumCircuit(1, 1)
        changed.measure(0, 0)
        with patch("austins_algorithm.compilation._import_openqasm", return_value=(changed, "test")):
            with self.assertRaisesRegex(RuntimeError, "changed measurement probabilities"):
                compile_and_verify(circuit, target=line_target(1))


    @unittest.skipUnless(QISKIT_AVAILABLE and hasattr(qasm3, "loads_experimental"), "native parser unavailable")
    def test_native_preference_preserves_output_and_reports_actual_importer(self):
        circuit = build_austin_circuit()
        default = compile_and_verify(circuit, target=line_target(3))
        native = compile_and_verify(circuit, target=line_target(3), prefer_native_qasm=True)
        self.assertEqual(native.openqasm, default.openqasm)
        self.assertEqual(native.report["verification"], default.report["verification"])
        self.assertEqual(native.report["openqasm"]["importer"], "qiskit.qasm3.loads_experimental")
        self.assertEqual(native.report["openqasm"]["preferred_importer"], "qiskit.qasm3.loads_experimental")
        if find_spec("qiskit_qasm3_import") is not None:
            self.assertEqual(default.report["openqasm"]["importer"], "qiskit.qasm3.loads")

    @unittest.skipUnless(QISKIT_AVAILABLE and hasattr(qasm3, "loads_experimental")
                         and find_spec("qiskit_qasm3_import") is not None, "both importers are required")
    def test_native_parse_rejection_uses_standard_and_verifies_against_compiled_circuit(self):
        from math import pi, sin

        from qiskit.circuit import Measure
        from qiskit.transpiler import Target

        body = QuantumCircuit(1, name="twist")
        body.rx(pi / 7, 0)
        gate = body.to_gate()
        circuit = QuantumCircuit(1, 1)
        circuit.append(gate, [0])
        circuit.measure(0, 0)
        target = Target(num_qubits=1)
        target.add_instruction(gate, {(0,): None})
        target.add_instruction(Measure(), {(0,): None})
        result = compile_and_verify(circuit, target=target, prefer_native_qasm=True)
        self.assertIn("gate twist", result.openqasm)
        self.assertEqual(result.report["openqasm"]["importer"], "qiskit.qasm3.loads")
        self.assertEqual(result.report["openqasm"]["preferred_importer"], "qiskit.qasm3.loads_experimental")
        self.assertAlmostEqual(result.report["verification"]["roundtrip_probabilities"]["1"], sin(pi / 14) ** 2)

    @unittest.skipUnless(QISKIT_AVAILABLE and hasattr(qasm3, "loads_experimental"), "native parser unavailable")
    def test_native_verification_failure_is_not_retried_with_standard_importer(self):
        circuit = QuantumCircuit(1, 1)
        circuit.x(0)
        circuit.measure(0, 0)
        changed = QuantumCircuit(1, 1)
        changed.measure(0, 0)
        with (
            patch.object(qasm3, "loads_experimental", return_value=changed),
            patch.object(qasm3, "loads") as standard,
            self.assertRaisesRegex(RuntimeError, "changed measurement probabilities"),
        ):
            compile_and_verify(circuit, target=line_target(1), prefer_native_qasm=True)
        standard.assert_not_called()

    def test_stock_distribution_matches_qiskit_with_ordered_wires_and_disjoint_gates(self):
        from qiskit.quantum_info import Statevector

        from austins_algorithm.compilation import _distribution

        circuit = QuantumCircuit(5, 2, global_phase=0.37)
        for step in range(20):
            circuit.rz((step - 8) / 17, 3)
            circuit.sx(3)
            circuit.cx(3, 1)
            circuit.cz(1, 4)
            circuit.x(4)
        circuit.measure(3, 1)
        circuit.barrier()
        circuit.sx(1)
        circuit.measure(1, 0)
        with patch("austins_algorithm.compilation._stock_statevector", return_value=None):
            expected = _distribution(circuit)
        self.assertEqual(expected[1], [1, 3])
        with patch.object(Statevector, "from_instruction", side_effect=AssertionError("stock path expected")):
            self.assertEqual(_distribution(circuit), expected)

    def test_stock_distribution_defers_custom_and_instance_gate_semantics(self):
        from qiskit.circuit.library import CXGate, RZGate, XGate
        from qiskit.quantum_info import Statevector

        from austins_algorithm.compilation import _distribution, _stock_statevector

        stock_prefix = [(XGate(), (1,))] * 64
        custom = QuantumCircuit(1, name="rz")
        custom.sx(0)
        overridden = RZGate(0.2)
        overridden.to_matrix = XGate().to_matrix
        custom_broadcast = RZGate(0.2)
        custom_broadcast.broadcast_arguments = lambda qargs, cargs: iter(())
        for operation, wires in (
            (custom.to_gate(), (1,)),
            (CXGate(ctrl_state=0), (0, 1)),
            (overridden, (1,)),
            (custom_broadcast, (1,)),
        ):
            with self.subTest(operation=operation):
                self.assertIsNone(_stock_statevector(2, 0.0, [*stock_prefix, (operation, wires)]))

        circuit = QuantumCircuit(2, 1)
        for operation, wires in [*stock_prefix, (custom.to_gate(), (1,))]:
            circuit.append(operation, wires)
        circuit.measure(1, 0)
        with patch.object(Statevector, "from_instruction", wraps=Statevector.from_instruction) as simulate:
            probabilities, measured = _distribution(circuit)
        simulate.assert_called_once()
        self.assertEqual(measured, [1])
        self.assertAlmostEqual(probabilities["1"], 0.5)

    def test_nonfinite_and_unnormalized_probabilities_fail_verification(self):
        from qiskit.quantum_info import Statevector

        circuit = QuantumCircuit(1, 1)
        circuit.measure(0, 0)
        for value in (float("nan"), float("inf"), 0.9):
            with (
                self.subTest(value=value),
                patch.object(Statevector, "from_instruction") as simulate,
                self.assertRaisesRegex(RuntimeError, "finite, normalized"),
            ):
                simulate.return_value.probabilities_dict.return_value = {"0": value}
                compile_and_verify(circuit, target=line_target(1))


if __name__ == "__main__":
    unittest.main()
