from __future__ import annotations

import argparse
import json
import sys

from .core import AustinAlgorithmError, austin_predicate, run_austin_search, validate_marked_states


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {
        "compile", "search", "constraint", "constraint-classical", "constraint-sweep", "constraint-cover", "compare",
        "witness-schedule", "unknown-count-plan", "unknown-count-select", "unknown-count-snapshot-select",
        "unknown-count-snapshot-profile-export", "unknown-count-snapshot-profile-verify",
        "unknown-count-snapshot-profile-compare", "unknown-count-snapshot-layout-plan", "snapshot-calibration-compare",
        "snapshot-calibration-budget-plan", "snapshot-calibration-bundle-inspect", "snapshot-calibration-bundle-compose",
        "snapshot-calibration-overlap-receipt-verify",
        "snapshot-calibration-study", "snapshot-calibration-study-verify",
        "constraint-replay",
        "constraint-receipt-verify", "constraint-receipt-compare",
        "constraint-regression", "placement-receipt-verify", "placement-receipt-compare", "placement-receipt-replay",
        "smoke-environment-compare", "smoke-run-index", "smoke-run-index-verify", "smoke-run-index-compare",
        "diffuser-replay", "diffuser-receipt-verify", "diffuser-receipt-compare",
        "diffuser-regression", "diffuser-benchmark", "diffuser-benchmark-verify", "diffuser-benchmark-compare",
    }:
        from .workflows_cli import main as workflow_main

        workflow_main(sys.argv[1:])
        return
    parser = argparse.ArgumentParser(description="Run a local ideal simulation of Grover search.")
    parser.epilog = (
        "Additional commands: compile, search, constraint, constraint-classical, "
        "constraint-sweep, constraint-cover, compare, witness-schedule, unknown-count-plan, unknown-count-select, "
        "unknown-count-snapshot-select, unknown-count-snapshot-profile-export, unknown-count-snapshot-profile-verify, "
        "unknown-count-snapshot-profile-compare, unknown-count-snapshot-layout-plan, snapshot-calibration-compare, "
        "snapshot-calibration-budget-plan, snapshot-calibration-bundle-inspect, snapshot-calibration-bundle-compose, "
        "snapshot-calibration-overlap-receipt-verify, "
        "snapshot-calibration-study, snapshot-calibration-study-verify, "
        "constraint-replay, "
        "constraint-receipt-verify, constraint-receipt-compare, constraint-regression, "
        "placement-receipt-verify, placement-receipt-compare, placement-receipt-replay, "
        "smoke-environment-compare, smoke-run-index, smoke-run-index-verify, smoke-run-index-compare, "
        "diffuser-replay, diffuser-receipt-verify, diffuser-receipt-compare, diffuser-regression, "
        "diffuser-benchmark, diffuser-benchmark-verify, diffuser-benchmark-compare."
    )
    parser.add_argument(
        "--engine", choices=("cpu", "qiskit"), default="cpu",
        help="CPU reference (default) or the built-in Austin circuit in Qiskit",
    )
    parser.add_argument("--qubits", type=int, default=3)
    parser.add_argument("--marked", nargs="+", help="explicit MSB-first marked bitstrings")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--shots", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument(
        "--trace-amplitudes",
        action="store_true",
        help="include the initial and per-round ideal amplitude trace in the JSON report",
    )
    args = parser.parse_args()
    if args.engine == "qiskit":
        if args.qubits != 3 or args.marked is not None:
            parser.error("--engine qiskit runs the built-in 3-qubit Austin circuit")
        if args.trace_amplitudes:
            parser.error("--trace-amplitudes is available with --engine cpu")
        try:
            from .qiskit_adapter import run_austin_circuit_local_statevector

            result = run_austin_circuit_local_statevector(
                iterations=1 if args.iterations is None else args.iterations,
                shots=args.shots,
                seed=args.seed,
            )
        except ImportError as exc:
            if exc.name == "qiskit":
                parser.exit(
                    1, 'Qiskit is required for this engine. From the project folder, run python -m pip install ".[qiskit]".\n',
                )
            parser.exit(1, f"Qiskit circuit execution could not import a dependency: {exc}\n")
        except ValueError as exc:
            parser.error(str(exc))
        except RuntimeError as exc:
            parser.exit(1, f"Qiskit circuit execution failed: {exc}\n")
        print(json.dumps(result, indent=2))
        return

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
            include_amplitude_trace=args.trace_amplitudes,
        )
    except AustinAlgorithmError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
