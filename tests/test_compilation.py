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


@unittest.skipUnless(
    QISKIT_AVAILABLE
    and (hasattr(qasm3, "loads_experimental") or find_spec("qiskit_qasm3_import") is not None),
    "optional Qiskit OpenQASM tools are unavailable",
)
class CompilationTests(unittest.TestCase):
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

    def test_reimported_width_and_work_are_checked_before_statevector(self):
        from austins_algorithm.compilation import _distribution

        circuit = QuantumCircuit(1, 1)
        circuit.measure(0, 0)
        wrong_width = QuantumCircuit(17, 1)
        too_many_gates = QuantumCircuit(1, 1)
        for _ in range(6):
            too_many_gates.x(0)
        too_many_gates.measure(0, 0)
        for returned, error, message in (
            (object(), RuntimeError, "did not return"),
            (wrong_width, RuntimeError, "changed the declared wires"),
            (too_many_gates, ValueError, "statevector verification work limit"),
        ):
            with (
                self.subTest(message=message),
                patch("austins_algorithm.compilation._import_openqasm", return_value=(returned, "test")),
                patch("austins_algorithm.compilation._distribution", wraps=_distribution) as distribution,
                patch("austins_algorithm.compilation.MAX_EXACT_WORK", 8),
                self.assertRaisesRegex(error, message),
            ):
                compile_and_verify(circuit, target=line_target(1))
            self.assertEqual(distribution.call_count, 2)

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
