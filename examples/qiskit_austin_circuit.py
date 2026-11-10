"""Optional Qiskit ideal-simulator example; this script submits no job."""

from austins_algorithm.qiskit_adapter import build_austin_circuit, run_local_statevector


if __name__ == "__main__":
    print(build_austin_circuit(iterations=1).draw())
    print(run_local_statevector(3, ("011", "101"), iterations=1, shots=1024, seed=7))
