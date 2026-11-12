"""Local placement replay identity and seeded sampling regressions."""

from contextlib import redirect_stdout
from dataclasses import replace
from hashlib import sha256
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.__main__ import main
from austins_algorithm.placement_replay import replay_placement_receipt
from austins_algorithm.replay_receipt import _software_versions, _write_replay_receipt
from austins_algorithm.snapshot_placement import PreparedMod4PlacementCheck, _sample_snapshot, compile_and_verify


_UNCONFIRMED_TIMEOUT = {
    "status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None,
    "local_job_cancelled": False, "timing_seconds": {},
}


def _test_directory():
    root = Path(__file__).resolve().parents[1]
    build = (root / ".build").resolve()
    if not build.is_relative_to(root):
        raise RuntimeError("placement replay test output must stay in the package")
    build.mkdir(exist_ok=True)
    return TemporaryDirectory(dir=build, prefix="placement-replay-test-")


class PlacementReplayOfflineTests(unittest.TestCase):
    def test_digest_error_precedes_receipt_parsing_and_runtime_setup(self):
        with _test_directory() as directory:
            receipt = Path(directory)
            _write_replay_receipt(receipt, request=b"{}", report={}, openqasm=None)
            (receipt / "request.json").write_bytes(b"[]")
            with self.assertRaisesRegex(ValueError, r"SHA-256 mismatch for request\.json"):
                replay_placement_receipt(receipt)


@unittest.skipUnless(
    all(find_spec(name) is not None for name in ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "scipy")),
    "optional IBM model tools are unavailable",
)
class PlacementReplayLocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workspace = _test_directory()
        cls.addClassCleanup(workspace.cleanup)
        cls.receipt = Path(workspace.name) / "receipt"
        plan = PreparedMod4PlacementCheck(model="manila", transpiler_seed=11, prefer_native_qasm=True)
        plan.run(minimum_hit_rate=.20, shots=4, evaluation_seeds=(13, 31), receipt_dir=cls.receipt)

    def test_seeded_local_replay_matches_every_histogram_and_preserves_source(self):
        original = {path.name: path.read_bytes() for path in self.receipt.iterdir()}
        replay = replay_placement_receipt(self.receipt)
        self.assertEqual(replay["status"], "PLACEMENT_REPLAY_MATCHED")
        summary = replay["summary"]
        self.assertEqual(summary["verified_exports"], 4)
        self.assertEqual(summary["replayed_trials"], 8)
        self.assertEqual(summary["matched_histograms"], 8)
        self.assertEqual(summary["changed_histograms"], 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.receipt.iterdir()}, original)

    def test_sampling_software_drift_withholds_histogram_deltas_despite_exact_counts(self):
        simulated_versions = {**_software_versions(), "qiskit-aer": "test-version-drift"}
        with patch("austins_algorithm.placement_replay._software_versions", return_value=simulated_versions):
            replay = replay_placement_receipt(self.receipt)
        self.assertEqual(replay["status"], "PLACEMENT_REPLAY_SOFTWARE_CHANGED")
        self.assertFalse(replay["matching_sampling_software_versions"])
        self.assertEqual(replay["summary"]["matched_histograms"], 8)
        self.assertEqual(replay["summary"]["changed_histograms"], 0)
        for cell in replay["models"]["manila"]["cells"]:
            for trial in cell["trials"]:
                with self.subTest(placement=cell["placement"], rounds=cell["rounds"], seed=trial["seed"]):
                    self.assertEqual(trial["status"], "SAMPLING_CONDITIONS_CHANGED")
                    self.assertTrue(trial["counts_match"])
                    self.assertIsNone(trial["count_deltas"])

    def test_check_only_command_verifies_identity_without_constructing_simulator(self):
        stdout = StringIO()
        arguments = ["austins_algorithm", "placement-receipt-replay", str(self.receipt), "--check-only"]
        with (
            patch.object(sys, "argv", arguments),
            patch("austins_algorithm.placement_replay._snapshot_simulator") as simulator,
            patch("austins_algorithm.snapshot_placement._sample_snapshot") as sample,
            redirect_stdout(stdout),
        ):
            main()
        simulator.assert_not_called()
        sample.assert_not_called()
        replay = json.loads(stdout.getvalue())
        self.assertEqual(replay["status"], "PLACEMENT_REPLAY_IDENTITIES_VERIFIED")
        self.assertEqual(replay["summary"]["verified_exports"], 4)
        self.assertEqual(replay["summary"]["replayed_trials"], 0)

    def test_late_circuit_identity_change_prevents_all_sampling(self):
        compiled_cells = 0

        def compile_with_late_drift(*args, **kwargs):
            nonlocal compiled_cells
            compiled_cells += 1
            program = compile_and_verify(*args, **kwargs)
            if compiled_cells < 4:
                return program
            text = program.openqasm + "\n// Same circuit with different export bytes.\n"
            data = text.encode("utf-8")
            metadata = {**program.report["openqasm"], "bytes": len(data), "sha256": sha256(data).hexdigest()}
            return replace(program, openqasm=text, report={**program.report, "openqasm": metadata})

        with (
            patch("austins_algorithm.snapshot_placement.compile_and_verify", side_effect=compile_with_late_drift),
            patch("austins_algorithm.placement_replay._snapshot_simulator") as simulator,
            patch("austins_algorithm.snapshot_placement._sample_snapshot") as sample,
        ):
            with self.assertRaisesRegex(ValueError, r"(?i)openqasm|sha.?256"):
                replay_placement_receipt(self.receipt)
        self.assertEqual(compiled_cells, 4)
        simulator.assert_not_called()
        sample.assert_not_called()

    def test_partial_receipt_replays_only_its_completed_seed(self):
        def sample_then_timeout(*args, **kwargs):
            return _sample_snapshot(*args, **kwargs) if kwargs["seed"] == 13 else _UNCONFIRMED_TIMEOUT

        with _test_directory() as directory:
            receipt = Path(directory) / "partial"
            plan = PreparedMod4PlacementCheck(model="manila", prefer_native_qasm=True)
            with patch("austins_algorithm.snapshot_placement._sample_snapshot", side_effect=sample_then_timeout) as sample:
                baseline = plan.run(minimum_hit_rate=.20, shots=4, evaluation_seeds=(13, 31), receipt_dir=receipt)
            self.assertEqual(sample.call_count, 2)
            self.assertEqual(baseline["status"], "PARTIAL")
            replay = replay_placement_receipt(receipt)
        self.assertEqual(replay["status"], "PLACEMENT_REPLAY_INCOMPLETE")
        self.assertEqual(replay["summary"]["verified_exports"], 1)
        self.assertEqual(replay["summary"]["replayed_trials"], 1)
        self.assertEqual(replay["summary"]["matched_histograms"], 1)

    def test_new_unconfirmed_timeout_stops_remaining_replay_trials(self):
        with patch(
            "austins_algorithm.snapshot_placement._sample_snapshot", return_value=_UNCONFIRMED_TIMEOUT,
        ) as sample:
            replay = replay_placement_receipt(self.receipt)
        sample.assert_called_once()
        self.assertEqual(sample.call_args.kwargs["seed"], 13)
        self.assertEqual(replay["status"], "PLACEMENT_REPLAY_INCOMPLETE")
        self.assertTrue(replay["local_cancellation_unconfirmed"])
        self.assertEqual(replay["summary"]["verified_exports"], 4)
        self.assertEqual(replay["summary"]["replayed_trials"], 1)
        self.assertEqual(replay["summary"]["completed_replay_trials"], 0)


if __name__ == "__main__":
    unittest.main()
