import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    import cirq
except Exception as exc:  # Optional framework may be absent.
    cirq = None
    CIRQ_IMPORT_ERROR = repr(exc)
else:
    CIRQ_IMPORT_ERROR = ""

from austins_algorithm.cirq_adapter import build_austin_circuit, classical_solutions, run_local  # noqa: E402


@unittest.skipIf(cirq is None, f"optional Cirq is unavailable: {CIRQ_IMPORT_ERROR}")
class CirqAdapterTests(unittest.TestCase):
    def test_circuit_returns_only_the_two_marked_constraint_solutions(self):
        self.assertEqual(classical_solutions(), ("011", "101"))
        result = run_local(iterations=1, shots=256, seed=7)
        self.assertEqual(set(result["counts"]), {"011", "101"})
        self.assertFalse(result["quantum_hardware_used"])
        self.assertFalse(result["provider_job_submitted"])
        self.assertEqual(len(build_austin_circuit(iterations=1).all_qubits()), 3)


if __name__ == "__main__":
    unittest.main()
