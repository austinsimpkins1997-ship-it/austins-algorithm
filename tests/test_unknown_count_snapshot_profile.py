"""SDK-free tests for compact snapshot profile evidence and replay."""

from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm.__main__ import main as cli_main
from austins_algorithm.unknown_count_snapshot_profile import (
    export_snapshot_profile_bundle,
    verify_snapshot_profile_bundle,
)
from austins_algorithm.unknown_count_snapshot_selection import select_snapshot_unknown_count_budgets
from test_unknown_count_snapshot_selection import _SUITE, _panel, _seal


_TRACE_MARKER = "synthetic-source-trial-detail"


def _source_panel():
    panel = _panel()
    suite = panel["observations"][_SUITE]
    suite["plans"] = {
        "z_first": {"selected_budget": {
            "policy": "stop", "max_shots": 2, "max_oracle_queries": 1, "max_rounds_per_shot": 256,
        }},
        "a_second": {"selected_budget": {
            "policy": "stop", "max_shots": 1, "max_oracle_queries": 0, "max_rounds_per_shot": 256,
        }},
    }
    for case in suite["cases"].values():
        comparison = case["comparison"]
        comparison["plans"] = {
            name: {
                "budget": plan["selected_budget"],
                "lanes": {"ideal": {"trials": [{"seed": comparison["seeds"][0], "marker": _TRACE_MARKER}]}},
            }
            for name, plan in suite["plans"].items()
        }
    return _seal(panel)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bundle_digest(bundle):
    return sha256(_encode({key: value for key, value in bundle.items() if key != "bundle_sha256"})).hexdigest()


def _select(evidence, **kwargs):
    return select_snapshot_unknown_count_budgets(
        evidence, _SUITE, target_probability=.4, shot_caps=[1, 2], query_caps=[0, 1], **kwargs,
    )


def _cli_bytes(*arguments):
    stdout = StringIO()
    with patch.object(sys, "argv", ["austins_algorithm", *map(str, arguments)]), redirect_stdout(stdout):
        cli_main()
    return stdout.getvalue().encode("utf-8")


