"""Check the real worker entry, fencing, restart and atomic effects."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from future_reader import live_failover as worker


PROGRAM = {"schema": "future-reader.program/1", "steps": [
    {"op": "Start", "state": "S"}, {"op": "Move", "to": "B"}, {"op": "Move", "to": "G"},
    {"op": "Goal", "state": "G"},
]}


class LiveWorkerTests(unittest.TestCase):
    def test_public_demo_really_kills_and_recovers(self):
        with TemporaryDirectory() as directory:
            result = worker.demo(Path(directory) / "worker.sqlite")
        loss = result["injected_process_loss"]
        first, second = result["effects"]
        self.assertEqual(loss["checkpoint_cursor"], 1)
        self.assertNotEqual(loss["exit_code"], 0)
        self.assertEqual(first["worker_pid"], loss["worker_pid"])
        self.assertNotEqual(second["worker_pid"], loss["worker_pid"])
        self.assertEqual([first["step"], second["step"]], [0, 1])
        self.assertEqual([first["generation"], second["generation"]], [1, 2])
        self.assertEqual(result["generations"][1]["reason"], "OWNED_PROCESS_EXITED")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["checkpoint"]["cost"], 3)

    def test_expired_heartbeat_fences_an_old_generation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker.sqlite"
            worker.create_job(path, "practice", PROGRAM, work_ms=100)
            with worker._db(path) as connection:
                old = worker._claim(connection, "practice", baseline=(0, 0), reason="START")
                self.assertTrue(worker._heartbeat(connection, "practice", *old, 123, 0, begin=True))
                self.assertEqual(worker._commit(connection, "practice", *old, 123, 0), "RUNNING")
                self.assertEqual(worker._commit(connection, "practice", *old, 123, 0), "ALREADY_COMMITTED")
                recovered = worker.run(path, "practice", lease_ms=400, timeout_s=10)
                self.assertFalse(worker._heartbeat(connection, "practice", *old, 123, 1100))
                self.assertEqual(worker._commit(connection, "practice", *old, 123, 1), "FENCED")
            self.assertEqual(recovered["generations"][1]["reason"], "HEARTBEAT_STALLED")
            self.assertEqual(recovered["cursor"], 2)
            self.assertEqual([effect["generation"] for effect in recovered["effects"]], [1, 2])

    def test_two_supervisors_share_one_commit_sequence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker.sqlite"
            worker.create_job(path, "practice", PROGRAM, work_ms=140)

            def supervise(_):
                return worker.run(path, "practice", timeout_s=10)

            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(supervise, (1, 2)))
            self.assertEqual(results[0], results[1])
            self.assertEqual([row["step"] for row in results[0]["effects"]], [0, 1])
            self.assertEqual(len(results[0]["generations"]), 1)

    def test_interruption_rolls_back_effect_and_cursor(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker.sqlite"
            worker.create_job(path, "practice", PROGRAM)
            with worker._db(path) as connection:
                claim = worker._claim(connection, "practice", baseline=(0, 0), reason="START")
                worker._heartbeat(connection, "practice", *claim, 123, 0, begin=True)
                connection.execute("""
                    CREATE TEMP TRIGGER interrupt AFTER INSERT ON live_effects BEGIN
                      SELECT RAISE(ABORT, 'injected write interruption');
                    END
                """)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected write interruption"):
                    worker._commit(connection, "practice", *claim, 123, 0)
                self.assertEqual(worker.status(path, "practice")["cursor"], 0)
                connection.execute("DROP TRIGGER interrupt")
                self.assertEqual(worker._commit(connection, "practice", *claim, 123, 0), "RUNNING")
                connection.execute("UPDATE live_effects SET effect='{}' WHERE job='practice'")
                with self.assertRaisesRegex(ValueError, "journal diverges"):
                    worker.status(path, "practice")

    def test_missing_database_and_ineligible_window_have_no_effect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "absent.sqlite"
            with self.assertRaises(sqlite3.OperationalError):
                worker.status(path, "practice")
            with self.assertRaisesRegex(ValueError, "DEADLINE"):
                worker.create_job(path, "practice", PROGRAM, now=7)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
