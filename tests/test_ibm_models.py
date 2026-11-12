import json
import sys
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from hashlib import sha256
from importlib.metadata import version
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.core import austin_predicate  # noqa: E402
from austins_algorithm.grover_search import mod4_range_predicate  # noqa: E402
from austins_algorithm.ibm_models import (  # noqa: E402
    compile_ibm_snapshot,
    run_ibm_snapshot_comparison,
    run_mod4_round_sweep,
    run_mod4_snapshot_comparison,
)


SNAPSHOT_COMPILER_DEPENDENCIES = all(
    find_spec(module) is not None for module in ("qiskit", "qiskit_ibm_runtime")
)
IBM_MODEL_DEPENDENCIES = all(
    find_spec(module) is not None
    for module in ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import")
)


class SnapshotInputTests(unittest.TestCase):
    def test_snapshot_compilation_rejects_invalid_model_and_seed_before_loading_backends(self):
        with patch("austins_algorithm.ibm_models._snapshot_specs") as load:
            for model, seed, message in (
                ("all", 7, "model"), ("fake_manila", 7, "model"),
                ("manila", -1, "seed"), ("manila", 2**32, "seed"), ("manila", True, "seed"),
            ):
                with self.subTest(model=model, seed=seed), self.assertRaisesRegex(ValueError, message):
                    compile_ibm_snapshot(None, model=model, seed=seed)
        load.assert_not_called()

    def test_comparison_runners_reject_nonboolean_importer_preference(self):
        from austins_algorithm.snapshot_placement import run_mod4_placement_check

        runners = (
            (run_ibm_snapshot_comparison, {}),
            (run_mod4_snapshot_comparison, {}),
            (run_mod4_round_sweep, {}),
            (run_mod4_placement_check, {"minimum_hit_rate": 0.4}),
        )
        with (
            patch("austins_algorithm.ibm_models._snapshot_specs") as load_comparison,
            patch("austins_algorithm.snapshot_placement._snapshot_specs") as load_placement,
        ):
            for runner, options in runners:
                for invalid in (1, "true"):
                    with (
                        self.subTest(runner=runner.__name__, preference=invalid),
                        self.assertRaisesRegex(ValueError, "prefer_native_qasm must be a boolean"),
                    ):
                        runner(**options, prefer_native_qasm=invalid)
        load_comparison.assert_not_called()
        load_placement.assert_not_called()

    def test_compare_cli_passes_importer_preference_to_every_snapshot_runner(self):
        from austins_algorithm.workflows_cli import main

        cases = (
            ("ibm_models.run_ibm_snapshot_comparison", []),
            ("ibm_models.run_mod4_snapshot_comparison", ["--problem", "mod4-range"]),
            ("ibm_models.run_mod4_round_sweep", ["--problem", "mod4-range", "--round-sweep"]),
            ("snapshot_placement.run_mod4_placement_check", [
                "--problem", "mod4-range", "--placement-check", "--minimum-hit-rate", "0.4",
            ]),
        )
        for name, options in cases:
            for flags in ([], ["--native-qasm"]):
                with (
                    self.subTest(runner=name, native=bool(flags)),
                    patch(f"austins_algorithm.{name}", return_value={"status": "COMPLETED"}) as run,
                    redirect_stdout(StringIO()),
                ):
                    main(["compare", "--ibm-model", "manila", "--shots", "2", *options, *flags])
                    run.assert_called_once()
                    self.assertEqual(run.call_args.kwargs["model"], "manila")
                    self.assertEqual(run.call_args.kwargs["shots"], 2)
                    self.assertIs(run.call_args.kwargs.get("prefer_native_qasm", False), bool(flags))

    def test_compare_cli_rejects_native_preference_before_starting_non_snapshot_experiments(self):
        from austins_algorithm.workflows_cli import main

        cases = (
            ([], "--native-qasm requires --ibm-model"),
            ([
                "--backend", "unused", "--account", "unused", "--instance", "unused",
                "--max-execution-seconds", "1",
            ], "--native-qasm requires --ibm-model"),
            (["--ibm-model", "manila", "--backend", "unused"], "cannot be combined with physical job options"),
        )
        with patch("austins_algorithm.device_experiment.run_austin_device_comparison") as run:
            for options, message in cases:
                with (
                    self.subTest(options=options),
                    redirect_stderr(StringIO()) as stderr,
                    self.assertRaises(SystemExit) as error,
                ):
                    main(["compare", "--native-qasm", *options])
                self.assertEqual(error.exception.code, 1)
                self.assertIn(message, stderr.getvalue())
        run.assert_not_called()

    def test_round_sweep_rejects_reused_seeds_and_out_of_bounds_shots(self):
        with self.assertRaisesRegex(ValueError, "seeds must differ"):
            run_mod4_round_sweep(calibration_seed=19, validation_seed=19)
        with self.assertRaisesRegex(ValueError, "round sweep shots"):
            run_mod4_round_sweep(shots=1025)

    def test_rejects_unknown_model_and_invalid_limits_before_optional_imports(self):
        for values in (
            {"model": "fake_manila"}, {"shots": 0}, {"shots": 4097},
            {"seed": -1}, {"seed": 2**32}, {"shots": True},
        ):
            for runner in (run_ibm_snapshot_comparison, run_mod4_snapshot_comparison):
                with self.subTest(values=values, runner=runner.__name__), self.assertRaises(ValueError):
                    runner(**values)


