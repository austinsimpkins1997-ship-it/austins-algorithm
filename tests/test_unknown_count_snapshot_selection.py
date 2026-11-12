"""SDK-free checks for selecting budgets from indexed snapshot probabilities."""

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

from austins_algorithm.__main__ import main as cli_main
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.unknown_count_planning import select_unknown_count_budgets
from austins_algorithm.unknown_count_snapshot_selection import select_snapshot_unknown_count_budgets


_SUITE = "work_tradeoff"
_BITS = ("00", "01", "10", "11")
_GATES = "worst_case_expected_compiled_two_qubit_gates"


def _seal(panel):
    semantic = {key: panel[key] for key in ("configuration", "observations")}
    raw = json.dumps(semantic, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    panel["observations_sha256"] = sha256(raw).hexdigest()
    return panel


def _round_report(rounds, witnesses, probabilities):
    distribution = dict(zip(_BITS, probabilities, strict=True))
    return {
        "compilation": {
            "status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION",
            "target": {"kind": "BACKEND_V2", "name": "fake_jakarta", "qubits": 7},
            "seed_transpiler": 7,
            "initial_logical_to_target_qubit": [0, 1],
            "compiled": {"two_qubit_gates": 4 * rounds},
            "openqasm": {
                "sha256": f"{rounds + 1:064x}",
                "export_wire_to_target_qubit": [0, 1],
                "classical_bit_to_target_qubit": [0, 1],
            },
            "provider_job_submitted": False,
            "quantum_hardware_used": False,
        },
        "density_preparation": {"status": "COMPLETED"},
        "snapshot_probabilities": {
            "outcome_probabilities": distribution,
            "verified_hit_probability": sum(distribution[bits] for bits in witnesses),
        },
    }


def _case(count):
    witnesses = list(_BITS[:count])
    clauses = {1: ((-2,), (-1,)), 2: ((-2,),), 3: ((-2, -1),)}[count]
    weights = ((.25, .25, .25, .25), (.75, .125, .125, 0)) if count == 1 else ((.0625, .0625, 0, .875),) * 2
    return {
        "role": "DECLARED" if count == 1 else "HOLDOUT",
        "reference": {
            "states_checked": 4, "solution_count": count, "witnesses": witnesses,
            "constraint_sha256": BooleanConstraint(2, clauses).digest(), "synthesis_used": "disjoint-prefix",
        },
        "comparison": {
            "status": "COMPLETED", "execution_mode": "SIMULATOR", "search_qubits": 2,
            "seeds": [8000 + count], "seed_transpiler": 7,
            "snapshot": {
                "model": "jakarta", "backend_name": "fake_jakarta", "backend_version": "1.0.0",
                "last_update_date": "2024-01-01T00:00:00+00:00", "configuration_properties_sha256": "a" * 64,
            },
            "versions": {"qiskit": "2.6.2", "qiskit-aer": "0.18.2", "qiskit-ibm-runtime": "0.51.0", "numpy": "2.5.4"},
            "audit": {"states_checked": 4, "marked_count": count, "marked_states": witnesses},
            "anchor_layout": [0, 1], "rounds_to_prepare": [0, 1],
            "compilations": {str(rounds): _round_report(rounds, witnesses, probabilities)
                             for rounds, probabilities in enumerate(weights)},
            "provider_job_submitted": False, "quantum_hardware_used": False,
        },
    }


def _panel():
    selection = select_unknown_count_budgets(2, [1], target_probability=.4, shot_caps=[1, 2], query_caps=[0, 1])
    return _seal({
        "schema_version": 1, "experiment": "LOCAL_UNKNOWN_COUNT_SELECTION_SNAPSHOT_COMPARISON",
        "status": "COMPLETED", "provider_job_submitted": False, "quantum_hardware_used": False,
        "configuration": {"model": "jakarta", "seed_transpiler": 7, "trials_per_arm": 1},
        "observations": {
            _SUITE: {"selection": selection, "plans": {}, "cases": {str(count): _case(count) for count in (1, 2, 3)}},
        },
        "case_statuses": {"COMPLETED": 3},
    })


def _select(panel, **overrides):
    options = {"target_probability": .4, "shot_caps": [1, 2], "query_caps": [0, 1]}
    return select_snapshot_unknown_count_budgets(panel, _SUITE, **{**options, **overrides})


def _candidate(report, policy, shots, queries):
    return next(row for row in report["candidates"]
                if (row["policy"], row["max_shots"], row["max_oracle_queries"]) == (policy, shots, queries))


class UnknownCountSnapshotSelectionTests(unittest.TestCase):
    def test_noisy_grid_uses_only_declared_counts_and_exact_expected_gate_work(self):
        panel = _panel()
        report = _select(panel, expected_observations_sha256=panel["observations_sha256"])
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_MET")
        self.assertEqual(report["candidate_count"], 8)
        self.assertEqual(report["feasible_count"], 3)
        for row in report["candidates"]:
            self.assertEqual([hypothesis["solution_count"] for hypothesis in row["hypotheses"]], [1])

        zero = _candidate(report, "affordable", 2, 0)
        one = _candidate(report, "affordable", 2, 1)
        self.assertAlmostEqual(zero["minimum_found_probability"], .4375)
        self.assertAlmostEqual(one["minimum_found_probability"], .625)
        self.assertEqual(zero[_GATES], 0)
        self.assertAlmostEqual(one[_GATES], 1.5)
        self.assertAlmostEqual(one["worst_case_expected_shots"], 1.75)
        self.assertAlmostEqual(one["worst_case_expected_oracle_queries"], .375)
        self.assertEqual(report["classical_distinct_uniform"]["2"]["1"], {
            "found_probability": .5, "expected_verifier_calls": 1.75,
        })
        recommendation = report["recommendations"]["fewest_expected_compiled_two_qubit_gates"]
        self.assertEqual((recommendation["policy"], recommendation["max_shots"], recommendation["max_oracle_queries"]),
                         ("affordable", 2, 0))

    def test_unmet_target_keeps_best_attainable_without_a_recommendation(self):
        report = _select(_panel(), target_probability=.7)
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_INFEASIBLE")
        self.assertEqual(report["feasible_count"], 0)
        self.assertTrue(all(row is None for row in report["recommendations"].values()))
        self.assertAlmostEqual(report["best_attainable"]["minimum_found_probability"], .625)

    def test_multiple_declared_cases_use_worst_probability_and_same_snapshot(self):
        panel = _panel()
        suite = panel["observations"][_SUITE]
        suite["selection"] = select_unknown_count_budgets(
            2, [1, 2], target_probability=.4, shot_caps=[1, 2], query_caps=[0, 1],
        )
        suite["cases"]["2"]["role"] = "DECLARED"
        report = _select(_seal(panel))
        self.assertEqual(report["status"], "UNKNOWN_COUNT_TARGET_INFEASIBLE")
        row = _candidate(report, "affordable", 2, 1)
        self.assertEqual([item["solution_count"] for item in row["hypotheses"]], [1, 2])
        self.assertAlmostEqual(row["minimum_found_probability"], .234375)

        suite["cases"]["2"]["comparison"]["snapshot"]["configuration_properties_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, r"(?i)snapshot|calibration"):
            _select(_seal(panel))

    def test_source_digest_detects_changed_content_and_external_mismatch(self):
        panel = _panel()
        with self.assertRaisesRegex(ValueError, r"(?i)digest|sha.?256"):
            _select(panel, expected_observations_sha256="f" * 64)
        panel["observations"][_SUITE]["cases"]["1"]["comparison"]["compilations"]["1"]["compiled_probe"] = True
        with self.assertRaisesRegex(ValueError, r"(?i)digest|sha.?256"):
            _select(panel)

    def test_resealed_round_gaps_and_unavailable_candidate_rounds_are_rejected(self):
        panel = _panel()
        comparison = panel["observations"][_SUITE]["cases"]["1"]["comparison"]
        comparison["compilations"].pop("1")
        with self.assertRaisesRegex(ValueError, r"(?i)round|compil"):
            _select(_seal(panel))

        comparison["rounds_to_prepare"] = [0]
        _seal(panel)
        with self.assertRaisesRegex(ValueError, r"(?i)round|compil"):
            _select(panel)
        self.assertEqual(_select(panel, query_caps=[0])["status"], "UNKNOWN_COUNT_TARGET_MET")

    def test_resealed_invalid_provenance_and_probabilities_are_rejected(self):
        baseline = _panel()
        for field, message in (
            ("status", "complete"), ("role", "declared|role"), ("snapshot", "snapshot|model"), ("weights", "probabil|normali"),
        ):
            with self.subTest(field=field):
                panel = deepcopy(baseline)
                case = panel["observations"][_SUITE]["cases"]["1"]
                if field == "status":
                    panel["status"] = "INCOMPLETE"
                elif field == "role":
                    case["role"] = "HOLDOUT"
                elif field == "snapshot":
                    case["comparison"]["snapshot"]["model"] = "manila"
                else:
                    case["comparison"]["compilations"]["0"]["snapshot_probabilities"]["outcome_probabilities"]["11"] = .5
                with self.assertRaisesRegex(ValueError, rf"(?i){message}"):
                    _select(_seal(panel))

    def test_cli_validates_fixed_budgets_on_fresh_sdk_free_replay_seeds(self):
        panel = _panel()
        suite = panel["observations"][_SUITE]
        suite["plans"] = {
            "baseline": {"selected_budget": {
                "policy": "stop", "max_shots": 2, "max_oracle_queries": 1, "max_rounds_per_shot": 256,
            }},
        }
        _seal(panel)
        raw = json.dumps(panel, indent=2).encode("utf-8")
        build = (ROOT / ".build").resolve()
        if not build.is_relative_to(ROOT):
            raise RuntimeError("snapshot selection test output must stay in the package")
        build.mkdir(exist_ok=True)
        stdout = StringIO()
        with TemporaryDirectory(dir=build, prefix="snapshot-selection-test-") as directory:
            source = Path(directory) / "panel.json"
            source.write_bytes(raw)
            arguments = [
                "austins_algorithm", "unknown-count-snapshot-select", str(source), "--suite", _SUITE,
                "--target-probability", "0.4", "--shot-caps", "1", "2", "--query-caps", "0", "1",
                "--validate-trials", "2", "--expected-observations-sha256", panel["observations_sha256"],
            ]
            with patch.object(sys, "argv", arguments), redirect_stdout(stdout):
                cli_main()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["source"]["file_sha256"], sha256(raw).hexdigest())
        self.assertEqual(report["source"]["observations_sha256"], panel["observations_sha256"])
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        validation = report["validation"]
        self.assertEqual(validation["method"], "SEEDED_CATEGORICAL_REPLAY_OF_SAVED_SNAPSHOT_DISTRIBUTIONS")
        self.assertEqual(validation["trials_per_arm"], 2)
        budgets = {budget["id"]: budget for budget in validation["budgets"]}
        self.assertEqual({(row["policy"], row["max_shots"], row["max_oracle_queries"]) for row in budgets.values()},
                         {("affordable", 2, 0), ("stop", 2, 1)})
        self.assertTrue(any("source_ideal_baseline" in row["labels"] for row in budgets.values()))
        self.assertAlmostEqual(validation["confidence"]["per_arm"], 1 - .05 / 6)
        self.assertEqual(set(validation["cases"]), {"1", "2", "3"})
        seen = {seed for case in suite["cases"].values() for seed in case["comparison"]["seeds"]}
        for count, case in validation["cases"].items():
            self.assertEqual(case["role"], "DECLARED" if count == "1" else "HOLDOUT")
            self.assertEqual(len(set(case["seeds"])), 2)
            self.assertTrue(seen.isdisjoint(case["seeds"]))
            seen.update(case["seeds"])
            self.assertEqual(set(case["plans"]), set(budgets))
            for plan_id, arm in case["plans"].items():
                budget = budgets[plan_id]
                observed = arm["observed"]
                self.assertEqual(observed["trials"], 2)
                self.assertLessEqual(observed["shots"], 2 * budget["max_shots"])
                self.assertLessEqual(observed["oracle_queries"], 2 * budget["max_oracle_queries"])
                self.assertEqual(observed["nominal_compiled_two_qubit_gates"], 4 * observed["oracle_queries"])
                self.assertLessEqual(arm["interval"]["lower"], observed["found_rate"])
                self.assertGreaterEqual(arm["interval"]["upper"], observed["found_rate"])
                self.assertRegex(arm["traces_sha256"], r"^[0-9a-f]{64}$")

        suite["cases"]["2"]["comparison"]["seeds"] = validation["cases"]["1"]["seeds"][:1]
        with self.assertRaisesRegex(ValueError, "overlap"):
            _select(_seal(panel), validation_trials=2)


if __name__ == "__main__":
    unittest.main()
