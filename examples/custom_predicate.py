"""Example domain predicate: pick four-bit assignments with exactly two ones."""

from austins_algorithm import run_austin_search


def exactly_two_ones(bits: str) -> bool:
    return bits.count("1") == 2


if __name__ == "__main__":
    result = run_austin_search(4, exactly_two_ones, shots=512, seed=7)
    print("Status:", result["status"])
    print("Classically enumerated marked examples:", result["solution_examples"])
    print("Ideal success probability:", result["statevector_success_probability"])
    print("Caveat:", result["limits"][1])
