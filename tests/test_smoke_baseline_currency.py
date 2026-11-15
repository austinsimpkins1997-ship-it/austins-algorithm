"""Catch release-version drift before the optional simulator replay job."""

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm import __version__  # noqa: E402
from austins_algorithm.replay_receipt import verify_replay_receipt  # noqa: E402


class SmokeBaselineCurrencyTests(unittest.TestCase):
    def test_packaged_baselines_bind_the_current_release_and_pinned_sdk(self):
        examples = PACKAGE_ROOT / "examples"
        pins = dict(
            line.split("==", 1)
            for line in (examples / "smoke-regression/requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
        baselines = (
            "smoke-regression/baseline",
            "diffuser-smoke-regression/baseline",
            "diffuser-smoke-regression/baseline-native",
            "placement-smoke-regression/baseline",
        )
        for relative in baselines:
            with self.subTest(baseline=relative):
                receipt = verify_replay_receipt(examples / relative)
                recorded = receipt["software_versions"]
                self.assertEqual(
                    recorded["austins_algorithm"], __version__,
                    "Regenerate and compare the simulator baselines after a package-version change; "
                    "do not relabel historical evidence or relax software identity checks.",
                )
                self.assertEqual({name: recorded[name] for name in pins}, pins)
                self.assertEqual(recorded["python"], "3.12.15")


if __name__ == "__main__":
    unittest.main()