@unittest.skipUnless(SNAPSHOT_COMPILER_DEPENDENCIES, "optional IBM snapshot compiler tools are unavailable")
class SnapshotCompilerTests(unittest.TestCase):
    def test_snapshot_compilation_rejects_circuit_wider_than_backend_before_compiling(self):
        from qiskit import QuantumCircuit

        with (
            patch("austins_algorithm.ibm_models.compile_and_verify") as compile_circuit,
            self.assertRaisesRegex(ValueError, "width|qubit"),
        ):
            compile_ibm_snapshot(QuantumCircuit(6, 1), model="manila")
        compile_circuit.assert_not_called()

    @unittest.skipUnless(find_spec("qiskit_qasm3_import") is not None, "standard parser is unavailable")
    def test_snapshot_cli_preserves_each_export_and_its_analytic_measurements(self):
        from qiskit import qasm3
        from qiskit.exceptions import ExperimentalWarning

        from austins_algorithm.__main__ import main as cli_main
        from austins_algorithm.compilation import MEASUREMENT_TOLERANCE, _distance, _roundtrip_distribution

        austin_probabilities = {f"{index:03b}": (0.5 if index in (3, 5) else 0.0) for index in range(8)}
        # After two Grover rounds with two of sixteen states marked, each marked
        # state has probability 121/256 and each unmarked state has 1/256.
        mod4_probabilities = {f"{index:04b}": (121 if index in (9, 13) else 1) / 256 for index in range(16)}
        standard = "qiskit.qasm3.loads"
        native = "qiskit.qasm3.loads_experimental"
        loaders = {standard: qasm3.loads}
        if hasattr(qasm3, "loads_experimental"):
            loaders[native] = qasm3.loads_experimental
        # These exact named fixtures are known to load natively on the recorded
        # stack. Other supported versions can record verified standard fallback.
        require_native = version("qiskit") == "2.6.2" and version("qiskit-ibm-runtime") == "0.51.0"
        package = Path(__file__).resolve().parents[1]
        build = (package / ".build").resolve()
        self.assertTrue(build.is_relative_to(package))
        build.mkdir(exist_ok=True)
        cases = (
            ("manila", "austin", "direct", 3, austin_probabilities),
            ("jakarta", "mod4-range", "direct", 4, mod4_probabilities),
            ("torino", "mod4-range", "work-qubit", 5, mod4_probabilities),
        )
        with TemporaryDirectory(dir=build, prefix="qasm-target-test-") as directory, warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ExperimentalWarning, module=r"qiskit\.qasm3")
            for model, problem, oracle, input_width, expected in cases:
                exports = []
                snapshots = []
                for prefer_native in (False, True):
                    with self.subTest(model=model, problem=problem, oracle=oracle, native=prefer_native):
                        path = Path(directory) / f"{model}-{prefer_native}.qasm"
                        argv = [
                            "austins-algorithm", "compile", "--problem", problem, "--compile-target", model,
                            "--qasm-output", str(path),
                        ]
                        if problem == "mod4-range":
                            argv.extend(("--oracle", oracle))
                        if prefer_native:
                            argv.append("--native-qasm")
                        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as output:
                            cli_main()
                        report = json.loads(output.getvalue())
                        self.assertEqual(report["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
                        self.assertEqual(report["input"]["qubits"], input_width)
                        self.assertFalse(report["provider_job_submitted"])
                        self.assertFalse(report["quantum_hardware_used"])
                        snapshot = report["snapshot"]
                        self.assertEqual(snapshot["model"], model)
                        self.assertEqual(snapshot["backend_name"], report["target"]["name"])
                        self.assertRegex(snapshot["configuration_properties_sha256"], r"^[0-9a-f]{64}$")
                        self.assertIsNotNone(datetime.fromisoformat(snapshot["last_update_date"]).tzinfo)
                        snapshots.append(snapshot)
                        raw = path.read_bytes()
                        exports.append(raw)
                        info = report["openqasm"]
                        self.assertEqual(info["bytes"], len(raw))
                        self.assertEqual(info["sha256"], sha256(raw).hexdigest())
                        if prefer_native:
                            self.assertEqual(info["preferred_importer"], native)
                            if require_native:
                                self.assertEqual(info["importer"], native)
                        else:
                            self.assertEqual(info["importer"], standard)
                        active = info["export_wire_to_target_qubit"]
                        classical = info["classical_bit_to_target_qubit"]
                        self.assertEqual(active, sorted(set(active)))
                        self.assertTrue(all(0 <= wire < report["target"]["qubits"] for wire in active))
                        self.assertEqual(len(classical), len(next(iter(expected))))
                        parsed = loaders[info["importer"]](raw.decode("utf-8"))
                        observed = _roundtrip_distribution(
                            parsed, qubits=len(active), clbits=len(classical),
                            measured=[active.index(wire) for wire in classical],
                        )
                        self.assertLessEqual(_distance(expected, observed), MEASUREMENT_TOLERANCE)
                with self.subTest(model=model, paired=True):
                    self.assertEqual(len(exports), 2)
                    self.assertEqual(exports[0], exports[1])
                    self.assertEqual(snapshots[0], snapshots[1])


@unittest.skipUnless(IBM_MODEL_DEPENDENCIES, "optional IBM model tools are unavailable")
class SnapshotComparisonTests(unittest.TestCase):
    def test_all_models_preserve_the_ideal_circuit_and_verify_noisy_shots(self):
        report = run_ibm_snapshot_comparison(shots=64)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(report["classical"]["solutions"], ["011", "101"])
        self.assertEqual(report["ideal"]["verified_shots"], 64)
        self.assertEqual(set(report["models"]), {"manila", "jakarta", "torino"})
        for model, expected_method in (
            ("manila", "density_matrix"), ("jakarta", "density_matrix"), ("torino", "matrix_product_state"),
        ):
            with self.subTest(model=model):
                details = report["models"][model]
                self.assertEqual(details["status"], "COMPLETED")
                self.assertIsNotNone(datetime.fromisoformat(details["snapshot"]["last_update_date"]).tzinfo)
                self.assertFalse(details["provider_job_submitted"])
                self.assertFalse(details["quantum_hardware_used"])
                compilation = details["compilation"]
                self.assertEqual(compilation["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")
                self.assertGreater(compilation["compiled"]["two_qubit_gates"], 0)
                simulation = details["simulation"]
                self.assertEqual(simulation["method"], expected_method)
                self.assertIn("measure", simulation["noise_instructions"])
                self.assertLessEqual(simulation["simulated_qubits"], report["limits"]["active_qubits"])
                self.assertEqual(simulation["simulated_qubits"], len(details["active_target_qubits"]))
                self.assertEqual(simulation["completed_shots"], 64)
                self.assertEqual(
                    simulation["verified_shots"],
                    sum(count for bits, count in simulation["counts"].items() if austin_predicate(bits)),
                )
                lo, hi = simulation["verified_hit_rate_wilson_95"]
                self.assertLessEqual(lo, simulation["verified_hit_rate"])
                self.assertLessEqual(simulation["verified_hit_rate"], hi)
        self.assertGreater(report["models"]["torino"]["compilation"]["target"]["qubits"], 7)
        self.assertEqual(report["models"]["torino"]["simulation"]["max_bond_dimension"], 8)
        json.dumps(report, allow_nan=False)

    def test_active_wire_cap_keeps_compilation_and_skips_simulation(self):
        with patch("austins_algorithm.ibm_models.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 2):
            report = run_ibm_snapshot_comparison(model="manila", shots=2)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(list(report["models"]), ["manila"])
        self.assertEqual(report["models"]["manila"]["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        self.assertIsNone(report["models"]["manila"]["simulation"])

    def test_mod4_compares_both_implementations_under_one_snapshot(self):
        from austins_algorithm.ibm_models import _sample_snapshot, _snapshot_simulator

        with (
            patch("austins_algorithm.ibm_models._snapshot_simulator", wraps=_snapshot_simulator) as create,
            patch("austins_algorithm.ibm_models._sample_snapshot", wraps=_sample_snapshot) as sample,
        ):
            report = run_mod4_snapshot_comparison(model="jakarta", shots=64)
        create.assert_called_once()
        self.assertEqual(sample.call_count, 2)
        self.assertIs(sample.call_args_list[0].args[2], sample.call_args_list[1].args[2])
        self.assertEqual(report["status"], "COMPLETED")
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(report["grover_rounds"], 2)
        self.assertEqual(report["oracle_audits"]["direct"], report["oracle_audits"]["work-qubit"])
        self.assertEqual(report["oracle_audits"]["direct"]["marked_states"], ["1001", "1101"])
        model = report["models"]["jakarta"]
        reference, direct = (model["variants"][name] for name in ("work-qubit", "direct"))
        self.assertEqual(reference["snapshot"], direct["snapshot"])
        self.assertLess(model["ideal_roundtrip_total_variation_distance"], 1e-10)
        self.assertEqual(reference["compilation"]["input"]["qubits"], 5)
        self.assertEqual(direct["compilation"]["input"]["qubits"], 4)
        for result in (reference, direct):
            sample = result["simulation"]
            self.assertEqual(sample["completed_shots"], 64)
            self.assertEqual(
                sample["verified_shots"],
                sum(count for bits, count in sample["counts"].items() if mod4_range_predicate(bits)),
            )
        self.assertAlmostEqual(
            model["direct_minus_work_qubit_hit_rate"],
            direct["simulation"]["verified_hit_rate"] - reference["simulation"]["verified_hit_rate"],
        )
        json.dumps(report, allow_nan=False)

    def test_mod4_partial_comparison_omits_hit_rate_change(self):
        from austins_algorithm.ibm_models import _sample_snapshot, _snapshot_simulator

        with (
            patch("austins_algorithm.ibm_models.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 4),
            patch("austins_algorithm.ibm_models._snapshot_simulator", wraps=_snapshot_simulator) as create,
            patch("austins_algorithm.ibm_models._sample_snapshot", wraps=_sample_snapshot) as sample,
        ):
            report = run_mod4_snapshot_comparison(model="manila", shots=2)
        create.assert_called_once()
        sample.assert_called_once()
        self.assertEqual(sample.call_args.args[0].report["input"]["qubits"], 4)
        self.assertEqual(report["status"], "PARTIAL")
        model = report["models"]["manila"]
        self.assertEqual(model["variants"]["work-qubit"]["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        self.assertEqual(model["variants"]["direct"]["status"], "COMPLETED")
        self.assertIsNone(model["direct_minus_work_qubit_hit_rate"])

    def test_mod4_comparison_skips_simulator_when_both_variants_are_compile_only(self):
        with (
            patch("austins_algorithm.ibm_models.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 3),
            patch("austins_algorithm.ibm_models._snapshot_simulator") as create,
        ):
            report = run_mod4_snapshot_comparison(model="manila", shots=2)
        create.assert_not_called()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(
            [variant["status"] for variant in report["models"]["manila"]["variants"].values()],
            ["COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"] * 2,
        )

    def test_mod4_comparison_halts_after_unconfirmed_cancellation(self):
        from austins_algorithm.compilation import compile_and_verify
        from austins_algorithm.ibm_models import _snapshot_simulator

        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "simulation": None,
            "local_job_cancelled": False,
            "timing_seconds": {},
        }
        with (
            patch("austins_algorithm.ibm_models.compile_and_verify", wraps=compile_and_verify) as compile_circuit,
            patch("austins_algorithm.ibm_models._snapshot_simulator", wraps=_snapshot_simulator) as create,
            patch("austins_algorithm.ibm_models._sample_snapshot", return_value=timeout) as sample,
        ):
            report = run_mod4_snapshot_comparison(shots=2)
        compile_circuit.assert_called_once()
        create.assert_called_once()
        sample.assert_called_once()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertIs(report["local_cancellation_unconfirmed"], True)
        self.assertEqual(report["models_not_run"], ["jakarta", "torino"])
        self.assertEqual(list(report["models"]), ["manila"])
        model = report["models"]["manila"]
        self.assertEqual(model["status"], "PARTIAL")
        self.assertEqual(list(model["variants"]), ["work-qubit"])
        self.assertEqual(model["variants"]["work-qubit"]["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
        self.assertEqual(model["variants_not_run"], ["direct"])
        self.assertIsNone(model["ideal_roundtrip_total_variation_distance"])
        self.assertIsNone(model["direct_minus_work_qubit_hit_rate"])

    def test_round_sweep_holds_placement_and_uses_fresh_validation_shots(self):
        from qiskit import transpile

        from austins_algorithm.compilation import _transpile_initial_layout, compile_and_verify

        with (
            patch("austins_algorithm.ibm_models._transpile_initial_layout", wraps=_transpile_initial_layout) as discover,
            patch("austins_algorithm.ibm_models.compile_and_verify", wraps=compile_and_verify) as compile_circuit,
        ):
            report = run_mod4_round_sweep(
                model="jakarta", shots=64, transpiler_seed=23, prefer_native_qasm=True,
            )
        discover.assert_called_once()
        anchor, = discover.call_args.args
        backend = discover.call_args.kwargs["backend"]
        self.assertEqual(discover.call_args.kwargs["seed"], 23)
        reference = transpile(anchor, backend=backend, optimization_level=1, seed_transpiler=23)
        layout = reference.layout.initial_index_layout(filter_ancillas=True)
        self.assertEqual(compile_circuit.call_count, 4)
        for call in compile_circuit.call_args_list:
            self.assertEqual(call.kwargs["initial_layout"], layout)
            self.assertIs(call.kwargs["prefer_native_qasm"], True)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["ideal_rounds"], 2)
        self.assertEqual(report["round_candidates"], [0, 1, 2, 3])
        self.assertFalse(report["provider_job_submitted"])
        result = report["models"]["jakarta"]
        self.assertEqual(result["initial_logical_to_target_qubit"], layout)
        self.assertEqual(set(result["calibration"]), {0, 1, 2, 3})
        self.assertEqual(set(result["validation"]), {result["selected_rounds"], 2})
        for compiled in result["compilations"].values():
            self.assertEqual(
                compiled["initial_logical_to_target_qubit"], result["initial_logical_to_target_qubit"]
            )
            self.assertEqual(compiled["openqasm"]["preferred_importer"], "qiskit.qasm3.loads_experimental")
            if version("qiskit") == "2.6.2" and version("qiskit-ibm-runtime") == "0.51.0":
                self.assertEqual(compiled["openqasm"]["importer"], "qiskit.qasm3.loads_experimental")
        for stage in ("calibration", "validation"):
            for trial in result[stage].values():
                self.assertEqual(trial["simulation"]["completed_shots"], 64)
        json.dumps(report, allow_nan=False)

    def test_round_selection_does_not_change_after_validation(self):
        def sample(hits):
            return {"status": "COMPLETED", "simulation": {"verified_shots": hits, "verified_hit_rate": hits / 64}}

        with patch("austins_algorithm.ibm_models._sample_snapshot", side_effect=map(sample, (10, 40, 40, 20, 1, 50))) as run:
            report = run_mod4_round_sweep(model="manila", shots=64, calibration_seed=11, validation_seed=23)
        model = report["models"]["manila"]
        self.assertEqual(model["selected_rounds"], 1)
        self.assertLess(model["validation_selected_minus_ideal_hit_rate"], 0)
        calls = run.call_args_list
        self.assertEqual([call.kwargs["seed"] for call in calls], [11, 11, 11, 11, 23, 23])
        self.assertIs(calls[1].args[0], calls[4].args[0])
        self.assertIs(calls[2].args[0], calls[5].args[0])

    def test_round_sweep_does_not_launch_more_work_after_uncancelled_timeout(self):
        timeout = {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None, "local_job_cancelled": False}
        with patch("austins_algorithm.ibm_models._sample_snapshot", return_value=timeout) as run:
            report = run_mod4_round_sweep(shots=2)
        run.assert_called_once()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["models_not_run"], ["jakarta", "torino"])
        model = report["models"]["manila"]
        self.assertIsNone(model["selected_rounds"])
        self.assertIsNone(model["validation_selected_minus_ideal_hit_rate"])


if __name__ == "__main__":
    unittest.main()
