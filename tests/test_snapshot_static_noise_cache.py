"""Check study-local static channels and compact simulator isolation."""

from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm import ibm_models


_AER_AVAILABLE = all(find_spec(name) is not None for name in ("qiskit", "qiskit_aer"))


def _channels(union):
    return (
        (("cx", (1, 0), {"details": {"physical_wires": tuple(union)}}),),
        [((0,), {"probabilities": [[.75, .25], [.125, .875]]})],
    )


class SnapshotStaticChannelCacheTests(unittest.TestCase):
    def test_derives_once_per_exact_union_including_equal_width_different_wires(self):
        target = SimpleNamespace(num_qubits=8)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        first, second, expanded = (2, 5), (2, 6), (2, 5, 6)
        with patch.object(
            ibm_models, "_derive_static_snapshot_channels", side_effect=lambda target, union: _channels(union),
        ) as derive:
            for union in (first, second, expanded, second, first, expanded):
                self.assertEqual(cache.for_union(target, union), _channels(union))
        self.assertEqual(derive.call_args_list, [call(target, union) for union in (first, second, expanded)])

    def test_gate_descriptors_are_fixed_and_readout_buffers_are_independent(self):
        target = SimpleNamespace(num_qubits=8)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        union = (2, 5)
        source = _channels(union)
        expected = deepcopy(source)
        with patch.object(ibm_models, "_derive_static_snapshot_channels", return_value=source) as derive:
            first = cache.for_union(target, union)
            second = cache.for_union(target, union)
            self.assertIs(first[0], second[0])
            self.assertIsInstance(first[0], tuple)
            self.assertIsInstance(first[0][0][1], tuple)
            first[1][0][1]["probabilities"][0][0] = 0
            self.assertEqual(second, expected)
            second[1].clear()
            self.assertEqual(cache.for_union(target, union), expected)
        self.assertEqual(source, expected)
        derive.assert_called_once_with(target, union)

    def test_failed_derivation_leaves_no_entry_and_the_same_union_can_retry(self):
        target = SimpleNamespace(num_qubits=8)
        union = (2, 5)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        with patch.object(
            ibm_models, "_derive_static_snapshot_channels",
            side_effect=[RuntimeError("incomplete channel derivation"), _channels(union)],
        ) as derive:
            with self.assertRaisesRegex(RuntimeError, "incomplete channel"):
                cache.for_union(target, union)
            self.assertEqual(cache.for_union(target, union), _channels(union))
            self.assertEqual(cache.for_union(target, union), _channels(union))
        self.assertEqual(derive.call_args_list, [call(target, union), call(target, union)])

    def test_rejects_distinct_target_identity_on_both_cache_hit_and_miss(self):
        target = SimpleNamespace(num_qubits=8)
        different_target = SimpleNamespace(num_qubits=8)
        self.assertEqual(target, different_target)
        union = (2, 5)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        with patch.object(ibm_models, "_derive_static_snapshot_channels", return_value=_channels(union)) as derive:
            cache.for_union(target, union)
            for attempted in (union, (2, 6)):
                with self.subTest(union=attempted), self.assertRaisesRegex(ValueError, r"(?i)(target|identity)"):
                    cache.for_union(different_target, attempted)
            self.assertEqual(cache.for_union(target, union), _channels(union))
        derive.assert_called_once_with(target, union)

    def test_projects_retained_gate_direction_and_order_with_independent_readout(self):
        target = SimpleNamespace(num_qubits=8)
        parent, subset = (1, 2, 5, 7), (2, 7)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        gates = [
            ("cx", [3, 1], "retained reverse"),
            ("cx", [1, 2], "partly outside"),
            ("sx", [1], "retained local"),
            ("cx", [1, 3], "retained forward"),
            ("sx", [0], "entirely outside"),
        ]
        readout = [([wire], {"probabilities": [[.125 * wire, 1 - .125 * wire]]}) for wire in (3, 2, 1, 0)]
        with patch.object(ibm_models, "_derive_static_snapshot_channels", return_value=(gates, readout)) as derive:
            cache.for_union(target, parent)
            first_gates, first_readout = cache.for_union(target, subset)
            self.assertEqual(first_gates, (
                ("cx", (1, 0), "retained reverse"), ("sx", (0,), "retained local"),
                ("cx", (0, 1), "retained forward"),
            ))
            self.assertEqual([tuple(wires) for wires, _ in first_readout], [(1,), (0,)])
            expected = [deepcopy(readout[index][1]) for index in (0, 2)]
            self.assertEqual([channel for _, channel in first_readout], expected)
            first_readout[0][1]["probabilities"][0][0] = 0
            first_readout.pop()
            second_gates, second_readout = cache.for_union(target, subset)
            self.assertIs(second_gates, first_gates)
            self.assertEqual([channel for _, channel in second_readout], expected)
            _, parent_readout = cache.for_union(target, parent)
            self.assertEqual([channel for _, channel in parent_readout], [channel for _, channel in readout])
        derive.assert_called_once_with(target, parent)

    def test_selects_smallest_superset_then_uses_exact_hits_and_derives_incomparable_unions(self):
        target = SimpleNamespace(num_qubits=8)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        large, higher, lower = (0, 1, 2, 4, 6), (1, 2, 5, 6), (0, 2, 3, 6)
        subset, closer, incomparable = (2, 6), (2, 6, 7), (0, 5, 6)

        def derive_origin(target, union):
            return [("x", (union.index(6),), union)], []

        with patch.object(ibm_models, "_derive_static_snapshot_channels", side_effect=derive_origin) as derive:
            for union in (large, higher, lower):
                cache.for_union(target, union)
            self.assertEqual(cache.for_union(target, subset)[0], (("x", (1,), lower),))
            cache.for_union(target, closer)
            self.assertEqual(cache.for_union(target, subset)[0], (("x", (1,), lower),))
            self.assertEqual(cache.for_union(target, incomparable)[0], (("x", (2,), incomparable),))
        self.assertEqual(derive.call_args_list, [call(target, union) for union in (
            large, higher, lower, closer, incomparable,
        )])


