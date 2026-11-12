"""Verify isolated, bounded compilation reuse for dated snapshot comparisons."""

from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_study import run_snapshot_calibration_study


_SDK_MODULES = ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import")
_MARK_00 = BooleanConstraint(2, ((-1,), (-2,)))
_MARK_11 = BooleanConstraint(2, ((1,), (2,)))


def _pinned(configuration, properties):
    return comparison.SnapshotCalibrationFiles(
        configuration, properties, sha256(configuration.read_bytes()).hexdigest(),
        sha256(properties.read_bytes()).hexdigest(),
    )


class SnapshotCalibrationCompilationCacheInputTests(unittest.TestCase):
    def test_cache_starts_empty_with_bounded_defaults(self):
        cache = SnapshotCalibrationCompilationCache()
        self.assertEqual(cache.stats(), {
            "entries": 0, "bytes": 0, "hits": 0, "misses": 0, "evictions": 0,
            "max_entries": 64, "max_bytes": 8 * 1024 * 1024,
        })
        for limits in ({"max_entries": True}, {"max_entries": 0}, {"max_bytes": True}, {"max_bytes": 1023}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                SnapshotCalibrationCompilationCache(**limits)

    def test_both_entrypoints_reject_invalid_cache_type_before_archive_reads(self):
        archive = comparison.SnapshotCalibrationFiles("configuration.json", "properties.json", "a" * 64, "b" * 64)
        with patch.object(comparison, "_read_archive") as read:
            with self.assertRaisesRegex(TypeError, "compilation_cache"):
                comparison.compare_snapshot_calibrations(
                    _MARK_00, archive, archive, initial_layout=(0, 1), compilation_cache=object(),
                )
            with self.assertRaisesRegex(TypeError, "compilation_cache"):
                run_snapshot_calibration_study(
                    _MARK_00, archive, archive, initial_layouts=[(0, 1)], target_probability=.7,
                    shot_caps=[2], query_caps=[1], compilation_cache=object(),
                )
        read.assert_not_called()


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationCompilationCacheTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        build = (PACKAGE / ".build").resolve()
        if not build.is_relative_to(PACKAGE):
            raise RuntimeError("calibration cache test output must stay in the package")
        build.mkdir(exist_ok=True)
        directory = TemporaryDirectory(dir=build, prefix="calibration-compile-cache-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "config.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        properties = self.directory / "later.json"
        properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, properties)
        self.earlier = self.earlier_archive("earlier", "2021-07-26T11:47:01-04:00", .25, .125)

    def earlier_archive(self, name, date, false_one, false_zero):
        contents = json.loads(Path(self.later.properties).read_bytes())
        contents["last_update_date"] = date
        values = {"prob_meas1_prep0": false_one, "prob_meas0_prep1": false_zero,
                  "readout_error": (false_one + false_zero) / 2}
        for item in contents["qubits"][0]:
            if item["name"] in values:
                item["value"] = values[item["name"]]
        properties = self.directory / f"{name}.json"
        properties.write_text(json.dumps(contents), encoding="utf-8")
        return _pinned(Path(self.later.configuration), properties)

    def compare(self, cache=None, *, constraint=_MARK_00, earlier=None, later=None, **overrides):
        options = {"initial_layout": (0, 1), "max_rounds": 0, **overrides}
        return comparison.compare_snapshot_calibrations(
            constraint, self.earlier if earlier is None else earlier, self.later if later is None else later,
            compilation_cache=cache, **options,
        )

    def study(self, cache, layouts=((0, 1),)):
        return run_snapshot_calibration_study(
            _MARK_00, self.earlier, self.later, initial_layouts=layouts, target_probability=.7,
            shot_caps=[2], query_caps=[1], compilation_cache=cache,
        )

    def test_cross_entrypoint_and_horizon_reuse_compiles_only_missing_rounds(self):
        reference = self.compare(max_rounds=1)
        self.assertEqual(reference["status"], "COMPLETED")
        cache = SnapshotCalibrationCompilationCache()
        with (
            patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round,
            patch.object(comparison, "_prepare_density_snapshot",
                         wraps=comparison._prepare_density_snapshot) as prepare_density,
            patch.object(comparison, "_read_archive", wraps=comparison._read_archive) as read_archive,
        ):
            prefix = self.compare(cache)
            study = self.study(cache)
            warm = self.compare(cache, max_rounds=1)
        self.assertEqual(compile_round.call_count, 2)
        self.assertEqual(prepare_density.call_count, 10)
        self.assertEqual(read_archive.call_count, 12)
        self.assertEqual(prefix["rounds"]["0"], reference["rounds"]["0"])
        self.assertEqual(study["captures"]["0,1"]["report"], reference)
        self.assertEqual(warm, reference)
        stats = cache.stats()
        self.assertEqual((stats["entries"], stats["hits"], stats["misses"], stats["evictions"]), (2, 3, 2, 0))
        self.assertGreater(stats["bytes"], 0)

    def test_invalid_archive_pin_is_rejected_before_a_warm_cache_lookup(self):
        cache = SnapshotCalibrationCompilationCache()
        self.assertEqual(self.compare(cache)["status"], "COMPLETED")
        original_stats = cache.stats()
        for archive in (self.earlier, self.later):
            path = Path(archive.properties)
            original = path.read_bytes()
            with self.subTest(path=path.name):
                try:
                    path.write_bytes(original + b"\n")
                    with (
                        patch.object(comparison, "compile_and_verify") as compile_round,
                        patch.object(comparison, "_prepare_density_snapshot") as simulate,
                        self.assertRaisesRegex(ValueError, "expected SHA-256 digest"),
                    ):
                        self.compare(cache)
                    compile_round.assert_not_called()
                    simulate.assert_not_called()
                finally:
                    path.write_bytes(original)
                self.assertEqual(cache.stats(), original_stats)

    def test_invalidation_follows_later_target_logical_circuit_seed_and_ordered_layout(self):
        from qiskit.transpiler import InstructionProperties

        cache = SnapshotCalibrationCompilationCache()
        reference = self.compare(cache, max_rounds=1)
        new_earlier = self.earlier_archive("earlier-alt", "2022-01-01T01:00:00+00:00", .375, .0625)
        repinned_path = self.directory / "repinned-later.json"
        repinned_path.write_bytes(Path(self.later.properties).read_bytes() + b"\n")
        repinned_later = _pinned(Path(self.later.configuration), repinned_path)
        build_backend = comparison._offline_backend
        sdk_version = comparison.version
        later_date = json.loads(Path(self.later.properties).read_bytes())["last_update_date"]

        def changed_backend(configuration, properties):
            backend = build_backend(configuration, properties)
            if properties["last_update_date"] == later_date:
                current = backend.target["sx"][(0,)]
                backend.target.update_instruction_properties(
                    "sx", (0,), InstructionProperties(duration=current.duration, error=.125),
                )
            return backend

        with patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round:
            changed_earlier = self.compare(cache, earlier=new_earlier, max_rounds=1)
            self.assertEqual(changed_earlier["status"], "COMPLETED")
            compile_round.assert_not_called()
            self.assertEqual(changed_earlier["rounds"]["1"]["later"], reference["rounds"]["1"]["later"])
            self.assertNotEqual(changed_earlier["rounds"]["1"]["earlier"], reference["rounds"]["1"]["earlier"])
            changed_oracle = self.compare(cache, constraint=_MARK_11, max_rounds=1)
            self.assertEqual(changed_oracle["audit"]["marked_states"], ["11"])
            self.assertEqual(compile_round.call_count, 1)
            for index, options in enumerate((
                {"seed_transpiler": 11}, {"initial_layout": (1, 0)}, {"later": repinned_later},
            ), start=2):
                with self.subTest(options=options):
                    self.assertEqual(self.compare(cache, **options)["status"], "COMPLETED")
                    self.assertEqual(compile_round.call_count, index)
            with patch.object(comparison, "_offline_backend", side_effect=changed_backend):
                changed_target = self.compare(cache)
            self.assertEqual(changed_target["status"], "COMPLETED")
            self.assertEqual(changed_target["snapshots"]["later"], reference["snapshots"]["later"])
            self.assertNotEqual(changed_target["rounds"]["0"]["later"], reference["rounds"]["0"]["later"])
            self.assertEqual(compile_round.call_count, 5)
            with patch.object(
                comparison, "version", side_effect=lambda name: "test-version" if name == "qiskit" else sdk_version(name),
            ):
                changed_sdk = self.compare(cache)
            self.assertEqual(changed_sdk["status"], "COMPLETED")
            self.assertEqual(changed_sdk["versions"]["qiskit"], "test-version")
            self.assertEqual(compile_round.call_count, 6)

    def test_failed_layout_publishes_no_rounds_and_keeps_prior_completed_layout(self):
        cache = SnapshotCalibrationCompilationCache()
        prepare = comparison._prepare_density_snapshot
        attempts = 0

        def prepare_or_timeout(*args, **options):
            nonlocal attempts
            attempts += 1
            if attempts == 5:
                return None, {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": True}
            return prepare(*args, **options)

        with patch.object(comparison, "_prepare_density_snapshot", side_effect=prepare_or_timeout):
            study = self.study(cache, layouts=((0, 1), (1, 0)))
        self.assertEqual(attempts, 5)
        self.assertEqual(study["status"], "CALIBRATION_STUDY_PARTIAL")
        self.assertEqual(study["coverage"]["completed_layouts"], ["0,1"])
        self.assertEqual(study["coverage"]["unavailable_layouts"], ["1,0"])
        self.assertEqual((cache.stats()["entries"], cache.stats()["misses"]), (2, 4))
        with patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round:
            self.assertEqual(self.compare(cache, max_rounds=1), study["captures"]["0,1"]["report"])
            compile_round.assert_not_called()
            retried = self.compare(cache, initial_layout=(1, 0), max_rounds=1)
            self.assertEqual(retried["status"], "COMPLETED")
            self.assertEqual(compile_round.call_count, 2)
        self.assertEqual((cache.stats()["entries"], cache.stats()["hits"], cache.stats()["misses"]), (4, 2, 6))

    def test_returned_reports_and_simulation_circuits_cannot_mutate_cached_entries(self):
        cache = SnapshotCalibrationCompilationCache()
        with patch.object(comparison, "_compact_snapshot_inputs", wraps=comparison._compact_snapshot_inputs) as compact:
            cold = self.compare(cache)
            expected = deepcopy(cold)
            cold_program = compact.call_args_list[0].args[1][0]
            cold_program.circuit.x(0)
            cold["rounds"]["0"]["compilation"]["openqasm"]["sha256"] = "changed"
            warm = self.compare(cache)
            warm_program = compact.call_args_list[2].args[1][0]
            self.assertEqual(warm, expected)
            self.assertIsNot(warm_program.circuit, cold_program.circuit)
            warm_program.circuit.x(0)
            warm["rounds"]["0"]["compilation"]["compiled"]["operations"].clear()
            again = self.compare(cache)
            self.assertEqual(again, expected)
            again_program = compact.call_args_list[4].args[1][0]
            self.assertIsNot(again_program.circuit, warm_program.circuit)
        self.assertEqual((cache.stats()["entries"], cache.stats()["hits"], cache.stats()["misses"]), (1, 2, 1))

    def test_entry_lru_refresh_and_oversized_entry_preserve_both_bounds(self):
        cache = SnapshotCalibrationCompilationCache(max_entries=2)
        first, second, third = (0, 1), (1, 0), (1, 2)
        with patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round:
            for layout, expected in ((first, 1), (second, 2), (first, 2), (third, 3), (first, 3), (second, 4)):
                with self.subTest(layout=layout, expected=expected):
                    self.assertEqual(self.compare(cache, initial_layout=layout)["status"], "COMPLETED")
                    self.assertEqual(compile_round.call_count, expected)
        stats = cache.stats()
        self.assertEqual((stats["entries"], stats["evictions"], stats["hits"], stats["misses"]), (2, 2, 2, 4))
        self.assertLessEqual(stats["bytes"], stats["max_bytes"])

        size_probe = SnapshotCalibrationCompilationCache()
        self.compare(size_probe)
        entry_bytes = size_probe.stats()["bytes"]
        self.assertGreater(entry_bytes, 1024)
        limited = SnapshotCalibrationCompilationCache(max_bytes=entry_bytes - 1)
        self.assertEqual(self.compare(limited), self.compare(limited))
        stats = limited.stats()
        self.assertEqual((stats["entries"], stats["bytes"], stats["hits"], stats["misses"]), (0, 0, 0, 2))


if __name__ == "__main__":
    unittest.main()