class UnknownCountSnapshotProfileTests(unittest.TestCase):
    def test_export_preserves_bound_evidence_and_discards_source_trials_and_grid(self):
        panel = _source_panel()
        original = deepcopy(panel)
        bundle = export_snapshot_profile_bundle(panel, expected_observations_sha256=panel["observations_sha256"])
        self.assertEqual(panel, original)
        self.assertEqual(bundle["schema_version"], 1)
        self.assertEqual(bundle["artifact"], "LOCAL_UNKNOWN_COUNT_SNAPSHOT_PROFILE_BUNDLE")
        self.assertEqual(bundle["source"]["observations_sha256"], panel["observations_sha256"])
        self.assertEqual(bundle["bundle_sha256"], _bundle_digest(bundle))
        self.assertEqual(bundle["suites"][_SUITE]["solution_counts"], [1])

        source_bytes, bundle_bytes = _encode(panel), _encode(bundle)
        for detail in (b'"candidates":', b'"trials":', _TRACE_MARKER.encode("utf-8")):
            self.assertIn(detail, source_bytes)
            self.assertNotIn(detail, bundle_bytes)
        verified = verify_snapshot_profile_bundle(
            bundle, source_panel=panel, expected_bundle_sha256=bundle["bundle_sha256"],
        )
        self.assertEqual(verified["status"], "SNAPSHOT_PROFILE_BUNDLE_VERIFIED")
        self.assertEqual(verified["bundle_sha256"], bundle["bundle_sha256"])
        self.assertTrue(verified["source_panel_matched"])

    def test_sorted_key_round_trip_preserves_every_selection_and_replay_field(self):
        panel = _source_panel()
        bundle = json.loads(_encode(export_snapshot_profile_bundle(panel)))
        expected = _select(panel, validation_trials=2)
        observed = _select(
            bundle, validation_trials=2, expected_observations_sha256=panel["observations_sha256"],
            expected_bundle_sha256=bundle["bundle_sha256"],
        )
        self.assertEqual(observed["source"].pop("bundle_sha256"), bundle["bundle_sha256"])
        self.assertEqual(observed, expected)
        source_labels = [
            label for budget in observed["validation"]["budgets"] for label in budget["labels"]
            if label.startswith("source_ideal_")
        ]
        self.assertEqual(source_labels, ["source_ideal_z_first", "source_ideal_a_second"])

    def test_verification_rejects_stale_and_wrong_pinned_hashes(self):
        panel = _source_panel()
        bundle = export_snapshot_profile_bundle(panel)
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            export_snapshot_profile_bundle(panel, expected_observations_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            verify_snapshot_profile_bundle(bundle, expected_bundle_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            _select(bundle, expected_bundle_sha256="f" * 64)

        bundle["source"]["observations_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            verify_snapshot_profile_bundle(bundle)
        with self.assertRaisesRegex(ValueError, r"(?i)sha.?256|digest|fingerprint"):
            _select(bundle)

    def test_resealed_valid_mutation_disagrees_with_its_source(self):
        panel = _source_panel()
        bundle = export_snapshot_profile_bundle(panel)
        plans = bundle["suites"][_SUITE]["source_plans"]
        plans.reverse()
        bundle["bundle_sha256"] = _bundle_digest(bundle)
        verified = verify_snapshot_profile_bundle(bundle, expected_bundle_sha256=bundle["bundle_sha256"])
        self.assertFalse(verified["source_panel_matched"])
        with self.assertRaisesRegex(ValueError, r"(?i)source|profile"):
            verify_snapshot_profile_bundle(bundle, source_panel=panel)

    def test_blocked_holdout_is_preserved_without_replay_evidence(self):
        panel = _source_panel()
        blocked = panel["observations"][_SUITE]["cases"]["2"]["comparison"]
        blocked.update(
            status="INSUFFICIENT_TARGET_WIDTH", circuit_qubits=8,
            audit=None, anchor_layout=None, compilations={}, plans={},
        )
        panel.update(status="COMPLETED_WITH_BLOCKED_CASES", case_statuses={"COMPLETED": 2, "INSUFFICIENT_TARGET_WIDTH": 1})
        bundle = export_snapshot_profile_bundle(_seal(panel))
        verified = verify_snapshot_profile_bundle(bundle, source_panel=panel)
        self.assertEqual(verified["suites"][_SUITE]["declared_counts"], [1])
        self.assertEqual(verified["suites"][_SUITE]["completed_counts"], [1, 3])
        self.assertEqual(verified["suites"][_SUITE]["blocked_counts"], [2])
        comparison = bundle["suites"][_SUITE]["cases"]["2"]["comparison"]
        self.assertEqual(comparison["status"], "INSUFFICIENT_TARGET_WIDTH")
        self.assertEqual(comparison["compilations"], {})
        self.assertEqual(comparison["rounds_to_prepare"], [])
        self.assertEqual(_select(bundle)["status"], "UNKNOWN_COUNT_TARGET_MET")
        with self.assertRaisesRegex(ValueError, r"(?i)complete|blocked"):
            _select(bundle, validation_trials=2)

    def test_cli_exports_verifies_and_selects_from_compact_file(self):
        panel = _source_panel()
        panel_bytes = json.dumps(panel, indent=2).encode("utf-8")
        build = (TESTS.parent / ".build").resolve()
        if not build.is_relative_to(TESTS.parent):
            raise RuntimeError("snapshot profile test output must stay in the package")
        build.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=build, prefix="snapshot-profile-test-") as directory:
            source = Path(directory) / "panel.json"
            compact = Path(directory) / "bundle.json"
            source.write_bytes(panel_bytes)
            bundle_bytes = _cli_bytes(
                "unknown-count-snapshot-profile-export", source,
                "--expected-observations-sha256", panel["observations_sha256"],
            )
            compact.write_bytes(bundle_bytes)
            bundle = json.loads(bundle_bytes)
            self.assertEqual(bundle_bytes, _encode(bundle) + b"\n")
            digest = bundle["bundle_sha256"]
            verified = json.loads(_cli_bytes(
                "unknown-count-snapshot-profile-verify", compact, "--source-panel", source,
                "--expected-bundle-sha256", digest,
            ))
            selected = json.loads(_cli_bytes(
                "unknown-count-snapshot-select", compact, "--suite", _SUITE, "--target-probability", ".4",
                "--shot-caps", "1", "2", "--query-caps", "0", "1", "--expected-bundle-sha256", digest,
            ))
        self.assertTrue(verified["source_panel_matched"])
        self.assertEqual(verified["source_panel_file_sha256"], sha256(panel_bytes).hexdigest())
        self.assertEqual(verified["bundle_file_sha256"], sha256(bundle_bytes).hexdigest())
        self.assertEqual(selected["status"], "UNKNOWN_COUNT_TARGET_MET")
        self.assertEqual(selected["source"]["bundle_sha256"], digest)
        self.assertEqual(selected["source"]["bundle_file_sha256"], verified["bundle_file_sha256"])


if __name__ == "__main__":
    unittest.main()
