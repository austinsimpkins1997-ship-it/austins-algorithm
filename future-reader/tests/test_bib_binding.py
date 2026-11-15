"""Verify the optional native BIB boundary against an explicit local implementation."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from future_reader.bib_binding import export_bib_fixture, inspect_bib


class BindingTests(unittest.TestCase):
    def setUp(self):
        build = Path(__file__).resolve().parents[1] / ".build"
        build.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="bib-test-", dir=build)
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_ledger_rejects_ambiguous_json_before_native_runner(self):
        source = self.directory / "missing.py"
        ledger = self.directory / "ledger.json"
        for payload in ('{"field":0,"field":1}', "[]", "[null]", "[NaN]"):
            ledger.write_text(payload, encoding="ascii")
            with self.assertRaises(ValueError):
                inspect_bib(ledger, bib_source=source)

    def test_unreviewed_source_is_rejected_without_execution(self):
        source = self.directory / "ledger.py"
        source.write_text("raise SystemExit('must not execute')", encoding="ascii")
        ledger = self.directory / "chain.json"
        ledger.write_text("[{}]", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "reviewed digest"):
            inspect_bib(ledger, bib_source=source)
        with self.assertRaisesRegex(ValueError, "reviewed digest"):
            export_bib_fixture(self.directory / "output.json", bib_source=source)
        self.assertFalse((self.directory / "output.json").exists())

    @unittest.skipUnless(os.environ.get("FUTURE_READER_BIB_SOURCE"), "set FUTURE_READER_BIB_SOURCE for native integration")
    def test_native_export_readback_preserves_axes_and_detects_tampering(self):
        source = Path(os.environ["FUTURE_READER_BIB_SOURCE"])
        source_file = source / "bib" / "ledger.py" if source.is_dir() else source
        before = source_file.read_bytes()
        ledger = self.directory / "export.json"
        report = export_bib_fixture(ledger, bib_source=source)
        self.assertEqual(report["entries"], 2)
        original = ledger.read_bytes()
        inspected = inspect_bib(ledger, bib_source=source)
        self.assertEqual(inspected["status"], "NATIVE_CHAIN_VERIFIED")
        self.assertEqual(inspected["source_sha256"], report["source_sha256"])
        self.assertEqual(inspected["records"][1]["native_temporal_standing"], "TO_BE")
        self.assertEqual(inspected["records"][1]["native_evidence_status"], "NOT_ASSESSED")
        self.assertIsNone(inspected["records"][1]["times_as_recorded"]["event_time"])
        self.assertNotIn("chronotense_standing", inspected["records"][1])
        with self.assertRaises(FileExistsError):
            export_bib_fixture(ledger, bib_source=source)
        self.assertEqual(ledger.read_bytes(), original)
        tampered = self.directory / "tampered.json"
        entries = deepcopy(json.loads(original))
        entries[0]["claim"] = "changed after verification"
        tampered.write_text(json.dumps(entries), encoding="ascii")
        with self.assertRaisesRegex(ValueError, "Native BIB verification rejected"):
            inspect_bib(tampered, bib_source=source)
        self.assertEqual(source_file.read_bytes(), before)
