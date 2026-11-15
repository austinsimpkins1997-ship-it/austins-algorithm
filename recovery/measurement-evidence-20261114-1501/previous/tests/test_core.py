import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm import (  # noqa: E402
    AustinAlgorithmError,
    austin_predicate,
    recommended_iterations,
    run_austin_search,
    theoretical_success_probability,
    validate_marked_states,
)


class CoreTests(unittest.TestCase):
    def test_builtin_predicate_and_one_iteration_match_exact_solution_set(self):
        result = run_austin_search(3, austin_predicate, shots=128, seed=7)
        self.assertEqual(result["solution_examples"], ["011", "101"])
        self.assertEqual(result["recommended_iterations_given_known_solution_count"], 1)
        self.assertAlmostEqual(result["statevector_success_probability"], 1.0)
        self.assertEqual(result["observed_success_fraction"], 1.0)
        self.assertEqual(result["resource_estimate"]["raw_complex128_statevector_bytes_lower_bound"], 128)
        self.assertFalse(result["execution"]["quantum_hardware_used"])
        json.dumps(result, allow_nan=False)

    def test_known_solution_count_uses_nearest_first_peak(self):
        self.assertEqual(recommended_iterations(4, 2), 2)
        self.assertEqual(recommended_iterations(4, 1), 3)
        self.assertEqual(recommended_iterations(3, 2), 1)
        self.assertEqual(recommended_iterations(3, 4), 0)
        self.assertGreater(theoretical_success_probability(4, 2, 2), theoretical_success_probability(4, 2, 1))

    def test_seeded_sampling_is_repeatable(self):
        first = run_austin_search(3, austin_predicate, shots=64, seed=11)
        second = run_austin_search(3, austin_predicate, shots=64, seed=11)
        self.assertEqual(first["sample_counts"], second["sample_counts"])

    def test_no_solution_and_all_solution_cases_are_defined(self):
        none = run_austin_search(2, lambda _bits: False, shots=16, seed=1)
        all_states = run_austin_search(2, lambda _bits: True, shots=16, seed=1)
        self.assertEqual(none["statevector_success_probability"], 0.0)
        self.assertEqual(none["recommended_iterations_given_known_solution_count"], 0)
        self.assertEqual(all_states["statevector_success_probability"], 1.0)
        self.assertEqual(all_states["recommended_iterations_given_known_solution_count"], 0)

    def test_validation_rejects_bad_width_markers_predicate_and_caps(self):
        for call in (
            lambda: validate_marked_states(3, ["01x"]),
            lambda: validate_marked_states(3, ["011", "011"]),
            lambda: run_austin_search(17, lambda _bits: False),
            lambda: run_austin_search(2, lambda _bits: 1),
            lambda: run_austin_search(2, lambda _bits: False, iterations=257),
        ):
            with self.subTest(call=call), self.assertRaises(AustinAlgorithmError):
                call()

    def test_core_import_does_not_load_optional_quantum_frameworks(self):
        environment = os.environ.copy()
        source = str(PACKAGE_ROOT / "src")
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.run(
            [sys.executable, "-c", "import sys, austins_algorithm; assert 'qiskit' not in sys.modules; assert 'cirq' not in sys.modules"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
