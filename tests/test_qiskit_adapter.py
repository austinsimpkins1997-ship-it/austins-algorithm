import sys
import unittest
from pathlib import Path


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

    def test_austin_circuit_finds_only_the_two_satisfying_states(self):
        result = run_local_statevector(3, ("011", "101"), iterations=1, shots=256, seed=7)
        self.assertEqual(set(result["counts"]), {"011", "101"})
        self.assertEqual(result["observed_success_fraction"], 1.0)
        self.assertFalse(result["quantum_hardware_used"])
        self.assertFalse(result["provider_job_submitted"])
        self.assertEqual(build_austin_circuit(iterations=1).num_qubits, 3)

    def test_local_transpilation_requires_a_caller_supplied_target(self):
        with self.assertRaises(ValueError):
            transpile_for_backend(None, None)


if __name__ == "__main__":
    unittest.main()
