"""Optional Cirq ideal-simulator example; this script submits no job."""

from austins_algorithm.cirq_adapter import build_austin_circuit, run_local


if __name__ == "__main__":
    print(build_austin_circuit(iterations=1))
    print(run_local(iterations=1, shots=1024, seed=7))
