"""Preserve calibration evidence and stop behavior with bounded Aer density batches."""

from base64 import b64decode, b64encode
from concurrent.futures import TimeoutError
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from importlib.util import find_spec
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import ibm_models, snapshot_calibration_study as study
from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm.__main__ import main
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_bundle_overlap import _argv
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_overlap_receipt import _receipt_args


_AER_MODULES = ("qiskit", "qiskit_aer")
_TWO_ROUNDS = {"target_probability": .7, "shot_caps": [2], "query_caps": [1]}
_TIMEOUT = "LOCAL_RESULT_WAIT_TIMED_OUT"


def _without_batch_marker(study_report):
    report = deepcopy(study_report)
    del report["inputs"]["batch_density"]
    for layout, capture in report["captures"].items():
        del capture["report"]["batch_density"]
        capture["report_sha256"] = _digest(capture["report"])
        report["planning"]["layouts"][layout]["source_capture_sha256"] = capture["report_sha256"]
    return report


class SnapshotCalibrationDensityBatchInputTests(unittest.TestCase):
    def test_requires_boolean_batch_flag_before_archives(self):
        archive = comparison.SnapshotCalibrationFiles("config.json", "props.json", "a" * 64, "b" * 64)
        with patch.object(comparison, "_read_archive") as read:
            for bad in (1, "batch", None):
                with self.subTest(flag=bad):
                    with self.assertRaisesRegex((TypeError, ValueError), "batch_density"):
                        comparison.compare_snapshot_calibrations(
                            _MARK_00, archive, archive, initial_layout=(0, 1), batch_density=bad,
                        )
                    with self.assertRaisesRegex((TypeError, ValueError), "batch_density"):
                        study.run_snapshot_calibration_study(
                            _MARK_00, archive, archive, initial_layouts=[(0, 1)], batch_density=bad, **_TWO_ROUNDS,
                        )
        read.assert_not_called()

    def test_groups_contiguous_simulator_identities_and_splits_at_three(self):
        same = SimpleNamespace(tag="identical options")
        other = SimpleNamespace(tag="identical options")
        self.assertEqual(same, other)
        compact = dict(enumerate(zip(range(7), (same, same, same, same, other, same, same), strict=True)))
        prepared = {"status": "COMPLETED"}
        calls = []

        def single(circuit, simulator, backend_name):
            calls.append((circuit,))
            return (circuit, simulator), prepared

        def batch(circuits, simulator, backend_name):
            calls.append(tuple(circuits))
            return [(circuit, simulator) for circuit in circuits], prepared

        with (
            patch.object(comparison, "_prepare_density_snapshot", side_effect=single),
            patch.object(comparison, "_prepare_density_snapshot_batch", side_effect=batch),
        ):
            groups = list(comparison._calibration_density_rounds(compact, "earlier", batch_density=True))
            self.assertEqual(calls, [(0, 1, 2), (3,), (4,), (5, 6)])
            calls.clear()
            ordinary = list(comparison._calibration_density_rounds(compact, "earlier", batch_density=False))
            self.assertEqual(calls, [(index,) for index in range(7)])

        self.assertEqual([indices for indices, _, _ in groups], [(0, 1, 2), (3,), (4,), (5, 6)])
        flattened = [replay for _, replays, _ in groups for replay in replays]
        for (index, simulator), (wanted_index, wanted_simulator) in zip(flattened, compact.values(), strict=True):
            self.assertEqual(index, wanted_index)
            self.assertIs(simulator, wanted_simulator)
        self.assertEqual([indices for indices, _, _ in ordinary], [(index,) for index in range(7)])

    def test_cli_rejects_batching_in_key_coverage_without_loading_the_bundle(self):
        directory = _test_directory(self)
        archive = comparison.SnapshotCalibrationFiles("config.json", "props.json", "a" * 64, "b" * 64)
        argv = _argv(directory, archive, archive, [])
        argv.extend(("--batch-density", "--load-compilation-bundle", "missing.json", "c" * 64,
                     "--check-compilation-bundle"))
        with (
            patch.object(SnapshotCalibrationCompilationCache, "load_bundle") as load,
            patch.object(sys, "argv", argv), redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as rejected,
        ):
            main()
        self.assertNotEqual(rejected.exception.code, 0)
        self.assertIn("--batch-density requires a study that runs snapshot simulations", stderr.getvalue())
        load.assert_not_called()


