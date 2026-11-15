"""Reference, timing boundary and adverse-state checks for practice operations."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from future_reader import controller


class PracticeControllerTests(unittest.TestCase):
    def check_rejection(self, reason, action, *args, **kwargs):
        with self.assertRaises(controller.ControllerError) as rejected:
            action(*args, **kwargs)
        self.assertEqual(rejected.exception.reason, reason)

    def test_timing_uses_complete_reserved_path_and_inclusive_boundary(self):
        self.assertEqual(controller.opportunity(5)["recommended"]["path"], ["S", "B", "G"])
        last = controller.opportunity(6)
        self.assertEqual(last["status"], "NARROWING")
        self.assertEqual(last["recommended"]["path"], ["S", "A", "G"])
        self.assertEqual(last["recommended"]["arrival"], 13)
        self.assertEqual(last["recommended"]["arrival"] + 2 + 1 + 1, 17)
        self.assertEqual(controller.opportunity(7)["status"], "MISSED")
        self.assertIsNone(controller.opportunity(6, False)["recommended"])
        self.assertEqual(controller.routes("A", "G", 0)[0]["arrival"], 12)
        self.assertIsNone(controller.opportunity(17)["recommended"])
        self.check_rejection("INVALID_INPUT", controller.opportunity, True)

    def test_wait_decision_preserves_unknown_check_and_fallback(self):
        at_threshold = controller.compare_wait(4, check_returns=6)
        self.assertEqual(at_threshold["act_now"]["cost"], 3)
        self.assertEqual(at_threshold["wait_positive_check"]["cost"], 6)
        self.assertIsNone(at_threshold["wait_negative_check"])
        self.assertEqual(at_threshold["latest_fallback_start"], 5)
        self.assertEqual(at_threshold["latest_decisive_return"], 6)
        self.assertTrue(at_threshold["can_change_eligibility"])
        too_late = controller.compare_wait(4, check_returns=7)
        self.assertFalse(too_late["can_change_eligibility"])
        self.assertIsNone(too_late["wait_positive_check"])

    def test_critical_path_uses_parallel_prerequisites_and_detects_invalid_order(self):
        stages = [
            {"id": "verify", "duration": 2, "requires": ["execute"]},
            {"id": "sync", "duration": 4, "requires": []},
            {"id": "approval", "duration": 3, "requires": []},
            {"id": "execute", "duration": 6, "requires": ["sync", "approval"]},
        ]
        result = controller.critical_path(stages, deadline=17, margin=1, clock_uncertainty=1)
        self.assertEqual(result["critical_path"], ["sync", "execute", "verify"])
        self.assertEqual(result["critical_path_bound"], 12)
        self.assertEqual(result["latest_feasible_start"], 5)
        self.assertEqual(result["latest_safe_start"], 3)
        stages[1]["requires"] = ["unknown"]
        self.check_rejection("MISSING_PREREQUISITE", controller.critical_path, stages, deadline=17)
        stages[1]["requires"] = ["verify"]
        self.check_rejection("DEPENDENCY_CYCLE", controller.critical_path, stages, deadline=17)

    def test_pending_preview_retention_does_not_drop_committed_receipt(self):
        with controller.Controller() as practice:
            practice.set_grant("operator", True)
            old = practice.travel("operator")
            for _ in range(controller._MAX_OPEN_PREVIEWS):
                recent = practice.travel("operator")
            self.check_rejection("UNKNOWN_PREVIEW", practice.commit, "operator", old["preview_id"])
            receipt = practice.commit("operator", recent["preview_id"])
            for _ in range(controller._MAX_OPEN_PREVIEWS):
                practice.reverse("operator")
            self.assertEqual(practice.commit("operator", recent["preview_id"]), receipt)

    def test_frontier_rejects_tampering_or_changed_window_and_exhausts(self):
        result = controller.search_slice(budget=1)
        self.assertEqual(result["status"], "PAUSED")
        saved = json.loads(json.dumps(result["checkpoint"]))
        changed = deepcopy(saved)
        changed["pending"] = []
        self.check_rejection("INVALID_CHECKPOINT", controller.search_slice, checkpoint=changed)
        self.check_rejection("STALE_FRONTIER", controller.search_slice, now=1, checkpoint=saved)
        self.check_rejection("STALE_FRONTIER", controller.search_slice, check_available=False, checkpoint=saved)
        while result["checkpoint"] is not None:
            result = controller.search_slice(budget=1, checkpoint=result["checkpoint"])
        self.assertEqual(result["status"], "EXHAUSTED")
        self.assertEqual(result["expanded"], 5)
        self.assertEqual(result["routes"], controller.routes())

    def test_program_checks_both_branch_shapes_and_enforces_transition_at_execution(self):
        program = {
            "schema": "future-reader.program/1",
            "steps": [{"op": "Start", "state": "S"}, {
                "op": "If", "condition": "independent_check",
                "then": [{"op": "Check", "condition": "independent_check"}, {"op": "Move", "to": "A"}],
                "else": [{"op": "Move", "to": "B"}],
            }, {"op": "Repeat", "times": 1, "steps": [{"op": "Move", "to": "G"}]},
                {"op": "Goal", "state": "G"}],
        }
        self.assertEqual(controller.execute_program(program)["path"], ["S", "A", "G"])
        self.assertEqual(controller.execute_program(program, check_available=False)["path"], ["S", "B", "G"])
        invalid = deepcopy(program)
        invalid["steps"][1]["else"] = [{"op": "Shell", "command": "not allowed"}]
        self.check_rejection("UNSUPPORTED_PIECE", controller.execute_program, invalid)
        invalid["steps"][1]["then"] = [{"op": "Move", "to": "G"}]
        invalid["steps"][1]["else"] = []
        self.check_rejection("INVALID_TRANSITION", controller.execute_program, invalid)

    def test_auth_revocation_and_context_changes_invalidate_previews(self):
        with controller.Controller() as practice:
            self.check_rejection("FORBIDDEN", practice.travel, "operator")
            practice.set_grant("operator", True)
            first = practice.travel("operator", path=["S", "A", "G"])
            self.assertEqual(practice.pinpoint()["state"], "S")
            self.assertEqual(practice.checkpoint()["content"]["history"], [])
            practice.set_grant("operator", False)
            self.check_rejection("FORBIDDEN", practice.commit, "operator", first["preview_id"])
            practice.set_grant("operator", True)
            self.check_rejection("STALE_PERMISSION", practice.commit, "operator", first["preview_id"])
            second = practice.travel("operator", path=["S", "A", "G"])
            practice.set_context(now=6, check_available=False)
            self.check_rejection("STALE_PREVIEW", practice.commit, "operator", second["preview_id"])
            self.assertEqual(practice.travel("operator")["status"], "UNAVAILABLE")
            self.check_rejection("CLOCK_REGRESSION", practice.set_context, now=5)

    def test_preview_expiry_and_clock_regression_do_not_commit(self):
        with controller.Controller() as practice:
            practice.set_grant("operator", True)
            with patch.object(controller.time, "time_ns", return_value=10_000_000):
                preview = practice.travel("operator")
            for stamp in (9, 300_011):
                with patch.object(controller.time, "time_ns", return_value=stamp * 1_000_000):
                    self.check_rejection("EXPIRED_PREVIEW", practice.commit, "operator", preview["preview_id"])
            self.assertEqual(practice.pinpoint()["revision"], 0)

    def test_failed_event_insert_rolls_back_position_and_receipt(self):
        with controller.Controller() as practice:
            practice.set_grant("operator", True)
            preview = practice.travel("operator")
            practice.connection.execute("""
                CREATE TEMP TRIGGER fail_event BEFORE INSERT ON practice_events BEGIN
                    SELECT RAISE(ABORT, 'injected interruption');
                END
            """)
            with self.assertRaisesRegex(controller.sqlite3.IntegrityError, "injected interruption"):
                practice.commit("operator", preview["preview_id"])
            self.assertEqual(practice.pinpoint()["state"], "S")
            self.assertEqual(practice.checkpoint()["content"]["history"], [])
            practice.connection.execute("DROP TRIGGER fail_event")
            result = practice.commit("operator", preview["preview_id"])
            self.assertEqual(result["state"], "G")
            self.assertEqual(practice.commit("operator", preview["preview_id"]), result)

    def test_two_writers_and_concurrent_retry_commit_once(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "practice.sqlite"
            with controller.Controller(path) as owner:
                owner.set_grant("operator", True)
                first = owner.travel("operator")["preview_id"]
                second = owner.travel("operator", path=["S", "A", "G"])["preview_id"]
            barrier = Barrier(2, timeout=10)

            def commit(preview):
                with controller.Controller(path) as contender:
                    barrier.wait()
                    try:
                        return contender.commit("operator", preview)
                    except controller.ControllerError as error:
                        return {"status": error.reason}

            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(commit, (first, second)))
            self.assertEqual(sorted(row["status"] for row in results), ["COMMITTED", "STALE_PREVIEW"])
            winner = next(row for row in results if row["status"] == "COMMITTED")
            with ThreadPoolExecutor(2) as pool:
                receipts = list(pool.map(commit, (winner["preview_id"], winner["preview_id"])))
            self.assertEqual(receipts, [winner, winner])
            with controller.Controller(path) as owner:
                self.assertEqual(len(owner.checkpoint()["content"]["history"]), 1)

    def test_recovery_retains_history_and_rechecks_window_after_restore(self):
        with controller.Controller() as practice:
            practice.set_grant("operator", True)
            moved = practice.commit("operator", practice.travel("operator")["preview_id"])
            practice.set_context(now=17)
            restored = practice.commit("operator", practice.reverse("operator")["preview_id"])
            self.assertEqual((moved["state"], restored["state"], restored["now"]), ("G", "S", 17))
            self.assertEqual(practice.travel("operator")["status"], "UNAVAILABLE")
            archive = practice.checkpoint()
            self.assertEqual([event["state"] for event in archive["content"]["history"]], ["G", "S"])
            self.assertTrue(controller.verify_checkpoint(archive))
            self.assertFalse(controller.verify_checkpoint(archive, expected_sha256="0" * 64))
            forged = deepcopy(archive)
            forged["content"]["history"][0]["plan"]["cost"] = 1
            canonical = json.dumps(forged["content"], sort_keys=True, separators=(",", ":")).encode("ascii")
            forged["sha256"] = sha256(canonical).hexdigest()
            self.assertFalse(controller.verify_checkpoint(forged))

    def test_checkpoint_handoff_fences_source_and_preserves_eligible_continuation(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "practice.sqlite"
            with controller.Controller(path) as source, controller.Controller(path) as stale:
                source.set_grant("operator", True)
                at_b = source.travel("operator", goal="B")
                source.commit("operator", at_b["preview_id"])
                prior = stale.travel("operator")
                expected = source.checkpoint()["sha256"]
                receipt = source.handoff("operator", "replacement", expected_sha256=expected)
                self.assertEqual((receipt["position"], receipt["revision"]), ("B", 1))
                self.assertFalse(source.pinpoint()["writable"])
                self.check_rejection("WRITER_FENCED", stale.commit, "operator", prior["preview_id"])
                self.assertTrue(controller.verify_checkpoint(source.checkpoint()))
                with controller.Controller(path, "replacement") as target:
                    self.assertEqual(target.pinpoint()["state"], "B")
                    self.assertEqual(target.commit("operator", target.travel("operator")["preview_id"])["state"], "G")
                    self.assertTrue(controller.verify_checkpoint(target.checkpoint()))

    def test_handoff_rejects_stale_or_expired_checkpoint_without_fencing(self):
        with controller.Controller() as practice:
            practice.set_grant("operator", True)
            self.check_rejection("INVALID_CHECKPOINT", practice.handoff, "operator", "next", expected_sha256=None)
            old = practice.checkpoint()["sha256"]
            practice.set_context(now=7)
            self.check_rejection("STALE_CHECKPOINT", practice.handoff, "operator", "next", expected_sha256=old)
            current = practice.checkpoint()["sha256"]
            self.check_rejection("WINDOW_CLOSED", practice.handoff, "operator", "next", expected_sha256=current)
            self.assertTrue(practice.pinpoint()["writable"])

    def test_certificates_are_scoped_recomputed_and_compared_with_reference(self):
        for tick in range(19):
            for available in (True, False):
                expected_cost = 3 if tick <= 5 else 6 if tick == 6 and available else None
                result = controller.route_certificate(now=tick, check_available=available)
                self.assertTrue(controller.verify_certificate(result))
                self.assertEqual(result["certificate"]["cost"], expected_cost)
        forged = controller.route_certificate()
        forged["certificate"]["cost"] = 2
        self.assertFalse(controller.verify_certificate(forged))
        forged = controller.route_certificate(goal="B")
        forged["certificate"]["cost"] = True
        self.assertFalse(controller.verify_certificate(forged))

    def test_checked_in_conformance_fixture_matches_python(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "operational.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), controller.conformance_fixture())


if __name__ == "__main__":
    unittest.main()
