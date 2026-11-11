import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from qiskit.quantum_info import Operator
except Exception as exc:  # Optional framework may be absent or unimportable.
    Operator = None
    QISKIT_IMPORT_ERROR = repr(exc)
else:
    QISKIT_IMPORT_ERROR = ""

from austins_algorithm.qiskit_adapter import (  # noqa: E402
    build_austin_circuit,
    build_phase_oracle,
    run_austin_circuit_local_statevector,
    run_local_statevector,
    transpile_for_backend,
)


@unittest.skipIf(Operator is None, f"optional Qiskit is unavailable: {QISKIT_IMPORT_ERROR}")
class QiskitAdapterTests(unittest.TestCase):
    def test_generic_phase_oracle_marks_only_the_requested_basis_states(self):
        matrix = Operator(build_phase_oracle(3, ("011", "101"))).data
        marked = {"011", "101"}
        for index in range(8):
            state = format(index, "03b")
            self.assertAlmostEqual(matrix[index, index].real, -1.0 if state in marked else 1.0)
            for other in range(8):
                if other != index:
                    self.assertAlmostEqual(abs(matrix[other, index]), 0.0)

    def test_generic_sampler_finds_only_explicitly_marked_states(self):
        result = run_local_statevector(3, ("011", "101"), iterations=1, shots=256, seed=7)
        self.assertEqual(set(result["counts"]), {"011", "101"})
        self.assertEqual(result["observed_success_fraction"], 1.0)
        self.assertFalse(result["quantum_hardware_used"])
        self.assertFalse(result["provider_job_submitted"])

    def test_exact_austin_circuit_amplifies_the_predicate(self):
        from qiskit.quantum_info import Statevector
        from austins_algorithm.core import austin_predicate

        for rounds, expected in ((0, 0.25), (1, 1.0), (2, 0.25)):
            with self.subTest(rounds=rounds):
                circuit = build_austin_circuit(iterations=rounds)
                self.assertEqual(circuit.num_qubits, 3)
                state = Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
                probabilities = state.probabilities_dict()
                self.assertAlmostEqual(
                    sum(probability for bits, probability in probabilities.items() if austin_predicate(bits)),
                    expected,
                )

    def test_exact_sampler_and_cli_report_verified_candidates(self):
        from austins_algorithm.__main__ import main

        expected = run_austin_circuit_local_statevector(shots=256, seed=7)
        output = io.StringIO()
        argv = ["austins-algorithm", "--engine", "qiskit", "--shots", "256", "--seed", "7"]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result, expected)
        self.assertEqual(result["execution_mode"], "SIMULATOR")
        self.assertEqual(result["basis_order"], "q2 q1 q0")
        self.assertEqual(result["verified_candidates"], ["011", "101"])
        self.assertEqual(result["verified_shots"], 256)
        self.assertEqual(sum(result["counts"].values()), 256)
        self.assertFalse(result["quantum_hardware_used"])
        self.assertFalse(result["provider_job_submitted"])

        unamplified = run_austin_circuit_local_statevector(iterations=0, shots=256, seed=7)
        self.assertGreater(len(unamplified["counts"]), len(unamplified["verified_candidates"]))
        self.assertEqual(unamplified["verified_candidates"], ["011", "101"])
        self.assertEqual(
            unamplified["verified_shots"],
            unamplified["counts"]["011"] + unamplified["counts"]["101"],
        )

    def test_local_transpilation_requires_a_caller_supplied_target(self):
        with self.assertRaises(ValueError):
            transpile_for_backend(None, None)


if __name__ == "__main__":
    unittest.main()
