"""Check verified receipt comparison semantics and offline failure paths."""

import json
import sys
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.replay_receipt import _write_replay_receipt  # noqa: E402
from austins_algorithm.replay_receipt_comparison import (  # noqa: E402
    _compare_verified_bundles, _read_comparison_bundle, compare_replay_receipts,
)


REQUEST = b'{"schema_version":1,"witness":{"validation_seeds":[41,43]}}'
OPENQASM = "OPENQASM 3.0;\n"


def _report(timing):
    batch = {
        "counts": {"010": 15, "101": 1},
        "timing_seconds": {"verification": timing},
        "simulation": {"aer_experiment_seconds": timing, "status": "COMPLETED"},
    }
    return {
        "snapshot": {
            "limits": {"local_result_wait_seconds": 30},
            "candidates": {"complement": {"batches": [deepcopy(batch)]}},
            "state_preparation": {"direct": {"wall_seconds": timing}},
        },
        "witness_budget": {
            "timing_seconds": {"workflow_wall": timing},
            "classical_reference": {"wall_seconds": timing, "valid": True},
            "candidates": {"direct": {"validation": deepcopy(batch), "validation_batches": [deepcopy(batch)]}},
        },
    }


class ReplayReceiptComparisonTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = TemporaryDirectory(prefix="replay-receipt-compare-", dir=build)
        self.addCleanup(temp.cleanup)
        self.left = Path(temp.name) / "left"
        self.right = Path(temp.name) / "right"

    @staticmethod
    def _bundle(directory, *, request=REQUEST, report=None, openqasm=OPENQASM):
        directory.mkdir()
        _write_replay_receipt(
            directory, request=request, report=_report(0.1) if report is None else report, openqasm=openqasm,
        )

    @staticmethod
    def _replace_report(directory, raw):
        (directory / "report.json").write_bytes(raw)
        path = directory / "receipt.json"
        receipt = json.loads(path.read_bytes())
        receipt["artifacts"]["report.json"] = {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_matches_formatted_request_and_different_report_timings(self):
        self._bundle(self.left)
        self._bundle(
            self.right,
            request=b'\xef\xbb\xbf{ "witness": {"validation_seeds": [41, 43]}, "schema_version": 1 }\r\n',
            report=_report(0.9),
        )
        result = compare_replay_receipts(self.left, self.right)
        self.assertEqual(result["status"], "REPLAY_RECEIPTS_MATCH")
        self.assertTrue(all(section["matches"] for section in result["sections"].values()))
        for name in ("request", "report"):
            self.assertFalse(result["sections"][name]["exact_bytes_match"])
            self.assertIsNone(result["sections"][name]["first_difference"])

    def test_probability_roundoff_records_only_known_compilation_and_cover_leaves(self):
        left = _report(0.1)
        left["candidates"] = {"direct": {"compilation": {"verification": {
            "compilation_total_variation_distance": 1e-15,
            "compiled_probabilities": {"00": 0.25},
        }}}}
        left["cross_cover_total_variation_distance"] = 0.0
        right = deepcopy(left)
        right["candidates"]["direct"]["compilation"]["verification"].update(
            compilation_total_variation_distance=2e-15, compiled_probabilities={"00": 0.250000000001},
        )
        right["cross_cover_total_variation_distance"] = 5e-16
        self._bundle(self.left, report=left)
        self._bundle(self.right, report=right)

        def compare():
            return _compare_verified_bundles(
                self.left, self.right, _read_comparison_bundle(self.left), _read_comparison_bundle(self.right),
                probability_roundoff=True,
            )

        self.assertEqual(compare_replay_receipts(self.left, self.right)["status"], "REPLAY_RECEIPTS_DIFFER")
        result = compare()
        self.assertEqual(result["status"], "REPLAY_RECEIPTS_MATCH")
        report = result["sections"]["report"]
        self.assertFalse(report["exact_bytes_match"])
        evidence = report["probability_roundoff"]
        self.assertEqual(evidence["accepted_count"], 3)
        self.assertEqual(evidence["maximum_absolute_difference"], "1E-12")
        self.assertTrue(evidence["comparison_complete"])
        self.assertIn({
            "path": "/candidates/direct/compilation/verification/compiled_probabilities/00",
            "left": "0.25", "right": "0.250000000001", "absolute_difference": "1E-12",
        }, evidence["accepted_differences"])

        right["snapshot"]["candidates"]["complement"]["batches"][0]["counts"]["010"] = 14
        self._replace_report(self.right, json.dumps(right).encode("utf-8"))
        report = compare()["sections"]["report"]
        self.assertFalse(report["matches"])
        self.assertFalse(report["probability_roundoff"]["comparison_complete"])
        self.assertEqual(report["first_difference"]["path"], "/snapshot/candidates/complement/batches/0/counts/010")

        compiled = right["candidates"]["direct"]["compilation"]["verification"]["compiled_probabilities"]
        compiled["00"] = 0.250000000002
        self._replace_report(self.right, json.dumps(right).encode("utf-8"))
        report = compare()["sections"]["report"]
        self.assertFalse(report["matches"])
        self.assertEqual(report["first_difference"]["path"],
                         "/candidates/direct/compilation/verification/compiled_probabilities/00")

    def test_finds_reordered_seed_and_changed_counts_with_qasm_and_version_drift(self):
        self._bundle(self.left)
        report = _report(0.9)
        report["snapshot"]["candidates"]["complement"]["batches"][0]["counts"] = {"010": 14, "101": 2}
        self._bundle(
            self.right, request=REQUEST.replace(b"[41,43]", b"[43,41]"), report=report, openqasm=OPENQASM + "// changed\n",
        )
        path = self.right / "receipt.json"
        receipt = json.loads(path.read_bytes())
        receipt["software_versions"]["python"] += "-different"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        result = compare_replay_receipts(self.left, self.right)
        self.assertEqual(result["status"], "REPLAY_RECEIPTS_DIFFER")
        sections = result["sections"]
        self.assertEqual(sections["request"]["first_difference"],
                         {"path": "/witness/validation_seeds/0", "kind": "VALUE"})
        self.assertEqual(sections["report"]["first_difference"],
                         {"path": "/snapshot/candidates/complement/batches/0/counts/010", "kind": "VALUE"})
        self.assertEqual(sections["software_versions"]["first_difference"], {"path": "/python", "kind": "VALUE"})
        self.assertFalse(sections["selected_qasm"]["matches"])
        self.assertNotEqual(sections["selected_qasm"]["left"]["sha256"], sections["selected_qasm"]["right"]["sha256"])

    def test_reports_missing_null_and_types_with_escaped_paths(self):
        self._bundle(self.left, request=b'{"k~/":[null,true]}')
        self._bundle(self.right, request=b'{"k~/":[null,1]}')
        result = compare_replay_receipts(self.left, self.right)
        self.assertEqual(result["sections"]["request"]["first_difference"], {"path": "/k~0~1/1", "kind": "TYPE"})
        self._replace_report(self.left, b'{"k~/": [null]}')
        self._replace_report(self.right, b'{"k~/": []}')
        report = compare_replay_receipts(self.left, self.right)["sections"]["report"]
        self.assertEqual(report["first_difference"], {"path": "/k~0~1/0", "kind": "RIGHT_MISSING"})
        self._replace_report(self.right, b'{}')
        report = compare_replay_receipts(self.left, self.right)["sections"]["report"]
        self.assertEqual(report["first_difference"], {"path": "/k~0~1", "kind": "RIGHT_MISSING"})

    def test_compares_absent_and_present_exports(self):
        self._bundle(self.left, openqasm=None)
        self._bundle(self.right)
        self.assertEqual(compare_replay_receipts(self.left, self.left)["sections"]["selected_qasm"],
                         {"matches": True, "left": None, "right": None})
        comparison = compare_replay_receipts(self.left, self.right)["sections"]["selected_qasm"]
        self.assertFalse(comparison["matches"])
        self.assertIsNone(comparison["left"])
        self.assertEqual(comparison["right"]["bytes"], len(OPENQASM))

    def test_keeps_timeout_limits_and_unrecognized_timing_paths(self):
        self._bundle(self.left)
        report = _report(0.9)
        report["snapshot"]["limits"]["local_result_wait_seconds"] = 35
        self._bundle(self.right, report=report)
        expected = {"path": "/snapshot/limits/local_result_wait_seconds", "kind": "VALUE"}
        self.assertEqual(compare_replay_receipts(self.left, self.right)["sections"]["report"]["first_difference"], expected)
        self._replace_report(self.left, b'{"timing_seconds":1}')
        self._replace_report(self.right, b'{"timing_seconds":2}')
        expected = {"path": "/timing_seconds", "kind": "VALUE"}
        self.assertEqual(compare_replay_receipts(self.left, self.right)["sections"]["report"]["first_difference"], expected)

    def test_rejects_changed_bytes_even_when_comparing_a_bundle_to_itself(self):
        self._bundle(self.left)
        path = self.left / "report.json"
        path.write_bytes(path.read_bytes().replace(b'"010": 15', b'"010": 14', 1))
        with self.assertRaisesRegex(ValueError, "left: receipt SHA-256 mismatch for report.json"):
            compare_replay_receipts(self.left, self.left)

    def test_rejects_invalid_comparison_json_even_with_matching_fingerprints(self):
        self._bundle(self.left)
        self._bundle(self.right)
        for raw in (b'{"counts": NaN}', b'{"counts": 1, "counts": 2}', b'[]'):
            with self.subTest(raw=raw):
                self._replace_report(self.right, raw)
                with self.assertRaisesRegex(ValueError, "right report"):
                    compare_replay_receipts(self.left, self.right)

    def test_reports_precise_numeric_changes(self):
        self._bundle(self.left)
        self._bundle(self.right)
        self._replace_report(self.left, b'{"probability":0.1000000000000000001}')
        self._replace_report(self.right, b'{"probability":0.1000000000000000002}')
        expected = {"path": "/probability", "kind": "VALUE"}
        self.assertEqual(compare_replay_receipts(self.left, self.right)["sections"]["report"]["first_difference"], expected)


if __name__ == "__main__":
    unittest.main()

