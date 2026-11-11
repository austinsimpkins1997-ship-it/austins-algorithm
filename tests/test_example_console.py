import os
import subprocess
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PACKAGE_ROOT / "examples"
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

try:
    from qiskit.quantum_info import Operator  # noqa: F401
except Exception as exc:  # Optional framework may be absent or unimportable.
    QISKIT_AVAILABLE = False
    QISKIT_IMPORT_ERROR = repr(exc)
else:
    QISKIT_AVAILABLE = True
    QISKIT_IMPORT_ERROR = ""

try:
    import cirq  # noqa: F401
except Exception as exc:  # Optional framework may be absent or unimportable.
    CIRQ_AVAILABLE = False
    CIRQ_IMPORT_ERROR = repr(exc)
else:
    CIRQ_AVAILABLE = True
    CIRQ_IMPORT_ERROR = ""


def _run_with_legacy_encoding(script_name: str) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"
    environment["PYTHONUTF8"] = "0"
    source_path = str(PACKAGE_ROOT / "src")
    environment["PYTHONPATH"] = source_path + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(EXAMPLES / script_name)],
        cwd=PACKAGE_ROOT,
        env=environment,
        capture_output=True,
        timeout=60,
        check=False,
    )


class ExampleConsoleTests(unittest.TestCase):
    @unittest.skipUnless(QISKIT_AVAILABLE, f"optional Qiskit is unavailable: {QISKIT_IMPORT_ERROR}")
    def test_qiskit_example_runs_when_console_starts_with_cp1252(self):
        result = _run_with_legacy_encoding("qiskit_austin_circuit.py")
        output = result.stdout.decode("utf-8", errors="replace")
        error = result.stderr.decode("cp1252", errors="replace")
        self.assertEqual(result.returncode, 0, error)
        self.assertIn("LOCAL_IDEAL_STATEVECTOR_SIMULATION", output)
        self.assertIn("'provider_job_submitted': False", output)

    @unittest.skipUnless(CIRQ_AVAILABLE, f"optional Cirq is unavailable: {CIRQ_IMPORT_ERROR}")
    def test_cirq_example_uses_ascii_diagram_when_console_is_cp1252(self):
        result = _run_with_legacy_encoding("cirq_austin_circuit.py")
        output = result.stdout.decode("ascii", errors="replace")
        error = result.stderr.decode("cp1252", errors="replace")
        self.assertEqual(result.returncode, 0, error)
        self.assertIn("LOCAL_IDEAL_CIRQ_SIMULATION", output)
        self.assertIn("provider_job_submitted", output)


if __name__ == "__main__":
    unittest.main()
