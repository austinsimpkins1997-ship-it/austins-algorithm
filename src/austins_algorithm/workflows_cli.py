"""Command line access to the optional Grover workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from .classical_constraint import CLASSICAL_METHODS
from .constraint_cover_comparison import COVER_SNAPSHOT_METHODS, PhaseCoverComparison
from .constraint_diffuser_comparison import DEFAULT_DIFFUSER_SHOTS, DIFFUSER_SNAPSHOT_METHODS
from .constraint_oracle import SYNTHESIS_METHODS
from .core import MAX_ITERATIONS
from .grover_search import DIFFUSER_METHODS, IDEAL_SAMPLERS, QUERY_BUDGET_POLICIES
from .ibm_models import IBM_SNAPSHOT_MODELS
from .witness_budget import MAX_WITNESS_BATCHES_PER_COVER

if TYPE_CHECKING:
    from qiskit import QuantumCircuit

    from .compilation import CompiledProgram


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


def _compile_for_target(
    circuit: QuantumCircuit, *, target_name: str | None, line_width: int, seed: int, native_qasm: bool,
) -> CompiledProgram:
    if target_name in (None, "line"):
        from .compilation import compile_and_verify, line_target

        if line_width < circuit.num_qubits:
            raise ValueError(f"synthetic line width {line_width} is smaller than the {circuit.num_qubits}-wire circuit")
        return compile_and_verify(
            circuit, target=line_target(line_width, seed=seed), seed=seed, prefer_native_qasm=native_qasm,
        )

    from .ibm_models import compile_ibm_snapshot

    return compile_ibm_snapshot(circuit, model=target_name, seed=seed, prefer_native_qasm=native_qasm)


def _compile(args: argparse.Namespace) -> dict:
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
    compiled = _compile_for_target(
        circuit, target_name=args.compile_target, line_width=target_width, seed=args.seed, native_qasm=args.native_qasm,
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
        _validate_query_budget_policy,
        audit_phase_oracle,
        build_austin_phase_oracle,
        mod4_range_predicate,
        run_grover_search,
    )

    _validate_query_budget_policy(args.query_budget_policy, known_count=args.strategy == "known")
    variant = _oracle_variant(args)
    if args.problem == "austin":
        oracle, width, verifier = build_austin_phase_oracle(), 3, austin_predicate
    else:
        oracle, width, verifier = _mod4_oracle(variant), 4, mod4_range_predicate
    report = run_grover_search(
        oracle, width, verifier,
        marked_count=2 if args.strategy == "known" else None,
        max_shots=args.max_shots, max_oracle_queries=args.max_oracle_queries, seed=args.seed,
        ideal_sampler=args.ideal_sampler, query_budget_policy=args.query_budget_policy,
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
    if args.compile_target is not None and not args.compile:
        raise ValueError("--compile-target requires --compile")
    if args.line_width is not None:
        if not args.compile:
            raise ValueError("--line-width requires --compile")
        if args.compile_target not in (None, "line"):
            raise ValueError("--line-width requires the line compile target")
        if not 1 <= args.line_width <= MAX_EXACT_QUBITS:
            raise ValueError(f"--line-width must be from 1 to {MAX_EXACT_QUBITS}")
    if args.snapshot_shots is not None and not args.ibm_model:
        raise ValueError("--snapshot-shots requires --ibm-model")
    if args.native_qasm and not (args.compile or args.ibm_model):
        raise ValueError("--native-qasm requires --compile or --ibm-model")
    constraint = load_constraint(args.formula)
    last_attempt = _LastAttempt() if args.compile or args.ibm_model else None
    report = run_constraint_search(
        constraint, strategy=args.strategy, max_shots=args.max_shots,
        max_oracle_queries=args.max_oracle_queries, seed=args.seed, synthesis=args.synthesis, diffuser=args.diffuser,
        classical_method=args.classical_method, ideal_sampler=args.ideal_sampler,
        query_budget_policy=args.query_budget_policy, _last_attempt=last_attempt,
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
        started = perf_counter()
        width = circuit.num_qubits if args.line_width is None else args.line_width
        compiled = _compile_for_target(
            circuit, target_name=args.compile_target, line_width=width, seed=args.seed, native_qasm=args.native_qasm,
        )
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
            prefer_native_qasm=args.native_qasm,
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
    if args.density_fusion and args.snapshot_method != "prepared_density_matrix":
        raise ValueError("--density-fusion requires --snapshot-method prepared_density_matrix")
    if args.snapshot_shots is not None and (args.ibm_model is None or args.initial_layout is None):
        raise ValueError("--snapshot-shots requires --ibm-model and a shared --initial-layout")
    witness = None
    validation_seeds = args.witness_seed_schedule if args.witness_seed_schedule is not None else args.witness_seeds
    if args.witness_probability is None:
        if args.witness_stop_on_futility:
            raise ValueError("--witness-stop-on-futility requires --witness-probability")
        if args.witness_max_shots is not None or validation_seeds is not None:
            raise ValueError("witness shot and seed options require --witness-probability")
    else:
        if args.snapshot_shots is None:
            raise ValueError("--witness-probability requires --snapshot-shots")
        from .witness_budget import WitnessBudgetRequest

        budget_options = {"stop_on_futility": args.witness_stop_on_futility}
        if args.witness_max_shots is not None:
            budget_options["max_shots"] = args.witness_max_shots
        if validation_seeds is not None:
            budget_options["validation_seeds"] = validation_seeds
        witness = WitnessBudgetRequest(args.witness_probability, **budget_options)

    if args.save_replay_request is not None:
        if witness is None:
            raise ValueError("--save-replay-request requires --witness-probability and snapshot options")
        if args.qasm_output is not None:
            raise ValueError("--save-replay-request cannot be combined with --qasm-output")
    elif args.tolerated_misses is not None:
        raise ValueError("--tolerated-misses requires --save-replay-request for constraint-cover")

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
        if args.save_replay_request is not None:
            from .constraint_replay import save_constraint_replay

            return save_constraint_replay(
                args.save_replay_request, constraint, model=args.ibm_model, shots=args.snapshot_shots,
                evaluation_seeds=seeds, witness_budget=witness, simulation_method=args.snapshot_method,
                density_fusion=args.density_fusion, tolerated_misses=args.tolerated_misses or 0, **options,
            )
        result = run_constraint_cover_snapshot(
            constraint, model=args.ibm_model, shots=args.snapshot_shots, evaluation_seeds=seeds,
            witness_budget=witness, simulation_method=args.snapshot_method,
            density_fusion=args.density_fusion, **options,
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
    return _write_constraint_cover_export(report, result, args.qasm_output)


def _write_constraint_cover_export(
    report: dict, result: PhaseCoverComparison, qasm_output: Path | None,
) -> dict:
    if qasm_output is not None:
        report["qasm_output"] = None
        if result.selected is not None:
            with qasm_output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(result.selected.openqasm)
            report["qasm_output"] = str(qasm_output)
    return report


def _constraint_replay(args: argparse.Namespace) -> dict:
    from .constraint_replay import check_constraint_replay, run_constraint_replay

    if args.check_only:
        return check_constraint_replay(args.request)
    if args.qasm_output is not None and args.qasm_output.exists():
        raise FileExistsError(f"OpenQASM file already exists: {args.qasm_output}")
    result = run_constraint_replay(args.request, receipt_directory=args.receipt_dir)
    return _write_constraint_cover_export(dict(result.report), result, args.qasm_output)


def _constraint_receipt_verify(args: argparse.Namespace) -> dict:
    from .replay_receipt import verify_replay_receipt

    return verify_replay_receipt(args.directory)


def _placement_receipt_verify(args: argparse.Namespace) -> dict:
    from .placement_receipt import verify_placement_receipt

    return verify_placement_receipt(args.directory)


def _placement_receipt_compare(args: argparse.Namespace) -> dict:
    from .placement_receipt_comparison import compare_placement_receipts

    return compare_placement_receipts(args.left, args.right)


def _placement_receipt_replay(args: argparse.Namespace) -> dict:
    from .placement_replay import replay_placement_receipt

    return replay_placement_receipt(args.directory, check_only=args.check_only)


def _smoke_environment_compare(args: argparse.Namespace) -> dict:
    from .smoke_environment_comparison import compare_smoke_environments

    return compare_smoke_environments(args.left, args.right)


def _smoke_run_index(args: argparse.Namespace) -> dict:
    from .smoke_run_index import write_smoke_run_index

    return write_smoke_run_index(args.runner_directory, args.index)


def _smoke_run_index_verify(args: argparse.Namespace) -> dict:
    from .smoke_run_index import verify_smoke_run_index

    return verify_smoke_run_index(args.index, args.artifact_directory, runner_layout=args.runner_layout)


def _smoke_run_index_compare(args: argparse.Namespace) -> dict:
    from .smoke_run_comparison import compare_smoke_run_indexes

    return compare_smoke_run_indexes(
        args.left_index, args.left_artifacts, args.right_index, args.right_artifacts,
        left_runner_layout=args.left_runner_layout, right_runner_layout=args.right_runner_layout,
    )


def _constraint_receipt_compare(args: argparse.Namespace) -> dict:
    from .replay_receipt_comparison import compare_replay_receipts

    return compare_replay_receipts(args.left, args.right)


def _constraint_regression(args: argparse.Namespace) -> dict:
    from .replay_regression import run_replay_regression

    return run_replay_regression(args.manifest, output_directory=args.output_dir)


def _unknown_count_plan(args: argparse.Namespace) -> dict:
    from .unknown_count_planning import plan_unknown_count_search

    return plan_unknown_count_search(
        args.qubits, args.solutions, max_shots=args.max_shots, max_oracle_queries=args.max_oracle_queries,
        max_rounds_per_shot=args.max_rounds_per_shot,
    )


def _unknown_count_select(args: argparse.Namespace) -> dict:
    from .unknown_count_planning import select_unknown_count_budgets

    return select_unknown_count_budgets(
        args.qubits, args.solutions, target_probability=args.target_probability,
        shot_caps=args.shot_caps, query_caps=args.query_caps, max_rounds_per_shot=args.max_rounds_per_shot,
    )


def _unknown_count_snapshot_select(args: argparse.Namespace) -> dict:
    from .unknown_count_snapshot_selection import load_snapshot_selection_panel, select_snapshot_unknown_count_budgets

    try:
        panel, file_digest = load_snapshot_selection_panel(args.panel)
    except OSError as exc:
        raise ValueError(f"could not read snapshot panel: {exc}") from exc
    result = select_snapshot_unknown_count_budgets(
        panel, args.suite, target_probability=args.target_probability,
        shot_caps=args.shot_caps, query_caps=args.query_caps, max_rounds_per_shot=args.max_rounds_per_shot,
        validation_trials=args.validate_trials, expected_observations_sha256=args.expected_observations_sha256,
        expected_bundle_sha256=args.expected_bundle_sha256,
    )
    digest_field = "bundle_file_sha256" if "bundle_sha256" in result["source"] else "file_sha256"
    result["source"][digest_field] = file_digest
    return result


def _unknown_count_snapshot_profile_export(args: argparse.Namespace) -> dict:
    from .unknown_count_snapshot_profile import export_snapshot_profile_bundle, load_snapshot_selection_panel

    panel, _ = load_snapshot_selection_panel(args.panel)
    return export_snapshot_profile_bundle(panel, expected_observations_sha256=args.expected_observations_sha256)


def _unknown_count_snapshot_profile_verify(args: argparse.Namespace) -> dict:
    from .unknown_count_snapshot_profile import load_snapshot_selection_panel, verify_snapshot_profile_bundle

    bundle, bundle_file_digest = load_snapshot_selection_panel(args.bundle)
    panel, panel_file_digest = (None, None)
    if args.source_panel is not None:
        panel, panel_file_digest = load_snapshot_selection_panel(args.source_panel)
    result = verify_snapshot_profile_bundle(bundle, source_panel=panel, expected_bundle_sha256=args.expected_bundle_sha256)
    result["bundle_file_sha256"] = bundle_file_digest
    if panel_file_digest is not None:
        result["source_panel_file_sha256"] = panel_file_digest
    return result


def _unknown_count_snapshot_profile_compare(args: argparse.Namespace) -> dict:
    from .unknown_count_snapshot_profile import load_snapshot_selection_panel
    from .unknown_count_snapshot_profile_comparison import compare_snapshot_profile_bundles

    left, left_file_sha256 = load_snapshot_selection_panel(args.left)
    right, right_file_sha256 = load_snapshot_selection_panel(args.right)
    result = compare_snapshot_profile_bundles(
        left, right, args.suite, target_probability=args.target_probability,
        shot_caps=args.shot_caps, query_caps=args.query_caps, max_rounds_per_shot=args.max_rounds_per_shot,
        validation_trials=args.validate_trials, expected_left_bundle_sha256=args.expected_left_bundle_sha256,
        expected_right_bundle_sha256=args.expected_right_bundle_sha256,
    )
    result["artifacts"]["left"]["file_sha256"] = left_file_sha256
    result["artifacts"]["right"]["file_sha256"] = right_file_sha256
    return result


def _unknown_count_snapshot_layout_plan(args: argparse.Namespace) -> dict:
    from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES, plan_snapshot_layout_budgets
    from .unknown_count_snapshot_profile import load_snapshot_selection_panel

    if not 2 <= len(args.bundles) <= MAX_LAYOUT_PROFILES:
        raise ValueError(f"layout planning requires 2 to {MAX_LAYOUT_PROFILES} snapshot profile bundles")
    loaded = [load_snapshot_selection_panel(path) for path in args.bundles]
    result = plan_snapshot_layout_budgets(
        [bundle for bundle, _ in loaded], args.suite, target_probability=args.target_probability,
        shot_caps=args.shot_caps, query_caps=args.query_caps, max_rounds_per_shot=args.max_rounds_per_shot,
        validation_trials=args.validate_trials, expected_bundle_sha256=args.expected_bundle_sha256,
        probability_radius=args.probability_radius, probability_radii=args.probability_radii,
    )
    files = {bundle["bundle_sha256"]: digest for bundle, digest in loaded}
    for row in result["layouts"].values():
        row["file_sha256"] = files[row["bundle_sha256"]]
    return result


def _snapshot_calibration_compare(args: argparse.Namespace) -> dict:
    from .constraints import load_constraint
    from .snapshot_calibration_comparison import SnapshotCalibrationFiles, compare_snapshot_calibrations

    return compare_snapshot_calibrations(
        load_constraint(args.constraint), SnapshotCalibrationFiles(*args.earlier), SnapshotCalibrationFiles(*args.later),
        initial_layout=args.initial_layout, max_rounds=args.max_rounds, seed_transpiler=args.seed_transpiler,
        prefer_native_qasm=args.native_qasm, batch_density=args.batch_density, density_fusion=args.density_fusion,
    )


def _snapshot_calibration_budget_plan(args: argparse.Namespace) -> dict:
    from .constraints import load_constraint
    from .snapshot_calibration_planning import plan_snapshot_calibration_budgets
    from .unknown_count_snapshot_layout import MAX_LAYOUT_PROFILES
    from .unknown_count_snapshot_profile import _sha256, load_snapshot_selection_panel

    if not 1 <= len(args.reports) <= MAX_LAYOUT_PROFILES:
        raise ValueError(f"budget planning requires 1 to {MAX_LAYOUT_PROFILES} calibration comparison reports")
    pins = args.expected_report_sha256
    if pins is not None and len(pins) != len(args.reports):
        raise ValueError("expected report digests must align with the supplied calibration reports")
    references = args.fusion_reference
    tolerance = args.fusion_probability_tolerance
    if (references is None) != (tolerance is None):
        raise ValueError("--fusion-reference and --fusion-probability-tolerance must be supplied together")
    if references is not None and len(references) != len(args.reports):
        raise ValueError("each fused report requires one pinned reference report")

    def load_report(path: Path, pin: str | None, label: str) -> tuple[dict, str]:
        report, digest = load_snapshot_selection_panel(path)
        if pin is not None and digest != _sha256(pin, f"expected {label} digest"):
            raise ValueError(f"{label} does not match its expected SHA-256 digest")
        return report, digest

    loaded = [
        load_report(path, None if pins is None else pins[index], f"calibration report {index + 1}")
        for index, path in enumerate(args.reports)
    ]
    constraint = load_constraint(args.constraint)
    options = {
        "target_probability": args.target_probability, "shot_caps": args.shot_caps, "query_caps": args.query_caps,
        "max_rounds_per_shot": args.max_rounds_per_shot, "validation_trials": args.validate_trials,
    }
    if references is None:
        result = plan_snapshot_calibration_budgets([report for report, _ in loaded], constraint, **options)
        plan, plan_inputs, pins_matched = result, loaded, True if pins is not None else None
    else:
        from .snapshot_calibration_fusion import compare_snapshot_calibration_fusion_budgets

        reference_reports = [
            load_report(Path(path), pin, f"calibration reference report {index + 1}")
            for index, (path, pin) in enumerate(references)
        ]
        result = compare_snapshot_calibration_fusion_budgets(
            [report for report, _ in reference_reports], [report for report, _ in loaded],
            constraint, probability_tolerance=tolerance, **options,
        )
        plan, plan_inputs, pins_matched = result["reference_plan"], reference_reports, True
        result["report_files"] = {
            side: [{"initial_layout": report["initial_layout"], "sha256": digest, "pin_matched": pinned}
                   for report, digest in group]
            for side, group, pinned in (
                ("reference", reference_reports, True), ("fused", loaded, True if pins is not None else None),
            )
        }

    for report, digest in plan_inputs:
        layout = ",".join(map(str, report["initial_layout"]))
        plan["layouts"][layout]["source_report_sha256"] = digest
        plan["layouts"][layout]["source_report_pin_matched"] = pins_matched
    return result


def _snapshot_calibration_bundle_inspect(args: argparse.Namespace) -> dict:
    from .snapshot_calibration_compilation_bundle import inspect_snapshot_calibration_compilation_bundle

    return inspect_snapshot_calibration_compilation_bundle(args.bundle, expected_sha256=args.expected_sha256)


def _snapshot_calibration_bundle_compose(args: argparse.Namespace) -> dict:
    from .snapshot_calibration_compilation_bundle import compose_snapshot_calibration_compilation_bundles

    return compose_snapshot_calibration_compilation_bundles(args.destination, bundles=args.bundles)


def _snapshot_calibration_overlap_receipt_verify(args: argparse.Namespace) -> dict:
    from .snapshot_calibration_overlap_receipt import verify_snapshot_calibration_overlap_receipt

    output, output_pin = args.output_bundle
    return verify_snapshot_calibration_overlap_receipt(
        args.receipt, expected_receipt_sha256=args.expected_receipt_sha256,
        output_bundle=output, expected_output_sha256=output_pin, sources=args.bundles,
    )


def _snapshot_calibration_study_verify(args: argparse.Namespace) -> dict:
    from .constraints import load_constraint
    from .snapshot_calibration_study_verification import verify_snapshot_calibration_study

    return verify_snapshot_calibration_study(
        load_constraint(args.constraint), args.study, expected_sha256=args.expected_study_sha256,
    )


def _snapshot_calibration_study(args: argparse.Namespace) -> dict:
    from .constraints import load_constraint
    from .snapshot_calibration_comparison import SnapshotCalibrationFiles
    from .snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
    from .snapshot_calibration_study import (
        check_snapshot_calibration_compilation_coverage, compare_snapshot_calibration_compilation_coverage,
        compose_snapshot_calibration_compilation_for_study, replay_snapshot_calibration_overlap_receipt,
        run_snapshot_calibration_study, run_snapshot_calibration_study_with_overlap_receipt,
    )

    destination = args.save_compilation_bundle
    candidates = args.compare_compilation_bundle
    overlaps = args.resolve_compilation_overlap
    replay = args.replay_overlap_receipt
    source_bundles = args.overlap_source_bundle
    simulation_free = (
        args.check_compilation_bundle or candidates is not None or overlaps is not None
        or (replay is not None and not args.run_after_overlap_replay)
    )
    if args.batch_density and simulation_free:
        raise ValueError("--batch-density requires a study that runs snapshot simulations")
    fusion_options = {}
    if args.fusion_probability_tolerance is not None:
        if simulation_free:
            raise ValueError("--fusion-probability-tolerance requires a study that runs snapshot simulations")
        from .snapshot_calibration_fusion import _validate_fusion_probability_tolerance

        fusion_options["fusion_probability_tolerance"] = _validate_fusion_probability_tolerance(
            args.fusion_probability_tolerance
        )
    if args.run_after_overlap_replay and replay is None:
        raise ValueError("--run-after-overlap-replay requires --replay-overlap-receipt")
    if replay is not None:
        if args.load_compilation_bundle is None or source_bundles is None:
            raise ValueError("--replay-overlap-receipt requires --load-compilation-bundle and --overlap-source-bundle")
        if destination is not None or args.check_compilation_bundle:
            raise ValueError("--replay-overlap-receipt cannot be combined with bundle saving or coverage checking")
    elif source_bundles is not None:
        raise ValueError("--overlap-source-bundle requires --replay-overlap-receipt")
    if candidates is not None and (args.check_compilation_bundle or destination is not None):
        raise ValueError(
            "--compare-compilation-bundle cannot be combined with --check-compilation-bundle or --save-compilation-bundle"
        )
    if overlaps is not None and destination is None:
        raise ValueError("--resolve-compilation-overlap requires --save-compilation-bundle PATH")
    if args.check_compilation_bundle and args.load_compilation_bundle is None:
        raise ValueError("--check-compilation-bundle requires --load-compilation-bundle PATH SHA256")
    if destination is not None and (destination.exists() or destination.is_symlink()):
        raise FileExistsError(f"compilation bundle already exists: {destination}")
    cache = None
    if args.load_compilation_bundle is not None and replay is None:
        path, pin = args.load_compilation_bundle
        cache = SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=pin)
    elif destination is not None and overlaps is None:
        cache = SnapshotCalibrationCompilationCache(max_entries=256, max_bytes=32 * 1024 * 1024)

    inputs = (
        load_constraint(args.constraint), SnapshotCalibrationFiles(*args.earlier), SnapshotCalibrationFiles(*args.later),
    )
    options = {
        "initial_layouts": args.initial_layout, "target_probability": args.target_probability,
        "shot_caps": args.shot_caps, "query_caps": args.query_caps, "max_rounds_per_shot": args.max_rounds_per_shot,
        "seed_transpiler": args.seed_transpiler, "validation_trials": args.validate_trials,
        "prefer_native_qasm": args.native_qasm,
    }
    if replay is not None:
        receipt, receipt_pin = replay
        output, output_pin = args.load_compilation_bundle
        receipt_options = {
            "receipt": receipt, "expected_receipt_sha256": receipt_pin, "output_bundle": output,
            "expected_output_sha256": output_pin, "sources": source_bundles,
        }
        if args.run_after_overlap_replay:
            return run_snapshot_calibration_study_with_overlap_receipt(
                *inputs, **receipt_options, **options, **fusion_options, batch_density=args.batch_density,
            )
        return replay_snapshot_calibration_overlap_receipt(*inputs, **receipt_options, **options)
    if candidates is not None:
        return compare_snapshot_calibration_compilation_coverage(*inputs, bundles=candidates, **options)
    if overlaps is not None:
        return compose_snapshot_calibration_compilation_for_study(
            *inputs, destination=destination, bundles=overlaps, **options,
        )

    if args.check_compilation_bundle:
        return check_snapshot_calibration_compilation_coverage(*inputs, compilation_cache=cache, **options)
    result = run_snapshot_calibration_study(
        *inputs, compilation_cache=cache, **options, **fusion_options, batch_density=args.batch_density,
    )
    if destination is not None:
        cache.save_bundle(destination)
    return result


def _witness_schedule(args: argparse.Namespace) -> dict:
    from .witness_budget import DEFAULT_COVER_EVALUATION_SEEDS, check_witness_schedule, plan_witness_schedule

    if args.comparison == "diffuser":
        from .constraint_diffuser_comparison import plan_diffuser_witness_schedule

        if args.evaluation_seeds is not None:
            raise ValueError("--evaluation-seeds is only used for the phase-cover comparison")
        return plan_diffuser_witness_schedule(
            target_probability=args.target_probability, tolerated_misses=args.tolerated_misses,
            max_batches_per_variant=args.max_batches_per_cover, validation_seeds=args.witness_seed_schedule,
            assumed_batch_success_probability=args.assumed_batch_success_probability,
            minimum_validation_power=args.minimum_validation_power,
        )

    if args.assumed_batch_success_probability is not None or args.minimum_validation_power is not None:
        raise ValueError("validation power options require --comparison diffuser")
    options = {
        "target_probability": args.target_probability,
        "tolerated_misses": args.tolerated_misses,
        "max_batches_per_cover": args.max_batches_per_cover,
    }
    if args.witness_seed_schedule is None:
        if args.evaluation_seeds is not None:
            raise ValueError("--evaluation-seeds requires --witness-seed-schedule for witness-schedule")
        return plan_witness_schedule(**options)
    return check_witness_schedule(
        validation_seeds=args.witness_seed_schedule,
        evaluation_seeds=args.evaluation_seeds if args.evaluation_seeds is not None else DEFAULT_COVER_EVALUATION_SEEDS,
        **options,
    )


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


def _diffuser_replay(args: argparse.Namespace) -> dict:
    from .diffuser_replay import run_diffuser_replay

    return run_diffuser_replay(args.request, check_only=args.check_only, receipt_directory=args.receipt_dir)


def _diffuser_receipt_verify(args: argparse.Namespace) -> dict:
    from .diffuser_receipt import verify_diffuser_receipt

    return verify_diffuser_receipt(args.directory)


def _diffuser_receipt_compare(args: argparse.Namespace) -> dict:
    from .diffuser_receipt_comparison import compare_diffuser_receipts

    return compare_diffuser_receipts(args.left, args.right, include_timings=args.timings)


def _diffuser_benchmark(args: argparse.Namespace) -> dict:
    from .diffuser_benchmark import run_diffuser_benchmark

    return run_diffuser_benchmark(
        args.left, args.right, output_directory=args.output_dir, pairs=args.pairs, timeout_seconds=args.timeout_seconds,
    )


def _diffuser_benchmark_verify(args: argparse.Namespace) -> dict:
    from .diffuser_benchmark_verification import verify_diffuser_benchmark

    return verify_diffuser_benchmark(args.directory)


def _diffuser_benchmark_compare(args: argparse.Namespace) -> dict:
    from .diffuser_benchmark_comparison import compare_diffuser_benchmarks

    return compare_diffuser_benchmarks(args.baseline, args.candidate)


def _diffuser_regression(args: argparse.Namespace) -> dict:
    from .diffuser_regression import run_diffuser_regression, run_diffuser_regression_suite

    if args.manifest is not None:
        if args.max_sampling_shots is not None or args.timeout_seconds is not None:
            raise ValueError("the manifest supplies per-case shot limits and timeouts")
        return run_diffuser_regression_suite(args.manifest, output_directory=args.output_dir)
    if args.max_sampling_shots is None or args.timeout_seconds is None:
        raise ValueError("a single baseline requires --max-sampling-shots and --timeout-seconds")
    return run_diffuser_regression(
        args.baseline, output_directory=args.output_dir, max_sampling_shots=args.max_sampling_shots,
        timeout_seconds=args.timeout_seconds,
    )


def _constraint_sweep(args: argparse.Namespace) -> dict:
    from .constraint_snapshot_sweep import run_constraint_snapshot_sweep
    from .constraints import load_constraint

    evaluation_seeds = args.evaluation_seed_schedule or args.evaluation_seeds
    shots = DEFAULT_DIFFUSER_SHOTS if args.shots is None else args.shots
    if args.compare_diffusers:
        from .constraint_diffuser_comparison import DEFAULT_EVALUATION_SEEDS, run_constraint_diffuser_comparison

        sweep_seeds_provided = args.calibration_seed is not None or args.validation_seed is not None
        if args.snapshot_method == "modeled_probabilities" and (
            args.shots is not None or evaluation_seeds is not None or sweep_seeds_provided
        ):
            raise ValueError("modeled_probabilities uses no shot or sampling seed options")
        if sweep_seeds_provided:
            raise ValueError("use --evaluation-seeds with --compare-diffusers")
        options = dict(
            model=args.ibm_model, placements=args.placement, synthesis=args.synthesis, transpiler_seed=args.transpiler_seed,
            witness_probability=args.witness_probability, witness_max_shots=args.witness_max_shots,
            witness_seed_schedule=args.witness_seed_schedule, witness_stop_on_futility=args.witness_stop_on_futility,
            witness_validation_power=args.witness_validation_power, prefer_native_qasm=args.native_qasm,
        )
        if args.save_replay_request is not None:
            from .diffuser_replay import save_diffuser_replay

            if args.snapshot_method != "modeled_probabilities" or args.witness_search_max_batches is not None:
                raise ValueError("--save-replay-request requires a modeled diffuser sweep with a fixed seed schedule")
            return save_diffuser_replay(args.save_replay_request, load_constraint(args.formula), **options)
        return run_constraint_diffuser_comparison(
            load_constraint(args.formula), **options, shots=shots,
            evaluation_seeds=DEFAULT_EVALUATION_SEEDS if evaluation_seeds is None else evaluation_seeds,
            simulation_method=args.snapshot_method, witness_plan_only=args.witness_plan_only,
            witness_search_max_batches=args.witness_search_max_batches,
        )
    witness_options = (
        args.witness_probability, args.witness_max_shots, args.witness_seed_schedule,
        args.witness_validation_power, args.witness_search_max_batches, args.save_replay_request,
    )
    if args.witness_stop_on_futility or args.witness_plan_only or any(value is not None for value in witness_options):
        raise ValueError("witness budget flags require --compare-diffusers with modeled_probabilities")
    if args.snapshot_method is not None:
        raise ValueError("--snapshot-method requires --compare-diffusers")
    if args.native_qasm:
        raise ValueError("--native-qasm requires --compare-diffusers")
    if evaluation_seeds is not None:
        flag = "--evaluation-seed-schedule" if args.evaluation_seed_schedule is not None else "--evaluation-seeds"
        raise ValueError(f"{flag} requires --compare-diffusers")
    return run_constraint_snapshot_sweep(
        load_constraint(args.formula), model=args.ibm_model, placements=args.placement,
        synthesis=args.synthesis, diffuser=args.diffuser, shots=shots, transpiler_seed=args.transpiler_seed,
        calibration_seed=7 if args.calibration_seed is None else args.calibration_seed,
        validation_seed=19 if args.validation_seed is None else args.validation_seed,
    )


def _compare(args: argparse.Namespace) -> dict:
    device_fields = (args.backend, args.account, args.instance, args.max_execution_seconds)
    if args.native_qasm and not args.ibm_model:
        raise ValueError("--native-qasm requires --ibm-model")
    if (args.round_sweep or args.placement_check) and (args.problem != "mod4-range" or not args.ibm_model):
        raise ValueError("--round-sweep and --placement-check require --problem mod4-range and --ibm-model")
    if args.validation_seed is not None and not args.round_sweep:
        raise ValueError("--validation-seed requires --round-sweep")
    if args.transpiler_seed is not None and not (args.round_sweep or args.placement_check):
        raise ValueError("--transpiler-seed requires --round-sweep or --placement-check")
    if args.placement_check and args.minimum_hit_rate is None:
        raise ValueError("--placement-check requires --minimum-hit-rate")
    if args.receipt_dir is not None and not args.placement_check:
        raise ValueError("--receipt-dir requires --placement-check")
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
                prefer_native_qasm=args.native_qasm, receipt_dir=args.receipt_dir,
            )
        from .ibm_models import run_ibm_snapshot_comparison, run_mod4_round_sweep, run_mod4_snapshot_comparison

        if args.round_sweep:
            return run_mod4_round_sweep(
                model=args.ibm_model, shots=args.shots, calibration_seed=args.seed,
                validation_seed=19 if args.validation_seed is None else args.validation_seed,
                transpiler_seed=7 if args.transpiler_seed is None else args.transpiler_seed,
                prefer_native_qasm=args.native_qasm,
            )
        runner = run_mod4_snapshot_comparison if args.problem == "mod4-range" else run_ibm_snapshot_comparison
        return runner(model=args.ibm_model, shots=args.shots, seed=args.seed, prefer_native_qasm=args.native_qasm)

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
    compile_parser.add_argument("--compile-target", choices=("line", *IBM_SNAPSHOT_MODELS),
                                help="synthetic line or bundled IBM snapshot for compilation (default: line)")
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
    search_parser.add_argument("--query-budget-policy", choices=QUERY_BUDGET_POLICIES, default="stop",
                               help="allow an affordable redraw in unknown-count mode")
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
    constraint_parser.add_argument("--query-budget-policy", choices=QUERY_BUDGET_POLICIES, default="stop",
                                   help="allow an affordable redraw in unknown-count mode")
    constraint_parser.add_argument("--seed", type=int, default=7)
    constraint_parser.add_argument("--compile", action="store_true", help="compile and verify the last attempted circuit")
    constraint_parser.add_argument("--compile-target", choices=("line", *IBM_SNAPSHOT_MODELS),
                                   help="synthetic line or bundled IBM snapshot for --compile (default: line)")
    constraint_parser.add_argument("--line-width", type=int,
                                   help="fixed synthetic line width for --compile (default: circuit width)")
    constraint_parser.add_argument("--qasm-output", type=Path, help="new file for verified portable OpenQASM")
    constraint_parser.add_argument(
        "--native-qasm", action="store_true",
        help="try Qiskit's native OpenQASM importer for --compile and --ibm-model",
    )
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
    cover_parser.add_argument(
        "--density-fusion", action="store_true",
        help="use Aer gate fusion during prepared_density_matrix evolution",
    )
    cover_parser.add_argument("--evaluation-seeds", type=int, nargs=4,
                              help="distinct seeds in direct, complement, direct, complement order")
    cover_parser.add_argument("--witness-probability", type=float,
                              help="target probability of at least one verified answer using snapshot evidence")
    cover_parser.add_argument("--witness-max-shots", type=int, help="maximum shots per fresh witness batch")
    cover_parser.add_argument(
        "--witness-stop-on-futility", action="store_true",
        help="stop a holdout once its fixed confidence target is unreachable",
    )
    witness_seeds = cover_parser.add_mutually_exclusive_group()
    witness_seeds.add_argument("--witness-seeds", type=int, nargs=2, help="fresh direct and complement validation seeds")
    witness_seeds.add_argument("--witness-seed-schedule", type=int, nargs="+",
                               help="prespecified validation seeds in interleaved direct, complement pairs")
    cover_parser.add_argument("--qasm-output", type=Path, help="new file for the selected verified OpenQASM")
    cover_parser.add_argument("--native-qasm", action="store_true", help="try Qiskit's native OpenQASM importer")
    cover_parser.add_argument("--save-replay-request", type=Path, help="save a new checked offline replay request")
    cover_parser.add_argument("--tolerated-misses", type=int, help="miss allowance for a saved request's schedule check")
    cover_parser.set_defaults(handler=_constraint_cover)

    replay_parser = commands.add_parser("constraint-replay", help="check or run an offline witness replay request")
    replay_parser.add_argument("request", type=Path)
    replay_mode = replay_parser.add_mutually_exclusive_group()
    replay_mode.add_argument("--check-only", action="store_true", help="verify the request and compile without sampling")
    replay_mode.add_argument("--qasm-output", type=Path, help="new file for the selected verified OpenQASM")
    replay_mode.add_argument("--receipt-dir", type=Path, help="new directory for the request, report, export and receipt")
    replay_parser.set_defaults(handler=_constraint_replay)

    receipt_parser = commands.add_parser("constraint-receipt-verify", help="verify a local replay receipt's artifact bytes")
    receipt_parser.add_argument("directory", type=Path)
    receipt_parser.set_defaults(handler=_constraint_receipt_verify)

    receipt_comparison = commands.add_parser("constraint-receipt-compare", help="compare two verified replay receipts")
    receipt_comparison.add_argument("left", type=Path)
    receipt_comparison.add_argument("right", type=Path)
    receipt_comparison.set_defaults(handler=_constraint_receipt_compare)

    regression_parser = commands.add_parser("constraint-regression", help="rerun named offline baselines under fixed limits")
    regression_parser.add_argument("manifest", type=Path)
    regression_parser.add_argument("--output-dir", type=Path, required=True, help="new directory for evidence and summary")
    regression_parser.set_defaults(handler=_constraint_regression)

    unknown_plan = commands.add_parser("unknown-count-plan", help="model ideal hit probability and work under both query policies")
    unknown_plan.add_argument("--qubits", type=int, required=True, help="number of search bits")
    unknown_plan.add_argument("--solutions", type=int, required=True, help="assumed marked states for the model")
    unknown_plan.add_argument("--max-shots", type=int, default=64)
    unknown_plan.add_argument("--max-oracle-queries", type=int, default=256)
    unknown_plan.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    unknown_plan.set_defaults(handler=_unknown_count_plan)

    unknown_select = commands.add_parser("unknown-count-select", help="compare budgets against an ideal witness target")
    unknown_select.add_argument("--qubits", type=int, required=True, help="number of search bits")
    unknown_select.add_argument("--solutions", type=int, nargs="+", required=True, help="marked-count hypotheses")
    unknown_select.add_argument("--target-probability", type=float, required=True, help="minimum ideal witness probability")
    unknown_select.add_argument("--shot-caps", type=int, nargs="+", required=True, help="candidate shot caps")
    unknown_select.add_argument("--query-caps", type=int, nargs="+", required=True, help="candidate logical query caps")
    unknown_select.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    unknown_select.set_defaults(handler=_unknown_count_select)

    snapshot_select = commands.add_parser(
        "unknown-count-snapshot-select", help="compare budgets from a saved IBM snapshot panel without SDKs",
    )
    snapshot_select.add_argument("panel", type=Path, help="saved snapshot benchmark JSON or a compact profile bundle")
    snapshot_select.add_argument("--suite", required=True, help="suite name in the saved panel")
    snapshot_select.add_argument("--target-probability", type=float, required=True)
    snapshot_select.add_argument("--shot-caps", type=int, nargs="+", required=True)
    snapshot_select.add_argument("--query-caps", type=int, nargs="+", required=True)
    snapshot_select.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    snapshot_select.add_argument("--validate-trials", type=int, default=0, help="fresh seeded trials per case and plan")
    snapshot_select.add_argument("--expected-observations-sha256", help="known semantic digest of the source panel")
    snapshot_select.add_argument("--expected-bundle-sha256", help="known digest of the compact input")
    snapshot_select.set_defaults(handler=_unknown_count_snapshot_select)

    profile_export = commands.add_parser(
        "unknown-count-snapshot-profile-export", help="verify a full snapshot panel and emit a compact selection bundle",
    )
    profile_export.add_argument("panel", type=Path, help="saved snapshot benchmark JSON")
    profile_export.add_argument("--expected-observations-sha256", help="known semantic digest of the source panel")
    profile_export.set_defaults(handler=_unknown_count_snapshot_profile_export)

    profile_verify = commands.add_parser(
        "unknown-count-snapshot-profile-verify", help="verify a compact profile bundle and optional source derivation",
    )
    profile_verify.add_argument("bundle", type=Path)
    profile_verify.add_argument("--source-panel", type=Path, help="full panel to compare with the bundle projection")
    profile_verify.add_argument("--expected-bundle-sha256", help="known digest of the compact input")
    profile_verify.set_defaults(handler=_unknown_count_snapshot_profile_verify)

    profile_compare = commands.add_parser(
        "unknown-count-snapshot-profile-compare", help="compare verified snapshot bundles on a shared grid",
    )
    profile_compare.add_argument("left", type=Path)
    profile_compare.add_argument("right", type=Path)
    profile_compare.add_argument("--suite", required=True)
    profile_compare.add_argument("--target-probability", type=float, required=True)
    profile_compare.add_argument("--shot-caps", type=int, nargs="+", required=True)
    profile_compare.add_argument("--query-caps", type=int, nargs="+", required=True)
    profile_compare.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    profile_compare.add_argument("--validate-trials", type=int, default=0, help="matched seeded trials per case and plan")
    profile_compare.add_argument("--expected-left-bundle-sha256", help="known digest of the left bundle")
    profile_compare.add_argument("--expected-right-bundle-sha256", help="known digest of the right bundle")
    profile_compare.set_defaults(handler=_unknown_count_snapshot_profile_compare)

    layout_plan = commands.add_parser(
        "unknown-count-snapshot-layout-plan", help="rank fixed layouts and budgets on one verified snapshot",
    )
    layout_plan.add_argument("bundles", type=Path, nargs="+", help="profile bundles with distinct fixed layouts")
    layout_plan.add_argument("--suite", required=True)
    layout_plan.add_argument("--target-probability", type=float, required=True)
    layout_plan.add_argument("--shot-caps", type=int, nargs="+", required=True)
    layout_plan.add_argument("--query-caps", type=int, nargs="+", required=True)
    layout_plan.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    layout_plan.add_argument("--validate-trials", type=int, default=0, help="matched fresh trials per case and budget")
    layout_radii = layout_plan.add_mutually_exclusive_group()
    layout_radii.add_argument("--probability-radius", type=float, help="absolute uncertainty bound on each round hit probability")
    layout_radii.add_argument("--probability-radii", type=float, nargs="+",
                              help="compare absolute uncertainty bounds and bracket each fixed plan's tolerance")
    layout_plan.add_argument("--expected-bundle-sha256", nargs="+", help="one expected digest per bundle, in input order")
    layout_plan.set_defaults(handler=_unknown_count_snapshot_layout_plan)

    calibration_compare = commands.add_parser(
        "snapshot-calibration-compare", help="simulate identical compiled Grover circuits on two dated IBM snapshots",
    )
    calibration_compare.add_argument("constraint", type=Path)
    archive_fields = ("CONFIG_JSON", "PROPS_JSON", "CONFIG_SHA256", "PROPS_SHA256")
    calibration_compare.add_argument("--earlier", nargs=4, metavar=archive_fields, required=True)
    calibration_compare.add_argument("--later", nargs=4, metavar=archive_fields, required=True)
    calibration_compare.add_argument("--initial-layout", type=int, nargs="+", required=True)
    calibration_compare.add_argument("--max-rounds", type=int, default=1)
    calibration_compare.add_argument("--seed-transpiler", type=int, default=7)
    calibration_compare.add_argument("--native-qasm", action="store_true",
                                     help="prefer Qiskit's native OpenQASM importer")
    calibration_compare.add_argument("--batch-density", action="store_true",
                                     help="group consecutive rounds on the same local density simulator")
    calibration_compare.add_argument("--density-fusion", action="store_true",
                                     help="capture the opt-in Aer density fusion profile")
    calibration_compare.set_defaults(handler=_snapshot_calibration_compare)

    calibration_plan = commands.add_parser(
        "snapshot-calibration-budget-plan", help="rank shared search budgets across two saved IBM calibration models",
    )
    calibration_plan.add_argument("constraint", type=Path)
    calibration_plan.add_argument("reports", type=Path, nargs="+", help="matched calibration comparison reports")
    calibration_plan.add_argument("--target-probability", type=float, required=True)
    calibration_plan.add_argument("--shot-caps", type=int, nargs="+", required=True)
    calibration_plan.add_argument("--query-caps", type=int, nargs="+", required=True)
    calibration_plan.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    calibration_plan.add_argument("--validate-trials", type=int, default=0,
                                  help="matched seeded trials per selected calibration or classical arm")
    calibration_plan.add_argument("--expected-report-sha256", nargs="+", help="known raw file digest for each report")
    calibration_plan.add_argument("--fusion-reference", nargs=2, action="append", metavar=("PATH", "SHA256"),
                                  help="pinned default capture to match a fused report by layout, repeat as needed")
    calibration_plan.add_argument("--fusion-probability-tolerance", type=float, metavar="TV",
                                  help="maximum observed total variation per matched round and date")
    calibration_plan.set_defaults(handler=_snapshot_calibration_budget_plan)

    calibration_bundle_inspect = commands.add_parser(
        "snapshot-calibration-bundle-inspect", help="inspect a SHA-256-pinned compilation bundle without the SDK",
    )
    calibration_bundle_inspect.add_argument("bundle", type=Path)
    calibration_bundle_inspect.add_argument("--expected-sha256", required=True, help="known SHA-256 of the bundle file")
    calibration_bundle_inspect.set_defaults(handler=_snapshot_calibration_bundle_inspect)

    calibration_bundle_compose = commands.add_parser(
        "snapshot-calibration-bundle-compose", help="combine complementary pinned compilation bundles without the SDK",
    )
    calibration_bundle_compose.add_argument("destination", type=Path, help="new file for the composed bundle")
    calibration_bundle_compose.add_argument(
        "--bundle", dest="bundles", nargs=2, action="append", required=True, metavar=("PATH", "SHA256"),
        help="input bundle and expected SHA-256, repeat for two to eight inputs",
    )
    calibration_bundle_compose.set_defaults(handler=_snapshot_calibration_bundle_compose)

    overlap_receipt_verify = commands.add_parser(
        "snapshot-calibration-overlap-receipt-verify",
        help="verify pinned overlap receipt, source and output bytes without the SDK",
    )
    overlap_receipt_verify.add_argument("receipt", type=Path, help="saved JSON report from successful overlap resolution")
    overlap_receipt_verify.add_argument("--expected-receipt-sha256", required=True, help="known SHA-256 of the receipt")
    overlap_receipt_verify.add_argument("--output-bundle", nargs=2, required=True, metavar=("PATH", "SHA256"))
    overlap_receipt_verify.add_argument(
        "--bundle", dest="bundles", nargs=2, action="append", required=True, metavar=("PATH", "SHA256"),
        help="pinned original source bundle, repeat for each source",
    )
    overlap_receipt_verify.set_defaults(handler=_snapshot_calibration_overlap_receipt_verify)

    study_verify = commands.add_parser(
        "snapshot-calibration-study-verify", help="verify a pinned paired calibration study offline",
    )
    study_verify.add_argument("constraint", type=Path)
    study_verify.add_argument("study", type=Path)
    study_verify.add_argument("--expected-study-sha256", required=True, help="known SHA-256 of the study file")
    study_verify.set_defaults(handler=_snapshot_calibration_study_verify)

    calibration_study = commands.add_parser(
        "snapshot-calibration-study", help="capture, plan, and replay a shared budget grid across ordered layouts",
    )
    calibration_study.add_argument("constraint", type=Path)
    calibration_study.add_argument("--earlier", nargs=4, metavar=archive_fields, required=True)
    calibration_study.add_argument("--later", nargs=4, metavar=archive_fields, required=True)
    calibration_study.add_argument("--initial-layout", type=int, nargs="+", action="append", required=True,
                                   help="ordered physical wires for one layout, repeat for additional layouts")
    calibration_study.add_argument("--target-probability", type=float, required=True)
    calibration_study.add_argument("--shot-caps", type=int, nargs="+", required=True)
    calibration_study.add_argument("--query-caps", type=int, nargs="+", required=True)
    calibration_study.add_argument("--max-rounds-per-shot", type=int, default=MAX_ITERATIONS)
    calibration_study.add_argument("--seed-transpiler", type=int, default=7)
    calibration_study.add_argument("--native-qasm", action="store_true",
                                   help="prefer Qiskit's native OpenQASM importer")
    calibration_study.add_argument("--batch-density", action="store_true",
                                   help="group consecutive rounds on the same local density simulator")
    calibration_study.add_argument("--fusion-probability-tolerance", type=float, metavar="TV",
                                   help="also run Aer density fusion and audit it against the default capture")
    calibration_study.add_argument("--validate-trials", type=int, default=0)
    bundle_output = calibration_study.add_mutually_exclusive_group()
    bundle_output.add_argument("--save-compilation-bundle", type=Path, metavar="PATH",
                               help="write completed compiled rounds to a new local bundle")
    bundle_output.add_argument("--check-compilation-bundle", action="store_true",
                               help="check saved key coverage for the requested rounds without simulating")
    bundle_input = calibration_study.add_mutually_exclusive_group()
    bundle_input.add_argument("--load-compilation-bundle", nargs=2, metavar=("PATH", "SHA256"),
                              help="reuse compiled rounds from a trusted, SHA-256-pinned bundle")
    bundle_input.add_argument("--compare-compilation-bundle", nargs=2, action="append", metavar=("PATH", "SHA256"),
                              help="compare coverage without simulating, repeat for two to eight pinned bundles")
    bundle_input.add_argument("--resolve-compilation-overlap", nargs=2, action="append", metavar=("PATH", "SHA256"),
                              help="verify differing QPY rows against this study, repeat for two to eight pinned bundles")
    calibration_study.add_argument("--replay-overlap-receipt", nargs=2, metavar=("PATH", "SHA256"),
                                   help="replay recorded physical checks against this exact study request")
    calibration_study.add_argument("--overlap-source-bundle", nargs=2, action="append", metavar=("PATH", "SHA256"),
                                   help="pinned original source bundle for receipt replay, repeat for each source")
    calibration_study.add_argument("--run-after-overlap-replay", action="store_true",
                                   help="run the dated study after successful receipt replay using the same preparation")
    calibration_study.set_defaults(handler=_snapshot_calibration_study)

    schedule_parser = commands.add_parser("witness-schedule", help="size or check a fixed witness holdout before sampling")
    schedule_parser.add_argument("--comparison", choices=("cover", "diffuser"), default="cover",
                                 help="phase-cover or eight-variant diffuser holdout (default: cover)")
    schedule_parser.add_argument("--target-probability", type=float, required=True, help="target batch success probability")
    schedule_parser.add_argument("--tolerated-misses", type=int, default=0, help="completed misses allowed per circuit")
    schedule_parser.add_argument(
        "--max-batches-per-cover", "--max-batches-per-variant", type=int, default=MAX_WITNESS_BATCHES_PER_COVER,
        help="cap on planned batches per circuit (default: %(default)s)",
    )
    schedule_parser.add_argument(
        "--witness-seed-schedule", type=int, nargs="+",
        help="proposed seeds in alternating direct/complement or standard/clean order",
    )
    schedule_parser.add_argument(
        "--evaluation-seeds", type=int, nargs=4, help="calibration seeds for the proposed schedule",
    )
    schedule_parser.add_argument(
        "--assumed-batch-success-probability", type=float,
        help="assumed witness chance per batch for diffuser validation power",
    )
    schedule_parser.add_argument(
        "--minimum-validation-power", type=float,
        help="find the smallest eligible diffuser schedule meeting this probability of target support",
    )
    schedule_parser.set_defaults(handler=_witness_schedule)

    sweep_parser = commands.add_parser("constraint-sweep", help="compare constraint rounds at two fixed IBM snapshot placements")
    sweep_parser.add_argument("formula", type=Path)
    sweep_parser.add_argument("--ibm-model", choices=("manila", "jakarta", "torino"), required=True)
    sweep_parser.add_argument("--snapshot-method", choices=DIFFUSER_SNAPSHOT_METHODS,
                              help="density-matrix sampling or modeled probabilities with --compare-diffusers")
    sweep_parser.add_argument("--placement", action="append", type=int, nargs="+", required=True,
                              help="physical qubit indices in logical order, including work qubits (use twice)")
    sweep_parser.add_argument("--synthesis", choices=SYNTHESIS_METHODS, default="auto")
    diffusers = sweep_parser.add_mutually_exclusive_group()
    diffusers.add_argument("--diffuser", choices=DIFFUSER_METHODS, default="standard",
                           help="optionally reuse the first clean work qubit during diffusion")
    diffusers.add_argument("--compare-diffusers", action="store_true",
                           help="evaluate both diffusers at one and two rounds with fresh seed batches")
    sweep_parser.add_argument("--shots", type=int, help="shots per placement, round count, and batch (default: 64)")
    sweep_parser.add_argument("--witness-probability", type=float, help="target witness probability in modeled mode")
    sweep_parser.add_argument("--witness-max-shots", type=int, help="shot cap for the modeled witness plan")
    sweep_parser.add_argument("--witness-seed-schedule", type=int, nargs="+",
                              help="fresh witness validation seeds in alternating standard and clean order")
    sweep_parser.add_argument("--witness-stop-on-futility", action="store_true",
                              help="skip remaining seeds when a variant can no longer support the target")
    sweep_parser.add_argument("--witness-validation-power", type=float,
                              help="minimum modeled chance the fixed witness holdout will support its target")
    sweep_parser.add_argument("--witness-search-max-batches", type=int,
                              help="compare power-targeted schedules up to this batch count without sampling")
    sweep_parser.add_argument("--native-qasm", action="store_true",
                              help="try Qiskit's native OpenQASM importer with --compare-diffusers")
    sweep_parser.add_argument("--witness-plan-only", action="store_true",
                              help="report modeled holdout cost and validation power without sampling")
    sweep_parser.add_argument("--save-replay-request", type=Path,
                              help="save a fully feasible fixed-seed modeled diffuser plan without sampling")
    sweep_parser.add_argument("--calibration-seed", type=int, help="sweep calibration seed (default: 7)")
    sweep_parser.add_argument("--validation-seed", type=int, help="sweep validation seed (default: 19)")
    sweep_seeds = sweep_parser.add_mutually_exclusive_group()
    sweep_seeds.add_argument("--evaluation-seeds", type=int, nargs=4,
                             help="four distinct seeds for standard, clean, standard, clean in diffuser comparison")
    sweep_seeds.add_argument("--evaluation-seed-schedule", type=int, nargs="+",
                             help="even list of distinct diffuser evaluation seeds in alternating standard and clean order")
    sweep_parser.add_argument("--transpiler-seed", type=int, default=7)
    sweep_parser.set_defaults(handler=_constraint_sweep)

    diffuser_replay = commands.add_parser("diffuser-replay", help="check or run a saved offline diffuser holdout")
    diffuser_replay.add_argument("request", type=Path)
    diffuser_replay_mode = diffuser_replay.add_mutually_exclusive_group()
    diffuser_replay_mode.add_argument("--check-only", action="store_true", help="verify the plan without measurement batches")
    diffuser_replay_mode.add_argument("--receipt-dir", type=Path, help="new directory for the request, report and receipt")
    diffuser_replay.set_defaults(handler=_diffuser_replay)

    diffuser_receipt = commands.add_parser("diffuser-receipt-verify", help="verify a diffuser receipt and its recorded workload offline")
    diffuser_receipt.add_argument("directory", type=Path)
    diffuser_receipt.set_defaults(handler=_diffuser_receipt_verify)

    diffuser_comparison = commands.add_parser("diffuser-receipt-compare", help="compare verified diffuser runs by circuit and seed")
    diffuser_comparison.add_argument("left", type=Path)
    diffuser_comparison.add_argument("right", type=Path)
    diffuser_comparison.add_argument("--timings", action="store_true", help="include aligned compiler timing diagnostics")
    diffuser_comparison.set_defaults(handler=_diffuser_receipt_compare)

    diffuser_benchmark = commands.add_parser("diffuser-benchmark", help="time alternating checked diffuser requests locally")
    diffuser_benchmark.add_argument("left", type=Path)
    diffuser_benchmark.add_argument("right", type=Path)
    diffuser_benchmark.add_argument("--output-dir", type=Path, required=True, help="new directory for benchmark evidence")
    diffuser_benchmark.add_argument("--pairs", type=int, default=4, help="number of alternating pairs (default: %(default)s)")
    diffuser_benchmark.add_argument("--timeout-seconds", type=int, default=60,
                                    help="local timeout per worker (default: %(default)s)")
    diffuser_benchmark.set_defaults(handler=_diffuser_benchmark)

    benchmark_verification = commands.add_parser("diffuser-benchmark-verify", help="verify saved compiler benchmark evidence offline")
    benchmark_verification.add_argument("directory", type=Path)
    benchmark_verification.set_defaults(handler=_diffuser_benchmark_verify)

    benchmark_comparison = commands.add_parser("diffuser-benchmark-compare", help="compare two verified benchmark bundles offline")
    benchmark_comparison.add_argument("baseline", type=Path)
    benchmark_comparison.add_argument("candidate", type=Path)
    benchmark_comparison.set_defaults(handler=_diffuser_benchmark_compare)

    diffuser_regression = commands.add_parser("diffuser-regression", help="rerun verified diffuser baselines under local limits")
    diffuser_input = diffuser_regression.add_mutually_exclusive_group(required=True)
    diffuser_input.add_argument("baseline", type=Path, nargs="?")
    diffuser_input.add_argument("--manifest", type=Path, help="JSON manifest of named baseline receipts")
    diffuser_regression.add_argument("--output-dir", type=Path, required=True, help="new directory for replay evidence")
    diffuser_regression.add_argument("--max-sampling-shots", type=int, help="maximum planned measurement shots for one baseline")
    diffuser_regression.add_argument("--timeout-seconds", type=int, help="local replay worker timeout for one baseline")
    diffuser_regression.set_defaults(handler=_diffuser_regression)

    placement_receipt = commands.add_parser("placement-receipt-verify", help="verify a placement receipt offline")
    placement_receipt.add_argument("directory", type=Path)
    placement_receipt.set_defaults(handler=_placement_receipt_verify)

    placement_comparison = commands.add_parser("placement-receipt-compare", help="compare two verified placement receipts")
    placement_comparison.add_argument("left", type=Path)
    placement_comparison.add_argument("right", type=Path)
    placement_comparison.set_defaults(handler=_placement_receipt_compare)

    placement_replay = commands.add_parser("placement-receipt-replay", help="rebuild and replay a verified placement receipt")
    placement_replay.add_argument("directory", type=Path)
    placement_replay.add_argument("--check-only", action="store_true", help="verify installed identities without sampling")
    placement_replay.set_defaults(handler=_placement_receipt_replay)

    smoke_environment = commands.add_parser("smoke-environment-compare", help="compare two saved smoke environment reports")
    smoke_environment.add_argument("left", type=Path)
    smoke_environment.add_argument("right", type=Path)
    smoke_environment.set_defaults(handler=_smoke_environment_compare)

    smoke_index = commands.add_parser("smoke-run-index", help="index available Manila smoke evidence")
    smoke_index.add_argument("runner_directory", type=Path)
    smoke_index.add_argument("index", type=Path)
    smoke_index.set_defaults(handler=_smoke_run_index)

    smoke_verify = commands.add_parser("smoke-run-index-verify", help="verify indexed Manila smoke evidence")
    smoke_verify.add_argument("index", type=Path)
    smoke_verify.add_argument("artifact_directory", type=Path)
    smoke_verify.add_argument("--runner-layout", action="store_true", help="use runner directory names instead of artifact names")
    smoke_verify.set_defaults(handler=_smoke_run_index_verify)

    smoke_compare = commands.add_parser("smoke-run-index-compare", help="compare indexed evidence from two smoke runs")
    smoke_compare.add_argument("left_index", type=Path)
    smoke_compare.add_argument("left_artifacts", type=Path)
    smoke_compare.add_argument("right_index", type=Path)
    smoke_compare.add_argument("right_artifacts", type=Path)
    smoke_compare.add_argument("--left-runner-layout", action="store_true", help="use runner directory names on the left")
    smoke_compare.add_argument("--right-runner-layout", action="store_true", help="use runner directory names on the right")
    smoke_compare.set_defaults(handler=_smoke_run_index_compare)

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
    compare_parser.add_argument("--receipt-dir", type=Path, help="new folder for the local placement check receipt")
    compare_parser.add_argument("--noise-probability", type=float, help="synthetic local noise probability")
    compare_parser.add_argument("--ibm-model", choices=(*IBM_SNAPSHOT_MODELS, "all"),
                                help="offline snapshot simulation with the ibm package extra")
    compare_parser.add_argument("--native-qasm", action="store_true",
                                help="try Qiskit's native OpenQASM importer with --ibm-model")
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
    if args.workflow == "unknown-count-snapshot-profile-export":
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        print(json.dumps(result, indent=2, allow_nan=False))
    if args.workflow in ("constraint-regression", "diffuser-regression") and result["status"] != "REGRESSION_MATCHED":
        parser.exit(1)
    if args.workflow == "diffuser-benchmark" and result["status"] != "BENCHMARK_COMPLETED":
        parser.exit(1)
    if args.workflow == "smoke-run-index-verify" and result["status"] != "SMOKE_RUN_INDEX_VERIFIED":
        parser.exit(1)
    if args.workflow == "smoke-run-index-compare" and result["status"] != "SMOKE_RUN_INDEXES_COMPARED":
        parser.exit(1)
    if args.workflow == "snapshot-calibration-bundle-compose" and result["status"] != "COMPILATION_BUNDLE_COMPOSED":
        parser.exit(1)
    if (
        args.workflow == "snapshot-calibration-study" and args.resolve_compilation_overlap is not None
        and result["status"] != "COMPILATION_BUNDLE_COMPOSED"
    ):
        parser.exit(1)
    if args.workflow == "placement-receipt-replay" and result["status"] not in (
        "PLACEMENT_REPLAY_IDENTITIES_VERIFIED", "PLACEMENT_REPLAY_MATCHED",
    ):
        parser.exit(1)
