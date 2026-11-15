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
    def test_cli_distinguishes_circuit_certainty_from_sampled_outcomes(self):
        def run_cli(*args):
            environment = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT / "src")}
            process = subprocess.run(
                [sys.executable, "-m", "austins_algorithm", "--shots", "1", *args],
                env=environment, capture_output=True, text=True, check=True,
            )
            return json.loads(process.stdout)

        first = run_cli("--seed", "1")
        second = run_cli("--seed", "2")
        self.assertEqual(first["measurement"], {
            "source": "IDEAL_SIMULATION", "sampled": True, "seed_scope": "LOCAL_SAMPLING",
            "ideal_validity": "GUARANTEED", "ideal_outcome": "MULTIPLE", "ideal_possible_outcomes": 2,
        })
        self.assertEqual(first["measurement"], second["measurement"])
        self.assertEqual(first["observed_success_fraction"], 1)
        self.assertEqual(second["observed_success_fraction"], 1)
        self.assertEqual(first["sample_counts"], {"011": 1})
        self.assertEqual(second["sample_counts"], {"101": 1})

        sample_only = run_cli("--qubits", "2", "--marked", "00", "--iterations", "0", "--seed", "1")
        self.assertEqual(sample_only["measurement"]["ideal_validity"], "PROBABILISTIC")
        self.assertAlmostEqual(sample_only["statevector_success_probability"], 0.25)
        self.assertEqual(sample_only["observed_success_fraction"], 1)

    def test_ideal_outcome_support_classifies_overshoot_and_exact_hits(self):
        from austins_algorithm.measurement import ideal_grover_outcomes

        for marked_count, rounds, validity, outcome, support in (
            (1, 1, "GUARANTEED", "SINGLE", 1),
            (1, 4, "GUARANTEED", "SINGLE", 1),
            (1, 2, "PROBABILISTIC", "MULTIPLE", 4),
            (2, 16, "PROBABILISTIC", "MULTIPLE", 4),
            (3, 1, "IMPOSSIBLE", "SINGLE", 1),
            (0, 1, "IMPOSSIBLE", "MULTIPLE", 4),
            (4, 1, "GUARANTEED", "MULTIPLE", 4),
        ):
            with self.subTest(marked_count=marked_count, rounds=rounds):
                result = ideal_grover_outcomes(2, marked_count, rounds)
                self.assertEqual(
                    (result["ideal_validity"], result["ideal_outcome"], result["ideal_possible_outcomes"]),
                    (validity, outcome, support),
                )

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
