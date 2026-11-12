"""Check conservative dated-snapshot pruning of unused static gate channels."""

from importlib.util import find_spec
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm import ibm_models
from austins_algorithm.compilation import CompiledProgram


_AER_AVAILABLE = all(find_spec(name) is not None for name in ("qiskit", "qiskit_aer"))


def _channels(simulator, error_type):
    return {
        (operation, tuple(wires)): {key: value for key, value in row.items() if key != "id"}
        for row in simulator.options.noise_model.to_dict()["errors"]
        if row["type"] == error_type
        for operation in row["operations"]
        for wires in row["gate_qubits"]
    }


@unittest.skipUnless(_AER_AVAILABLE, "optional Qiskit and Aer are unavailable")
class SnapshotCalibrationNoisePruningTests(unittest.TestCase):
    def setUp(self):
        from qiskit.circuit import Measure
        from qiskit.circuit.library import CXGate, RZGate, SXGate, XGate
        from qiskit.transpiler import InstructionProperties, Target
        from qiskit_aer import AerSimulator

        class AsymmetricReadout(InstructionProperties):
            def __init__(self, false_one, false_zero):
                super().__init__(error=(false_one + false_zero) / 2)
                self.prob_meas1_prep0 = false_one
                self.prob_meas0_prep1 = false_zero

        self.target = Target(num_qubits=8)
        self.target.add_instruction(CXGate(), {
            pair: InstructionProperties(error=error)
            for pair, error in zip(((5, 2), (2, 5), (6, 5), (5, 6), (6, 2), (2, 6)),
                                   (.02, .04, .06, .08, .10, .12), strict=True)
        })
        self.target.add_instruction(SXGate(), {
            (wire,): InstructionProperties(error=.125) for wire in (2, 5, 6)
        })
        self.target.add_instruction(XGate(), {
            (wire,): InstructionProperties(error=.0625) for wire in (2, 5, 6)
        })
        self.target.add_instruction(RZGate(.125), {
            (wire,): InstructionProperties(error=0.0) for wire in (2, 5, 6)
        })
        self.target.add_instruction(Measure(), {
            (2,): AsymmetricReadout(.25, .125),
            (5,): AsymmetricReadout(.125, .0625),
            (6,): AsymmetricReadout(.0625, .03125),
        })
        self.backend = SimpleNamespace(name="directed-pruning-fixture", target=self.target,
                                       operation_names=list(self.target.operation_names))
        self.simulator_type = AerSimulator
        self.programs = {
            "first": self.program([(CXGate(), (5, 2)), (SXGate(), (6,))]),
            "second": self.program([(CXGate(), (6, 5)), (SXGate(), (2,))]),
            "narrow": self.program([(CXGate(), (6, 2))], measured=(6, 2)),
        }

    def program(self, gates, *, measured=(5, 2)):
        from qiskit import QuantumCircuit

        circuit = QuantumCircuit(8, len(measured))
        active = set(measured)
        for gate, wires in gates:
            circuit.append(gate, wires)
            active.update(wires)
        for classical, physical in enumerate(measured):
            circuit.measure(physical, classical)
        return CompiledProgram(circuit, "", {"openqasm": {
            "export_wire_to_target_qubit": sorted(active), "classical_bit_to_target_qubit": list(measured),
        }})

    def compact(self, programs=None, **options):
        return ibm_models._compact_snapshot_inputs(
            self.backend, self.programs if programs is None else programs, self.simulator_type, **options,
        )

    def test_prunes_unused_directions_using_all_programs_on_the_shared_map(self):
        import numpy as np

        full = self.compact()
        pruned = self.compact(prune_unused_gate_noise=True)
        self.assertIs(pruned["first"][1], pruned["second"][1])
        self.assertIsNot(pruned["first"][1], pruned["narrow"][1])
        wide = {("cx", (1, 0)), ("cx", (2, 1)), ("sx", (2,)), ("sx", (0,))}
        narrow = {("cx", (1, 0))}
        for name, expected in (("first", wide), ("second", wide), ("narrow", narrow)):
            with self.subTest(program=name):
                original, baseline = full[name]
                circuit, candidate = pruned[name]
                self.assertEqual(circuit, original)
                base_gates = _channels(baseline, "qerror")
                used_gates = _channels(candidate, "qerror")
                self.assertGreater(len(base_gates), len(expected))
                self.assertEqual(used_gates, {key: base_gates[key] for key in base_gates if key in expected})
                self.assertEqual(set(used_gates), expected)
                self.assertEqual(list(used_gates), [key for key in base_gates if key in expected])
                self.assertEqual(candidate.options.noise_model.basis_gates, baseline.options.noise_model.basis_gates)
                self.assertEqual(_channels(candidate, "roerror"), _channels(baseline, "roerror"))
                baseline_replay, baseline_status = ibm_models._prepare_density_snapshot(original, baseline, "fixture")
                pruned_replay, pruned_status = ibm_models._prepare_density_snapshot(circuit, candidate, "fixture")
                self.assertEqual(baseline_status["status"], "COMPLETED")
                self.assertEqual(pruned_status["status"], "COMPLETED")
                np.testing.assert_array_equal(pruned_replay[0].data[0].operation.params[0],
                                              baseline_replay[0].data[0].operation.params[0])
                self.assertEqual(pruned_replay[0].data[1:], baseline_replay[0].data[1:])
                verifier = lambda bits: bits == "01"
                self.assertEqual(
                    ibm_models._prepared_snapshot_probabilities(*pruned_replay, verifier=verifier),
                    ibm_models._prepared_snapshot_probabilities(*baseline_replay, verifier=verifier),
                )
        # Physical wire 6 participates in the wide programs but is never measured.
        [work_readout] = [row for (operation, wires), row in _channels(pruned["first"][1], "roerror").items()
                          if (operation, wires) == ("measure", (2,))]
        self.assertEqual(work_readout["probabilities"], [[.9375, .0625], [.03125, .96875]])

    def test_pruning_keeps_cached_templates_and_target_identity_isolated(self):
        from qiskit.quantum_info import SuperOp
        from qiskit.transpiler import Target

        cache = ibm_models._StaticSnapshotChannelCache(self.target)
        union = (2, 5, 6)
        gates, readout = cache.for_union(self.target, union)
        before = [(name, wires, SuperOp(error).data.copy()) for name, wires, error in gates]
        full = self.compact(static_channels=cache)
        subset = self.compact({"narrow": self.programs["narrow"]}, static_channels=cache, prune_unused_gate_noise=True)
        self.assertEqual(set(_channels(subset["narrow"][1], "qerror")), {("cx", (1, 0))})
        other = SimpleNamespace(target=Target(num_qubits=8), operation_names=[])
        with self.assertRaisesRegex(ValueError, r"(?i)different.*target"):
            ibm_models._compact_snapshot_inputs(
                other, self.programs, self.simulator_type, static_channels=cache, prune_unused_gate_noise=True,
            )

        recovered = self.compact(static_channels=cache)
        for name in full:
            self.assertIsNot(full[name][1], recovered[name][1])
            for error_type in ("qerror", "roerror"):
                self.assertEqual(_channels(full[name][1], error_type), _channels(recovered[name][1], error_type))
        gates_after, readout_after = cache.for_union(self.target, union)
        for (name, wires, error), (old_name, old_wires, matrix) in zip(gates_after, before, strict=True):
            self.assertEqual((name, wires), (old_name, old_wires))
            self.assertTrue((SuperOp(error).data == matrix).all())
        self.assertEqual(readout_after, readout)

    def test_labeled_open_control_and_custom_gates_fall_back_only_for_their_group(self):
        from qiskit.circuit import Gate
        from qiskit.circuit.library import CXGate, SXGate

        unsafe = {
            "labeled": [(CXGate(), (5, 2)), (SXGate(label="x"), (6,))],
            "open_control": [(CXGate(ctrl_state=0), (5, 2)), (SXGate(), (6,))],
            "custom_stock_name": [(CXGate(), (5, 2)), (Gate("x", 1, []), (6,))],
        }
        for name, gates in unsafe.items():
            with self.subTest(unsupported=name):
                programs = {**self.programs, "unsupported": self.program(gates)}
                full = self.compact(programs)
                pruned = self.compact(programs, prune_unused_gate_noise=True)
                self.assertIs(pruned["first"][1], pruned["unsupported"][1])
                self.assertEqual(_channels(pruned["first"][1], "qerror"), _channels(full["first"][1], "qerror"))
                self.assertEqual(set(_channels(pruned["narrow"][1], "qerror")), {("cx", (1, 0))})

    def test_ideal_only_used_gates_do_not_remove_all_nonideal_quantum_channels(self):
        from qiskit.circuit.library import RZGate

        programs = {"ideal-used": self.program([(RZGate(.125), (2,))])}
        full = self.compact(programs)
        candidate = self.compact(programs, prune_unused_gate_noise=True)
        gates = _channels(full["ideal-used"][1], "qerror")
        self.assertTrue(gates)
        self.assertFalse(any(operation == "rz" for operation, _ in gates))
        self.assertEqual(_channels(candidate["ideal-used"][1], "qerror"), gates)

    def test_pruning_cannot_hide_quantum_measurement_noise(self):
        from qiskit_aer.noise import pauli_error

        derive = ibm_models._derive_static_snapshot_channels

        def with_quantum_measurement_noise(target, union):
            gates, readout = derive(target, union)
            return [*gates, ("measure", (0,), pauli_error([("X", 1.0)]))], readout

        with patch.object(ibm_models, "_derive_static_snapshot_channels", side_effect=with_quantum_measurement_noise):
            for prune in (False, True):
                with self.subTest(prune=prune):
                    circuit, simulator = self.compact(
                        {"first": self.programs["first"]}, prune_unused_gate_noise=prune,
                    )["first"]
                    with (
                        patch.object(simulator, "run") as run,
                        self.assertRaisesRegex(ValueError, r"(?i)static gate noise"),
                    ):
                        ibm_models._prepare_density_snapshot(circuit, simulator, "fixture")
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
