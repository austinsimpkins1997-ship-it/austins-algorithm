"""Check replay receipt integrity and publication boundaries."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.constraint_replay import run_constraint_replay  # noqa: E402
from austins_algorithm.replay_receipt import verify_replay_receipt  # noqa: E402


REPLAY = "austins_algorithm.constraint_replay._evaluate_replay"
REQUEST = b'\xef\xbb\xbf{ "schema_version": 1 }\r\n'
REPORT = {"selected_cover": "direct", "counts": {"01": 16}}
OPENQASM = "OPENQASM 3.0;\n"


class ReplayReceiptTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = TemporaryDirectory(prefix="replay-receipt-", dir=build)
        self.addCleanup(temp.cleanup)
        self.request = Path(temp.name) / "input.json"
        self.request.write_bytes(REQUEST)
        self.bundle = Path(temp.name) / "run"
        self.receipt = self.bundle / "receipt.json"

    def _run(self, *, openqasm=OPENQASM):
        selected = None if openqasm is None else SimpleNamespace(openqasm=openqasm)
        with patch(REPLAY, return_value=({}, {}, SimpleNamespace(report=REPORT, selected=selected))):
            return run_constraint_replay(self.request, receipt_directory=self.bundle)

    def test_saves_the_bytes_consumed_by_the_replay(self):
        def evaluate(raw, *, check_only):
            self.assertEqual(raw, REQUEST)
            self.assertFalse(check_only)
            self.request.write_bytes(b"modified during evaluation")
            selected = SimpleNamespace(openqasm=OPENQASM)
            return {}, {}, SimpleNamespace(report=REPORT, selected=selected)

        with patch(REPLAY, side_effect=evaluate):
            run_constraint_replay(self.request, receipt_directory=self.bundle)
        self.assertEqual((self.bundle / "request.json").read_bytes(), REQUEST)
        self.assertEqual(json.loads((self.bundle / "report.json").read_bytes()), REPORT)
        self.assertEqual((self.bundle / "selected.qasm").read_bytes(), OPENQASM.encode("utf-8"))
        with patch(REPLAY, side_effect=AssertionError("verifier attempted evaluation")):
            verified = verify_replay_receipt(self.bundle)
        self.assertEqual(verified["status"], "REPLAY_RECEIPT_VERIFIED")
        self.assertTrue(verified["software_versions"]["austins_algorithm"])

    def test_detects_artifact_corruption_and_length_changes(self):
        self._run()
        for name in ("request.json", "report.json", "selected.qasm"):
            path = self.bundle / name
            original = path.read_bytes()
            for changed, reason in (
                (bytes([original[0] ^ 1]) + original[1:], "SHA-256 mismatch"),
                (original + b"x", "size mismatch"),
                (original[:-1], "size mismatch"),
            ):
                with self.subTest(name=name, reason=reason, length=len(changed)):
                    path.write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, reason):
                        verify_replay_receipt(self.bundle)
            path.write_bytes(original)
        self.assertEqual(verify_replay_receipt(self.bundle)["status"], "REPLAY_RECEIPT_VERIFIED")

    def test_no_selected_export_is_recorded_and_checked(self):
        self._run(openqasm=None)
        self.assertIsNone(verify_replay_receipt(self.bundle)["artifacts"]["selected.qasm"])
        (self.bundle / "selected.qasm").write_text(OPENQASM, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "did not record selected.qasm"):
            verify_replay_receipt(self.bundle)

    def test_rejects_invalid_receipt_fields_and_sizes(self):
        self._run()
        original = self.receipt.read_bytes()
        for field, value in (("bytes", True), ("bytes", 10**9), ("sha256", "invalid")):
            with self.subTest(field=field, value=value):
                document = json.loads(original)
                document["artifacts"]["request.json"][field] = value
                self.receipt.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid receipt fingerprint"):
                    verify_replay_receipt(self.bundle)
        document = json.loads(original)
        document["schema_version"] = True
        self.receipt.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported replay receipt schema_version"):
            verify_replay_receipt(self.bundle)
        self.receipt.write_bytes(b'{"schema_version":1,' + original.lstrip()[1:])
        with self.assertRaisesRegex(ValueError, "duplicate receipt JSON key"):
            verify_replay_receipt(self.bundle)

    def test_does_not_evaluate_into_an_existing_directory(self):
        self.bundle.mkdir()
        existing = self.bundle / "report.json"
        existing.write_bytes(b"keep")
        with patch(REPLAY) as evaluate:
            with self.assertRaises(FileExistsError):
                run_constraint_replay(self.request, receipt_directory=self.bundle)
            evaluate.assert_not_called()
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_failed_evaluation_releases_empty_reservation(self):
        with patch(REPLAY, side_effect=ValueError("evaluation failed")):
            with self.assertRaisesRegex(ValueError, "evaluation failed"):
                run_constraint_replay(self.request, receipt_directory=self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_failed_artifact_write_leaves_no_receipt(self):
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            if path == self.bundle / "selected.qasm":
                raise OSError("export unavailable")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            with self.assertRaisesRegex(OSError, "export unavailable"):
                self._run()
        self.assertEqual((self.bundle / "request.json").read_bytes(), REQUEST)
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()

