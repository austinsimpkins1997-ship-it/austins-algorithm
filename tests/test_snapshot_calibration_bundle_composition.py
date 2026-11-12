"""Verify deterministic no-clobber composition of pinned compilation envelopes."""

from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_compilation_bundle as bundle_format
from austins_algorithm.__main__ import main
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report


_A, _B, _C, _D, _E = (character * 64 for character in "abcde")


def _entry(qpy, *, phase="0x0.0p+0", report=None):
    return {"qpy": qpy, "global_phase_hex": phase, "report": {} if report is None else report}


class SnapshotCalibrationBundleCompositionTests(unittest.TestCase):
    def setUp(self):
        self.directory = _test_directory(self)
        self.versions = {**bundle_format._bundle_software_versions(), "qiskit": "composition-fixture-version"}

    def bundle(self, name, entries, **overrides):
        path = self.directory / f"{name}.json"
        document = {"schema_version": 1, "software_versions": self.versions, "entries": entries, **overrides}
        return path, _write_report(path, document)

    def compose(self, destination, bundles):
        return bundle_format.compose_snapshot_calibration_compilation_bundles(destination, bundles=bundles)

    def cli_args(self, destination, bundles):
        argv = ["austins-algorithm", "snapshot-calibration-bundle-compose", str(destination)]
        for path, pin in bundles:
            argv.extend(("--bundle", str(path), pin))
        return argv

    def test_exact_union_deduplicates_typed_rows_and_is_independent_of_source_order(self):
        # Equal report content is deliberately serialized with opposite field orders.
        left_only = _entry("opaque A", report={"input": {"wires": [0, 2]}})
        shared = _entry("opaque B", report={"z": 3, "a": [False, 1, 1.0]})
        shared_reordered = _entry("opaque B", report={"a": [False, 1, 1.0], "z": 3})
        right_only = _entry("opaque C", phase="0x1.0000000000000p-3", report={"seed": 19})
        left = self.bundle("left", {_B: shared, _A: left_only})
        right = self.bundle("right", {_C: right_only, _B: shared_reordered},
                            software_versions=dict(reversed(list(self.versions.items()))))
        duplicate = self.bundle("duplicate", {_B: shared})
        sources = (left, right, duplicate)
        inputs = {path: path.read_bytes() for path, _ in sources}
        destination = self.directory / "union.json"
        reversed_destination = self.directory / "reversed.json"
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            result = self.compose(destination, sources)
            reversed_result = self.compose(reversed_destination, sources[::-1])
        decode.assert_not_called()
        self.assertEqual(result["status"], "COMPILATION_BUNDLE_COMPOSED")
        self.assertEqual(result["software_versions"], self.versions)
        self.assertEqual({item["sha256"]: item["entry_count"] for item in result["sources"]}, {
            left[1]: 2, right[1]: 2, duplicate[1]: 1,
        })
        self.assertFalse(result["stored_qpy_decoded"])
        self.assertFalse(result["runtime_coverage_checked"])
        self.assertEqual(result["unique_entry_count"], 3)
        self.assertEqual(result["duplicate_entry_count"], 2)
        self.assertEqual(result["overlaps"], {_B: sorted(pin for _, pin in sources)})
        self.assertEqual(result["conflicts"], {})
        raw = destination.read_bytes()
        self.assertEqual(result["output"], {"sha256": sha256(raw).hexdigest(), "bytes": len(raw), "entry_count": 3})
        self.assertEqual(reversed_result, result)
        self.assertEqual(reversed_destination.read_bytes(), raw)
        document = json.loads(raw)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["software_versions"], self.versions)
        self.assertEqual(list(document["entries"]), [_A, _B, _C])
        self.assertEqual(document["entries"], {_A: left_only, _B: shared, _C: right_only})
        self.assertIs(type(document["entries"][_B]["report"]["a"][1]), int)
        self.assertIs(type(document["entries"][_B]["report"]["a"][2]), float)
        self.assertEqual({path: path.read_bytes() for path in inputs}, inputs)

    def test_qpy_phase_and_type_conflicts_are_explicit_and_never_publish(self):
        shared = _entry("opaque identical row")
        left = self.bundle("left", {
            _A: _entry("opaque left qpy"),
            _B: _entry("opaque phase"),
            _C: _entry("opaque report", report={"verified": True}),
            _D: shared,
            _E: _entry("opaque score", report={"score": 1.0}),
        })
        right = self.bundle("right", {
            _A: _entry("opaque right qpy"),
            _B: _entry("opaque phase", phase="0x1.0000000000000p-3"),
            _C: _entry("opaque report", report={"verified": 1}),
            _D: shared,
            _E: _entry("opaque score", report={"score": 1}),
        })
        destination = self.directory / "conflict.json"
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            report = self.compose(destination, [left, right])
        decode.assert_not_called()
        self.assertEqual(report["status"], "COMPILATION_BUNDLE_COMPOSITION_CONFLICT")
        self.assertEqual(report["unique_entry_count"], 5)
        self.assertEqual(report["duplicate_entry_count"], 1)
        source_pins = sorted((left[1], right[1]))
        self.assertEqual(report["overlaps"], {_D: source_pins})
        self.assertEqual(report["conflicts"], {
            key: {"source_sha256s": source_pins, "differing_fields": [field]}
            for key, field in ((_A, "qpy"), (_B, "global_phase_hex"), (_C, "report"), (_E, "report"))
        })
        self.assertIsNone(report["output"])
        self.assertFalse(report["stored_qpy_decoded"])
        self.assertFalse(report["runtime_coverage_checked"])
        with (
            patch.object(sys, "argv", self.cli_args(destination, [left, right])),
            redirect_stdout(StringIO()) as stdout, self.assertRaises(SystemExit) as exit_status,
        ):
            main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_checks_pins_candidate_shape_schema_and_matching_software(self):
        first = self.bundle("first", {_A: _entry("opaque A")})
        second = self.bundle("second", {_B: _entry("opaque B")})
        clone = self.directory / "clone.json"
        clone.write_bytes(first[0].read_bytes())
        bad_schema = self.bundle("schema", {_B: _entry("opaque B")}, schema_version=True)
        bad_versions = self.bundle("versions", {_B: _entry("opaque B")},
                                   software_versions={**self.versions, "qiskit": "different-version"})
        destination = self.directory / "must-stay-absent.json"
        cases = (
            ([first], r"(?i)(bundle|source|candidate)"),
            ([first] * 9, r"(?i)(bundle|source|candidate)"),
            ([first, (second[0],)], r"(?i)(path|pin|bundle|source)"),
            ([first, (second[0], "0" * 64)], r"(?i)(sha-?256|digest)"),
            ([first, (clone, first[1])], r"(?i)(duplicate|distinct)"),
            ([first, bad_schema], r"(?i)schema"),
            ([first, bad_versions], r"(?i)(software|version)"),
        )
        for sources, message in cases:
            with self.subTest(sources=sources), self.assertRaisesRegex(ValueError, message):
                self.compose(destination, sources)
            self.assertFalse(destination.exists())

    def test_enforces_aggregate_entry_and_output_byte_bounds(self):
        first = self.bundle("first", {_A: _entry("opaque A", report={"data": "x" * 4096})})
        second = self.bundle("second", {_B: _entry("opaque B", report={"data": "y" * 4096})})
        destination = self.directory / "too-large.json"
        with (
            patch.object(bundle_format, "_MAX_ENTRIES", 1),
            self.assertRaisesRegex(ValueError, r"(?i)(count|entry|round|limit)"),
        ):
            self.compose(destination, [first, second])
        self.assertFalse(destination.exists())
        largest_source_bytes = max(path.stat().st_size for path, _ in (first, second))
        with (
            patch.object(bundle_format, "_MAX_BUNDLE_BYTES", largest_source_bytes),
            self.assertRaisesRegex(ValueError, r"(?i)(size|byte|limit)"),
        ):
            self.compose(destination, [first, second])
        self.assertFalse(destination.exists())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"first.json", "second.json"})

    def test_preserves_occupied_destination_and_cleans_up_after_publication_race(self):
        first = self.bundle("first", {_A: _entry("opaque A")})
        second = self.bundle("second", {_B: _entry("opaque B")})
        destination = self.directory / "occupied.json"
        sentinel = b"previous destination"
        destination.write_bytes(sentinel)
        with self.assertRaises(FileExistsError):
            self.compose(destination, [first, second])
        self.assertEqual(destination.read_bytes(), sentinel)

        raced = self.directory / "raced.json"
        publish = bundle_format.os.link

        def racing_writer(staged, target):
            self.assertEqual(Path(target), raced)
            raced.write_bytes(b"won the publication race")
            return publish(staged, target)

        with (
            patch.object(bundle_format.os, "link", side_effect=racing_writer) as link,
            self.assertRaises(FileExistsError),
        ):
            self.compose(raced, [first, second])
        link.assert_called_once()
        self.assertEqual(raced.read_bytes(), b"won the publication race")
        self.assertEqual({path.name for path in self.directory.iterdir()}, {
            "first.json", "second.json", "occupied.json", "raced.json",
        })

    def test_cli_success_emits_output_pin_for_inspection(self):
        first = self.bundle("first", {_A: _entry("opaque A")})
        second = self.bundle("second", {_B: _entry("opaque B")})
        destination = self.directory / "union.json"
        argv = self.cli_args(destination, [first, second])
        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
            main()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "COMPILATION_BUNDLE_COMPOSED")
        inspected = bundle_format.inspect_snapshot_calibration_compilation_bundle(
            destination, expected_sha256=report["output"]["sha256"],
        )
        self.assertEqual(inspected["entry_keys"], [_A, _B])
        self.assertEqual(inspected["software_versions"], self.versions)


if __name__ == "__main__":
    unittest.main()
