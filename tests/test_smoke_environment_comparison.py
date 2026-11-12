"""SDK-free comparisons of recorded Manila smoke environments."""

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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from austins_algorithm.__main__ import main
from austins_algorithm.smoke_environment_comparison import compare_smoke_environments


PRIVATE_CANARY = "private-environment-comparison"


def _fingerprint(name, data):
    return {"name": name, "bytes": len(data), "sha256": sha256(data).hexdigest()}


def _environment(*, installed=None, pins=None):
    if installed is None:
        installed = {"austins-algorithm": "0.1.0", "numpy": "2.5.4", "removed-package": "1.0"}
    if pins is None:
        pins = {"numpy": "2.5.4"}
    pin_results = {}
    for name, expected in sorted(pins.items()):
        observed = installed.get(name)
        status = "MATCHED" if observed == expected else "VERSION_MISMATCH"
        if observed is None:
            status = "MISSING"
        pin_results[name] = {"expected": expected, "installed": observed, "status": status}
    captured = all(pin["status"] == "MATCHED" for pin in pin_results.values())
    requirements = "".join(f"{name}=={version}\n" for name, version in sorted(pins.items())).encode("utf-8")
    canonical = json.dumps(installed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "status": "SMOKE_ENVIRONMENT_CAPTURED" if captured else "SMOKE_ENVIRONMENT_PIN_MISMATCH",
        "observed_at_utc": "2026-11-11T22:00:00+00:00",
        "inputs": {
            "requirements": _fingerprint("requirements.txt", requirements),
            "source_distribution": _fingerprint("austins_algorithm-0.1.0.tar.gz", b"synthetic archive bytes"),
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
            "image_os": "win25", "image_version": "20261111.1.0",
            "trigger_commit": "a" * 40, "workflow_commit": "b" * 40, "run_id": "42", "run_attempt": "1",
        },
        "dependencies": {"installed": installed, "sha256": sha256(canonical).hexdigest(), "pins": pin_results},
    }


