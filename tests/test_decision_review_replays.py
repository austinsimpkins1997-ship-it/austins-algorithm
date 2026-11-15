import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from austins_algorithm.decision_review_replays import MAX_REPLAY_BYTES, inspect_replay, load_replay, replay_examples


class DecisionReviewReplayTests(unittest.TestCase):
    def test_recounts_synthetic_faults_and_recovery_by_work_id(self):
        result = inspect_replay(replay_examples()["containment"])
        normal, fault, queued, recovery = result["scenarios"]
        self.assertEqual(normal["completed_once"], 5)
        self.assertEqual([fault[key] for key in ("lost", "duplicated", "pending", "missing")], [1, 1, 1, 1])
        self.assertEqual(fault["duplicate_effects"], 1)
        self.assertEqual(fault["reconciled"], 3)
        self.assertEqual(fault["items"][-1]["status"], "missing")
        self.assertEqual(queued["pending"], 5)
        self.assertTrue(recovery["recovery_complete"])
        self.assertEqual(recovery["completed_once"], recovery["accepted"])

    def test_rejects_ledger_disagreement_and_unsubstantiated_recovery(self):
        replay = replay_examples()["containment"]
        replay["data"][1]["recovery_observed"] = True
        with self.assertRaisesRegex(ValueError, "recovery conflicts"):
            inspect_replay(replay)
        replay = replay_examples()["containment"]
        replay["data"][0]["ledger"][0]["status"] = "pending"
        with self.assertRaisesRegex(ValueError, "effects disagree"):
            inspect_replay(replay)

    def test_rejects_duplicate_ledger_ids_and_unhandled_evidence_kinds(self):
        replay = replay_examples()["containment"]
        replay["data"][0]["ledger"][1]["id"] = replay["data"][0]["ledger"][0]["id"]
        with self.assertRaisesRegex(ValueError, "Ledger IDs must be unique"):
            inspect_replay(replay)
        replay = replay_examples()["containment"]
        replay["provenance"]["origin"] = "live incident"
        with self.assertRaisesRegex(ValueError, "labeled synthetic"):
            inspect_replay(replay)

    def test_stasis_compares_recorded_phases_and_rejects_changed_claim(self):
        replay = replay_examples()["stasis"]
        summary = inspect_replay(replay)
        self.assertEqual(summary["first_divergence"], 3)
        self.assertEqual([step["legacy"] for step in summary["steps"]], ["STOP", "RESET_HELD", "ARMED", "RUN"])
        self.assertEqual([step["fixed"] for step in summary["steps"]], ["STOP", "RESET_HELD", "STOP", "STOP"])
        self.assertIn("fixed trace trips", summary["steps"][2]["description"])
        self.assertIn("step 3", summary["bench_checks"][0]["setup"])
        self.assertTrue(all(check["evidence"].startswith("PENDING PHYSICAL MEASUREMENT") for check in summary["bench_checks"]))
        replay["data"]["trace"][-1]["legacy_phase"] = 0
        with self.assertRaisesRegex(ValueError, "final trace row"):
            inspect_replay(replay)

    def test_native_model_import_preserves_file_fingerprint_and_replays(self):
        data = replay_examples()["stasis"]["data"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "formal-model.json"
            raw = json.dumps({"legacy_raw_start_counterexample": data}).encode()
            path.write_bytes(raw)
            replay = load_replay(path)
        self.assertEqual(replay["provenance"]["source"], path.name)
        self.assertEqual(replay["provenance"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(inspect_replay(replay)["first_divergence"], 3)

    def test_native_comparison_import_maps_matched_lanes(self):
        comparison = {
            "schema": "decision-containment-comparison/1", "scope": "SYNTHETIC_LOCAL",
            "scenarios": [{"id": "retry", "title": "Retry during recovery", "workload": [{"id": "request-1"}],
                           "baseline": {"metrics": {"accepted": 1, "recovery_step": None},
                                        "events": [{"step": 2, "event": "WORK_EFFECT"}],
                                        "ledger": [{"id": "request-1", "status": "processed", "effects": 2}]},
                           "guarded": {"metrics": {"accepted": 1, "recovery_step": 4}, "events": [],
                                       "ledger": [{"id": "request-1", "status": "processed", "effects": 1}]}}],
        }
        baseline, guarded = inspect_replay(comparison)["scenarios"]
        self.assertEqual(baseline["duplicated"], 1)
        self.assertEqual(baseline["timeline"], ["Step 2 · WORK_EFFECT"])
        self.assertTrue(guarded["recovery_complete"])

    def test_loader_rejects_duplicate_fields_and_oversized_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            path.write_bytes(b'{"schema":1,"schema":2}')
            with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
                load_replay(path)
            path.write_bytes(b" " * (MAX_REPLAY_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size limit"):
                load_replay(path)
