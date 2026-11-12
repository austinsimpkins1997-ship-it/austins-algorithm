"""SDK-free checks for pinned IBM snapshot calibration inputs and differences."""

from contextlib import redirect_stderr
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm.__main__ import main
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_comparison import (
    SnapshotCalibrationFiles,
    _distribution_difference,
    compare_snapshot_calibrations,
)


_CONSTRAINT = BooleanConstraint(2, ((1,), (2,)))
_EARLIER = "2024-01-01T01:00:00+00:00"
_LATER = "2024-01-01T02:00:00+00:00"


def _write_pinned(path, payload):
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def _snapshot(directory, label, date, *, name="ibmq_jakarta", width=7):
    configuration = directory / f"{label}-configuration.json"
    properties = directory / f"{label}-properties.json"
    identity = {"backend_name": name, "backend_version": "1.0.0"}
    configuration_sha256 = _write_pinned(configuration, {**identity, "n_qubits": width, "simulator": False})
    properties_sha256 = _write_pinned(properties, {
        **identity, "qubits": [[] for _ in range(width)], "last_update_date": date,
    })
    return SnapshotCalibrationFiles(configuration, properties, configuration_sha256, properties_sha256)


class SnapshotCalibrationInputTests(unittest.TestCase):
    def setUp(self):
        build = (PACKAGE / ".build").resolve()
        if not build.is_relative_to(PACKAGE):
            raise RuntimeError("calibration test output must stay in the package")
        build.mkdir(exist_ok=True)
        directory = TemporaryDirectory(dir=build, prefix="calibration-comparison-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.earlier = _snapshot(self.directory, "earlier", _EARLIER)
        self.later = _snapshot(self.directory, "later", _LATER)

    def compare(self, *, later=None, initial_layout=(0, 1), **options):
        return compare_snapshot_calibrations(
            _CONSTRAINT, self.earlier, self.later if later is None else later,
            initial_layout=initial_layout, **options,
        )

    def test_requires_matching_raw_byte_pins_for_both_file_types(self):
        for field in ("configuration", "properties"):
            with self.subTest(field=field):
                path = getattr(self.later, field)
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    with self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest)"):
                        self.compare()
                finally:
                    path.write_bytes(original)

    def test_requires_timezone_and_strictly_increasing_instant(self):
        invalid_dates = (
            ("2024-01-01T02:00:00", r"(?i)(timezone|offset|aware)"),
            ("2024-01-01T02:00:00+01:00", r"(?i)(strict|earlier|later|increas)"),
            ("2024-01-01T00:59:59+00:00", r"(?i)(strict|earlier|later|increas)"),
        )
        for date, message in invalid_dates:
            with self.subTest(date=date):
                later = _snapshot(self.directory, "invalid-date", date)
                with self.assertRaisesRegex(ValueError, message):
                    self.compare(later=later)

    def test_requires_same_device_name_and_width(self):
        cases = (
            ({"name": "ibmq_manila"}, r"(?i)(name|device|backend)"),
            ({"width": 6}, r"(?i)(width|qubit|device)"),
        )
        for change, message in cases:
            with self.subTest(change=change):
                later = _snapshot(self.directory, "different-device", _LATER, **change)
                with self.assertRaisesRegex(ValueError, message):
                    self.compare(later=later)

    def test_validates_fixed_layout_and_round_cap_before_quantum_imports(self):
        for layout in ((0, 0), (0, True), (0, -1), (0, 7)):
            with self.subTest(layout=layout), self.assertRaisesRegex(ValueError, r"(?i)(layout|qubit)"):
                self.compare(initial_layout=layout)
        for cap in (True, -1, 100_000):
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, r"(?i)round"):
                self.compare(max_rounds=cap)

    def test_cli_routes_raw_file_pins_to_comparison(self):
        constraint = self.directory / "constraint.json"
        _write_pinned(constraint, {"schema_version": 1, "qubits": 2, "clauses": [[1], [2]]})
        argv = ["austins-algorithm", "snapshot-calibration-compare", str(constraint)]
        for label, files in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((
                label, str(files.configuration), str(files.properties),
                files.configuration_sha256, files.properties_sha256,
            ))
        argv.extend(("--initial-layout", "0", "1"))
        self.later.properties.write_bytes(self.later.properties.read_bytes() + b"\n")
        with (
            patch.object(sys, "argv", argv), redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as rejected,
        ):
            main()
        self.assertNotEqual(rejected.exception.code, 0)
        self.assertIn("does not match the expected SHA-256 digest", stderr.getvalue())


class SnapshotCalibrationDistributionTests(unittest.TestCase):
    def test_aligned_distributions_compute_signed_deltas_including_zero_outcomes(self):
        earlier = {"00": .375, "01": .625, "10": 0, "11": 0}
        later = {"00": .125, "01": 0, "10": .625, "11": .25}
        result = _distribution_difference(earlier, later, {"01", "11"})
        self.assertEqual(result["earlier"]["verified_hit_probability"], .625)
        self.assertEqual(result["later"]["verified_hit_probability"], .25)
        self.assertEqual(result["right_minus_left_hit_probability"], -.375)
        self.assertEqual(result["outcome_total_variation_distance"], .875)
        self.assertEqual(result["right_minus_left_outcome_probabilities"], {
            "00": -.25, "01": -.625, "10": .625, "11": .25,
        })
        for side, source in (("earlier", earlier), ("later", later)):
            with self.subTest(side=side):
                normalized = result[side]["outcome_probabilities"]
                self.assertEqual(normalized, source)
                self.assertEqual(sum(normalized.values()), 1)

    def test_distribution_validation_requires_shared_support_witnesses_and_unit_mass(self):
        valid = {"0": .125, "1": .875}
        invalid = (
            ({"0": 1}, {"0"}, "share all outcomes"),
            (valid, {"00"}, "audited witnesses"),
            ({"0": float("nan"), "1": .875}, set(), "finite and nonnegative"),
            ({"0": -.125, "1": 1.125}, set(), "finite and nonnegative"),
            ({"0": .125, "1": .75}, set(), "unit mass"),
        )
        for later, witnesses, message in invalid:
            with self.subTest(later=later, witnesses=witnesses), self.assertRaisesRegex(ValueError, message):
                _distribution_difference(valid, later, witnesses)

    def test_roundoff_in_probability_mass_is_normalized_and_reported(self):
        scale = 1 + 2**-31
        result = _distribution_difference(
            {"0": .5, "1": .5}, {"0": .5 * scale, "1": .5 * scale}, {"1"},
        )
        self.assertEqual(result["later"]["input_probability_mass"], scale)
        self.assertEqual(result["later"]["outcome_probabilities"], {"0": .5, "1": .5})
        self.assertEqual(result["right_minus_left_hit_probability"], 0)
        self.assertEqual(result["outcome_total_variation_distance"], 0)

    def test_identical_distributions_and_empty_witness_set_have_zero_deltas(self):
        distribution = {"0": .125, "1": .875}
        result = _distribution_difference(distribution, distribution, set())
        self.assertEqual(result["earlier"]["verified_hit_probability"], 0)
        self.assertEqual(result["later"]["verified_hit_probability"], 0)
        self.assertEqual(result["right_minus_left_hit_probability"], 0)
        self.assertEqual(result["outcome_total_variation_distance"], 0)
        self.assertTrue(all(value == 0 for value in result["right_minus_left_outcome_probabilities"].values()))


if __name__ == "__main__":
    unittest.main()
