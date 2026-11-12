"""SDK-free comparisons of byte-verified Manila smoke run evidence."""

from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
from shutil import copytree
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.__main__ import main as cli_main
from austins_algorithm.smoke_run_comparison import compare_smoke_run_indexes
from austins_algorithm.smoke_run_index import write_smoke_run_index


_PLACEMENT_DIR = "manila-placement-smoke-results"
_PLACEMENT_ARTIFACT = "manila-placement-smoke-evidence"
_ENVIRONMENT_DIR = "manila-smoke-environment"
_PRIVATE_CANARY = "private-smoke-run-comparison"


def _fingerprint(name, data):
    return {"name": name, "bytes": len(data), "sha256": sha256(data).hexdigest()}


def _environment(run_id, *, version="1.0"):
    installed = {"austins-algorithm": "0.1.0", "numpy": "2.5.4", "sample-transitive": version}
    canonical = json.dumps(installed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "status": "SMOKE_ENVIRONMENT_CAPTURED",
        "observed_at_utc": "2026-11-11T22:00:00+00:00",
        "inputs": {
            "requirements": _fingerprint("requirements.txt", b"numpy==2.5.4\n"),
            "source_distribution": _fingerprint("austins_algorithm-0.1.0.tar.gz", b"synthetic archive"),
        },
        "tools": {"uv": "0.13.0", "powershell": "7.6.2"},
        "python": {
            "implementation": "cpython", "version": "3.12.15", "cache_tag": "cpython-312",
            "compiler": "MSC v.1944 64 bit (AMD64)", "pointer_bits": 64,
        },
        "host": {
            "os": "Windows", "architecture": "AMD64", "release": "11",
            "windows_build": 26100, "logical_cpu_count": 4,
        },
        "runner": {
            "environment": "github-hosted", "os": "Windows", "arch": "X64",
            "image_os": "win25", "image_version": "20261111.1.0", "trigger_commit": None,
            "workflow_commit": None, "run_id": run_id, "run_attempt": None,
        },
        "dependencies": {
            "installed": installed, "sha256": sha256(canonical).hexdigest(),
            "pins": {"numpy": {"expected": "2.5.4", "installed": "2.5.4", "status": "MATCHED"}},
        },
    }