@unittest.skipUnless(all(find_spec(name) is not None for name in _AER_MODULES), "optional Aer tools unavailable")
class SnapshotDensityBatchHelperTests(unittest.TestCase):
    def setUp(self):
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError, pauli_error

        self.circuits = []
        for index in range(3):
            circuit = QuantumCircuit(3, 2, name="repeated-circuit-name")
            circuit.h(0)
            circuit.cx(0, 1)
            if index:
                circuit.x(2)
            circuit.h(2)
            circuit.measure(2, 1)
            if index == 2:
                circuit.x(0)
            circuit.measure(0, 0)
            self.circuits.append(circuit)
        noise = NoiseModel()
        noise.add_quantum_error(pauli_error([("X", .125), ("I", .875)]), "h", [2])
        noise.add_readout_error(ReadoutError([[.75, .25], [.125, .875]]), [2])
        noise.add_readout_error(ReadoutError([[.875, .125], [.0625, .9375]]), [0])
        self.simulator = AerSimulator(method="density_matrix", noise_model=noise, max_parallel_threads=1)

    def test_one_serial_batch_job_preserves_exact_ordered_density_and_readout(self):
        import numpy as np
        from qiskit_aer.jobs.aerjob import AerJob

        singles = [ibm_models._prepare_density_snapshot(circuit, self.simulator, "fixture")[0]
                   for circuit in self.circuits]
        with (
            patch.object(self.simulator, "run", wraps=self.simulator.run) as run,
            patch.object(AerJob, "result", autospec=True, side_effect=AerJob.result) as wait,
        ):
            replays, preparation = ibm_models._prepare_density_snapshot_batch(self.circuits, self.simulator, "fixture")
        run.assert_called_once()
        wait.assert_called_once()
        self.assertEqual(wait.call_args.kwargs["timeout"], ibm_models.LOCAL_JOB_WAIT_SECONDS)
        self.assertEqual(run.call_args.kwargs, {"shots": 1, "seed_simulator": 0, "max_parallel_experiments": 1})
        submitted = run.call_args.args[0]
        self.assertEqual(len(submitted), 3)
        self.assertEqual(len({body.name for body in submitted}), 3)
        self.assertEqual([body.metadata["snapshot_density_batch_index"] for body in submitted], [0, 1, 2])
        self.assertTrue(all("measure" not in body.count_ops() for body in submitted))
        self.assertEqual([circuit.name for circuit in self.circuits], ["repeated-circuit-name"] * 3)
        self.assertTrue(all(not circuit.metadata for circuit in self.circuits))
        self.assertEqual(preparation["status"], "COMPLETED")
        for index, ((batch_circuit, batch_sampler), (single_circuit, single_sampler)) in enumerate(
            zip(replays, singles, strict=True),
        ):
            with self.subTest(experiment=index):
                np.testing.assert_array_equal(batch_circuit.data[0].operation.params[0],
                                              single_circuit.data[0].operation.params[0])
                self.assertEqual(batch_circuit.data[1:], single_circuit.data[1:])
                self.assertEqual(batch_sampler.options.noise_model.to_dict(),
                                 single_sampler.options.noise_model.to_dict())
                verify = lambda bits: bits == "10"
                self.assertEqual(ibm_models._prepared_snapshot_probabilities(batch_circuit, batch_sampler, verifier=verify),
                                 ibm_models._prepared_snapshot_probabilities(single_circuit, single_sampler, verifier=verify))

    def test_rejects_late_experiment_metadata_state_or_count_failures(self):
        import numpy as np
        from qiskit_aer.jobs.aerjob import AerJob

        original = AerJob.result

        def change_name(experiment):
            if isinstance(experiment.header, dict):
                experiment.header["name"] = "unexpected-experiment"
            else:
                experiment.header.name = "unexpected-experiment"

        def change_state(experiment):
            keys = [key for key in experiment.data.to_dict() if "density" in key]
            [key] = keys
            setattr(experiment.data, key, np.zeros((8, 8), dtype=complex))

        mutations = {
            "result_count": lambda result: result.results.pop(),
            "result_success": lambda result: setattr(result, "success", False),
            "late_success": lambda result: setattr(result.results[-1], "success", False),
            "late_name": lambda result: change_name(result.results[-1]),
            "late_index": lambda result: result.results[-1].header["metadata"].update(snapshot_density_batch_index=True),
            "late_method": lambda result: result.results[-1].metadata.update(method="statevector"),
            "late_wires": lambda result: result.results[-1].metadata.update(active_input_qubits=[0, 2]),
            "late_state": lambda result: change_state(result.results[-1]),
        }
        for label, mutate in mutations.items():
            def corrupt(job, *args, **options):
                result = original(job, *args, **options)
                mutate(result)
                return result

            with (
                self.subTest(validation=label),
                patch.object(AerJob, "result", autospec=True, side_effect=corrupt),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                ibm_models._prepare_density_snapshot_batch(self.circuits, self.simulator, "fixture")

    def test_timeout_uses_one_wait_and_one_cancellation_for_all_experiments(self):
        for cancelled in (False, True, None):
            simulator = Mock()
            simulator.options = self.simulator.options
            job = simulator.run.return_value
            job.result.side_effect = TimeoutError
            if cancelled is None:
                job.cancel.side_effect = RuntimeError("cancellation failed")
            else:
                job.cancel.return_value = cancelled
            with self.subTest(cancelled=cancelled):
                replays, preparation = ibm_models._prepare_density_snapshot_batch(self.circuits, simulator, "fixture")
                self.assertIsNone(replays)
                self.assertEqual(preparation["status"], _TIMEOUT)
                self.assertIs(preparation["local_job_cancelled"], cancelled is True)
                if cancelled is None:
                    self.assertEqual(preparation["local_job_cancellation_error"], "RuntimeError")
                else:
                    self.assertNotIn("local_job_cancellation_error", preparation)
                simulator.run.assert_called_once()
                job.result.assert_called_once_with(timeout=ibm_models.LOCAL_JOB_WAIT_SECONDS)
                job.cancel.assert_called_once_with()

    def test_batch_bounds_and_quantum_measurement_noise_are_rejected_before_run(self):
        from qiskit_aer.noise import NoiseModel, pauli_error

        self.assertEqual(ibm_models._MAX_DENSITY_BATCH_ROUNDS, 3)
        simulator = Mock()
        simulator.options = self.simulator.options
        for invalid in ([], self.circuits[:1], [*self.circuits, self.circuits[0]]):
            with self.subTest(count=len(invalid)), self.assertRaises(ValueError):
                ibm_models._prepare_density_snapshot_batch(invalid, simulator, "fixture")
        noise = NoiseModel()
        noise.add_quantum_error(pauli_error([("X", 1.0)]), "measure", [0])
        simulator.options = SimpleNamespace(noise_model=noise)
        with self.assertRaisesRegex(ValueError, "static gate noise"):
            ibm_models._prepare_density_snapshot_batch(self.circuits, simulator, "fixture")
        simulator.run.assert_not_called()


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationDensityBatchTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "configuration.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        later = self.directory / "later.json"
        later.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, later)
        properties = json.loads(later.read_bytes())
        properties["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier = self.directory / "earlier.json"
        earlier.write_text(json.dumps(properties), encoding="utf-8")
        self.earlier = _pinned(configuration, earlier)

    def test_dated_study_keeps_scientific_reports_and_compilation_cache_keys(self):
        constraint = BooleanConstraint(4, ((1, 2), (-1,)))
        options = {
            "initial_layouts": [(0, 1, 2, 3, 5), (1, 2, 3, 5, 0)], "prefer_native_qasm": True,
            "target_probability": .7, "shot_caps": [8], "query_caps": [3],
        }
        cache = SnapshotCalibrationCompilationCache()
        sequential = study.run_snapshot_calibration_study(
            constraint, self.earlier, self.later, compilation_cache=cache, **options,
        )
        self.assertEqual(sequential["status"], "CALIBRATION_STUDY_COMPLETED")
        self.assertEqual(sequential["required_rounds"], [0, 1, 2, 3])
        self.assertNotIn("batch_density", sequential["inputs"])
        self.assertEqual(cache.stats()["entries"], 8)
        with (
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot", wraps=comparison._prepare_density_snapshot) as single,
            patch.object(comparison, "_prepare_density_snapshot_batch",
                         wraps=comparison._prepare_density_snapshot_batch) as batch,
        ):
            batched = study.run_snapshot_calibration_study(
                constraint, self.earlier, self.later, compilation_cache=cache, batch_density=True, **options,
            )
        compile_round.assert_not_called()
        self.assertEqual(cache.stats()["hits"], 8)
        self.assertEqual(single.call_count, 4)
        self.assertEqual([len(call.args[0]) for call in batch.call_args_list], [3, 3, 3, 3])
        self.assertTrue(batched["inputs"]["batch_density"])
        self.assertEqual(json.dumps(_without_batch_marker(batched)), json.dumps(sequential))

    def test_timeout_marks_entire_group_without_fallback_or_cache_publication(self):
        options = {"initial_layouts": [(0, 1), (1, 0)], "prefer_native_qasm": True, **_TWO_ROUNDS}
        prepare = comparison._prepare_density_snapshot_batch
        for cancelled in (False, True):
            attempts = []

            def first_timeout(circuits, simulator, backend_name):
                attempts.append(len(circuits))
                if len(attempts) == 1:
                    evidence = {"status": _TIMEOUT, "local_job_cancelled": cancelled}
                    if not cancelled:
                        evidence["local_job_cancellation_error"] = "RuntimeError"
                    return None, evidence
                return prepare(circuits, simulator, backend_name)

            cache = SnapshotCalibrationCompilationCache()
            with (
                self.subTest(cancelled=cancelled),
                patch.object(comparison, "_prepare_density_snapshot_batch", side_effect=first_timeout),
                patch.object(comparison, "_prepare_density_snapshot") as singleton,
            ):
                result = study.run_snapshot_calibration_study(
                    _MARK_00, self.earlier, self.later, compilation_cache=cache, batch_density=True, **options,
                )
                singleton.assert_not_called()
                self.assertEqual(attempts, [2, 2, 2] if cancelled else [2])
                self.assertEqual(result["status"], "CALIBRATION_STUDY_PARTIAL" if cancelled else "CALIBRATION_STUDY_UNAVAILABLE")
                self.assertEqual(result["coverage"]["unattempted_layouts"], [] if cancelled else ["1,0"])
                self.assertEqual(result["coverage"]["completed_layouts"], ["1,0"] if cancelled else [])
                self.assertEqual(result["coverage"]["unavailable_layouts"], ["0,1"])
                capture = result["captures"]["0,1"]["report"]
                self.assertEqual(capture["status"], _TIMEOUT)
                self.assertEqual(capture["blocked_snapshot"], "earlier")
                self.assertEqual(capture["blocked_rounds"], [0, 1])
                self.assertNotIn("blocked_round", capture)
                self.assertIs(capture["local_job_cancelled"], cancelled)
                self.assertEqual(capture.get("local_job_cancellation_error"), None if cancelled else "RuntimeError")
                self.assertTrue(all("earlier" not in row and "later" not in row for row in capture["rounds"].values()))
                self.assertEqual(cache.stats()["entries"], 2 if cancelled else 0)

    def test_existing_overlap_receipt_replays_unchanged_before_opt_in_batched_study(self):
        from qiskit import qpy

        cache = SnapshotCalibrationCompilationCache()
        comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier, self.later, initial_layout=(0, 1), max_rounds=0, compilation_cache=cache,
            prefer_native_qasm=True,
        )
        first = self.directory / "first.json"
        first_pin = cache.save_bundle(first)
        document = json.loads(first.read_bytes())
        [row] = document["entries"].values()
        [circuit] = qpy.load(BytesIO(b64decode(row["qpy"])))
        circuit.metadata = {"batch_fixture": "equivalent alternate encoding"}
        encoded = BytesIO()
        qpy.dump(circuit, encoded)
        row["qpy"] = b64encode(encoded.getvalue()).decode("ascii")
        second = self.directory / "second.json"
        sources = [(first, first_pin), (second, _write_report(second, document))]
        output = self.directory / "composed.json"
        options = {"initial_layouts": [(0, 1)], "prefer_native_qasm": True, **_TWO_ROUNDS}
        receipt = study.compose_snapshot_calibration_compilation_for_study(
            _MARK_00, self.earlier, self.later, destination=output, bundles=sources, **options,
        )
        self.assertEqual(receipt["status"], "COMPILATION_BUNDLE_COMPOSED")
        self.assertNotIn("batch_density", receipt["study_overlap"]["request"]["inputs"])
        path = self.directory / "receipt.json"
        pin = _write_report(path, receipt)
        evidence = _receipt_args((path, pin), (output, receipt["output"]["sha256"]), sources)
        expected = study.run_snapshot_calibration_study_with_overlap_receipt(
            _MARK_00, self.earlier, self.later, **evidence, **options,
        )
        argv = _argv(self.directory, self.earlier, self.later, [])
        argv[argv.index("--shot-caps") + 1] = "2"
        argv[argv.index("--query-caps") + 1] = "1"
        argv.extend((
            "--native-qasm", "--batch-density", "--run-after-overlap-replay", "--replay-overlap-receipt", str(path), pin,
            "--load-compilation-bundle", str(output), receipt["output"]["sha256"],
        ))
        for source, digest in sources:
            argv.extend(("--overlap-source-bundle", str(source), digest))
        with (
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
            patch.object(comparison, "_prepare_density_snapshot_batch",
                         wraps=comparison._prepare_density_snapshot_batch) as batch,
        ):
            main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(batch.call_count, 2)
        self.assertEqual(result["status"], "COMPILATION_OVERLAP_RECEIPT_STUDY_COMPLETED")
        self.assertEqual(json.dumps(result["replay"]), json.dumps(expected["replay"]))
        self.assertEqual(json.dumps(_without_batch_marker(result["study"])), json.dumps(expected["study"]))


if __name__ == "__main__":
    unittest.main()
