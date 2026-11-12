"""SDK-free checks for packaged smoke environment provenance."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "examples" / "smoke-regression" / "capture_environment.py"
PRIVATE_CANARY = "private-provenance-canary"


class SmokeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        build = (ROOT / ".build").resolve()
        if not build.is_relative_to(ROOT):
            raise RuntimeError("smoke environment test output must stay in the package")
        build.mkdir(exist_ok=True)
        workspace = TemporaryDirectory(dir=build, prefix=PRIVATE_CANARY + "-")
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        self.distributions = self.directory / "synthetic-distributions"
        for directory, name, version in (("ZZ_Extra", "ZZ_Extra", "2.0"), ("AA_Matched", "AA.Matched", "1.2.3")):
            metadata = self.distributions / f"{directory}-{version}.dist-info"
            metadata.mkdir(parents=True)
            (metadata / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nHome-page: https://{PRIVATE_CANARY}.invalid\n",
                encoding="utf-8",
            )
            (metadata / "direct_url.json").write_text(
                json.dumps({"url": f"https://{PRIVATE_CANARY}.invalid"}), encoding="utf-8",
            )
        self.archive = self.directory / "fixture.tar.gz"
        self.archive.write_bytes(b"synthetic archive bytes\n")

    def _capture(self, requirements, *, expected_exit, trigger_commit):
        pins = self.directory / "requirements.txt"
        pins.write_bytes(requirements)
        output = self.directory / "environment.json"
        bootstrap = (
            f"import platform, runpy, sys; platform.machine = lambda: {PRIVATE_CANARY!r}; "
            "path = sys.argv.pop(1); sys.path.insert(0, sys.argv.pop(1)); "
            "runpy.run_path(path, run_name='__main__')"
        )
        environment = {
            **os.environ, "GITHUB_SHA": trigger_commit, "GITHUB_WORKFLOW_SHA": "b" * 40,
            "GITHUB_TOKEN": PRIVATE_CANARY, "RUNNER_NAME": PRIVATE_CANARY, "GITHUB_WORKSPACE": str(self.directory),
        }
        result = subprocess.run(
            [
                sys.executable, "-I", "-S", "-B", "-c", bootstrap, str(CAPTURE), str(self.distributions),
                "--requirements", str(pins), "--source-distribution", str(self.archive),
                "--uv-version", "uv 0.13.0 (abc123 2026-11-10)", "--powershell-version", "7.6.2",
                "--output", str(output),
            ],
            cwd=self.directory, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        raw = output.read_text(encoding="utf-8")
        self.assertNotIn(PRIVATE_CANARY, raw)
        return json.loads(raw)

    def test_captures_normalized_inventory_pins_and_input_fingerprints(self):
        requirements = b"# synthetic fixture\nAA_matched==1.2.3\n"
        report = self._capture(requirements, expected_exit=0, trigger_commit="a" * 40)
        self.assertEqual(report["status"], "SMOKE_ENVIRONMENT_CAPTURED")
        self.assertEqual(report["runner"]["trigger_commit"], "a" * 40)
        self.assertIsNone(report["host"]["architecture"])
        dependencies = report["dependencies"]
        expected = {"aa-matched": "1.2.3", "zz-extra": "2.0"}
        self.assertEqual(dependencies["installed"], expected)
        self.assertEqual(list(dependencies["installed"]), list(expected))
        canonical = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(dependencies["sha256"], sha256(canonical).hexdigest())
        self.assertEqual(dependencies["pins"], {
            "aa-matched": {"expected": "1.2.3", "installed": "1.2.3", "status": "MATCHED"},
        })
        for kind, name, data in (
            ("requirements", "requirements.txt", requirements),
            ("source_distribution", self.archive.name, self.archive.read_bytes()),
        ):
            self.assertEqual(report["inputs"][kind], {"name": name, "bytes": len(data), "sha256": sha256(data).hexdigest()})

    def test_pin_mismatch_retains_diagnostic_report_and_omits_invalid_runner_value(self):
        report = self._capture(
            b"AA.Matched==3.0\nMissing.Example==1.0\n", expected_exit=1, trigger_commit=PRIVATE_CANARY,
        )
        self.assertEqual(report["status"], "SMOKE_ENVIRONMENT_PIN_MISMATCH")
        self.assertIsNone(report["runner"].get("trigger_commit"))
        self.assertEqual(report["dependencies"]["pins"], {
            "aa-matched": {"expected": "3.0", "installed": "1.2.3", "status": "VERSION_MISMATCH"},
            "missing-example": {"expected": "1.0", "installed": None, "status": "MISSING"},
        })


if __name__ == "__main__":
    unittest.main()
