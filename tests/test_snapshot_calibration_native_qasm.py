"""Verify optional native OpenQASM preference throughout dated calibration."""

from base64 import b64decode, b64encode
from contextlib import redirect_stdout
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
from austins_algorithm.compilation import _NATIVE_QASM_IMPORTER
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_overlap_receipt import verify_snapshot_calibration_overlap_receipt
from test_snapshot_calibration_bundle_overlap import _GRID, _argv
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_overlap_receipt import _REPLAYED, _VERIFIED, _receipt_args
from test_snapshot_calibration_planning import _plan, _report


_STANDARD_QASM_IMPORTER = "qiskit.qasm3.loads"


def _export(report, round_index="0"):
    return report["rounds"][round_index]["compilation"]["openqasm"]


def _preferred_planner_report(layout=(0, 1), importer=_NATIVE_QASM_IMPORTER):
    report = _report(layout)
    report["prefer_native_qasm"] = True
    for index in report["rounds"]:
        _export(report, index).update(importer=importer, preferred_importer=_NATIVE_QASM_IMPORTER)
    return report


class SnapshotCalibrationNativeQasmInputTests(unittest.TestCase):
    def test_rejects_non_boolean_preference_before_opening_archives(self):
        files = comparison.SnapshotCalibrationFiles("configuration.json", "properties.json", "a" * 64, "b" * 64)
        with patch.object(comparison, "_read_archive") as read:
            for preference in (1, "native", None):
                with self.subTest(preference=preference), self.assertRaisesRegex((TypeError, ValueError), "prefer_native_qasm"):
                    comparison.compare_snapshot_calibrations(
                        _MARK_00, files, files, initial_layout=(0, 1), prefer_native_qasm=preference,
                    )
        read.assert_not_called()

    def test_planner_keeps_preference_and_actual_importer_distinct(self):
        native = _preferred_planner_report()
        fallback = _preferred_planner_report((0, 2), importer=_STANDARD_QASM_IMPORTER)
        result = _plan([native, fallback])
        self.assertTrue(result["prefer_native_qasm"])
        for layout, importer in (("0,1", _NATIVE_QASM_IMPORTER), ("0,2", _STANDARD_QASM_IMPORTER)):
            for program in result["layouts"][layout]["programs"]:
                self.assertEqual(program["preferred_importer"], _NATIVE_QASM_IMPORTER)
                self.assertEqual(program["importer"], importer)
        legacy = _plan([_report()])
        self.assertNotIn("prefer_native_qasm", legacy)
        self.assertNotIn("preferred_importer", legacy["layouts"]["0,1"]["programs"][0])
        with self.assertRaisesRegex(ValueError, r"(?i)(compiler|prefer|policy|scope)"):
            _plan([_report(), fallback])

    def test_planner_rejects_typed_preference_and_inconsistent_importer_evidence(self):
        bad_type = _preferred_planner_report()
        bad_type["prefer_native_qasm"] = 1
        no_marker = _preferred_planner_report()
        del _export(no_marker)["preferred_importer"]
        unexpected_marker = _report()
        _export(unexpected_marker)["preferred_importer"] = _NATIVE_QASM_IMPORTER
        unknown_importer = _report()
        _export(unknown_importer)["importer"] = "example.unverified_parser"
        for report in (bad_type, no_marker, unexpected_marker, unknown_importer):
            with self.subTest(preference=report.get("prefer_native_qasm"), export=_export(report)):
                with self.assertRaisesRegex(ValueError, r"(?i)(native|importer|export|compiler)"):
                    _plan([report])


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationNativeQasmTests(unittest.TestCase):
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

    def compare(self, cache=None, **options):
        return comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier, self.later, initial_layout=(0, 1), max_rounds=0, compilation_cache=cache, **options,
        )

    def coverage(self, cache, **options):
        return study.check_snapshot_calibration_compilation_coverage(
            _MARK_00, self.earlier, self.later, compilation_cache=cache, **_GRID, **options,
        )

    def test_policy_namespaces_cache_keys_and_default_report_keeps_exact_order(self):
        cache = SnapshotCalibrationCompilationCache()
        with patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compiler:
            default = self.compare(cache)
            native = self.compare(cache, prefer_native_qasm=True)
            self.assertEqual(json.dumps(self.compare(cache, prefer_native_qasm=False)), json.dumps(default))
            self.assertEqual(json.dumps(self.compare(cache, prefer_native_qasm=True)), json.dumps(native))
        self.assertEqual([call.kwargs.get("prefer_native_qasm", False) for call in compiler.call_args_list], [False, True])
        self.assertNotIn("prefer_native_qasm", default)
        self.assertTrue(native["prefer_native_qasm"])
        self.assertNotIn("preferred_importer", _export(default))
        self.assertEqual(_export(default)["importer"], _STANDARD_QASM_IMPORTER)
        self.assertEqual(_export(native)["importer"], _NATIVE_QASM_IMPORTER)
        self.assertEqual(_export(native)["preferred_importer"], _NATIVE_QASM_IMPORTER)
        self.assertEqual(_export(default)["sha256"], _export(native)["sha256"])
        self.assertEqual((cache.stats()["entries"], cache.stats()["hits"], cache.stats()["misses"]), (2, 2, 2))

        saved = self.directory / "both-policies.json"
        loaded = SnapshotCalibrationCompilationCache.load_bundle(saved, expected_sha256=cache.save_bundle(saved))
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            standard_coverage = self.coverage(loaded)
            native_coverage = self.coverage(loaded, prefer_native_qasm=True)
        decode.assert_not_called()
        self.assertNotIn("prefer_native_qasm", standard_coverage["inputs"])
        self.assertTrue(native_coverage["inputs"]["prefer_native_qasm"])
        self.assertNotEqual(standard_coverage["compiler_scope_sha256"], native_coverage["compiler_scope_sha256"])
        rows = [report["layouts"]["0,1"]["rounds"]["0"] for report in (standard_coverage, native_coverage)]
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        self.assertEqual([row["status"] for row in rows], ["PRESENT", "PRESENT"])
        self.assertEqual({row["key"] for row in rows}, set(json.loads(saved.read_bytes())["entries"]))
        with patch.object(comparison, "compile_and_verify") as compiler:
            self.assertEqual(json.dumps(self.compare(loaded, prefer_native_qasm=True)), json.dumps(native))
            self.assertEqual(json.dumps(self.compare(loaded)), json.dumps(default))
        compiler.assert_not_called()

    def test_persisted_native_preference_restores_recorded_standard_fallback_and_rejects_tampering(self):
        from qiskit import qasm3

        cache = SnapshotCalibrationCompilationCache()
        with (
            patch.object(qasm3, "loads_experimental", side_effect=qasm3.QASM3ImporterError("forced native fallback"))
            as try_native,
            patch.object(qasm3, "loads", wraps=qasm3.loads) as use_standard,
        ):
            fallback = self.compare(cache, prefer_native_qasm=True)
        try_native.assert_called_once()
        use_standard.assert_called_once()
        self.assertTrue(fallback["prefer_native_qasm"])
        self.assertEqual(_export(fallback)["importer"], _STANDARD_QASM_IMPORTER)
        self.assertEqual(_export(fallback)["preferred_importer"], _NATIVE_QASM_IMPORTER)
        saved = self.directory / "native-fallback.json"
        loaded = SnapshotCalibrationCompilationCache.load_bundle(saved, expected_sha256=cache.save_bundle(saved))
        self.assertEqual(self.coverage(loaded)["coverage"]["status"], "NONE_PRESENT")
        with (
            patch.object(qasm3, "loads") as standard_import,
            patch.object(qasm3, "loads_experimental") as native_import,
            patch.object(comparison, "compile_and_verify") as compiler,
        ):
            self.assertEqual(json.dumps(self.compare(loaded, prefer_native_qasm=True)), json.dumps(fallback))
        for mock in (standard_import, native_import, compiler):
            mock.assert_not_called()

        for field, value in (("preferred_importer", None), ("importer", "example.unverified_parser")):
            with self.subTest(field=field):
                document = json.loads(saved.read_bytes())
                [entry] = document["entries"].values()
                if value is None:
                    del entry["report"]["openqasm"][field]
                else:
                    entry["report"]["openqasm"][field] = value
                tampered = self.directory / "tampered.json"
                pin = _write_report(tampered, document)
                cache = SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=pin)
                with (
                    patch.object(comparison, "compile_and_verify") as compiler,
                    patch.object(comparison, "_prepare_density_snapshot") as density,
                    self.assertRaisesRegex(ValueError, r"(?i)(importer|qasm|report|verification)"),
                ):
                    self.compare(cache, prefer_native_qasm=True)
                compiler.assert_not_called()
                density.assert_not_called()

    def test_native_overlap_receipt_binds_mode_in_sdk_free_verification_and_replay(self):
        from qiskit import qpy

        cache = SnapshotCalibrationCompilationCache()
        self.compare(cache, prefer_native_qasm=True)
        first = self.directory / "first.json"
        first_pin = cache.save_bundle(first)
        document = json.loads(first.read_bytes())
        [key] = document["entries"]
        row = deepcopy(document["entries"][key])
        [circuit] = qpy.load(BytesIO(b64decode(row["qpy"])))
        circuit.metadata = {"native_overlap_fixture": "different encoding"}
        encoded = BytesIO()
        qpy.dump(circuit, encoded)
        row["qpy"] = b64encode(encoded.getvalue()).decode("ascii")
        document["entries"][key] = row
        second = self.directory / "second.json"
        sources = [(first, first_pin), (second, _write_report(second, document))]
        output = self.directory / "composed.json"
        argv = [*_argv(self.directory, self.earlier, self.later, sources, output), "--native-qasm"]
        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
            main()
        raw = stdout.getvalue().encode("utf-8")
        composition = json.loads(raw)
        self.assertEqual(composition["status"], "COMPILATION_BUNDLE_COMPOSED")
        self.assertTrue(composition["study_overlap"]["request"]["inputs"]["prefer_native_qasm"])
        path = self.directory / "receipt.json"
        path.write_bytes(raw)
        args = _receipt_args((path, sha256(raw).hexdigest()), (output, composition["output"]["sha256"]), sources)
        with (
            patch.object(bundle_format, "_load_physical_circuit") as decode,
            patch.object(overlap, "_restore_compiled_round") as restore,
        ):
            verified = verify_snapshot_calibration_overlap_receipt(**args)
            self.assertEqual(verified["status"], _VERIFIED)
            self.assertTrue(verified["study_overlap"]["request"]["inputs"]["prefer_native_qasm"])
            self.assertFalse(verified["qpy_decoded"])
            with self.assertRaisesRegex(ValueError, r"(?i)(request|scope|policy)"):
                study.replay_snapshot_calibration_overlap_receipt(
                    _MARK_00, self.earlier, self.later, **args, **_GRID,
                )
            changed = deepcopy(composition)
            del changed["study_overlap"]["request"]["inputs"]["prefer_native_qasm"]
            tampered = self.directory / "wrong-mode-receipt.json"
            pin = _write_report(tampered, changed)
            with self.assertRaisesRegex(ValueError, r"(?i)(prefer|native|importer|policy)"):
                verify_snapshot_calibration_overlap_receipt(
                    **{**args, "receipt": tampered, "expected_receipt_sha256": pin},
                )
        decode.assert_not_called()
        restore.assert_not_called()
        replay = study.replay_snapshot_calibration_overlap_receipt(
            _MARK_00, self.earlier, self.later, **args, **_GRID, prefer_native_qasm=True,
        )
        self.assertEqual(replay["status"], _REPLAYED)
        self.assertEqual(replay["replayed_overlaps"], composition["overlap_verification"])
        self.assertTrue(replay["request"]["inputs"]["prefer_native_qasm"])


if __name__ == "__main__":
    unittest.main()
