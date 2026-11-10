from __future__ import annotations

import argparse
import json

from .core import AustinAlgorithmError, austin_predicate, run_austin_search, validate_marked_states


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded CPU statevector reference for Grover search.")
    parser.add_argument("--qubits", type=int, default=3)
    parser.add_argument("--marked", nargs="+", help="explicit MSB-first marked bitstrings")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--shots", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=20261109)
    args = parser.parse_args()
    try:
        if args.marked is not None:
            states = set(validate_marked_states(args.qubits, args.marked))
            predicate = lambda bits: bits in states
            name = "explicit caller-supplied marked bitstrings"
        else:
            if args.qubits != 3:
                parser.error("the built-in Austin predicate uses 3 qubits; pass --marked for another search")
            predicate = austin_predicate
            name = "q0 == 1 and q1 != q2"
        result = run_austin_search(
            args.qubits,
            predicate,
            iterations=args.iterations,
            shots=args.shots,
            seed=args.seed,
            predicate_name=name,
        )
    except AustinAlgorithmError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