class SmokeRunComparisonTests(unittest.TestCase):
    def setUp(self):
        build = (ROOT / ".build").resolve()
        if not build.is_relative_to(ROOT):
            raise RuntimeError("smoke comparison test output must stay in the package")
        build.mkdir(exist_ok=True)
        workspace = TemporaryDirectory(dir=build, prefix=_PRIVATE_CANARY + "-")
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.left = self.directory / "left-runner"
        self.right = self.directory / "right-runner"
        self.downloads = self.directory / "downloads"
        for directory in (self.left, self.right, self.downloads):
            directory.mkdir()
        self.left_index = self.directory / "left.json"
        self.right_index = self.directory / "right.json"

    def _write_placement(self, root, *, status="PLACEMENT_REPLAY_MATCHED", exit_code=0):
        target = root / _PLACEMENT_DIR
        target.mkdir(exist_ok=True)
        (target / "replay.stdout.json").write_bytes(json.dumps({"status": status}).encode("utf-8"))
        if exit_code is not None:
            (target / "replay.exit.txt").write_bytes(f"{exit_code}\n".encode("utf-8"))
        return target

    def _write_environment(self, root, report):
        target = root / _ENVIRONMENT_DIR
        target.mkdir(exist_ok=True)
        (target / "environment.json").write_bytes(json.dumps(report).encode("utf-8"))
        (target / "capture.exit.txt").write_bytes(b"0\n")

    def _record(self, root, index, run_id):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": run_id}, clear=True):
            write_smoke_run_index(root, index)

    def _compare_runners(self, *, right_index=None):
        return compare_smoke_run_indexes(
            self.left_index, self.left, right_index or self.right_index, self.right,
            left_runner_layout=True, right_runner_layout=True,
        )

    def test_cli_compares_verified_phase_and_environment_drift_across_layouts(self):
        self._write_placement(self.left)
        self._write_placement(self.right, status="PLACEMENT_REPLAY_DIFFERED", exit_code=1)
        self._write_environment(self.left, _environment("41"))
        self._write_environment(self.right, _environment("42", version="2.0"))
        self._record(self.left, self.left_index, "41")
        self._record(self.right, self.right_index, "42")
        copytree(self.right / _PLACEMENT_DIR, self.downloads / _PLACEMENT_ARTIFACT)
        copytree(self.right / _ENVIRONMENT_DIR, self.downloads / _ENVIRONMENT_DIR)

        stdout = StringIO()
        arguments = [
            "austins_algorithm", "smoke-run-index-compare", str(self.left_index), str(self.left),
            str(self.right_index), str(self.downloads), "--left-runner-layout",
        ]
        environment_opens = []
        original_open = Path.open

        def track_open(path, *args, **kwargs):
            if path.name == "environment.json":
                environment_opens.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", track_open), patch.object(sys, "argv", arguments), redirect_stdout(stdout):
            cli_main()
        self.assertCountEqual(environment_opens, [
            self.left / _ENVIRONMENT_DIR / "environment.json",
            self.downloads / _ENVIRONMENT_DIR / "environment.json",
        ])
        self.assertNotIn(_PRIVATE_CANARY, stdout.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "SMOKE_RUN_INDEXES_COMPARED")
        placement = report["bundles"]["placement"]
        self.assertEqual(placement["phase_changes"]["replay"]["outcomes"], {
            "exit_code": {"left": 0, "right": 1},
            "reported_status": {"left": "PLACEMENT_REPLAY_MATCHED", "right": "PLACEMENT_REPLAY_DIFFERED"},
        })
        self.assertEqual(set(placement["file_changes"]["changed"]), {"replay.exit.txt", "replay.stdout.json"})
        environment = report["environment"]
        self.assertEqual(environment["status"], "COMPARED")
        self.assertEqual(environment["comparison"]["dependencies"]["changed"], {
            "sample-transitive": {"left": "1.0", "right": "2.0"},
        })

    def test_partial_comparison_distinguishes_withheld_and_unrecorded_evidence(self):
        self._write_placement(self.left)
        self._write_placement(self.right, status="PLACEMENT_REPLAY_INCOMPLETE", exit_code=None)
        self._write_environment(self.left, _environment("41"))
        self._record(self.left, self.left_index, "41")
        self._record(self.right, self.right_index, "42")
        copytree(self.left / _PLACEMENT_DIR, self.downloads / _PLACEMENT_ARTIFACT)

        report = compare_smoke_run_indexes(
            self.left_index, self.downloads, self.right_index, self.right, right_runner_layout=True,
        )
        self.assertEqual(report["status"], "SMOKE_RUN_INDEXES_PARTIALLY_COMPARED")
        environment = report["environment"]
        self.assertEqual(environment["status"], "UNAVAILABLE")
        self.assertEqual(environment["left"]["status"], "UNAVAILABLE")
        self.assertEqual(environment["right"]["status"], "NOT_RECORDED")
        replay = report["bundles"]["placement"]["phase_changes"]["replay"]
        self.assertEqual(replay["observations"]["exit_observation"], {"left": "OBSERVED", "right": "NOT_RECORDED"})
        self.assertNotIn("exit_code", replay["outcomes"])
        self.assertEqual(replay["outcomes"]["reported_status"], {
            "left": "PLACEMENT_REPLAY_MATCHED", "right": "PLACEMENT_REPLAY_INCOMPLETE",
        })
        empty = self.directory / "without-placement"
        empty.mkdir()
        withheld = compare_smoke_run_indexes(
            self.left_index, empty, self.right_index, self.right, right_runner_layout=True,
        )["bundles"]["placement"]
        self.assertEqual(withheld["unavailable_phases"]["replay"]["left"], ["exit_observation", "status_observation"])
        self.assertNotIn("replay", withheld["phase_changes"])

    def test_same_size_tampering_makes_comparison_unavailable(self):
        self._write_placement(self.left)
        right = self._write_placement(self.right)
        self._record(self.left, self.left_index, "41")
        self._record(self.right, self.right_index, "42")
        (right / "replay.exit.txt").write_bytes(b"1\n")

        report = self._compare_runners()
        self.assertEqual(report["status"], "SMOKE_RUN_COMPARISON_UNAVAILABLE")
        self.assertEqual(report["right"]["verification"]["status"], "SMOKE_RUN_INDEX_MISMATCH")
        self.assertNotIn("bundles", report)
        self.assertNotIn("environment", report)

    def test_environment_report_needs_semantic_consistency_after_byte_verification(self):
        self._write_placement(self.left)
        self._write_placement(self.right)
        self._write_environment(self.left, _environment("41"))
        self._record(self.left, self.left_index, "41")
        invalid = _environment("42")
        invalid["dependencies"]["sha256"] = "0" * 64
        for expected, evidence in (("INVALID_REPORT", invalid), ("RUN_IDENTITY_CONFLICT", _environment("99"))):
            with self.subTest(expected=expected):
                self._write_environment(self.right, evidence)
                index = self.directory / f"{expected}.json"
                self._record(self.right, index, "42")
                report = self._compare_runners(right_index=index)
                self.assertEqual(report["status"], "SMOKE_RUN_INDEXES_PARTIALLY_COMPARED")
                self.assertEqual(report["right"]["verification"]["status"], "SMOKE_RUN_INDEX_VERIFIED")
                self.assertEqual(report["environment"]["status"], "UNAVAILABLE")
                self.assertEqual(report["environment"]["left"]["status"], "AVAILABLE")
                self.assertEqual(report["environment"]["right"]["status"], expected)


if __name__ == "__main__":
    unittest.main()
