"""Verify study-bound overlap resolution without weakening strict composition."""

from base64 import b64decode, b64encode
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundle_format
from austins_algorithm import snapshot_calibration_overlap as overlap
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_bundle_composition import _A, _B, _entry


_GRID = {"initial_layouts": [(0, 1)], "target_probability": .7, "shot_caps": [1], "query_caps": [0]}
_CONFLICT = "COMPILATION_BUNDLE_COMPOSITION_CONFLICT"


def _argv(directory, earlier, later, bundles, destination=None):
    constraint = directory / "constraint.json"
    constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
    argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
    for flag, files in (("--earlier", earlier), ("--later", later)):
        argv.extend((flag, str(files.configuration), str(files.properties),
                     files.configuration_sha256, files.properties_sha256))
    argv.extend(("--initial-layout", "0", "1", "--target-probability", ".7", "--shot-caps", "1", "--query-caps", "0"))
    for path, pin in bundles:
        argv.extend(("--resolve-compilation-overlap", str(path), pin))
    if destination is not None:
        argv.extend(("--save-compilation-bundle", str(destination)))
    return argv


class SnapshotCalibrationBundleOverlapInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = _test_directory(self)
        self.versions = {**bundle_format._bundle_software_versions(), "qiskit": "foreign-overlap-version"}
        self.archive = comparison.SnapshotCalibrationFiles(
            self.directory / "missing-config.json", self.directory / "missing-properties.json", "a" * 64, "b" * 64,
        )

    def bundle(self, name, entries):
        path = self.directory / f"{name}.json"
        return path, _write_report(path, {"schema_version": 1, "software_versions": self.versions, "entries": entries})

    def compose(self, destination, bundles):
        return study.compose_snapshot_calibration_compilation_for_study(
            _MARK_00, self.archive, self.archive, destination=destination, bundles=bundles, **_GRID,
        )

    def test_study_compose_keeps_strict_behavior_without_eligible_overlap(self):
        shared = _entry("opaque shared circuit")
        bundles = [
            self.bundle("first", {_A: shared}),
            self.bundle("second", {_B: _entry("opaque second circuit"), _A: shared}),
        ]
        destination = self.directory / "study.json"
        strict_destination = self.directory / "strict.json"
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(overlap, "_restore_compiled_round") as restore,
        ):
            result = self.compose(destination, bundles)
            strict = bundle_format.compose_snapshot_calibration_compilation_bundles(strict_destination, bundles=bundles)
        prepare.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(result["study_overlap"]["status"], "NOT_REQUIRED")
        self.assertIsNone(result["study_overlap"]["request"])
        self.assertFalse(result["study_overlap"]["oracle_audit_performed"])
        self.assertEqual(result["overlap_verification"], {})
        self.assertEqual({key: value for key, value in result.items()
                          if key not in ("study_overlap", "overlap_verification")}, strict)
        self.assertEqual(destination.read_bytes(), strict_destination.read_bytes())

    def test_different_reports_or_phases_stay_strictly_blocked_without_restore(self):
        bundles = [
            self.bundle("first", {_A: _entry("A1"), _B: _entry("B1", report={"flag": True})}),
            self.bundle("second", {
                _A: _entry("A2", phase="0x1.0000000000000p-3"), _B: _entry("B2", report={"flag": 1}),
            }),
        ]
        destination = self.directory / "absent.json"
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(overlap, "_restore_compiled_round") as restore,
        ):
            report = self.compose(destination, bundles)
        prepare.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(report["status"], _CONFLICT)
        self.assertEqual(report["study_overlap"]["status"], "NOT_REQUIRED")
        self.assertEqual(report["overlap_verification"], {})
        self.assertEqual(report["conflicts"][_A]["differing_fields"], ["global_phase_hex", "qpy"])
        self.assertEqual(report["conflicts"][_B]["differing_fields"], ["qpy", "report"])
        self.assertFalse(report["stored_qpy_decoded"])
        self.assertIsNone(report["output"])
        self.assertFalse(destination.exists())

    def test_foreign_qpy_conflict_returns_json_exit_one_without_preparation_or_output(self):
        bundles = [
            self.bundle("first", {_A: _entry("foreign opaque A1")}),
            self.bundle("second", {_A: _entry("foreign opaque A2")}),
        ]
        destination = self.directory / "absent.json"
        argv = _argv(self.directory, self.archive, self.archive, bundles, destination)
        files = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(overlap, "_restore_compiled_round") as restore,
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main()
        self.assertEqual(exit_status.exception.code, 1)
        prepare.assert_not_called()
        restore.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report, self.compose(destination, bundles))
        self.assertEqual(report["status"], _CONFLICT)
        self.assertEqual(report["study_overlap"]["status"], "INCOMPATIBLE_SOFTWARE")
        self.assertEqual(report["study_overlap"]["differing_versions"]["qiskit"], {
            "recorded": self.versions["qiskit"], "current": bundle_format._bundle_software_versions()["qiskit"],
        })
        evidence = report["overlap_verification"][_A]
        self.assertEqual(evidence["status"], "INCOMPATIBLE_SOFTWARE")
        self.assertEqual(evidence["source_sha256s"], sorted(pin for _, pin in bundles))
        self.assertIsNone(evidence["selected_source_sha256"])
        self.assertFalse(evidence["stored_qpy_decoded"])
        self.assertIsNone(report["output"])
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, files)

        without_destination = argv[:-2]
        with (
            patch.object(sys, "argv", without_destination), redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as rejected,
        ):
            main()
        self.assertNotEqual(rejected.exception.code, 0)
        self.assertIn("requires --save-compilation-bundle", stderr.getvalue())


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationBundleOverlapTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "configuration.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        later_properties = self.directory / "later.json"
        later_properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, later_properties)
        earlier = json.loads(later_properties.read_bytes())
        earlier["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier_properties = self.directory / "earlier.json"
        earlier_properties.write_text(json.dumps(earlier), encoding="utf-8")
        self.earlier = _pinned(configuration, earlier_properties)
        cache = SnapshotCalibrationCompilationCache()
        self.reference = self.compare(cache)
        self.assertEqual(self.reference["status"], "COMPLETED")
        path = self.directory / "original.json"
        self.original = path, cache.save_bundle(path)
        self.document = json.loads(path.read_bytes())
        [self.key] = self.document["entries"]

    def compare(self, cache):
        return comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier, self.later, initial_layout=(0, 1), max_rounds=0, compilation_cache=cache,
        )

    def compose(self, destination, bundles, **overrides):
        return study.compose_snapshot_calibration_compilation_for_study(
            _MARK_00, self.earlier, self.later, destination=destination, bundles=bundles, **{**_GRID, **overrides},
        )

    def variant(self, name, *, modify=None, encoded=None):
        from qiskit import qpy

        document = deepcopy(self.document)
        entry = document["entries"][self.key]
        if modify is not None:
            [circuit] = qpy.load(BytesIO(b64decode(entry["qpy"])))
            modify(circuit)
            payload = BytesIO()
            qpy.dump(circuit, payload)
            encoded = b64encode(payload.getvalue()).decode("ascii")
        self.assertIsInstance(encoded, str)
        self.assertNotEqual(entry["qpy"], encoded)
        entry["qpy"] = encoded
        path = self.directory / f"{name}.json"
        return path, _write_report(path, document)

    def metadata_variant(self):
        def modify(circuit):
            circuit.metadata = {"encoding_fixture": "independent physical circuit serialization"}

        return self.variant("metadata", modify=modify)

    def test_equivalent_encodings_keep_exact_selected_source_row_and_restore(self):
        bundles = sorted((self.original, self.metadata_variant()), key=lambda source: source[1], reverse=True)
        strict_destination = self.directory / "strict.json"
        strict = bundle_format.compose_snapshot_calibration_compilation_bundles(strict_destination, bundles=bundles)
        self.assertEqual(strict["status"], _CONFLICT)
        self.assertNotIn("overlap_verification", strict)
        self.assertFalse(strict_destination.exists())

        destination = self.directory / "resolved.json"
        argv = _argv(self.directory, self.earlier, self.later, bundles, destination)
        with (
            patch.object(overlap, "_restore_compiled_round", wraps=overlap._restore_compiled_round) as restore,
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
        ):
            main()
        self.assertEqual(restore.call_count, 2)
        compile_round.assert_not_called()
        density.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "COMPILATION_BUNDLE_COMPOSED")
        self.assertTrue(result["stored_qpy_decoded"])
        self.assertFalse(result["runtime_coverage_checked"])
        self.assertEqual(result["conflicts"], {})
        self.assertEqual(result["study_overlap"]["status"], "REQUEST_PREPARED")
        self.assertTrue(result["study_overlap"]["oracle_audit_performed"])
        self.assertFalse(result["study_overlap"]["snapshot_simulation_performed"])
        evidence = result["overlap_verification"][self.key]
        self.assertEqual(evidence["status"], "EQUIVALENT_PHYSICAL_CIRCUITS")
        self.assertEqual(evidence["initial_layout"], [0, 1])
        self.assertEqual(evidence["round"], 0)
        self.assertEqual(evidence["differing_fields"], [])
        self.assertEqual(evidence["source_sha256s"], sorted(pin for _, pin in bundles))
        selected_path, selected_pin = min(bundles, key=lambda source: source[1])
        self.assertEqual(evidence["selected_source_sha256"], selected_pin)
        expected_row = json.loads(selected_path.read_bytes())["entries"][self.key]
        output = destination.read_bytes()
        self.assertEqual(json.loads(output)["entries"][self.key], expected_row)
        self.assertEqual(result["output"]["sha256"], sha256(output).hexdigest())
        self.assertEqual({row["status"] for row in evidence["sources"].values()}, {"RESTORED"})
        self.assertEqual(len({row["qpy_sha256"] for row in evidence["sources"].values()}), 2)
        self.assertEqual(len({row["physical_signature_sha256"] for row in evidence["sources"].values()}), 1)
        loaded = SnapshotCalibrationCompilationCache.load_bundle(destination, expected_sha256=result["output"]["sha256"])
        with patch.object(comparison, "compile_and_verify") as compile_round:
            self.assertEqual(json.dumps(self.compare(loaded)), json.dumps(self.reference))
        compile_round.assert_not_called()

    def test_out_of_request_and_ambiguous_overlaps_do_not_decode_or_publish(self):
        bundles = [self.original, self.metadata_variant()]
        destination = self.directory / "absent.json"
        with patch.object(overlap, "_restore_compiled_round") as restore:
            outside = self.compose(destination, bundles, initial_layouts=[(1, 0)])
            with patch.object(study, "_calibration_round_key", return_value=self.key):
                ambiguous = self.compose(destination, bundles, shot_caps=[2], query_caps=[1])
        restore.assert_not_called()
        for result, status in ((outside, "REQUEST_KEY_NOT_FOUND"), (ambiguous, "AMBIGUOUS_REQUEST_KEY")):
            with self.subTest(status=status):
                self.assertEqual(result["status"], _CONFLICT)
                self.assertEqual(result["study_overlap"]["status"], "REQUEST_PREPARED")
                self.assertFalse(result["stored_qpy_decoded"])
                evidence = result["overlap_verification"][self.key]
                self.assertEqual(evidence["status"], status)
                self.assertIsNone(evidence["selected_source_sha256"])
                self.assertIsNone(result["output"])
        self.assertEqual(ambiguous["overlap_verification"][self.key]["requested_cells"], [
            {"initial_layout": [0, 1], "round": 0}, {"initial_layout": [0, 1], "round": 1},
        ])
        self.assertFalse(destination.exists())

    def test_invalid_qpy_or_changed_gate_is_rejected_without_falling_back_to_compile(self):
        from qiskit.circuit.library import XGate

        def gate_change(circuit):
            index = next(index for index, item in enumerate(circuit.data) if item.operation.name == "sx")
            circuit.data[index] = circuit.data[index].replace(operation=XGate())

        candidates = [self.variant("invalid", encoded="bad QPY!"), self.variant("gate", modify=gate_change)]
        for candidate in candidates:
            destination = self.directory / "absent.json"
            with (
                self.subTest(variant=candidate[0].name),
                patch.object(comparison, "compile_and_verify") as compile_round,
                patch.object(comparison, "_prepare_density_snapshot") as density,
            ):
                result = self.compose(destination, [self.original, candidate])
                compile_round.assert_not_called()
                density.assert_not_called()
                self.assertEqual(result["status"], _CONFLICT)
                evidence = result["overlap_verification"][self.key]
                self.assertEqual(evidence["status"], "RESTORE_REJECTED")
                self.assertIsNone(evidence["selected_source_sha256"])
                self.assertEqual(evidence["sources"][candidate[1]]["status"], "RESTORE_REJECTED")
                self.assertTrue(evidence["sources"][candidate[1]]["error"])
                self.assertEqual(evidence["sources"][self.original[1]]["status"], "RESTORED")
                self.assertIsNone(result["output"])
                self.assertFalse(destination.exists())

    def test_labels_and_final_routing_are_independently_compared_after_restore(self):
        from qiskit.transpiler import Layout

        def change_label(circuit):
            index = next(index for index, item in enumerate(circuit.data) if item.operation.name == "sx")
            item = circuit.data[index]
            gate = item.operation.to_mutable()
            gate.label = "different physical instruction label"
            circuit.data[index] = item.replace(operation=gate)

        def change_route(circuit):
            physical = list(range(circuit.num_qubits))
            physical[0], physical[1] = physical[1], physical[0]
            circuit.layout.final_layout = Layout(dict(zip(circuit.qubits, physical, strict=True)))

        for name, modify, field in (("label", change_label, "operations"), ("route", change_route, "routing_permutation")):
            with self.subTest(variant=name):
                candidate = self.variant(name, modify=modify)
                destination = self.directory / "absent.json"
                result = self.compose(destination, [self.original, candidate])
                self.assertEqual(result["status"], _CONFLICT)
                evidence = result["overlap_verification"][self.key]
                self.assertEqual(evidence["status"], "PHYSICAL_CIRCUITS_DIFFER")
                self.assertIn(field, evidence["differing_fields"])
                self.assertEqual({row["status"] for row in evidence["sources"].values()}, {"RESTORED"})
                self.assertIsNone(evidence["selected_source_sha256"])
                self.assertIsNone(result["output"])
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