class SmokeEnvironmentComparisonTests(unittest.TestCase):
    def setUp(self):
        build = (ROOT / ".build").resolve()
        if not build.is_relative_to(ROOT):
            raise RuntimeError("smoke comparison test output must stay in the package")
        build.mkdir(exist_ok=True)
        workspace = TemporaryDirectory(dir=build, prefix=PRIVATE_CANARY + "-")
        self.addCleanup(workspace.cleanup)
        directory = Path(workspace.name)
        self.left = directory / "left.json"
        self.right = directory / "right.json"

    def _write(self, left, right):
        self.left.write_bytes(json.dumps(left).encode("utf-8"))
        self.right.write_bytes(json.dumps(right).encode("utf-8"))

    def test_cli_reports_run_identity_changes_without_environment_drift(self):
        left = _environment()
        self._write(left, left)
        same = compare_smoke_environments(self.left, self.right)
        for name in (
            "distribution_changes", "pin_changes", "recorded_environment_changes", "input_changes", "run_identity_changes",
        ):
            self.assertEqual(same["summary"][name], 0)

        right = deepcopy(left)
        right["observed_at_utc"] = "2026-11-11T23:00:00+00:00"
        right["runner"]["run_id"] = "43"
        self._write(left, right)
        hashes = {side: sha256(path.read_bytes()).hexdigest() for side, path in (("left", self.left), ("right", self.right))}
        stdout = StringIO()
        arguments = ["austins_algorithm", "smoke-environment-compare", str(self.left), str(self.right)]
        with patch.object(sys, "argv", arguments), redirect_stdout(stdout):
            main()
        output = stdout.getvalue()
        self.assertNotIn(PRIVATE_CANARY, output)
        report = json.loads(output)
        self.assertEqual(report["status"], "SMOKE_ENVIRONMENTS_COMPARED")
        self.assertEqual(report["summary"]["run_identity_changes"], 2)
        self.assertEqual(report["summary"]["recorded_environment_changes"], 0)
        for side, expected_hash in hashes.items():
            self.assertEqual(report[side]["sha256"], expected_hash)

    def test_reports_typed_package_pin_input_and_environment_drift(self):
        left = _environment()
        right = _environment(
            installed={"austins-algorithm": "0.1.0", "numpy": "2.5.5", "added-package": "2.0"},
            pins={"numpy": "2.5.5", "added-package": "2.0"},
        )
        right["inputs"]["source_distribution"]["name"] = "renamed-archive.tar.gz"
        right["tools"]["powershell"] = "7.6.3"
        right["python"]["version"] = "3.12.16"
        right["host"]["logical_cpu_count"] = 8
        right["runner"]["image_version"] = "20261112.1.0"
        self._write(left, right)
        report = compare_smoke_environments(self.left, self.right)
        self.assertEqual(report["dependencies"], {
            "added": {"added-package": "2.0"}, "removed": {"removed-package": "1.0"},
            "changed": {"numpy": {"left": "2.5.4", "right": "2.5.5"}},
        })
        self.assertIn("added-package", report["pins"]["added"])
        self.assertEqual(set(report["pins"]["changed"]["numpy"]), {"left", "right"})
        self.assertEqual(report["summary"]["distribution_changes"], 3)
        self.assertEqual(report["summary"]["pin_changes"], 2)
        self.assertEqual(report["summary"]["recorded_environment_changes"], 4)
        self.assertEqual(report["summary"]["input_changes"], 2)
        inputs = report["input_changes"]
        self.assertFalse(inputs["requirements"]["same_content_fingerprint"])
        self.assertTrue(inputs["source_distribution"]["same_content_fingerprint"])
        self.assertEqual(inputs["source_distribution"]["right"], right["inputs"]["source_distribution"])
        self.assertEqual(report["environment_changes"]["host"]["logical_cpu_count"], {"left": 4, "right": 8})

    def test_reports_unavailable_observations(self):
        left, right = _environment(), _environment()
        left["host"]["architecture"] = None
        right["host"]["logical_cpu_count"] = None
        left["runner"]["image_version"] = right["runner"]["image_version"] = None
        self._write(left, right)
        report = compare_smoke_environments(self.left, self.right)
        fields = report["unavailable_fields"]
        self.assertEqual(fields["host.architecture"], "LEFT_UNAVAILABLE")
        self.assertEqual(fields["host.logical_cpu_count"], "RIGHT_UNAVAILABLE")
        self.assertEqual(fields["runner.image_version"], "BOTH_UNAVAILABLE")
        self.assertEqual(report["summary"]["recorded_environment_changes"], 2)

    def test_accepts_a_genuine_pin_mismatch_and_rejects_false_state(self):
        left = _environment()
        right = _environment(pins={"numpy": "3.0", "missing-package": "1.0"})
        self._write(left, right)
        report = compare_smoke_environments(self.left, self.right)
        self.assertEqual(report["status"], "SMOKE_ENVIRONMENTS_COMPARED")
        self.assertEqual(report["left"]["status"], "SMOKE_ENVIRONMENT_CAPTURED")
        self.assertEqual(report["right"]["status"], "SMOKE_ENVIRONMENT_PIN_MISMATCH")
        self.assertEqual(report["pins"]["added"]["missing-package"]["status"], "MISSING")

        wrong_pin = deepcopy(right)
        wrong_pin["dependencies"]["pins"]["numpy"]["status"] = "MATCHED"
        wrong_capture = {**right, "status": "SMOKE_ENVIRONMENT_CAPTURED"}
        for field, document in (("pin", wrong_pin), ("capture status", wrong_capture)):
            with self.subTest(field=field):
                self._write(left, document)
                with self.assertRaisesRegex(ValueError, rf"(?i)right.*{field}"):
                    compare_smoke_environments(self.left, self.right)

    def test_rejects_tampered_digest_and_redacts_invalid_input_values(self):
        left, right = _environment(), _environment()
        left["dependencies"]["installed"]["removed-package"] = "9.9"
        self._write(left, right)
        with self.assertRaisesRegex(ValueError, r"(?i)left.*(?:digest|sha.?256)"):
            compare_smoke_environments(self.left, self.right)

        left = _environment()
        right["inputs"]["requirements"]["name"] = f"C:/{PRIVATE_CANARY}/requirements.txt"
        self._write(left, right)
        with self.assertRaises(ValueError) as path_error:
            compare_smoke_environments(self.left, self.right)
        self.assertNotIn(PRIVATE_CANARY, str(path_error.exception))

        key = json.dumps(PRIVATE_CANARY)
        self.right.write_text(f'{{{key}: 1, {key}: 2,' + json.dumps(_environment())[1:], encoding="utf-8")
        with self.assertRaises(ValueError) as duplicate_error:
            compare_smoke_environments(self.left, self.right)
        self.assertNotIn(PRIVATE_CANARY, str(duplicate_error.exception))


if __name__ == "__main__":
    unittest.main()
