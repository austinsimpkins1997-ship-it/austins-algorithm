"""Optional Qiskit ideal-simulator example; this script submits no job."""

import sys

from austins_algorithm.qiskit_adapter import build_austin_circuit, run_austin_circuit_local_statevector


def _print_terminal_safe(value: object) -> None:
    """Keep the circuit diagram runnable in Windows consoles without UTF-8."""
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(text)


if __name__ == "__main__":
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass
    _print_terminal_safe(build_austin_circuit(iterations=1).draw())
    print(run_austin_circuit_local_statevector(iterations=1, shots=1024, seed=7))
