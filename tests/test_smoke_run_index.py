"""Offline checks for Manila smoke evidence indexing and verification."""

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
from austins_algorithm.smoke_run_index import verify_smoke_run_index, write_smoke_run_index


_LAYOUTS = {
    "environment": ("manila-smoke-environment", "manila-smoke-environment"),
    "phase-cover": ("manila-smoke-results", "manila-smoke-evidence"),
    "diffuser": ("manila-diffuser-smoke-results", "manila-diffuser-smoke-evidence"),
    "diffuser-native": ("manila-diffuser-native-smoke-results", "manila-diffuser-native-smoke-evidence"),
    "placement": ("manila-placement-smoke-results", "manila-placement-smoke-evidence"),
}
_PRIVATE_CANARY = "private-smoke-run-index"


class SmokeRunIndexTests(unittest.TestCase):
    def setUp(self):
        build = (ROOT / ".build").resolve()
        if not build.is_relative_to(ROOT):
            raise RuntimeError("smoke run index test output must stay in the package")
        build.mkdir(exist_ok=True)
        workspace = TemporaryDirectory(dir=build, prefix=_PRIVATE_CANARY + "-")
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.runner = self.directory / "runner"
        self.downloads = self.directory / "downloads"
        self.index = self.directory / "index.json"
        self.runner.mkdir()
        self.downloads.mkdir()

    def _bundle(self, group):
        directory = self.runner / _LAYOUTS[group][0]
        directory.mkdir(exist_ok=True)
        return directory

    def _phase(self, group, phase, filename, status):
        directory = self._bundle(group)
        (directory / f"{phase}.exit.txt").write_bytes(b"0\r\n")
        (directory / filename).write_bytes(json.dumps({"status": status}).encode("utf-8"))

    def _complete_run(self):
        self._phase("environment", "capture", "environment.json", "SMOKE_ENVIRONMENT_CAPTURED")
        for group in ("phase-cover", "diffuser", "diffuser-native"):
            self._phase(group, "replay", "summary.json", "REGRESSION_MATCHED")
        case = self._bundle("phase-cover") / "01"
        case.mkdir()
        (case / "stderr.log").write_bytes(_PRIVATE_CANARY.encode("utf-8"))
        for phase, status in (
            ("verify", "PLACEMENT_RECEIPT_VERIFIED"),
            ("check-only", "PLACEMENT_REPLAY_IDENTITIES_VERIFIED"),
            ("replay", "PLACEMENT_REPLAY_MATCHED"),
        ):
            self._phase("placement", phase, f"{phase}.stdout.json", status)

    def _invoke_cli(self, *arguments):
        stdout = StringIO()
        with patch.object(sys, "argv", ["austins_algorithm", *map(str, arguments)]), redirect_stdout(stdout):
            cli_main()
        return json.loads(stdout.getvalue())

    def test_keeps_partial_evidence_and_missing_phases_distinct(self):
        placement = self._bundle("placement")
        (placement / "verify.exit.txt").write_bytes(b"1\r\n")
        (placement / "verify.stdout.json").write_bytes(b"")
        with patch.dict(os.environ, {
            "MANILA_STEP_PLACEMENT": "failure", "MANILA_STEP_REGRESSION": "success",
            "MANILA_UPLOAD_PLACEMENT": "success", "MANILA_ARTIFACT_PLACEMENT": "",
            "GITHUB_TOKEN": _PRIVATE_CANARY, "RUNNER_NAME": _PRIVATE_CANARY,
        }, clear=True):
            report = write_smoke_run_index(self.runner, self.index)
        self.assertEqual(report["status"], "SMOKE_RUN_INDEX_CREATED")
        self.assertNotIn(_PRIVATE_CANARY, self.index.read_text(encoding="utf-8"))
        self.assertEqual(set(report["bundles"]), set(_LAYOUTS))
        self.assertEqual(report["steps"]["placement"], "failure")
        self.assertEqual(report["steps"]["regression"], "success")
        partial = report["bundles"]["placement"]
        self.assertEqual(partial["upload"], {"outcome": "success", "artifact_id": None})
        self.assertEqual(partial["files"]["verify.stdout.json"]["sha256"], sha256(b"").hexdigest())
        phases = partial["phases"]
        self.assertEqual(phases["verify"]["exit_code"], 1)
        self.assertIsNone(phases["verify"]["reported_status"])
        self.assertEqual(phases["verify"]["status_observation"], "EMPTY")
        self.assertEqual(phases["check-only"]["exit_observation"], "NOT_RECORDED")
        self.assertEqual(phases["replay"]["status_observation"], "NOT_RECORDED")
        absent = report["bundles"]["phase-cover"]
        self.assertFalse(absent["files"])
        self.assertEqual(absent["phases"]["replay"]["status_observation"], "NOT_RECORDED")

    def test_verifies_runner_and_relocated_download_layouts_and_detects_tampering(self):
        self._complete_run()
        created = self._invoke_cli("smoke-run-index", self.runner, self.index)
        self.assertEqual(created["status"], "SMOKE_RUN_INDEX_CREATED")
        self.assertNotIn(_PRIVATE_CANARY, self.index.read_text(encoding="utf-8"))
        case_log = created["bundles"]["phase-cover"]["files"]["01/stderr.log"]
        self.assertEqual(case_log["sha256"], sha256(_PRIVATE_CANARY.encode("utf-8")).hexdigest())
        runner = verify_smoke_run_index(self.index, self.runner, runner_layout=True)
        self.assertEqual(runner["status"], "SMOKE_RUN_INDEX_VERIFIED")

        for group, (source, artifact) in _LAYOUTS.items():
            copytree(self.runner / source, self.downloads / artifact)
            if group == "environment":
                partial = verify_smoke_run_index(self.index, self.downloads)
                self.assertEqual(partial["status"], "SMOKE_RUN_INDEX_PARTIALLY_VERIFIED")
                self.assertEqual(partial["bundles"]["placement"]["status"], "NOT_PROVIDED")

        checked = self._invoke_cli("smoke-run-index-verify", self.index, self.downloads)
        self.assertEqual(checked["status"], "SMOKE_RUN_INDEX_VERIFIED")

        artifact = self.downloads / _LAYOUTS["placement"][1] / "verify.stdout.json"
        original = artifact.read_bytes()
        changed = original.replace(b"VERIFIED", b"TAMPERED")
        self.assertNotEqual(original, changed)
        self.assertEqual(len(original), len(changed))
        artifact.write_bytes(changed)
        tampered = verify_smoke_run_index(self.index, self.downloads)
        self.assertEqual(tampered["status"], "SMOKE_RUN_INDEX_MISMATCH")
        self.assertEqual(tampered["bundles"]["placement"]["changed_files"], [artifact.name])

        artifact.unlink()
        missing = verify_smoke_run_index(self.index, self.downloads)
        self.assertEqual(missing["status"], "SMOKE_RUN_INDEX_MISMATCH")
        self.assertEqual(missing["bundles"]["placement"]["missing_files"], [artifact.name])

    def test_whole_directory_gap_leaves_relocated_evidence_unassessed(self):
        source, artifact = _LAYOUTS["placement"]
        (self.runner / source).write_bytes(b"ordinary file at the bundle root")
        created = write_smoke_run_index(self.runner, self.index)
        recorded = created["bundles"]["placement"]
        self.assertEqual(recorded["directory_observation"], "UNAVAILABLE")
        self.assertEqual(recorded["gaps"], {".": "UNSAFE_ENTRY"})

        directory = self.downloads / artifact
        directory.mkdir()
        (directory / "verify.exit.txt").write_bytes(b"0\n")
        (directory / "verify.stdout.json").write_bytes(b'{"status":"PLACEMENT_RECEIPT_VERIFIED"}')
        verified = verify_smoke_run_index(self.index, self.downloads)
        self.assertEqual(verified["status"], "SMOKE_RUN_INDEX_PARTIALLY_VERIFIED")
        observed = verified["bundles"]["placement"]
        self.assertEqual(observed["status"], "PARTIAL")
        self.assertEqual(observed["unexpected_files"], [])
        self.assertEqual(observed["verified_files"], 0)

    def test_unrelated_stderr_gap_preserves_comparable_phase_mismatch(self):
        self._phase("placement", "verify", "verify.stdout.json", "PLACEMENT_RECEIPT_VERIFIED")
        placement = self._bundle("placement")
        (placement / "verify.stderr.txt").mkdir()
        created = write_smoke_run_index(self.runner, self.index)
        recorded = created["bundles"]["placement"]
        self.assertEqual(recorded["gaps"], {"verify.stderr.txt": "UNSAFE_ENTRY"})
        self.assertEqual(recorded["phases"]["verify"]["exit_code"], 0)

        recorded["phases"]["verify"]["exit_code"] = 1
        self.index.write_text(json.dumps(created), encoding="utf-8")
        verified = verify_smoke_run_index(self.index, self.runner, runner_layout=True)
        self.assertEqual(verified["status"], "SMOKE_RUN_INDEX_MISMATCH")
        observed = verified["bundles"]["placement"]
        self.assertEqual(observed["status"], "MISMATCH")
        self.assertEqual(observed["changed_files"], [])
        self.assertFalse(observed["phase_observations_match"])

    def test_refuses_to_overwrite_existing_index(self):
        self.index.write_bytes(b"original index placeholder")
        with self.assertRaises((OSError, ValueError)):
            write_smoke_run_index(self.runner, self.index)
        self.assertEqual(self.index.read_bytes(), b"original index placeholder")

    def test_rejects_relative_path_escape_in_supplied_index(self):
        self._phase("environment", "capture", "environment.json", "SMOKE_ENVIRONMENT_CAPTURED")
        report = write_smoke_run_index(self.runner, self.index)
        files = report["bundles"]["environment"]["files"]
        files[f"../{_PRIVATE_CANARY}.json"] = files.pop("environment.json")
        self.index.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            verify_smoke_run_index(self.index, self.runner, runner_layout=True)
        self.assertNotIn(_PRIVATE_CANARY, str(error.exception))


if __name__ == "__main__":
    unittest.main()
