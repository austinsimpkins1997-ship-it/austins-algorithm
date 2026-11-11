"""Check matched snapshot sampling of audited, already compiled phase covers."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.compilation import compile_and_verify  # noqa: E402
from austins_algorithm.constraint_cover_comparison import run_constraint_cover_snapshot  # noqa: E402
from austins_algorithm.constraints import BooleanConstraint  # noqa: E402


MODULE = "austins_algorithm.constraint_cover_comparison"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))
CONSTRAINT = BooleanConstraint(2, ((1, 2), (-1, -2)))
LAYOUT = (0, 1)
SEEDS = (11, 17, 23, 31)


def _completed(shots: int, hits: int) -> dict:
    return {
        "status": "COMPLETED",
        "simulation": {
            "requested_shots": shots,
            "completed_shots": shots,
            "verified_shots": hits,
            "verified_hit_rate": hits / shots,
            "counts": {"00": shots - hits, "01": hits},
        },
        "timing_seconds": {},
    }


class ConstraintCoverSnapshotInputTests(unittest.TestCase):
    def test_invalid_sampling_requests_fail_before_setup(self):
        for invalid in (
            {"model": "all"},
            {"model": "not-a-snapshot"},
            {"initial_layout": None},
            {"shots": 0},
            {"shots": True},
            {"shots": 257},
            {"evaluation_seeds": (11, 17, 23)},
            {"evaluation_seeds": (11, 17, 23, 11)},
            {"evaluation_seeds": (11, 17, 23, True)},
            {"evaluation_seeds": (11, 17, 23, 2**32)},
            {"simulation_method": "statevector"},
        ):
            kwargs = {"model": "manila", "initial_layout": LAYOUT, **invalid}
            with (
                self.subTest(invalid=invalid),
                patch(f"{MODULE}._snapshot_specs") as backends,
                patch(f"{MODULE}.compare_constraint_phase_covers") as compare,
                patch(f"{MODULE}._snapshot_simulator") as simulator,
            ):
                with self.assertRaises(ValueError):
                    run_constraint_cover_snapshot(CONSTRAINT, **kwargs)
                backends.assert_not_called()
                compare.assert_not_called()
                simulator.assert_not_called()


@unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
class ConstraintCoverSnapshotTests(unittest.TestCase):
    def test_reuses_verified_programs_and_keeps_gate_selection_separate(self):
        from scipy.stats import binomtest

        programs = {}
        compiler_backend = None
        simulator = object()

        def compile_one(circuit, **kwargs):
            nonlocal compiler_backend
            self.assertEqual(kwargs["initial_layout"], LAYOUT)
            if compiler_backend is None:
                compiler_backend = kwargs["backend"]
            self.assertIs(kwargs["backend"], compiler_backend)
            program = compile_and_verify(circuit, **kwargs)
            cover = ("direct", "complement")[len(programs)]
            programs[cover] = program
            return program

        def gate_cost(cover):
            report = programs[cover].report
            return (
                report["compiled"]["two_qubit_gates"],
                report["compiled"]["depth"],
                len(report["openqasm"]["export_wire_to_target_qubit"]),
                cover != "direct",
            )

        def sample(compiled, backend, sampler, *, method, shots, seed, verifier):
            self.assertIs(backend, compiler_backend)
            self.assertIs(sampler, simulator)
            self.assertEqual(method, "density_matrix")
            self.assertEqual(shots, 32)
            self.assertTrue(verifier("01"))
            self.assertFalse(verifier("00"))
            cover = "direct" if seed in SEEDS[::2] else "complement"
            self.assertIs(compiled, programs[cover])
            return _completed(shots, 2 if cover == min(programs, key=gate_cost) else 30)

        with (
            patch(f"{MODULE}.compile_and_verify", side_effect=compile_one) as compiler,
            patch(f"{MODULE}._snapshot_simulator", return_value=simulator) as create,
            patch(f"{MODULE}._sample_snapshot", side_effect=sample) as sampler,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=32, evaluation_seeds=SEEDS,
            )

        self.assertEqual(compiler.call_count, 2)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2], "density_matrix")
        self.assertEqual(sampler.call_count, 4)
        report = result.report
        snapshot = report["snapshot"]
        self.assertEqual(snapshot["status"], "COMPLETED")
        self.assertEqual(snapshot["method"], "density_matrix")
        winner = min(programs, key=gate_cost)
        self.assertEqual(report["selected_cover"], winner)
        self.assertIs(result.selected, programs[winner])
        self.assertNotEqual(snapshot["observed_hit_leader"], winner)
        self.assertFalse(snapshot["local_cancellation_unconfirmed"])

        summaries = {}
        for cover, seeds in (("direct", SEEDS[::2]), ("complement", SEEDS[1::2])):
            program = programs[cover]
            self.assertIs(result.programs[cover], program)
            self.assertEqual(program.report["initial_logical_to_target_qubit"], list(LAYOUT))
            row = snapshot["candidates"][cover]
            self.assertEqual(row["compilation_sha256"], hashlib.sha256(program.openqasm.encode("utf-8")).hexdigest())
            self.assertEqual(tuple(batch["seed"] for batch in row["batches"]), seeds)
            summary = row["summary"]
            self.assertEqual(summary["completed_shots"], 64)
            self.assertEqual(summary["verified_shots"], 4 if cover == winner else 60)
            summaries[cover] = binomtest(summary["verified_shots"], 64).proportion_ci(
                confidence_level=0.975, method="exact",
            )

        direct, complement = summaries["direct"], summaries["complement"]
        difference = snapshot["complement_minus_direct"]
        self.assertAlmostEqual(difference["estimate"], 56 / 64 if winner == "direct" else -56 / 64)
        self.assertAlmostEqual(difference["interval"][0], complement.low - direct.high)
        self.assertAlmostEqual(difference["interval"][1], complement.high - direct.low)
        json.dumps(report, allow_nan=False)

    def test_explicit_simulation_methods_reach_setup_sampling_and_report(self):
        for method in ("density_matrix", "matrix_product_state"):
            with (
                self.subTest(method=method),
                patch(f"{MODULE}._snapshot_simulator") as create,
                patch(f"{MODULE}._sample_snapshot", return_value=_completed(4, 2)) as sample,
            ):
                result = run_constraint_cover_snapshot(
                    CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=4, simulation_method=method,
                )
                create.assert_called_once()
                self.assertEqual(create.call_args.args[2], method)
                self.assertEqual(sample.call_count, 4)
                self.assertTrue(all(call.kwargs["method"] == method for call in sample.call_args_list))
                self.assertEqual(result.report["snapshot"]["method"], method)

    def test_compact_snapshot_remaps_directed_noise_and_readout(self):
        from types import SimpleNamespace

        from qiskit import QuantumCircuit
        from qiskit.circuit import Measure
        from qiskit.circuit.library import CZGate
        from qiskit.transpiler import InstructionProperties, Target
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError, pauli_error

        from austins_algorithm.compilation import CompiledProgram
        from austins_algorithm.ibm_models import _compact_snapshot_inputs, _sample_snapshot

        circuit = QuantumCircuit(8, 2)
        circuit.cz(5, 2)
        circuit.measure(5, 0)
        circuit.measure(2, 1)
        program = CompiledProgram(circuit, "", {"openqasm": {
            "export_wire_to_target_qubit": [2, 5],
            "classical_bit_to_target_qubit": [5, 2],
        }})
        target = Target(num_qubits=8)
        target.add_instruction(CZGate(), {(5, 2): InstructionProperties(error=0.02)})
        target.add_instruction(Measure(), {(2,): InstructionProperties(error=0.25), (5,): InstructionProperties(error=0.05)})
        backend = SimpleNamespace(name="ordered-noise-test", target=target, operation_names=list(target.operation_names))
        channel = pauli_error([("IX", 1.0)])
        readout = ReadoutError([[0.75, 0.25], [0.125, 0.875]])
        noise = NoiseModel(basis_gates=backend.operation_names)
        noise.add_quantum_error(channel, "cz", [5, 2])
        noise.add_readout_error(readout, [2])
        other_readout = ReadoutError([[0.9, 0.1], [0.0, 1.0]])
        noise.add_readout_error(other_readout, [5])

        other = QuantumCircuit(8, 2)
        other.measure(6, 0)
        other.measure(5, 1)
        other_program = CompiledProgram(other, "", {"openqasm": {
            "export_wire_to_target_qubit": [5, 6],
            "classical_bit_to_target_qubit": [6, 5],
        }})

        def gate_errors(*, target):
            return [("cz", qubits, channel) for qubits in target.get("cz", {})]

        with (
            patch("qiskit_aer.noise.device.basic_device_gate_errors", side_effect=gate_errors) as derive,
            patch(
                "qiskit_aer.noise.device.basic_device_readout_errors",
                return_value=[([0], readout), ([1], other_readout)],
            ),
        ):
            inputs = _compact_snapshot_inputs(
                backend, {"direct": program, "complement": program, "other": other_program}, AerSimulator,
            )
        derive.assert_called_once()
        derived_target = derive.call_args.kwargs["target"]
        self.assertEqual(derived_target.num_qubits, 3)
        self.assertEqual(list(derived_target["cz"]), [(1, 0)])
        compact, simulator = inputs["direct"]
        self.assertIs(simulator, inputs["complement"][1])
        self.assertIsNot(simulator, inputs["other"][1])
        other_noise = inputs["other"][1].options.noise_model
        self.assertEqual(other_noise.noise_instructions, ["measure"])
        self.assertEqual(other_noise.noise_qubits, [0])

        physical = AerSimulator(method="density_matrix", noise_model=noise, max_parallel_threads=1)
        options = {"method": "density_matrix", "shots": 64, "seed": 43, "verifier": lambda bits: bits == "01"}
        reference = _sample_snapshot(program, backend, physical, **options)["simulation"]
        candidate = _sample_snapshot(program, backend, simulator, sampling_circuit=compact, **options)["simulation"]
        self.assertEqual(set(candidate["counts"]), {"01", "11"})
        self.assertEqual(candidate["counts"], reference["counts"])
        self.assertEqual(candidate["verified_candidates"], reference["verified_candidates"])
        self.assertEqual(candidate["simulated_qubits"], 2)

    def test_reduced_noise_target_keeps_calibration_properties_and_skips_ideal_global_operations(self):
        from qiskit.circuit import Delay, Measure
        from qiskit.circuit.library import CZGate, XGate
        from qiskit.providers import QubitProperties
        from qiskit.transpiler import InstructionProperties, Target
        from qiskit_aer.noise.device import basic_device_readout_errors

        from austins_algorithm.ibm_models import _reduced_noise_target

        class AsymmetricReadout(InstructionProperties):
            def __init__(self):
                super().__init__(error=0.2)
                self.prob_meas1_prep0 = 0.3
                self.prob_meas0_prep1 = 0.1

        physical = Target(num_qubits=8, dt=2e-9, qubit_properties=[
            QubitProperties(t1=(100 + index) * 1e-6, t2=80e-6) for index in range(8)
        ])
        physical.add_instruction(CZGate(), {
            (5, 2): InstructionProperties(duration=2e-7, error=0.02),
            (2, 5): InstructionProperties(duration=1e-7, error=0.01),
            (6, 2): InstructionProperties(duration=3e-7, error=0.03),
        })
        physical.add_instruction(Measure(), {(5,): AsymmetricReadout()})
        physical.add_instruction(XGate())
        physical.add_instruction(Delay(1))

        reduced = _reduced_noise_target(physical, (2, 5))
        self.assertEqual(reduced.num_qubits, 2)
        self.assertEqual(reduced.dt, physical.dt)
        self.assertEqual([item.t1 for item in reduced.qubit_properties],
                         [physical.qubit_properties[index].t1 for index in (2, 5)])
        self.assertEqual(set(reduced.operation_names), {"cz", "measure"})
        self.assertEqual(list(reduced["cz"]), [(1, 0), (0, 1)])
        self.assertEqual(reduced["cz"][(1, 0)].duration, physical["cz"][(5, 2)].duration)
        self.assertEqual(reduced["cz"][(0, 1)].error, physical["cz"][(2, 5)].error)
        [(qubits, error)] = basic_device_readout_errors(target=reduced)
        self.assertEqual(qubits, [1])
        self.assertEqual(error.probabilities.tolist(), [[0.7, 0.3], [0.1, 0.9]])

        global_target = Target(num_qubits=8)
        global_target.add_instruction(XGate(), {None: InstructionProperties(error=0.01)})
        with self.assertRaisesRegex(ValueError, "explicit wire tuples"):
            _reduced_noise_target(global_target, (2, 5))

    def test_compact_snapshot_rejects_nonterminal_and_changed_maps_before_model_setup(self):
        from types import SimpleNamespace

        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator

        from austins_algorithm.compilation import CompiledProgram
        from austins_algorithm.ibm_models import _compact_snapshot_inputs

        terminal = QuantumCircuit(3, 1)
        terminal.x(2)
        terminal.measure(2, 0)
        nonterminal = terminal.copy()
        nonterminal.x(2)
        delayed = QuantumCircuit(3, 1)
        delayed.delay(2, 2)
        delayed.measure(2, 0)
        backend = SimpleNamespace(target=object())
        for circuit, measurement_map, error in (
            (nonterminal, [2], "bound static"),
            (delayed, [2], "bound static"),
            (terminal, [0], "measurement map"),
        ):
            with (
                self.subTest(error=error, circuit=circuit),
                patch("qiskit_aer.noise.device.basic_device_gate_errors") as gate_errors,
                self.assertRaisesRegex(ValueError, error),
            ):
                program = CompiledProgram(circuit, "", {"openqasm": {
                    "export_wire_to_target_qubit": [2],
                    "classical_bit_to_target_qubit": measurement_map,
                }})
                _compact_snapshot_inputs(backend, {"direct": program}, AerSimulator)
            gate_errors.assert_not_called()

    def test_compact_and_prepared_modes_reuse_samplers_for_calibration_and_validation(self):
        from austins_algorithm.witness_budget import WitnessBudgetRequest

        options = {
            "model": "manila", "initial_layout": LAYOUT, "shots": 32, "evaluation_seeds": SEEDS,
            "witness_budget": WitnessBudgetRequest(0.8, validation_seeds=(73, 79)),
        }
        physical = run_constraint_cover_snapshot(CONSTRAINT, **options, simulation_method="density_matrix")
        for mode in ("compact_density_matrix", "prepared_density_matrix"):
            with self.subTest(mode=mode):
                compact = run_constraint_cover_snapshot(CONSTRAINT, **options, simulation_method=mode)
                self.assertEqual(compact.selected.openqasm, physical.selected.openqasm)
                snapshot = compact.report["snapshot"]
                self.assertEqual(snapshot["method"], "density_matrix")
                self.assertEqual(snapshot["wire_representation"], "compact")
                self.assertEqual(compact.report["witness_budget"]["status"], "COMPLETED")
                if mode == "prepared_density_matrix":
                    self.assertEqual(set(snapshot["state_preparation"]), {"direct", "complement"})
                    self.assertTrue(all(row["status"] == "COMPLETED" for row in snapshot["state_preparation"].values()))
                for cover in ("direct", "complement"):
                    for stage, batches in (("snapshot", "batches"), ("witness_budget", "validation_batches")):
                        with self.subTest(cover=cover, stage=stage):
                            actual = compact.report[stage]["candidates"][cover][batches]
                            expected = physical.report[stage]["candidates"][cover][batches]
                            self.assertEqual(
                                [(row["seed"], row["simulation"]["counts"]) for row in actual],
                                [(row["seed"], row["simulation"]["counts"]) for row in expected],
                            )
                            self.assertEqual(len(actual), 2 if stage == "snapshot" else 1)

    def test_prepared_density_preserves_measurement_order_and_unmeasured_work_wire(self):
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        from qiskit_aer.noise import NoiseModel, ReadoutError, pauli_error

        from austins_algorithm.ibm_models import _prepare_density_snapshot

        circuit = QuantumCircuit(3, 2)
        circuit.h(0)
        circuit.cx(0, 1)
        circuit.h(2)
        circuit.measure(2, 1)
        circuit.x(0)
        circuit.measure(0, 0)
        noise = NoiseModel()
        noise.add_quantum_error(pauli_error([("X", 0.2), ("I", 0.8)]), "h", [2])
        noise.add_readout_error(ReadoutError([[0.75, 0.25], [0.125, 0.875]]), [2])
        simulator = AerSimulator(method="density_matrix", noise_model=noise, max_parallel_threads=1)
        (replay, readout), preparation = _prepare_density_snapshot(circuit, simulator, "ordered-preparation-test")
        self.assertEqual(readout.options.noise_model.noise_instructions, ["measure"])
        self.assertEqual(preparation["status"], "COMPLETED")
        self.assertEqual(preparation["matrix_bytes"], 1024)
        self.assertEqual([item.operation.name for item in replay.data], ["set_density_matrix", "measure", "measure"])
        self.assertEqual(
            [(replay.find_bit(item.qubits[0]).index, replay.find_bit(item.clbits[0]).index)
             for item in replay.data[1:]],
            [(2, 1), (0, 0)],
        )
        for seed in (43, 97):
            with self.subTest(seed=seed):
                original = simulator.run(circuit, shots=64, seed_simulator=seed).result(timeout=30)
                prepared = readout.run(replay, shots=64, seed_simulator=seed).result(timeout=30)
                self.assertEqual(original.get_counts(), prepared.get_counts())

    def test_prepared_density_rejects_quantum_noise_on_measurement(self):
        from unittest.mock import Mock

        from qiskit import QuantumCircuit
        from qiskit_aer.noise import NoiseModel, pauli_error

        from austins_algorithm.ibm_models import _prepare_density_snapshot

        noise = NoiseModel()
        noise.add_quantum_error(pauli_error([("X", 1.0)]), "measure", [0])
        simulator = Mock()
        simulator.options.noise_model = noise
        circuit = QuantumCircuit(1, 1)
        circuit.measure(0, 0)
        with self.assertRaisesRegex(ValueError, "static gate noise"):
            _prepare_density_snapshot(circuit, simulator, "quantum-measure-noise-test")
        simulator.run.assert_not_called()

    def test_preparation_timeout_halts_remaining_jobs_and_reports_unconfirmed_cancellation(self):
        from concurrent.futures import TimeoutError
        from unittest.mock import Mock

        from qiskit import QuantumCircuit
        from qiskit_aer.noise import NoiseModel

        from austins_algorithm.ibm_models import LOCAL_JOB_WAIT_SECONDS
        from austins_algorithm.witness_budget import WitnessBudgetRequest

        circuit = QuantumCircuit(2, 2)
        circuit.measure([0, 1], [0, 1])
        simulator = Mock()
        simulator.options.noise_model = NoiseModel()
        job = simulator.run.return_value
        job.result.side_effect = TimeoutError
        job.cancel.return_value = False
        prepared = {name: (circuit, simulator) for name in ("direct", "complement")}
        with (
            patch(f"{MODULE}._compact_snapshot_inputs", return_value=prepared),
            patch(f"{MODULE}._sample_snapshot") as sampler,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, simulation_method="prepared_density_matrix",
                witness_budget=WitnessBudgetRequest(0.8),
            )
        sampler.assert_not_called()
        simulator.run.assert_called_once()
        job.result.assert_called_once_with(timeout=LOCAL_JOB_WAIT_SECONDS)
        job.cancel.assert_called_once()
        snapshot = result.report["snapshot"]
        self.assertEqual(snapshot["status"], "STATE_PREPARATION_INCOMPLETE")
        self.assertTrue(snapshot["local_cancellation_unconfirmed"])
        self.assertEqual(set(snapshot["state_preparation"]), {"direct"})
        self.assertEqual(snapshot["state_preparation"]["direct"]["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
        witness = result.report["witness_budget"]
        self.assertEqual(witness["status"], "STATE_PREPARATION_INCOMPLETE")
        self.assertTrue(witness["local_cancellation_unconfirmed"])
        self.assertEqual(witness["candidates"], {})

    def test_unconfirmed_cancellation_halts_remaining_sampling(self):
        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "local_job_cancelled": False,
            "simulation": None,
            "timing_seconds": {},
        }
        with (
            patch(f"{MODULE}._snapshot_simulator"),
            patch(f"{MODULE}._sample_snapshot", side_effect=[_completed(4, 2), timeout]) as sample,
        ):
            result = run_constraint_cover_snapshot(
                CONSTRAINT, model="manila", initial_layout=LAYOUT, shots=4,
            )

        self.assertEqual(sample.call_count, 2)
        snapshot = result.report["snapshot"]
        self.assertEqual(snapshot["status"], "PARTIAL")
        self.assertTrue(snapshot["local_cancellation_unconfirmed"])
        self.assertIsNone(snapshot["observed_hit_leader"])
        self.assertIsNone(snapshot["complement_minus_direct"])
        statuses = [batch["status"] for row in snapshot["candidates"].values() for batch in row["batches"]]
        self.assertCountEqual(statuses, ["COMPLETED", "LOCAL_RESULT_WAIT_TIMED_OUT"])

    def test_no_alternate_or_eligible_pair_skips_simulator_creation(self):
        for constraint, kwargs, status in (
            (BooleanConstraint(2, ((1,),)), {}, "NO_ALTERNATE_COVER"),
            (CONSTRAINT, {"max_active_qubits": 1}, "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"),
        ):
            with (
                self.subTest(status=status),
                patch(f"{MODULE}._snapshot_simulator") as create,
                patch(f"{MODULE}._sample_snapshot") as sample,
            ):
                result = run_constraint_cover_snapshot(
                    constraint, model="manila", initial_layout=LAYOUT, **kwargs,
                )
                create.assert_not_called()
                sample.assert_not_called()
            snapshot = result.report["snapshot"]
            self.assertEqual(snapshot["status"], status)
            self.assertIsNone(snapshot["observed_hit_leader"])
            self.assertIsNone(snapshot["complement_minus_direct"])

    def test_snapshot_limit_stops_both_candidates_even_if_compile_selection_fits(self):
        with (
            patch(f"{MODULE}.MAX_IBM_SNAPSHOT_ACTIVE_QUBITS", 1),
            patch(f"{MODULE}._snapshot_simulator") as create,
            patch(f"{MODULE}._sample_snapshot") as sample,
        ):
            result = run_constraint_cover_snapshot(CONSTRAINT, model="manila", initial_layout=LAYOUT)

        self.assertIsNotNone(result.selected)
        self.assertEqual(result.report["snapshot"]["status"], "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT")
        create.assert_not_called()
        sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()

