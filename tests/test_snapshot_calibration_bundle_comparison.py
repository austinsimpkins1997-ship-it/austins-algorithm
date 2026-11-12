"""Compare independent pinned compilation bundles on one requested study grid."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
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
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned


_GRID = {"initial_layouts": [(1, 0), (0, 1)], "target_probability": .7, "shot_caps": [2], "query_caps": [1]}


def _foreign_bundle(directory, name):
    path = directory / f"{name}.json"
    versions = {**bundle_format._bundle_software_versions(), "qiskit": f"foreign-{name}"}
    entry = {"qpy": "opaque unvalidated data", "global_phase_hex": "0x0.0p+0", "report": {}}
    key = sha256(name.encode("utf-8")).hexdigest()
    pin = _write_report(path, {"schema_version": 1, "software_versions": versions, "entries": {key: entry}})
    return path, pin


def _argv(directory, earlier, later, bundles):
    constraint = directory / "constraint.json"
    constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
    argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
    for flag, files in (("--earlier", earlier), ("--later", later)):
        argv.extend((flag, str(files.configuration), str(files.properties),
                     files.configuration_sha256, files.properties_sha256))
    argv.extend((
        "--initial-layout", "1", "0", "--initial-layout", "0", "1",
        "--target-probability", ".7", "--shot-caps", "2", "--query-caps", "1",
    ))
    for path, pin in bundles:
        argv.extend(("--compare-compilation-bundle", str(path), pin))
    return argv


class SnapshotCalibrationBundleComparisonInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = _test_directory(self)
        self.foreign = [_foreign_bundle(self.directory, name) for name in ("first", "second")]
        self.archive = comparison.SnapshotCalibrationFiles(
            self.directory / "missing-config.json", self.directory / "missing-properties.json", "a" * 64, "b" * 64,
        )

    def compare(self, bundles):
        return study.compare_snapshot_calibration_compilation_coverage(
            _MARK_00, self.archive, self.archive, bundles=bundles, **_GRID,
        )

    def test_checks_bundle_count_pin_and_duplicate_bytes_before_sdk_preparation(self):
        first, second = self.foreign
        clone = self.directory / "duplicate.json"
        clone.write_bytes(first[0].read_bytes())
        bad_inputs = (
            ([], r"(?i)(bundle|candidate)"),
            ([first], r"(?i)(bundle|candidate)"),
            ([first] * 9, r"(?i)(bundle|candidate)"),
            ([first, (second[0],)], r"(?i)(path|pin|candidate)"),
            ([first, (second[0], "0" * 64)], r"(?i)(sha-?256|digest)"),
            ([first, (clone, first[1])], r"(?i)(duplicate|distinct)"),
        )
        with patch.object(study, "_prepare_study") as prepare:
            for bundles, message in bad_inputs:
                with self.subTest(bundles=bundles), self.assertRaisesRegex(ValueError, message):
                    self.compare(bundles)
        prepare.assert_not_called()

    def test_all_foreign_candidates_report_versions_without_preparing_request_keys(self):
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(bundle_format, "_load_physical_circuit") as decode,
        ):
            result = self.compare(self.foreign)
        prepare.assert_not_called()
        decode.assert_not_called()
        self.assertEqual(result["status"], "COMPILATION_BUNDLE_COVERAGE_COMPARED")
        self.assertFalse(result["request_keys_prepared"])
        self.assertFalse(result["oracle_audit_performed"])
        self.assertEqual(result["required_rounds"], [])
        self.assertEqual(result["layouts"], {})
        for field in ("inputs", "snapshots", "versions", "compiler_scope_sha256"):
            self.assertIsNone(result[field])
        for index, (candidate, (path, pin)) in enumerate(zip(result["bundles"], self.foreign, strict=True), start=1):
            with self.subTest(bundle=candidate["id"]):
                recorded = json.loads(path.read_bytes())["software_versions"]
                self.assertEqual(candidate["id"], f"bundle_{index}")
                self.assertEqual(candidate["sha256"], pin)
                self.assertEqual(candidate["status"], "INCOMPATIBLE_SOFTWARE")
                self.assertIsNone(candidate["coverage"])
                self.assertEqual(candidate["differing_versions"], {
                    "qiskit": {"recorded": recorded["qiskit"], "current": bundle_format._bundle_software_versions()["qiskit"]},
                })
        ranking = result["ranking"]
        self.assertEqual(ranking["status"], "NO_COMPATIBLE_BUNDLES")
        self.assertIsNone(ranking["best_covered_round_count"])
        self.assertIsNone(ranking["best_missing_round_count"])
        self.assertEqual(ranking["best_bundle_ids"], [])
        self.assertEqual(ranking["ranks"], [])

    def test_cli_accepts_repeated_comparison_flags_and_rejects_load_check_or_save(self):
        argv = _argv(self.directory, self.archive, self.archive, self.foreign)
        names = sorted(path.name for path in self.directory.iterdir())
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
        ):
            main()
        self.assertEqual(json.loads(stdout.getvalue()), self.compare(self.foreign))
        prepare.assert_not_called()
        output = self.directory / "no-bundle.json"
        first_path, first_pin = self.foreign[0]
        for extra in (
            ["--load-compilation-bundle", str(first_path), first_pin],
            ["--check-compilation-bundle"],
            ["--save-compilation-bundle", str(output)],
        ):
            with (
                self.subTest(option=extra[0]), patch.object(sys, "argv", [*argv, *extra]),
                redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as rejected,
            ):
                main()
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertIn(extra[0], stderr.getvalue())
            self.assertIn("--compare-compilation-bundle", stderr.getvalue())
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), names)


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationBundleComparisonTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "configuration.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        later_properties = self.directory / "later.json"
        later_properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, later_properties)
        early = json.loads(later_properties.read_bytes())
        early["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier_properties = self.directory / "earlier.json"
        earlier_properties.write_text(json.dumps(early), encoding="utf-8")
        self.earlier = _pinned(configuration, earlier_properties)

    def compare(self, bundles):
        return study.compare_snapshot_calibration_compilation_coverage(
            _MARK_00, self.earlier, self.later, bundles=bundles, **_GRID,
        )

    def write_subset(self, name, source, entries):
        path = self.directory / f"{name}.json"
        return path, _write_report(path, {**source, "entries": entries})

    def test_compares_disjoint_tied_bundles_and_selects_full_coverage_that_really_restores(self):
        cache = SnapshotCalibrationCompilationCache()
        reference = study.run_snapshot_calibration_study(
            _MARK_00, self.earlier, self.later, compilation_cache=cache, **_GRID,
        )
        self.assertEqual(reference["status"], "CALIBRATION_STUDY_COMPLETED")
        full = self.directory / "full.json"
        full_pin = cache.save_bundle(full)
        source = json.loads(full.read_bytes())
        entries = source["entries"]
        self.assertEqual(len(entries), 4)
        partials = {
            layout: {key: row for key, row in entries.items()
                     if tuple(row["report"]["initial_logical_to_target_qubit"]) == layout}
            for layout in ((0, 1), (1, 0))
        }
        self.assertEqual([len(half) for half in partials.values()], [2, 2])
        self.assertFalse(partials[(0, 1)].keys() & partials[(1, 0)].keys())
        reverse = self.write_subset("reverse", source, partials[(1, 0)])
        forward = self.write_subset("forward", source, partials[(0, 1)])
        unrelated = "0" * 64
        self.assertNotIn(unrelated, entries)
        miss = self.write_subset("miss", source, {unrelated: next(iter(entries.values()))})
        foreign = _foreign_bundle(self.directory, "mixed-foreign")
        bundles = [reverse, foreign, (full, full_pin), forward, miss]
        with (
            patch.object(study, "_prepare_study", wraps=study._prepare_study) as prepare,
            patch.object(comparison, "_read_archive", wraps=comparison._read_archive) as read_archive,
            patch.object(comparison, "audit_phase_oracle", wraps=comparison.audit_phase_oracle) as audit,
            patch.object(study, "_calibration_round_key", wraps=study._calibration_round_key) as round_key,
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            patch.object(cache_module, "_restore_compiled_round") as restore,
            patch.object(bundle_format, "_load_physical_circuit") as decode,
        ):
            result = self.compare(bundles)
        prepare.assert_called_once()
        audit.assert_called_once()
        self.assertEqual(read_archive.call_count, 4)
        self.assertEqual(round_key.call_count, 4)
        for mock in (compile_round, density, restore, decode):
            mock.assert_not_called()

        self.assertTrue(result["request_keys_prepared"])
        self.assertTrue(result["oracle_audit_performed"])
        self.assertFalse(result["stored_qpy_decoded"])
        self.assertFalse(result["snapshot_simulation_performed"])
        self.assertEqual(result["required_rounds"], [0, 1])
        self.assertEqual({key for layout in result["layouts"].values() for row in layout["rounds"].values()
                          if (key := row["key"]) is not None}, set(entries))
        candidates = {candidate["id"]: candidate for candidate in result["bundles"]}
        self.assertEqual(list(candidates), [f"bundle_{index}" for index in range(1, 6)])
        self.assertEqual([candidate["sha256"] for candidate in candidates.values()], [pin for _, pin in bundles])
        self.assertEqual(candidates["bundle_2"]["status"], "INCOMPATIBLE_SOFTWARE")
        for identifier, status, present, absent in (
            ("bundle_1", "PARTIAL", 2, 2), ("bundle_3", "ALL_PRESENT", 4, 0),
            ("bundle_4", "PARTIAL", 2, 2), ("bundle_5", "NONE_PRESENT", 0, 4),
        ):
            with self.subTest(bundle=identifier):
                candidate = candidates[identifier]
                self.assertEqual(candidate["status"], "COMPATIBLE")
                self.assertEqual(candidate["differing_versions"], {})
                self.assertEqual(candidate["coverage"], {
                    "status": status, "requested_round_count": 4, "present_round_count": present,
                    "absent_round_count": absent, "unkeyable_round_count": 0,
                    "requested_unique_key_count": 4, "present_unique_key_count": present,
                })
                self.assertEqual(len(candidate["missing_requested_rounds"]), absent)
        for identifier, missing_layout in (("bundle_1", "0,1"), ("bundle_4", "1,0")):
            self.assertEqual(candidates[identifier]["missing_requested_rounds"], [
                {"layout": missing_layout, "round": index,
                 "key": result["layouts"][missing_layout]["rounds"][str(index)]["key"], "status": "ABSENT"}
                for index in (0, 1)
            ])
        self.assertEqual(candidates["bundle_5"]["unused_keys"], [unrelated])
        ranking = result["ranking"]
        self.assertEqual(ranking["status"], "COVERAGE_RANKED")
        self.assertEqual(ranking["best_covered_round_count"], 4)
        self.assertEqual(ranking["best_missing_round_count"], 0)
        self.assertEqual(ranking["best_bundle_ids"], ["bundle_3"])
        self.assertEqual([row["bundle_ids"] for row in ranking["ranks"]], [["bundle_3"], ["bundle_1", "bundle_4"]])
        self.assertEqual([row["missing_round_count"] for row in ranking["ranks"]], [0, 2])

        loaded = SnapshotCalibrationCompilationCache.load_bundle(full, expected_sha256=full_pin)
        with patch.object(comparison, "compile_and_verify") as compile_round:
            replay = study.run_snapshot_calibration_study(
                _MARK_00, self.earlier, self.later, compilation_cache=loaded, **_GRID,
            )
        compile_round.assert_not_called()
        self.assertEqual(json.dumps(replay), json.dumps(reference))
        self.assertEqual((loaded.stats()["hits"], loaded.stats()["misses"]), (4, 0))

        tied = self.compare([reverse, forward])
        self.assertEqual(tied["ranking"]["best_covered_round_count"], 2)
        self.assertEqual(tied["ranking"]["best_bundle_ids"], ["bundle_1", "bundle_2"])
        argv = _argv(self.directory, self.earlier, self.later, [reverse, forward])
        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
            main()
        self.assertEqual(json.loads(stdout.getvalue()), tied)

        misses = self.compare([miss, foreign])["ranking"]
        self.assertEqual(misses["status"], "NO_COMPATIBLE_COVERAGE")
        self.assertEqual(misses["best_covered_round_count"], 0)
        self.assertEqual(misses["best_missing_round_count"], 4)
        self.assertEqual(misses["best_bundle_ids"], [])
        self.assertEqual(misses["ranks"], [])


if __name__ == "__main__":
    unittest.main()
