"""Failure signals, ordering and bounded same-store telemetry recovery."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from future_reader.controller import Controller
from future_reader.telemetry import SCHEMA, assess, demo, recover


def sample(sequence=1, worker_ms=1000, *, progress=0, revision=0, version=1, state="S", worker_state="BUSY"):
    return {
        "schema": SCHEMA, "kind": "PRACTICE", "session": "practice",
        "worker": "worker-one", "boot": "boot-one", "sequence": sequence,
        "event": {"clock": "utc-model", "time_ms": worker_ms, "uncertainty_ms": 10},
        "arrival": {"clock": "utc-model", "time_ms": worker_ms + 100, "uncertainty_ms": 10,
                    "receiver_clock": "host-boot-one", "mono_ms": worker_ms + 100},
        "worker_mono_ms": worker_ms, "transport": "loopback-fixture",
        "payload": {"state": state, "revision": revision, "state_version": version,
                    "progress": progress, "worker_state": worker_state},
    }


def monitor(now, *, healthy=True):
    return {"id": "monitor-one", "receiver_clock": "host-boot-one", "at_mono_ms": now, "healthy": healthy}


class TelemetryTests(unittest.TestCase):
    def test_heartbeat_timeout_equality_and_independent_current_monitor(self):
        report = assess([sample()], now_mono_ms=3600, monitor=monitor(3600))
        self.assertEqual(report["status"], "HEALTHY_PRACTICE_STREAM")
        self.assertEqual(report["delivery_latency_interval_ms"], [80, 120])
        report = assess([sample()], now_mono_ms=3601, monitor=monitor(3601))
        self.assertEqual(report["status"], "RECOVERY_CANDIDATE")
        self.assertEqual(report["flags"], ["WORKER_HEARTBEAT_LOST"])
        for evidence in (None, monitor(2600), monitor(3601, healthy=False)):
            report = assess([sample()], now_mono_ms=3601, monitor=evidence)
            self.assertEqual(report["status"], "HOLD_FOR_EVIDENCE")
            self.assertNotIn("WORKER_HEARTBEAT_LOST", report["flags"])

    def test_stalled_work_progress_and_new_progress(self):
        stalled = [sample(1, 1000), sample(2, 3500), sample(3, 5500)]
        report = assess(stalled, now_mono_ms=5600, monitor=monitor(5600))
        self.assertEqual(report["flags"], ["WORKER_STALLED"])
        self.assertEqual(report["busy_without_progress_ms"], 4500)
        stalled[-1] = sample(3, 5500, progress=1)
        report = assess(stalled, now_mono_ms=5600, monitor=monitor(5600))
        self.assertEqual(report["status"], "HEALTHY_PRACTICE_STREAM")
        self.assertEqual(report["busy_without_progress_ms"], 0)

    def test_delayed_delivery_and_uncertain_time_hold_recovery(self):
        value = sample(worker_state="FAILED")
        value["arrival"]["time_ms"] = 2000
        result = assess([value], now_mono_ms=1100, monitor=monitor(1100))
        self.assertIn("DELAYED_DELIVERY", result["flags"])
        self.assertEqual(result["status"], "HOLD_FOR_EVIDENCE")
        value["arrival"]["time_ms"] = 1740
        result = assess([value], now_mono_ms=1100, monitor=monitor(1100))
        self.assertIn("DELIVERY_LATENCY_UNCERTAIN", result["flags"])
        value["arrival"]["time_ms"] = 920
        result = assess([value], now_mono_ms=1100, monitor=monitor(1100))
        self.assertIn("CLOCK_CALIBRATION_INVALID", result["flags"])
        value["arrival"]["clock"] = "unsynchronized"
        result = assess([value], now_mono_ms=1100, monitor=monitor(1100))
        self.assertIn("CLOCK_CALIBRATION_UNAVAILABLE", result["flags"])
        self.assertIsNone(result["delivery_latency_interval_ms"])

    def test_duplicates_reordered_conflicting_or_missing_samples(self):
        first = sample()
        repeated = deepcopy(first)
        repeated["arrival"]["mono_ms"] = 3500
        report = assess([first, repeated], now_mono_ms=3601, monitor=monitor(3601))
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["heartbeat_age_ms"], 2501)
        repeated["payload"]["progress"] = 1
        second = sample(3, 1200)
        report = assess([first, second, repeated], now_mono_ms=3601, monitor=monitor(3601))
        self.assertIn("CONFLICTING_SEQUENCE", report["flags"])
        self.assertIn("SEQUENCE_GAP", report["flags"])
        later_first = deepcopy(first)
        later_first["arrival"]["mono_ms"] = 1500
        report = assess([later_first, second], now_mono_ms=3601, monitor=monitor(3601))
        self.assertIn("REORDERED_DELIVERY", report["flags"])

    def test_restart_clock_and_state_discontinuity_stay_unresolved(self):
        first, second = sample(1, 1000), sample(2, 900, state="B")
        second["arrival"]["mono_ms"] = 1200
        report = assess([first, second], now_mono_ms=1200, monitor=monitor(1200))
        self.assertTrue({"STATE_DISCONTINUITY", "WORKER_MONOTONIC_DISCONTINUITY", "EVENT_CLOCK_DISCONTINUITY"}
                        <= set(report["flags"]))
        second["boot"] = "boot-two"
        second["arrival"]["receiver_clock"] = "host-boot-two"
        report = assess([first, second], now_mono_ms=1200, monitor=monitor(1200))
        self.assertIn("WORKER_RESTART", report["flags"])
        self.assertIn("RECEIVER_CLOCK_DISCONTINUITY", report["flags"])
        self.assertIsNone(report["heartbeat_age_ms"])

    def test_simulation_and_hardware_reports_do_not_authorize_practice_transfer(self):
        value = sample()
        for kind, shots in (("SIMULATED_STATEVECTOR", None), ("REPORTED_HARDWARE_OUTCOME", 1)):
            value["kind"] = kind
            value["payload"] = {"basis": "Z", "backend": "fixture", "shots": shots, "result_ref": "case-1"}
            report = assess([value], now_mono_ms=1100, monitor=monitor(1100))
            self.assertEqual(report["evidence_class"], kind)
            self.assertIn("NO_PRACTICE_STATE", report["flags"])
            self.assertEqual(report["status"], "HOLD_FOR_EVIDENCE")

    def test_transfer_rechecks_authorization_checkpoint_and_both_deadlines(self):
        failed = sample(worker_state="FAILED")
        time = {"now_mono_ms": 1100, "monitor": monitor(1100)}
        with TemporaryDirectory() as temp:
            path = Path(temp) / "worker.sqlite"
            with Controller(path) as source:
                source.set_grant("practice", True)
                pin = source.checkpoint()["sha256"]
                kwargs = {"expected_sha256": pin, "samples": [failed], **time}
                self.assertEqual(recover(source, "practice", "replacement", **kwargs)["reason"],
                                 "RECOVERY_NOT_AUTHORIZED")
                self.assertEqual(recover(source, "practice", "replacement", authorized=True, limits={"deadline_mono_ms": 1099},
                                         **kwargs)["assessment"]["flags"],
                                 ["RECEIVER_DEADLINE_MISSED", "REPORTED_PRACTICE_FAILURE"])
                source.set_context(now=7)
                result = recover(source, "practice", "replacement", authorized=True, **kwargs)
                self.assertEqual(result["reason"], "TELEMETRY_CHECKPOINT_MISMATCH")
                failed["payload"]["state_version"] = source.pinpoint()["state_version"]
                kwargs["expected_sha256"] = source.checkpoint()["sha256"]
                result = recover(source, "practice", "replacement", authorized=True, **kwargs)
                self.assertEqual(result["reason"], "WINDOW_CLOSED")
                self.assertTrue(source.pinpoint()["writable"])
            with Controller(path, "another") as source:
                source.set_grant("practice", True)
                old = source.travel("practice")
                failed["session"] = "another"
                failed["payload"]["state_version"] = source.pinpoint()["state_version"]
                result = recover(source, "practice", "replacement", authorized=True, samples=[failed],
                                 expected_sha256=source.checkpoint()["sha256"], **time)
                self.assertEqual(result["status"], "TRANSFERRED")
                self.assertEqual(result["evidence_sha256"], result["receipt"]["recovery_evidence_sha256"])
                with self.assertRaisesRegex(ValueError, "historical"):
                    source.commit("practice", old["preview_id"])
                with Controller(path, "replacement") as replacement:
                    self.assertEqual(replacement.pinpoint()["state"], "S")
                    self.assertTrue(replacement.pinpoint()["writable"])

    def test_demo_and_bad_inputs(self):
        result = demo()
        self.assertFalse(result["original"]["writable"])
        self.assertEqual(result["recovery"]["status"], "TRANSFERRED")
        self.assertIn("DELAYED_DELIVERY", result["late_transport"]["flags"])
        with self.assertRaisesRegex(ValueError, "Arrival cannot"):
            assess([sample()], now_mono_ms=1099)
        with self.assertRaisesRegex(ValueError, "at most 256"):
            assess([sample()] * 257, now_mono_ms=1100)
        report = assess([], now_mono_ms=0, monitor=monitor(0))
        self.assertEqual(report["evidence_class"], "UNKNOWN")
        self.assertIn("NO_BASELINE", report["flags"])


if __name__ == "__main__":
    unittest.main()
