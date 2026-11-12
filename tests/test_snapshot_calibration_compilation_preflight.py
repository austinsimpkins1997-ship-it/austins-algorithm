"""Check read-only inspection and key coverage for pinned compilation bundles."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from importlib.util import find_spec
from io import StringIO
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
from austins_algorithm import snapshot_calibration_compilation_cache as cache_module
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_compilation_bundle import inspect_snapshot_calibration_compilation_bundle
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_planning import _report


class SnapshotCalibrationCompilationInspectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = _test_directory(self)
        self.path = self.directory / "synthetic-bundle.json"
        self.versions = {**bundle_format._bundle_software_versions(), "qiskit": "foreign-test-version"}
        self.compilations = {
            "d" * 64: _report((0, 1))["rounds"]["0"]["compilation"],
            "a" * 64: _report((1, 0))["rounds"]["1"]["compilation"],
        }
        self.pin = _write_report(self.path, {
            "schema_version": 1, "software_versions": self.versions,
            "entries": {key: {"qpy": "opaque unvalidated data", "global_phase_hex": "0x0.0p+0", "report": report}
                        for key, report in self.compilations.items()},
        })

    def test_inspects_foreign_software_metadata_and_never_decodes_qpy(self):
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            report = inspect_snapshot_calibration_compilation_bundle(self.path, expected_sha256=self.pin)
            with self.assertRaisesRegex(ValueError, "software versions"):
                SnapshotCalibrationCompilationCache.load_bundle(self.path, expected_sha256=self.pin)
        decode.assert_not_called()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["status"], "SNAPSHOT_CALIBRATION_COMPILATION_BUNDLE_INSPECTED")
        self.assertEqual(report["bundle_sha256"], self.pin)
        self.assertEqual(report["software_versions"], self.versions)
        self.assertEqual(report["entry_count"], 2)
        self.assertEqual(report["entry_keys"], sorted(self.compilations))
        self.assertFalse(report["qpy_decoded"])
        self.assertFalse(report["runtime_coverage_checked"])
        for key, record in report["entries"].items():
            compilation = self.compilations[key]
            self.assertEqual(record["recorded_initial_layout"], compilation["initial_logical_to_target_qubit"])
            self.assertEqual(record["recorded_seed_transpiler"], compilation["seed_transpiler"])
            self.assertEqual(record["recorded_openqasm_sha256"], compilation["openqasm"]["sha256"])
            self.assertNotIn("qpy", record)

    def test_inspection_checks_raw_pin_and_cli_matches_api_without_writing(self):
        with self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest)"):
            inspect_snapshot_calibration_compilation_bundle(self.path, expected_sha256="0" * 64)
        expected = inspect_snapshot_calibration_compilation_bundle(self.path, expected_sha256=self.pin)
        before = self.path.read_bytes()
        argv = ["austins-algorithm", "snapshot-calibration-bundle-inspect", str(self.path), "--expected-sha256", self.pin]
        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
            main()
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), before)


class SnapshotCalibrationCompilationCoverageInputTests(unittest.TestCase):
    def test_requires_loaded_pinned_cache_before_reading_archives(self):
        archive = comparison.SnapshotCalibrationFiles("configuration.json", "properties.json", "a" * 64, "b" * 64)
        for cache, error, message in (
            (None, TypeError, "compilation_cache"),
            (SnapshotCalibrationCompilationCache(), ValueError, "pinned bundle"),
        ):
            with (
                self.subTest(cache=cache), patch.object(comparison, "_read_archive") as read,
                self.assertRaisesRegex(error, message),
            ):
                study.check_snapshot_calibration_compilation_coverage(
                    _MARK_00, archive, archive, initial_layouts=[(0, 1)], target_probability=.7,
                    shot_caps=[1], query_caps=[0], compilation_cache=cache,
                )
            read.assert_not_called()


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationCompilationCoverageTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "configuration.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        properties = self.directory / "later.json"
        properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, properties)
        self.earlier = self.earlier_archive("earlier", "2021-07-26T11:47:01-04:00")
        cache = SnapshotCalibrationCompilationCache()
        self.assertEqual(self.compare(cache)["status"], "COMPLETED")
        self.bundle = self.directory / "compilation.json"
        self.pin = cache.save_bundle(self.bundle)
        self.loaded = SnapshotCalibrationCompilationCache.load_bundle(self.bundle, expected_sha256=self.pin)
        [self.saved_key] = json.loads(self.bundle.read_bytes())["entries"]

    def earlier_archive(self, name, date):
        props = json.loads(Path(self.later.properties).read_bytes())
        props["last_update_date"] = date
        properties = self.directory / f"{name}.json"
        properties.write_text(json.dumps(props), encoding="utf-8")
        return _pinned(Path(self.later.configuration), properties)

    def compare(self, cache):
        return comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier, self.later, initial_layout=(0, 1), max_rounds=0, compilation_cache=cache,
        )

    def coverage(self, *, cache=None, earlier=None, later=None, **overrides):
        options = {"initial_layouts": [(0, 1)], "target_probability": .7, "shot_caps": [1], "query_caps": [0]}
        return study.check_snapshot_calibration_compilation_coverage(
            _MARK_00, self.earlier if earlier is None else earlier, self.later if later is None else later,
            compilation_cache=self.loaded if cache is None else cache, **{**options, **overrides},
        )

    def test_prefix_key_coverage_matches_real_reuse_without_restoring_or_mutating_cache(self):
        options = {"initial_layouts": [(1, 0), (0, 1)], "shot_caps": [2], "query_caps": [1]}
        stats = self.loaded.stats()
        with (
            patch.object(self.loaded, "stats", side_effect=AssertionError("coverage queried cache statistics")),
            patch.object(cache_module, "_restore_compiled_round") as restore,
            patch.object(bundle_format, "_load_physical_circuit") as decode,
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            patch.object(study, "plan_snapshot_calibration_budgets") as plan,
        ):
            result = self.coverage(**options)
        for mock in (restore, decode, compile_round, density, plan):
            mock.assert_not_called()
        self.assertEqual(self.loaded.stats(), stats)
        self.assertEqual(result["status"], "COMPILATION_BUNDLE_COVERAGE_CHECKED")
        self.assertEqual(result["evidence_scope"], "REQUESTED_COMPILATION_KEYS_ONLY")
        self.assertTrue(result["oracle_audit_performed"])
        self.assertFalse(result["stored_qpy_decoded"])
        self.assertFalse(result["snapshot_simulation_performed"])
        self.assertEqual(result["required_rounds"], [0, 1])
        self.assertEqual(result["bundle"], {
            "sha256": self.pin, "key_count": 1, "unused_key_count": 0, "unused_keys": [],
        })
        self.assertEqual(result["coverage"], {
            "status": "PARTIAL", "requested_round_count": 4, "present_round_count": 1, "absent_round_count": 3,
            "unkeyable_round_count": 0, "requested_unique_key_count": 4, "present_unique_key_count": 1,
        })
        self.assertEqual(result["layouts"]["0,1"]["rounds"]["0"], {"key": self.saved_key, "status": "PRESENT"})
        self.assertEqual(result["layouts"]["0,1"]["rounds"]["1"]["status"], "ABSENT")
        self.assertEqual({row["status"] for row in result["layouts"]["1,0"]["rounds"].values()}, {"ABSENT"})
        self.assertNotIn("captures", result)
        self.assertNotIn("planning", result)

        with patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compiler:
            captured = study.run_snapshot_calibration_study(
                _MARK_00, self.earlier, self.later, compilation_cache=self.loaded, target_probability=.7, **options,
            )
        self.assertEqual(captured["status"], "CALIBRATION_STUDY_COMPLETED")
        compiled_layouts = Counter(tuple(call.kwargs["initial_layout"]) for call in compiler.call_args_list)
        self.assertEqual(compiled_layouts, {(0, 1): 1, (1, 0): 2})
        self.assertEqual((self.loaded.stats()["hits"], self.loaded.stats()["misses"]), (1, 3))

    def test_key_coverage_tracks_seed_layout_and_later_pin_independently_of_earlier_pin(self):
        changed_earlier = self.earlier_archive("earlier-alt", "2022-01-01T01:00:00+00:00")
        changed_properties = self.directory / "later-repinned.json"
        changed_properties.write_bytes(Path(self.later.properties).read_bytes() + b"\n")
        changed_later = _pinned(Path(self.later.configuration), changed_properties)
        baseline = self.coverage()
        earlier = self.coverage(earlier=changed_earlier)
        self.assertEqual(earlier["coverage"]["status"], "ALL_PRESENT")
        self.assertEqual(earlier["compiler_scope_sha256"], baseline["compiler_scope_sha256"])
        self.assertNotEqual(earlier["snapshots"]["earlier"], baseline["snapshots"]["earlier"])
        self.assertEqual(earlier["layouts"]["0,1"]["rounds"]["0"], {"key": self.saved_key, "status": "PRESENT"})

        for options in ({"seed_transpiler": 29}, {"initial_layouts": [(1, 0)]}, {"later": changed_later}):
            with self.subTest(options=options):
                changed = self.coverage(**options)
                self.assertEqual(changed["coverage"]["status"], "NONE_PRESENT")
                self.assertEqual(changed["coverage"]["absent_round_count"], 1)
                self.assertEqual(changed["bundle"]["unused_key_count"], 1)
                self.assertEqual(changed["bundle"]["unused_keys"], [self.saved_key])
                [layout] = changed["layouts"].values()
                self.assertNotEqual(layout["rounds"]["0"]["key"], self.saved_key)
                self.assertEqual(layout["rounds"]["0"]["status"], "ABSENT")

    def test_unkeyable_rounds_are_distinct_from_known_absent_keys(self):
        with patch.object(study, "_calibration_round_key", side_effect=[self.saved_key, None]):
            report = self.coverage(shot_caps=[2], query_caps=[1])
        self.assertEqual(report["layouts"]["0,1"]["rounds"]["1"], {"key": None, "status": "UNKEYABLE"})
        self.assertEqual(report["coverage"], {
            "status": "PARTIAL", "requested_round_count": 2, "present_round_count": 1, "absent_round_count": 0,
            "unkeyable_round_count": 1, "requested_unique_key_count": 1, "present_unique_key_count": 1,
        })

    def test_pinned_but_invalid_qpy_can_be_present_without_being_reusable(self):
        document = json.loads(self.bundle.read_bytes())
        document["entries"][self.saved_key]["qpy"] = "opaque invalid QPY"
        invalid = self.directory / "invalid-qpy.json"
        digest = _write_report(invalid, document)
        loaded = SnapshotCalibrationCompilationCache.load_bundle(invalid, expected_sha256=digest)
        stats = loaded.stats()
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            result = self.coverage(cache=loaded)
        decode.assert_not_called()
        self.assertEqual(result["coverage"]["status"], "ALL_PRESENT")
        self.assertEqual(result["bundle"]["sha256"], digest)
        self.assertEqual(loaded.stats(), stats)
        with (
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            self.assertRaisesRegex(ValueError, r"(?i)(qpy|circuit)"),
        ):
            self.compare(loaded)
        compile_round.assert_not_called()
        density.assert_not_called()

    def test_study_cli_check_is_read_only_and_rejects_save_and_missing_load(self):
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
        for flag, files in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(files.configuration), str(files.properties),
                         files.configuration_sha256, files.properties_sha256))
        argv.extend((
            "--initial-layout", "0", "1", "--target-probability", ".7", "--shot-caps", "1", "--query-caps", "0",
            "--check-compilation-bundle",
        ))
        load = ["--load-compilation-bundle", str(self.bundle), self.pin]
        names = sorted(path.name for path in self.directory.iterdir())
        content = self.bundle.read_bytes()
        with patch.object(sys, "argv", [*argv, *load]), redirect_stdout(StringIO()) as stdout:
            main()
        self.assertEqual(json.loads(stdout.getvalue()), self.coverage())

        output = self.directory / "should-not-exist.json"
        for extra, pattern in (([], r"--load-compilation-bundle"),
                               ([*load, "--save-compilation-bundle", str(output)], r"--save-compilation-bundle")):
            with (
                self.subTest(extra=extra), patch.object(sys, "argv", [*argv, *extra]),
                redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as failed,
            ):
                main()
            self.assertNotEqual(failed.exception.code, 0)
            self.assertRegex(stderr.getvalue(), pattern)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), names)
        self.assertEqual(self.bundle.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