@unittest.skipUnless(_AER_AVAILABLE, "optional Qiskit and Aer are unavailable")
class CompactSnapshotStaticChannelCacheTests(unittest.TestCase):
    def test_reuses_union_with_directed_errors_and_fresh_compact_simulators(self):
        from qiskit import QuantumCircuit
        from qiskit.circuit import Measure
        from qiskit.circuit.library import CXGate
        from qiskit.quantum_info import SuperOp
        from qiskit.transpiler import InstructionProperties, Target
        from qiskit_aer import AerSimulator

        from austins_algorithm.compilation import CompiledProgram

        class AsymmetricReadout(InstructionProperties):
            def __init__(self, false_one, false_zero):
                super().__init__(error=(false_one + false_zero) / 2)
                self.prob_meas1_prep0 = false_one
                self.prob_meas0_prep1 = false_zero

        def program(control, target):
            circuit = QuantumCircuit(8, 2)
            circuit.cx(control, target)
            circuit.measure(control, 0)
            circuit.measure(target, 1)
            return CompiledProgram(circuit, "", {"openqasm": {
                "export_wire_to_target_qubit": sorted((control, target)),
                "classical_bit_to_target_qubit": [control, target],
            }})

        def channels(simulator):
            return [
                {key: value for key, value in row.items() if key != "id"}
                for row in simulator.options.noise_model.to_dict()["errors"]
            ]

        target = Target(num_qubits=8)
        target.add_instruction(CXGate(), {
            (5, 2): InstructionProperties(error=.02), (6, 5): InstructionProperties(error=.04),
        })
        target.add_instruction(Measure(), {
            (2,): AsymmetricReadout(.25, .125),
            (5,): AsymmetricReadout(.125, .0625),
            (6,): AsymmetricReadout(.0625, .03125),
        })
        backend = SimpleNamespace(target=target, operation_names=list(target.operation_names))
        programs = {"direct": program(5, 2), "other": program(6, 5)}
        reference = ibm_models._compact_snapshot_inputs(backend, programs, AerSimulator)
        subset = {"direct": programs["direct"]}
        cold_subset = ibm_models._compact_snapshot_inputs(backend, subset, AerSimulator)
        cache = ibm_models._StaticSnapshotChannelCache(target)
        with patch.object(
            ibm_models, "_derive_static_snapshot_channels", wraps=ibm_models._derive_static_snapshot_channels,
        ) as derive:
            first = ibm_models._compact_snapshot_inputs(backend, programs, AerSimulator, static_channels=cache)
            second = ibm_models._compact_snapshot_inputs(
                backend, dict(reversed(tuple(programs.items()))), AerSimulator, static_channels=cache,
            )
            projected = ibm_models._compact_snapshot_inputs(backend, subset, AerSimulator, static_channels=cache)
        derive.assert_called_once_with(target, (2, 5, 6))
        for name, (circuit, simulator) in reference.items():
            with self.subTest(program=name):
                first_circuit, first_simulator = first[name]
                second_circuit, second_simulator = second[name]
                self.assertEqual(circuit, first_circuit)
                self.assertEqual(circuit, second_circuit)
                self.assertIsNot(first_simulator, second_simulator)
                self.assertIsNot(first_simulator.options.noise_model, second_simulator.options.noise_model)
                self.assertEqual(channels(first_simulator), channels(simulator))
                self.assertEqual(channels(second_simulator), channels(simulator))
                [directed] = [row for row in channels(second_simulator) if row["type"] == "qerror"]
                self.assertEqual(directed["gate_qubits"], [(1, 0)])

        projected_circuit, projected_simulator = projected["direct"]
        cold_circuit, cold_simulator = cold_subset["direct"]
        self.assertEqual(projected_circuit, cold_circuit)
        self.assertIsNot(projected_simulator.options.noise_model, cold_simulator.options.noise_model)
        self.assertEqual(channels(projected_simulator), channels(cold_simulator))

        full_cache = ibm_models._StaticSnapshotChannelCache(target)
        with patch.object(
            ibm_models, "_derive_static_snapshot_channels", wraps=ibm_models._derive_static_snapshot_channels,
        ) as derive_full:
            full_union = tuple(range(target.num_qubits))
            full_cache.for_union(target, full_union)
            full_projected = ibm_models._compact_snapshot_inputs(
                backend, subset, AerSimulator, static_channels=full_cache,
            )
        derive_full.assert_called_once_with(target, full_union)
        self.assertEqual(channels(full_projected["direct"][1]), channels(cold_simulator))

        direct = {tuple(row["gate_qubits"][0]): row["probabilities"]
                  for row in channels(second["direct"][1]) if row["type"] == "roerror"}
        self.assertEqual(direct, {(0,): [[.75, .25], [.125, .875]], (1,): [[.875, .125], [.0625, .9375]]})
        gate_templates = cache.for_union(target, (2, 5, 6))[0]
        original_channels = [SuperOp(error).data.copy() for _, _, error in gate_templates]
        options = {"shots": 64, "seed_simulator": 43}
        original_circuit, original_simulator = reference["direct"]
        replay_circuit, replay_simulator = second["direct"]
        original = original_simulator.run(original_circuit, **options).result(timeout=15).get_counts()
        replay = replay_simulator.run(replay_circuit, **options).result(timeout=15).get_counts()
        self.assertEqual(replay, original)
        projected_counts = projected_simulator.run(projected_circuit, **options).result(timeout=15).get_counts()
        cold_counts = cold_simulator.run(cold_circuit, **options).result(timeout=15).get_counts()
        self.assertEqual(projected_counts, cold_counts)
        for (_, _, error), before in zip(gate_templates, original_channels, strict=True):
            self.assertTrue((SuperOp(error).data == before).all())

    def test_warm_full_width_cache_keeps_cold_subset_error_for_global_calibration(self):
        from qiskit.circuit import Measure
        from qiskit.transpiler import InstructionProperties, Target

        target = Target(num_qubits=3)
        target.add_instruction(Measure(), {None: InstructionProperties(error=.2)})
        cache = ibm_models._StaticSnapshotChannelCache(target)
        gates, readout = cache.for_union(target, (0, 1, 2))
        self.assertFalse(gates)
        self.assertFalse(readout)
        subset = (1, 2)
        with self.assertRaisesRegex(ValueError, "explicit wire tuples"):
            ibm_models._derive_static_snapshot_channels(target, subset)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "explicit wire tuples"):
                cache.for_union(target, subset)


if __name__ == "__main__":
    unittest.main()
