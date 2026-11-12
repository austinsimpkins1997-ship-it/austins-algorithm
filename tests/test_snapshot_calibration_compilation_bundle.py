"""Exercise pinned, portable compilation reuse for archived IBM calibrations."""

from base64 import b64decode, b64encode
from contextlib import redirect_stdout
from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundle_format
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_study import run_snapshot_calibration_study
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned


def _test_directory(testcase):
    build = (PACKAGE / ".build").resolve()
    if not build.is_relative_to(PACKAGE):
        raise RuntimeError("compilation bundle test output must stay in the package")
    build.mkdir(exist_ok=True)
    directory = TemporaryDirectory(dir=build, prefix="calibration-bundle-test-")
    testcase.addCleanup(directory.cleanup)
    return Path(directory.name)


def _write_report(path, report):
    raw = json.dumps(report, allow_nan=False).encode("utf-8")
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


class SnapshotCalibrationCompilationBundleInputTests(unittest.TestCase):
    def test_requires_a_valid_matching_file_pin_before_parsing_any_json(self):
        path = _test_directory(self) / "invalid.json"
        invalid = b"this is deliberately invalid JSON"
        path.write_bytes(invalid)
        for pin in ("", "A" * 64, "g" * 64, "0" * 64):
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest)"):
                SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=pin)
        with self.assertRaisesRegex(ValueError, "JSON"):
            SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=sha256(invalid).hexdigest())

    def test_rejects_files_over_the_byte_cap_even_with_a_matching_pin(self):
        path = _test_directory(self) / "oversized.json"
        data = b'{"over":"small test byte cap"}'
        path.write_bytes(data)
        with patch.object(bundle_format, "_MAX_BUNDLE_BYTES", len(data) - 1):
            with self.assertRaisesRegex(ValueError, "size limit"):
                SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=sha256(data).hexdigest())


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationCompilationBundleTests(unittest.TestCase):
    def setUp(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "config.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        properties = self.directory / "later.json"
        properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, properties)
        self.earlier = self.earlier_archive("earlier", "2021-07-26T11:47:01-04:00", .25)

    def earlier_archive(self, name, date, false_one):
        props = json.loads(Path(self.later.properties).read_bytes())
        props["last_update_date"] = date
        values = {"prob_meas1_prep0": false_one, "prob_meas0_prep1": .125, "readout_error": (false_one + .125) / 2}
        for item in props["qubits"][0]:
            if item["name"] in values:
                item["value"] = values[item["name"]]
        properties = self.directory / f"{name}.json"
        properties.write_text(json.dumps(props), encoding="utf-8")
        return _pinned(Path(self.later.configuration), properties)

    def compare(self, cache=None, *, max_rounds=1, earlier=None):
        return comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier if earlier is None else earlier, self.later,
            initial_layout=(0, 1), max_rounds=max_rounds, compilation_cache=cache,
        )

    def bundle(self, *, max_rounds=1):
        cache = SnapshotCalibrationCompilationCache()
        report = self.compare(cache, max_rounds=max_rounds)
        self.assertEqual(report["status"], "COMPLETED")
        path = self.directory / "compilation.json"
        digest = cache.save_bundle(path)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), digest)
        return cache, path, digest, report

    def test_loaded_bundle_reuses_rounds_with_exact_report_order_and_fresh_calibration(self):
        cache, path, digest, reference = self.bundle()
        document = json.loads(path.read_bytes())
        self.assertEqual(document["schema_version"], 1)
        self.assertIsInstance(document["software_versions"], dict)
        self.assertEqual(len(document["entries"]), 2)
        for key, row in document["entries"].items():
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertTrue(b64decode(row["qpy"], validate=True))
            self.assertTrue(math.isfinite(float.fromhex(row["global_phase_hex"])))
            self.assertEqual(row["report"]["status"], "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION")

        loaded = SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=digest)
        self.assertIsNot(loaded, cache)
        self.assertEqual((loaded.stats()["entries"], loaded.stats()["hits"], loaded.stats()["misses"]), (0, 0, 0))
        different_earlier = self.earlier_archive("earlier-alt", "2022-01-01T01:00:00+00:00", .375)
        with (
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot",
                         wraps=comparison._prepare_density_snapshot) as prepare_density,
            patch.object(comparison, "_read_archive", wraps=comparison._read_archive) as read_archive,
        ):
            replay = self.compare(loaded)
            changed = self.compare(loaded, earlier=different_earlier)
        compile_round.assert_not_called()
        self.assertEqual(prepare_density.call_count, 8)
        self.assertEqual(read_archive.call_count, 8)
        self.assertEqual(json.dumps(replay), json.dumps(reference))
        self.assertEqual(changed["rounds"]["1"]["later"], reference["rounds"]["1"]["later"])
        self.assertNotEqual(changed["rounds"]["1"]["earlier"], reference["rounds"]["1"]["earlier"])
        self.assertEqual((loaded.stats()["entries"], loaded.stats()["hits"], loaded.stats()["misses"]), (2, 4, 0))
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            loaded.save_bundle(path)
        self.assertEqual(path.read_bytes(), original)

    def test_cli_loads_a_prefix_and_saves_an_extension_to_a_new_path(self):
        from qiskit import qpy

        _, prefix, digest, _ = self.bundle(max_rounds=0)
        original = prefix.read_bytes()
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        extended = self.directory / "extension.json"
        expected = run_snapshot_calibration_study(
            _MARK_00, self.earlier, self.later, initial_layouts=[(0, 1)], target_probability=.7,
            shot_caps=[2], query_caps=[1], validation_trials=4,
        )
        argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
        for flag, files in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(files.configuration), str(files.properties),
                         files.configuration_sha256, files.properties_sha256))
        argv.extend((
            "--initial-layout", "0", "1", "--target-probability", ".7", "--shot-caps", "2", "--query-caps", "1",
            "--validate-trials", "4", "--load-compilation-bundle", str(prefix), digest,
            "--save-compilation-bundle", str(extended),
        ))
        with (
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
            patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round,
            patch.object(qpy, "dump", wraps=qpy.dump) as serialize,
        ):
            main()
        self.assertEqual(compile_round.call_count, 1)
        compiled_exports = [call for call in serialize.call_args_list if call.args[0].layout is not None]
        self.assertEqual(len(compiled_exports), 1)
        self.assertEqual(json.dumps(json.loads(stdout.getvalue())), json.dumps(expected))
        self.assertEqual(prefix.read_bytes(), original)
        extension_bytes = extended.read_bytes()
        original_entries = json.loads(original)["entries"]
        new_entries = json.loads(extension_bytes)["entries"]
        self.assertEqual(len(original_entries), 1)
        self.assertEqual(len(new_entries), 2)
        self.assertEqual({key: new_entries[key] for key in original_entries}, original_entries)
        loaded = SnapshotCalibrationCompilationCache.load_bundle(
            extended, expected_sha256=sha256(extension_bytes).hexdigest(),
        )
        with patch.object(comparison, "compile_and_verify") as compile_round:
            self.assertEqual(self.compare(loaded), expected["captures"]["0,1"]["report"])
        compile_round.assert_not_called()

    def test_wrong_pin_invalid_base64_and_multiple_circuits_are_rejected_before_qpy_decode(self):
        from qiskit import QuantumCircuit, qpy

        _, path, digest, _ = self.bundle(max_rounds=0)
        document = json.loads(path.read_bytes())
        row = next(iter(document["entries"].values()))
        row["qpy"] = "not a base64 circuit!"
        tampered = self.directory / "bad-encoding.json"
        new_digest = _write_report(tampered, document)
        multiple = BytesIO()
        qpy.dump([QuantumCircuit(1), QuantumCircuit(1)], multiple)
        with patch.object(qpy, "load") as decode:
            with self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest)"):
                SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=digest)
            loaded = SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=new_digest)
            with self.assertRaisesRegex(ValueError, r"(?i)(base64|qpy|circuit)"):
                self.compare(loaded, max_rounds=0)
            row["qpy"] = b64encode(multiple.getvalue()).decode("ascii")
            new_digest = _write_report(tampered, document)
            loaded = SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=new_digest)
            with self.assertRaisesRegex(ValueError, r"(?i)(one bounded circuit|one circuit)"):
                self.compare(loaded, max_rounds=0)
        decode.assert_not_called()

    def test_rejects_resealed_schema_version_software_and_entry_key_before_qpy_decode(self):
        from qiskit import qpy

        _, path, _, _ = self.bundle(max_rounds=0)
        changes = (
            ("schema_version", 2, "schema version"),
            ("software_versions", {"qiskit": "mismatched"}, "software versions"),
            ("entries", {"not-a-digest": {}}, "round entry"),
            ("entries", {}, "round count"),
        )
        for field, value, message in changes:
            with self.subTest(field=field, value=value), patch.object(qpy, "load") as decode:
                document = json.loads(path.read_bytes())
                document[field] = value
                tampered = self.directory / "bad-document.json"
                digest = _write_report(tampered, document)
                with self.assertRaisesRegex(ValueError, message):
                    SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=digest)
                decode.assert_not_called()

    def test_repinned_inconsistent_compilation_evidence_is_rejected(self):
        _, path, _, _ = self.bundle()
        changes = (
            (("report", "initial_logical_to_target_qubit"), [1, 0], r"(?i)(layout|mapping|report)"),
            (("report", "openqasm", "classical_bit_to_target_qubit"), [1, 0], r"(?i)(map|qasm|report)"),
            (("report", "openqasm", "sha256"), "0" * 64, r"(?i)(qasm|digest|report)"),
            (("global_phase_hex",), "inf", r"(?i)(phase|qpy|circuit)"),
            (("global_phase_hex",), float(.125).hex(), r"(?i)global phase"),
            (("qpy",), b64encode(b"not QPY").decode("ascii"), r"(?i)(qpy|circuit|bundle)"),
        )
        for fields, value, message in changes:
            with self.subTest(fields=fields):
                document = json.loads(path.read_bytes())
                rows = document["entries"].values()
                row = min(rows, key=lambda item: item["report"]["input"]["two_qubit_gates"])
                parent = row
                for field in fields[:-1]:
                    parent = parent[field]
                parent[fields[-1]] = value
                tampered = self.directory / "repinned.json"
                digest = _write_report(tampered, document)
                with (
                    patch.object(comparison, "compile_and_verify") as compile_round,
                    patch.object(comparison, "_prepare_density_snapshot") as simulate,
                    self.assertRaisesRegex((RuntimeError, ValueError), message),
                ):
                    loaded = SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=digest)
                    self.compare(loaded)
                compile_round.assert_not_called()
                simulate.assert_not_called()

    def test_repinned_valid_qpy_for_a_different_round_cannot_silently_replace_evidence(self):
        _, path, _, _ = self.bundle()
        document = json.loads(path.read_bytes())
        first, second = sorted(document["entries"].values(), key=lambda item: item["report"]["input"]["two_qubit_gates"])
        first["qpy"] = second["qpy"]
        first["global_phase_hex"] = second["global_phase_hex"]
        tampered = self.directory / "wrong-round.json"
        digest = _write_report(tampered, document)
        with self.assertRaisesRegex((RuntimeError, ValueError), r"(?i)(qasm|verification|report|circuit)"):
            loaded = SnapshotCalibrationCompilationCache.load_bundle(tampered, expected_sha256=digest)
            self.compare(loaded)


if __name__ == "__main__":
    unittest.main()
