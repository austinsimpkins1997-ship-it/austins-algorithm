"""Command line access to the optional Grover workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from .classical_constraint import CLASSICAL_METHODS
from .constraint_cover_comparison import COVER_SNAPSHOT_METHODS
from .constraint_oracle import SYNTHESIS_METHODS
from .grover_search import DIFFUSER_METHODS, IDEAL_SAMPLERS


def _oracle_variant(args: argparse.Namespace) -> str:
    if args.problem == "austin":
        if args.oracle is not None:
            raise ValueError("--oracle is available with --problem mod4-range")
        return "austin-two-cz"
    return args.oracle or "direct"


def _mod4_oracle(variant: str):
    from .grover_search import build_mod4_range_direct_oracle, build_mod4_range_oracle

    if variant == "work-qubit":
        return build_mod4_range_oracle()
    return build_mod4_range_direct_oracle()


def _compile(args: argparse.Namespace) -> dict:
    from .compilation import compile_and_verify, line_target

    variant = _oracle_variant(args)
    if args.problem == "austin":
        from .qiskit_adapter import build_austin_circuit

        circuit = build_austin_circuit(iterations=1)
        target_width = 3
    else:
        from .grover_search import build_grover_search_circuit

        circuit = build_grover_search_circuit(_mod4_oracle(variant), 4, iterations=2)
        # Keep the target fixed for resource comparisons between oracle variants.
        target_width = 5
    compiled = compile_and_verify(
        circuit, target=line_target(target_width, seed=args.seed), seed=args.seed,
        prefer_native_qasm=args.native_qasm,
    )
    report = dict(compiled.report)
    report["problem"] = args.problem
    report["oracle_variant"] = variant
    if args.qasm_output is not None:
        with args.qasm_output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(compiled.openqasm)
        report["qasm_output"] = str(args.qasm_output)
    return report


def _search(args: argparse.Namespace) -> dict:
    from .core import austin_predicate
    from .grover_search import (
        audit_phase_oracle,
        build_austin_phase_oracle,
        mod4_range_predicate,
        run_grover_search,
    )

    variant = _oracle_variant(args)
    if args.problem == "austin":
        oracle, width, verifier = build_austin_phase_oracle(), 3, austin_predicate
    else:
        oracle, width, verifier = _mod4_oracle(variant), 4, mod4_range_predicate
    report = run_grover_search(
        oracle, width, verifier,
        marked_count=2 if args.strategy == "known" else None,
        max_shots=args.max_shots, max_oracle_queries=args.max_oracle_queries, seed=args.seed,
        ideal_sampler=args.ideal_sampler,
    )
    report["problem"] = args.problem
    report["oracle_variant"] = variant
    if args.audit_oracle:
        report["separate_exhaustive_oracle_audit"] = audit_phase_oracle(oracle, width, verifier)
    return report


def _constraint(args: argparse.Namespace) -> dict:
    from .compilation import MAX_EXACT_QUBITS
    from .constraint_search import run_constraint_search
    from .constraints import load_constraint
    from .grover_search import _LastAttempt

    if args.qasm_output is not None and not args.compile:
        raise ValueError("--qasm-output requires --compile")
    if args.line_width is not None:
        if not args.compile:
            raise ValueError("--line-width requires --compile")
        if not 1 <= args.line_width <= MAX_EXACT_QUBITS:
            raise ValueError(f"--line-width must be from 1 to {MAX_EXACT_QUBITS}")
    if args.snapshot_shots is not None and not args.ibm_model:
        raise ValueError("--snapshot-shots requires --ibm-model")
    constraint = load_constraint(args.formula)
    last_attempt = _LastAttempt() if args.compile or args.ibm_model else None
    report = run_constraint_search(
        constraint, strategy=args.strategy, max_shots=args.max_shots,
        max_oracle_queries=args.max_oracle_queries, seed=args.seed, synthesis=args.synthesis, diffuser=args.diffuser,
        classical_method=args.classical_method, ideal_sampler=args.ideal_sampler, _last_attempt=last_attempt,
    )
    if last_attempt is None:
        return report
    attempts = report["search"]["attempts"] if report["search"] is not None else []
    if not attempts:
        if args.compile:
            report["compilation"] = {"status": "NOT_RUN_NO_ATTEMPT"}
        if args.ibm_model:
            report["snapshot"] = {"status": "NOT_RUN_NO_ATTEMPT"}
        return report

    circuit = last_attempt.circuit
    if circuit is None:
        raise RuntimeError("constraint search did not retain its last completed attempt")
    rounds = attempts[-1]["rounds"]
    if args.compile:
        from .compilation import compile_and_verify, line_target

        started = perf_counter()
        width = circuit.num_qubits if args.line_width is None else args.line_width
        if width < circuit.num_qubits:
            raise ValueError(f"synthetic line width {width} is smaller than the {circuit.num_qubits}-wire circuit")
        compiled = compile_and_verify(circuit, target=line_target(width, seed=args.seed), seed=args.seed)
        compile_seconds = perf_counter() - started
        compilation = {
            **compiled.report,
            "attempt": len(attempts),
            "rounds": rounds,
            "attempt_build_seconds": last_attempt.build_seconds,
            "compile_export_verify_seconds": compile_seconds,
            "build_compile_export_verify_seconds": last_attempt.build_seconds + compile_seconds,
        }
        if args.qasm_output is not None:
            with args.qasm_output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(compiled.openqasm)
            compilation["qasm_output"] = str(args.qasm_output)
        report["compilation"] = compilation
    if args.ibm_model:
        from .ibm_models import run_constraint_snapshot

        snapshot = run_constraint_snapshot(
            circuit, constraint.matches, model=args.ibm_model,
            shots=64 if args.snapshot_shots is None else args.snapshot_shots, seed=args.seed,
        )
        report["snapshot"] = {**snapshot, "attempt": len(attempts), "rounds": rounds}
    return report


def _constraint_cover(args: argparse.Namespace) -> dict:
    from .constraint_cover_comparison import (
        DEFAULT_COVER_EVALUATION_SEEDS,
        compare_constraint_phase_covers,
        run_constraint_cover_snapshot,
    )
    from .constraints import load_constraint

    if args.evaluation_seeds is not None and args.snapshot_shots is None:
        raise ValueError("--evaluation-seeds requires --snapshot-shots")
    if args.snapshot_method is not None and args.snapshot_shots is None:
        raise ValueError("--snapshot-method requires --snapshot-shots")
    if args.snapshot_shots is not None and (args.ibm_model is None or args.initial_layout is None):
        raise ValueError("--snapshot-shots requires --ibm-model and a shared --initial-layout")
    witness = None
    validation_seeds = args.witness_seed_schedule if args.witness_seed_schedule is not None else args.witness_seeds
    if args.witness_probability is None:
        if args.witness_max_shots is not None or validation_seeds is not None:
            raise ValueError("witness shot and seed options require --witness-probability")
    else:
        if args.snapshot_shots is None:
            raise ValueError("--witness-probability requires --snapshot-shots")
        from .witness_budget import WitnessBudgetRequest

        budget_options = {}
        if args.witness_max_shots is not None:
            budget_options["max_shots"] = args.witness_max_shots
        if validation_seeds is not None:
            budget_options["validation_seeds"] = validation_seeds
        witness = WitnessBudgetRequest(args.witness_probability, **budget_options)

    load_started = perf_counter()
    constraint = load_constraint(args.formula)
    input_load_seconds = perf_counter() - load_started
    options = {
        "rounds": args.rounds, "seed": args.seed, "initial_layout": args.initial_layout,
        "max_active_qubits": args.max_active_qubits, "prefer_native_qasm": args.native_qasm,
    }
    backend = None
    if args.snapshot_shots is not None:
        seeds = DEFAULT_COVER_EVALUATION_SEEDS if args.evaluation_seeds is None else args.evaluation_seeds
        result = run_constraint_cover_snapshot(
            constraint, model=args.ibm_model, shots=args.snapshot_shots, evaluation_seeds=seeds,
            witness_budget=witness, simulation_method=args.snapshot_method, **options,
        )
    elif args.ibm_model is None:
        from .compilation import line_target

        target = line_target(args.line_width, seed=args.seed)
        result = compare_constraint_phase_covers(constraint, target=target, **options)
    else:
        from .ibm_models import _snapshot_specs

        factory, _ = _snapshot_specs()[args.ibm_model]
        backend = factory()
        result = compare_constraint_phase_covers(constraint, backend=backend, **options)
    report = dict(result.report)
    if witness is not None:
        report["witness_budget"]["timing_seconds"]["input_load"] = input_load_seconds
    if backend is not None:
        properties = backend.properties()
        report["snapshot"] = {
            "model": args.ibm_model,
            "backend_version": backend.backend_version,
            "last_update_date": None if properties is None else properties.last_update_date.isoformat(),
        }
    if args.qasm_output is not None:
        report["qasm_output"] = None
        if result.selected is not None:
            with args.qasm_output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(result.selected.openqasm)
            report["qasm_output"] = str(args.qasm_output)
    return report


def _constraint_classical(args: argparse.Namespace) -> dict:
    from .classical_constraint import classical_first_match
    from .constraints import load_constraint

    constraint = load_constraint(args.formula)
    result = classical_first_match(constraint, method=args.method)
    return {
        "status": "PROVEN_EMPTY" if result["candidate"] is None else "FOUND",
        "execution_mode": "CLASSICAL",
        "problem": "DECLARATIVE_BOOLEAN_CONSTRAINT",
        "constraint_sha256": constraint.digest(),
        "constraint_schema_version": 1,
        "search_qubits": constraint.qubits,
        "clause_count": len(constraint.clauses),
        "basis_order": " ".join(f"q{i}" for i in reversed(range(constraint.qubits))),
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        **result,
    }


def _constraint_sweep(args: argparse.Namespace) -> dict:
    from .constraint_snapshot_sweep import run_constraint_snapshot_sweep
    from .constraints import load_constraint

    if args.compare_diffusers:
        from .constraint_diffuser_comparison import DEFAULT_EVALUATION_SEEDS, run_constraint_diffuser_comparison

        if args.calibration_seed is not None or args.validation_seed is not None:
            raise ValueError("use --evaluation-seeds with --compare-diffusers")
        return run_constraint_diffuser_comparison(
            load_constraint(args.formula), model=args.ibm_model, placements=args.placement,
            synthesis=args.synthesis, shots=args.shots, transpiler_seed=args.transpiler_seed,
            evaluation_seeds=DEFAULT_EVALUATION_SEEDS if args.evaluation_seeds is None else args.evaluation_seeds,
        )
    if args.evaluation_seeds is not None:
        raise ValueError("--evaluation-seeds requires --compare-diffusers")
    return run_constraint_snapshot_sweep(
        load_constraint(args.formula), model=args.ibm_model, placements=args.placement,
        synthesis=args.synthesis, diffuser=args.diffuser, shots=args.shots, transpiler_seed=args.transpiler_seed,
        calibration_seed=7 if args.calibration_seed is None else args.calibration_seed,
        validation_seed=19 if args.validation_seed is None else args.validation_seed,
    )


def _compare(args: argparse.Namespace) -> dict:
    device_fields = (args.backend, args.account, args.instance, args.max_execution_seconds)
    if (args.round_sweep or args.placement_check) and (args.problem != "mod4-range" or not args.ibm_model):
        raise ValueError("--round-sweep and --placement-check require --problem mod4-range and --ibm-model")
    if args.validation_seed is not None and not args.round_sweep:
        raise ValueError("--validation-seed requires --round-sweep")
    if args.transpiler_seed is not None and not (args.round_sweep or args.placement_check):
        raise ValueError("--transpiler-seed requires --round-sweep or --placement-check")
    if args.placement_check and args.minimum_hit_rate is None:
        raise ValueError("--placement-check requires --minimum-hit-rate")
    if not args.placement_check and (args.minimum_hit_rate is not None or args.evaluation_seeds is not None):
        raise ValueError("--minimum-hit-rate and --evaluation-seeds require --placement-check")
    if args.placement_check and args.seed != 7:
        raise ValueError("use --evaluation-seeds for placement check sampling")
    if args.problem == "mod4-range" and not args.ibm_model:
        raise ValueError("--problem mod4-range requires --ibm-model")
    if args.ibm_model:
        if any(value is not None for value in device_fields) or args.wait_timeout_seconds is not None:
            raise ValueError("--ibm-model is a local workflow and cannot be combined with physical job options")
        if args.noise_probability is not None:
            raise ValueError("--ibm-model uses snapshot noise and does not accept --noise-probability")
        if args.placement_check:
            from .snapshot_placement import run_mod4_placement_check

            return run_mod4_placement_check(
                model=args.ibm_model, shots=args.shots, minimum_hit_rate=args.minimum_hit_rate,
                evaluation_seeds=(29, 43) if args.evaluation_seeds is None else args.evaluation_seeds,
                transpiler_seed=7 if args.transpiler_seed is None else args.transpiler_seed,
            )
        from .ibm_models import run_ibm_snapshot_comparison, run_mod4_round_sweep, run_mod4_snapshot_comparison

        if args.round_sweep:
            return run_mod4_round_sweep(
                model=args.ibm_model, shots=args.shots, calibration_seed=args.seed,
                validation_seed=19 if args.validation_seed is None else args.validation_seed,
                transpiler_seed=7 if args.transpiler_seed is None else args.transpiler_seed,
            )
        runner = run_mod4_snapshot_comparison if args.problem == "mod4-range" else run_ibm_snapshot_comparison
        return runner(model=args.ibm_model, shots=args.shots, seed=args.seed)

    from .device_experiment import IBMHardwareRun, run_austin_device_comparison

    hardware = None
    if any(value is not None for value in device_fields) or args.wait_timeout_seconds is not None:
        if any(value is None for value in device_fields):
            raise ValueError("a physical job requires --backend, --account, --instance and --max-execution-seconds")
        hardware = IBMHardwareRun(
            backend_name=args.backend,
            account_name=args.account,
            instance=args.instance,
            max_execution_seconds=args.max_execution_seconds,
            wait_timeout_seconds=120 if args.wait_timeout_seconds is None else args.wait_timeout_seconds,
        )
    return run_austin_device_comparison(
        shots=args.shots, seed=args.seed,
        noise_probability=0.02 if args.noise_probability is None else args.noise_probability,
        hardware=hardware,
    )


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="austins-algorithm")
    commands = parser.add_subparsers(dest="workflow", required=True)

    compile_parser = commands.add_parser("compile", help="compile and verify an OpenQASM 3 round trip")
    compile_parser.add_argument("--problem", choices=("austin", "mod4-range"), default="austin")
    compile_parser.add_argument("--oracle", choices=("direct", "work-qubit"),
                                help="mod4-range oracle variant (default: direct)")
    compile_parser.add_argument("--qasm-output", type=Path, help="new file for the verified portable OpenQASM")
    compile_parser.add_argument("--seed", type=int, default=7)
    compile_parser.add_argument("--native-qasm", action="store_true", help="try Qiskit's native OpenQASM importer")
    compile_parser.set_defaults(handler=_compile)

    search_parser = commands.add_parser("search", help="find a verified answer using a reversible oracle")
    search_parser.add_argument("--problem", choices=("austin", "mod4-range"), default="mod4-range")
    search_parser.add_argument("--oracle", choices=("direct", "work-qubit"),
                               help="mod4-range oracle variant (default: direct)")
    search_parser.add_argument("--strategy", choices=("known", "unknown"), default="known")
    search_parser.add_argument("--max-shots", type=int, default=64)
    search_parser.add_argument("--max-oracle-queries", type=int, default=256)
    search_parser.add_argument("--audit-oracle", action="store_true", help="also check every small basis input separately")
    search_parser.add_argument("--seed", type=int, default=7)
    search_parser.add_argument("--ideal-sampler", choices=IDEAL_SAMPLERS, default="statevector_sampler",
                               help="optionally reuse ideal states or probabilities for repeated circuits")
    search_parser.set_defaults(handler=_search)

    constraint_parser = commands.add_parser("constraint", help="audit and search a JSON Boolean constraint")
    constraint_parser.add_argument("formula", type=Path)
    constraint_parser.add_argument("--strategy", choices=("known", "unknown"), default="unknown")
    constraint_parser.add_argument("--synthesis", choices=SYNTHESIS_METHODS, default="auto")
    constraint_parser.add_argument("--diffuser", choices=DIFFUSER_METHODS, default="standard",
                                   help="optionally reuse the first clean work qubit during diffusion")
    constraint_parser.add_argument("--classical-method", choices=CLASSICAL_METHODS, default="scan",
                                   help="classical first-witness reference (default: scan)")
    constraint_parser.add_argument("--max-shots", type=int, default=64)
    constraint_parser.add_argument("--max-oracle-queries", type=int, default=256)
    constraint_parser.add_argument("--seed", type=int, default=7)
    constraint_parser.add_argument("--compile", action="store_true", help="compile and verify the last attempted circuit")
    constraint_parser.add_argument("--line-width", type=int,
                                   help="fixed synthetic line width for --compile (default: circuit width)")
    constraint_parser.add_argument("--qasm-output", type=Path, help="new file for verified portable OpenQASM")
    constraint_parser.add_argument("--ibm-model", choices=("manila", "jakarta", "torino"),
                                   help="compile and sample the last attempted circuit on one offline IBM snapshot")
    constraint_parser.add_argument("--snapshot-shots", type=int, help="IBM snapshot shots (default: 64)")
    constraint_parser.add_argument("--ideal-sampler", choices=IDEAL_SAMPLERS, default="statevector_sampler",
                                   help="optionally reuse ideal states or probabilities for repeated circuits")
    constraint_parser.set_defaults(handler=_constraint)

    classical_parser = commands.add_parser("constraint-classical", help="solve a JSON Boolean constraint with standard-library Python")
    classical_parser.add_argument("formula", type=Path)
    classical_parser.add_argument("--method", choices=CLASSICAL_METHODS, default="propagation")
    classical_parser.set_defaults(handler=_constraint_classical)

    cover_parser = commands.add_parser("constraint-cover", help="compare exact phase covers on a fixed compilation target")
    cover_parser.add_argument("formula", type=Path)
    cover_targets = cover_parser.add_mutually_exclusive_group(required=True)
    cover_targets.add_argument("--line-width", type=int, help="synthetic line width")
    cover_targets.add_argument("--ibm-model", choices=("manila", "jakarta", "torino"), help="offline IBM snapshot target")
    cover_parser.add_argument("--rounds", type=int, default=2, help="Grover rounds with the standard diffuser")
    cover_parser.add_argument("--seed", type=int, default=7, help="transpiler seed")
    cover_parser.add_argument("--initial-layout", type=int, nargs="+", help="shared physical indices in logical order")
    cover_parser.add_argument("--max-active-qubits", type=int, help="active wire cap for candidate selection")
    cover_parser.add_argument("--snapshot-shots", type=int, help="opt in to equal offline IBM shots per cover and batch")
    cover_parser.add_argument("--snapshot-method", choices=COVER_SNAPSHOT_METHODS,
                              help="override the snapshot method, including compact and reusable prepared density matrices")
    cover_parser.add_argument("--evaluation-seeds", type=int, nargs=4,
                              help="distinct seeds in direct, complement, direct, complement order")
    cover_parser.add_argument("--witness-probability", type=float,
                              help="target probability of at least one verified answer using snapshot evidence")
    cover_parser.add_argument("--witness-max-shots", type=int, help="maximum shots per fresh witness batch")
    witness_seeds = cover_parser.add_mutually_exclusive_group()
    witness_seeds.add_argument("--witness-seeds", type=int, nargs=2, help="fresh direct and complement validation seeds")
    witness_seeds.add_argument("--witness-seed-schedule", type=int, nargs="+",
                               help="prespecified validation seeds in interleaved direct, complement pairs")
    cover_parser.add_argument("--qasm-output", type=Path, help="new file for the selected verified OpenQASM")
    cover_parser.add_argument("--native-qasm", action="store_true", help="try Qiskit's native OpenQASM importer")
    cover_parser.set_defaults(handler=_constraint_cover)

    sweep_parser = commands.add_parser("constraint-sweep", help="compare constraint rounds at two fixed IBM snapshot placements")
    sweep_parser.add_argument("formula", type=Path)
    sweep_parser.add_argument("--ibm-model", choices=("manila", "jakarta", "torino"), required=True)
    sweep_parser.add_argument("--placement", action="append", type=int, nargs="+", required=True,
                              help="physical qubit indices in logical order, including work qubits (use twice)")
    sweep_parser.add_argument("--synthesis", choices=SYNTHESIS_METHODS, default="auto")
    diffusers = sweep_parser.add_mutually_exclusive_group()
    diffusers.add_argument("--diffuser", choices=DIFFUSER_METHODS, default="standard",
                           help="optionally reuse the first clean work qubit during diffusion")
    diffusers.add_argument("--compare-diffusers", action="store_true",
                           help="evaluate both diffusers at one and two rounds with fresh seed batches")
    sweep_parser.add_argument("--shots", type=int, default=64, help="shots per placement, round count, and batch")
    sweep_parser.add_argument("--calibration-seed", type=int, help="sweep calibration seed (default: 7)")
    sweep_parser.add_argument("--validation-seed", type=int, help="sweep validation seed (default: 19)")
    sweep_parser.add_argument("--evaluation-seeds", type=int, nargs=4,
                              help="four distinct seeds for standard, clean, standard, clean in diffuser comparison")
    sweep_parser.add_argument("--transpiler-seed", type=int, default=7)
    sweep_parser.set_defaults(handler=_constraint_sweep)

    compare_parser = commands.add_parser("compare", help="compare local simulation, IBM snapshots, or an explicit QPU job")
    compare_parser.add_argument("--problem", choices=("austin", "mod4-range"), default="austin")
    compare_parser.add_argument("--shots", type=int, default=1024)
    compare_parser.add_argument("--seed", type=int, default=7)
    experiment = compare_parser.add_mutually_exclusive_group()
    experiment.add_argument("--round-sweep", action="store_true", help="calibrate and validate mod4 Grover rounds")
    experiment.add_argument("--placement-check", action="store_true", help="test one and two mod4 rounds at two placements")
    compare_parser.add_argument("--validation-seed", type=int, help="round sweep holdout seed (default: 19)")
    compare_parser.add_argument("--transpiler-seed", type=int, help="round sweep or placement compiler seed (default: 7)")
    compare_parser.add_argument("--minimum-hit-rate", type=float, help="required verified shot rate for placement check")
    compare_parser.add_argument("--evaluation-seeds", type=int, nargs=2, help="placement check seeds (default: 29 43)")
    compare_parser.add_argument("--noise-probability", type=float, help="synthetic local noise probability")
    compare_parser.add_argument("--ibm-model", choices=("manila", "jakarta", "torino", "all"),
                                help="offline snapshot simulation with the ibm package extra")
    compare_parser.add_argument("--backend", help="submit one physical job on this named IBM backend")
    compare_parser.add_argument("--account", help="name of the locally configured IBM account")
    compare_parser.add_argument("--instance", help="IBM service instance for the physical job")
    compare_parser.add_argument("--max-execution-seconds", type=int, help="provider QPU execution time limit")
    compare_parser.add_argument("--wait-timeout-seconds", type=int, help="local wait deadline for the physical job")
    compare_parser.set_defaults(handler=_compare)

    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except ImportError as exc:
        parser.exit(1, f"Optional workflow dependency could not be imported: {exc}. See the README installation commands.\n")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"{args.workflow} failed: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
