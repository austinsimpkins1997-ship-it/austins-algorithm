import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.placement_statistics import _placement_exact_interval  # noqa: E402


class PlacementStatisticsTests(unittest.TestCase):
    def test_zero_and_full_hit_endpoints(self):
        confidence = 0.9875
        alpha = (1 - confidence) / 2
        for shots in (1, 2, 2048):
            with self.subTest(shots=shots):
                zero_lower, zero_upper = _placement_exact_interval(0, shots, confidence)
                full_lower, full_upper = _placement_exact_interval(shots, shots, confidence)
                self.assertEqual(zero_lower, 0.0)
                self.assertAlmostEqual(zero_upper, -math.expm1(math.log(alpha) / shots), places=14)
                self.assertAlmostEqual(full_lower, math.exp(math.log(alpha) / shots), places=14)
                self.assertEqual(full_upper, 1.0)

    def test_one_hit_in_two_shots(self):
        confidence = 0.9958333333333333
        alpha = (1 - confidence) / 2
        expected = -math.expm1(math.log1p(-alpha) / 2)
        lower, upper = _placement_exact_interval(1, 2, confidence)
        self.assertAlmostEqual(lower, expected, places=14)
        self.assertAlmostEqual(upper, 1 - expected, places=14)

    def test_invalid_inputs_are_rejected(self):
        for hits, shots, confidence in (
            (-1, 2, 0.95), (3, 2, 0.95), (True, 2, 0.95), (1.0, 2, 0.95),
            (0, 0, 0.95), (1, 2049, 0.95), (0, True, 0.95),
            (1, 2, 0.0), (1, 2, 1.0), (1, 2, True), (1, 2, float("nan")), (1, 2, float("inf")),
        ):
            with self.subTest(hits=hits, shots=shots, confidence=confidence):
                with self.assertRaises(ValueError):
                    _placement_exact_interval(hits, shots, confidence)

    def test_matches_scipy_for_placement_confidences(self):
        try:
            from scipy.stats import binomtest
        except ImportError:
            self.skipTest("optional SciPy is unavailable")
        for shots in (2, 64, 2048):
            for hits in sorted({0, 1, shots // 4, shots // 2, shots - 1, shots}):
                for cells in (4, 12):
                    with self.subTest(shots=shots, hits=hits, cells=cells):
                        confidence = 1 - (1 - 0.95) / cells
                        observed = _placement_exact_interval(hits, shots, confidence)
                        reference = binomtest(hits, shots).proportion_ci(confidence_level=confidence, method="exact")
                        for endpoint, expected in zip(observed, reference, strict=True):
                            self.assertLessEqual(abs(endpoint - expected), 2e-11)


if __name__ == "__main__":
    unittest.main()
