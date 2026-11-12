"""Placement receipt consistency tests with bounded synthetic and local fixtures."""

from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from austins_algorithm.core import theoretical_success_probability
from austins_algorithm.device_experiment import summarize_search_counts
from austins_algorithm.grover_search import mod4_range_predicate
from austins_algorithm.ibm_models import _local_limits, _wilson_interval
from austins_algorithm.placement_receipt import _canonical, verify_placement_receipt
from austins_algorithm.placement_receipt_comparison import compare_placement_receipts
from austins_algorithm.placement_statistics import _placement_exact_interval
from austins_algorithm.replay_receipt import _json_bytes, _software_versions, _write_replay_receipt
from austins_algorithm.snapshot_placement import PreparedMod4PlacementCheck


def _test_directory():
    root = Path(__file__).resolve().parents[1]
    build = (root / ".build").resolve()
    if not build.is_relative_to(root):
        raise RuntimeError("placement receipt test output must stay in the package")
    build.mkdir(exist_ok=True)
    return TemporaryDirectory(dir=build, prefix="placement-receipt-test-")


def _synthetic_evidence():
    """Exercise the offline structure using a small declared, non-simulated fixture."""
    request = {
        "schema_version": 1, "workflow": "MOD4_PLACEMENT_CHECK", "model": "manila",
        "minimum_hit_rate": .20, "shots": 2, "evaluation_seeds": [29, 43],
        "transpiler_seed": 7, "prefer_native_qasm": False,
    }
    confidence = 1 - (1 - .95) / 4
    edges = [[a, b] for index in range(4) for a, b in ((index, index + 1), (index + 1, index))]
    calibration = {
        "configuration": {
            "backend_name": "fake_manila", "backend_version": "1.0", "n_qubits": 5, "coupling_map": edges,
            "basis_gates": ["cx"],
        },
        "properties": None,
    }
    evidence = {
        "calibration": calibration, "sha256": sha256(_canonical(calibration)).hexdigest(),
        "coupling": edges, "initial_layout": [0, 1, 2, 3], "exports": {},
    }
    completed = summarize_search_counts({"1001": 2}, requested_shots=2, width=4, verifier=mod4_range_predicate)
    simulation = {
        **completed, "execution_mode": "SIMULATOR", "method": "density_matrix", "simulated_qubits": 4,
        "verified_hit_rate_wilson_95": _wilson_interval(2, 2),
    }
    model = {
        "status": "COMPLETED", "backend": "fake_manila", "backend_version": "1.0",
        "snapshot_last_update_date": None, "placements": [], "eligible_rounds": [1, 2], "selected_rounds": 1,
    }
    for label, layout in (("baseline", [0, 1, 2, 3]), ("alternate", [4, 1, 2, 3])):
        active = sorted(layout)
        placement = {"label": label, "initial_logical_to_target_qubit": layout, "rounds": {}}
        model["placements"].append(placement)
        evidence["exports"][label] = {}
        for rounds in (1, 2):
            text = (
                'OPENQASM 3.0;\ninclude "stdgates.inc";\nbit[4] c;\nqubit[4] q;\n'
                + "cx q[0], q[1];\n" * rounds
                + "".join(f"c[{i}] = measure q[{i}];\n" for i in range(4))
            )
            evidence["exports"][label][str(rounds)] = text
            raw = text.encode()
            ideal = theoretical_success_probability(4, 2, rounds)
            probabilities = {f"{i:04b}": ideal / 2 if i in (9, 13) else (1 - ideal) / 14 for i in range(16)}
            compilation = {
                "status": "VERIFIED_IDEAL_MEASUREMENT_DISTRIBUTION", "provider_job_submitted": False,
                "quantum_hardware_used": False, "seed_transpiler": 7, "optimization_level": 1,
                "initial_logical_to_target_qubit": layout,
                "target": {"kind": "BACKEND_V2", "name": "fake_manila", "qubits": 5},
                "compiled": {"qubits": 5, "two_qubit_gates": rounds, "operations": {"cx": rounds, "measure": 4}},
                "openqasm": {
                    "representation": "PORTABLE_COMPILED_WIRES", "bytes": len(raw), "sha256": sha256(raw).hexdigest(),
                    "importer": "qiskit.qasm3.loads", "export_wire_to_target_qubit": active,
                    "classical_bit_to_target_qubit": active,
                },
                "verification": {
                    "bit_order": "Highest classical bit index first", "tolerance": 1e-10,
                    "original_probabilities": probabilities, "compiled_probabilities": probabilities,
                    "roundtrip_probabilities": probabilities,
                    "compilation_total_variation_distance": 0.0, "export_total_variation_distance": 0.0,
                    "roundtrip_total_variation_distance": 0.0,
                },
            }
            placement["rounds"][str(rounds)] = {
                "compilation": compilation,
                "trials": [{"seed": seed, "status": "COMPLETED", "simulation": simulation} for seed in (29, 43)],
                "evaluation": {
                    "status": "MEETS_TARGET", "verified_shots": 4, "completed_shots": 4, "verified_hit_rate": 1.0,
                    "interval": list(_placement_exact_interval(4, 4, confidence)),
                },
            }
    versions = _software_versions()
    report = {
        "experiment": "LOCAL_IBM_SNAPSHOT_MOD4_PLACEMENT_CHECK", "execution_mode": "SIMULATOR",
        "provider_job_submitted": False, "quantum_hardware_used": False, "minimum_hit_rate": .20,
        "shots_per_circuit_per_seed": 2, "evaluation_seeds": [29, 43], "transpiler_seed": 7,
        "round_candidates": [1, 2], "basis_order": "q3 q2 q1 q0", "family_confidence": .95,
        "predeclared_cells": 4, "interval_confidence_per_cell": confidence,
        "interval_method": "Bonferroni-adjusted two-sided Clopper-Pearson",
        "placement_policy": "two-round anchor plus nearest connected one-wire move, ties by logical then physical index",
        "selection_policy": "pass at both placements, minimize summed two-qubit gates, break ties by fewer rounds",
        "oracle_audit": {"states_checked": 16, "marked_count": 2, "marked_states": ["1001", "1101"]},
        "limits": {**_local_limits(), "shots": 1024, "samples_per_model": 8},
        "versions": {name: versions[name] for name in ("qiskit", "qiskit-aer", "qiskit-ibm-runtime", "scipy")},
        "models": {"manila": model}, "models_not_run": [], "local_cancellation_unconfirmed": False,
        "status": "COMPLETED",
    }
    return request, {"schema_version": 1, "report": report, "snapshots": {"manila": evidence}}


class PlacementReceiptTests(unittest.TestCase):
    def test_offline_receipt_checks_circuit_counts_and_recomputes_selection(self):
        request, report = _synthetic_evidence()
        with _test_directory() as directory:
            path = Path(directory)
            _write_replay_receipt(path, request=_json_bytes(request), report=report, openqasm=None)
            outcome = verify_placement_receipt(path)
        self.assertEqual(outcome["status"], "PLACEMENT_RECEIPT_VERIFIED")
        self.assertEqual(outcome["verified_exports"], 4)
        self.assertEqual(outcome["completed_shots"], 16)
        self.assertEqual(outcome["selected_rounds"], {"manila": 1})

    def test_resealed_mutations_cannot_change_seed_calibration_export_or_decision(self):
        request, original = _synthetic_evidence()
        for fault, error in (
            ("seed", "seed trial"),
            ("calibration", "calibration SHA-256"),
            ("text", "OpenQASM identity"),
            ("gates", "compiled resources"),
            ("interval", "exact confidence interval"),
            ("decision", "placement selection"),
            ("missing-round", "truncates a placement"),
        ):
            with self.subTest(fault=fault), _test_directory() as directory:
                envelope = deepcopy(original)
                model = envelope["report"]["models"]["manila"]
                snapshot = envelope["snapshots"]["manila"]
                cells = model["placements"][0]["rounds"]
                cell = cells["1"]
                if fault == "seed":
                    cell["trials"][0]["seed"] = 30
                elif fault == "calibration":
                    snapshot["calibration"]["configuration"]["backend_version"] = "2.0"
                elif fault == "text":
                    snapshot["exports"]["baseline"]["1"] += " "
                elif fault == "gates":
                    cell["compilation"]["compiled"]["two_qubit_gates"] += 1
                elif fault == "interval":
                    cell["evaluation"]["interval"][0] += .01
                elif fault == "decision":
                    model["selected_rounds"] = 2
                else:
                    del cells["2"]
                    del snapshot["exports"]["baseline"]["2"]
                path = Path(directory)
                _write_replay_receipt(path, request=_json_bytes(request), report=envelope, openqasm=None)
                with self.assertRaisesRegex(ValueError, error):
                    verify_placement_receipt(path)

    @unittest.skipUnless(
        all(find_spec(name) is not None for name in ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "scipy")),
        "optional IBM model tools are unavailable",
    )
    def test_prepared_plan_receipt_keeps_only_the_reached_timeout_prefix(self):
        plan = PreparedMod4PlacementCheck(model="manila", prefer_native_qasm=True)
        plan.run(minimum_hit_rate=.20, shots=4)
        timeout = {
            "status": "LOCAL_RESULT_WAIT_TIMED_OUT", "simulation": None,
            "local_job_cancelled": False, "timing_seconds": {},
        }
        with _test_directory() as parent, patch(
            "austins_algorithm.snapshot_placement._sample_snapshot", return_value=timeout,
        ) as sample:
            target = Path(parent) / "receipt"
            report = plan.run(minimum_hit_rate=.20, shots=4, receipt_dir=target)
            outcome = verify_placement_receipt(target)
            envelope = json.loads((target / "report.json").read_bytes())
        sample.assert_called_once()
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(outcome["local_cancellation_unconfirmed"])
        self.assertEqual(outcome["verified_exports"], 1)
        self.assertEqual(outcome["completed_shots"], 0)
        exports = envelope["snapshots"]["manila"]["exports"]
        self.assertEqual(list(exports), ["baseline"])
        self.assertEqual(list(exports["baseline"]), ["1"])


def _write_receipt_pair(directory, left, right):
    paths = Path(directory) / "left", Path(directory) / "right"
    for path, (request, evidence) in zip(paths, (left, right), strict=True):
        path.mkdir()
        _write_replay_receipt(path, request=_json_bytes(request), report=evidence, openqasm=None)
    return paths


class PlacementReceiptComparisonTests(unittest.TestCase):
    def test_common_seed_reports_wire_map_changes_despite_identical_exports(self):
        left = _synthetic_evidence()
        request, evidence = right = _synthetic_evidence()
        request.update(evaluation_seeds=[43, 47], prefer_native_qasm=True)
        report = evidence["report"]
        report["evaluation_seeds"] = [43, 47]
        placements = report["models"]["manila"]["placements"]
        for placement in placements:
            for cell in placement["rounds"].values():
                qasm = cell["compilation"]["openqasm"]
                qasm.update(
                    importer="qiskit.qasm3.loads_experimental",
                    preferred_importer="qiskit.qasm3.loads_experimental",
                )
                for trial, seed in zip(cell["trials"], request["evaluation_seeds"], strict=True):
                    trial["seed"] = seed

        qasm = placements[0]["rounds"]["1"]["compilation"]["openqasm"]
        qasm["export_wire_to_target_qubit"] = [1, 2, 3, 4]
        qasm["classical_bit_to_target_qubit"] = [1, 2, 3, 4]
        self.assertEqual(
            evidence["snapshots"]["manila"]["exports"], left[1]["snapshots"]["manila"]["exports"],
        )
        with _test_directory() as directory:
            comparison = compare_placement_receipts(*_write_receipt_pair(directory, left, right))

        self.assertEqual(comparison["status"], "PLACEMENT_RECEIPTS_COMPARED")
        self.assertEqual(comparison["request_changes"]["evaluation_seeds"], {
            "left": [29, 43], "right": [43, 47],
        })
        self.assertEqual(comparison["summary"]["paired_cells"], 4)
        outcomes = comparison["summary"]["trial_outcomes"]
        self.assertEqual(outcomes["LEFT_ONLY_PLANNED_TRIAL"], 4)
        self.assertEqual(outcomes["RIGHT_ONLY_PLANNED_TRIAL"], 4)
        model = comparison["models"][0]
        self.assertEqual(model["model"], "manila")
        self.assertFalse(model["snapshot_changes"])
        self.assertFalse(model["decision_changed"])
        row = next(cell for cell in model["cells"] if cell["placement"] == "baseline" and cell["rounds"] == 1)
        self.assertFalse(row["matching_sampling_conditions"])
        self.assertTrue(row["sampling_condition_changes"])
        self.assertTrue(row["importer_changes"])
        common = next(trial for trial in row["trials"] if trial["seed"] == 43)
        self.assertEqual(common["status"], "SAMPLING_CONDITIONS_CHANGED")
        self.assertIs(common["counts_match"], True)
        self.assertIsNone(common["count_deltas"])

    def test_count_deltas_and_selection_under_matching_sampling_conditions(self):
        left = _synthetic_evidence()
        request, evidence = right = _synthetic_evidence()
        report = evidence["report"]
        model = report["models"]["manila"]
        cell = model["placements"][0]["rounds"]["1"]
        cell["trials"][0]["simulation"] = {
            **cell["trials"][0]["simulation"],
            **summarize_search_counts(
                {"0000": 1, "1001": 1}, requested_shots=request["shots"], width=4, verifier=mod4_range_predicate,
            ),
            "verified_hit_rate_wilson_95": _wilson_interval(1, 2),
        }
        cell["evaluation"].update(
            status="INCONCLUSIVE", verified_shots=3, verified_hit_rate=.75,
            interval=list(_placement_exact_interval(3, 4, report["interval_confidence_per_cell"])),
        )
        model.update(eligible_rounds=[2], selected_rounds=2)
        with _test_directory() as directory:
            comparison = compare_placement_receipts(*_write_receipt_pair(directory, left, right))

        outcomes = comparison["summary"]["trial_outcomes"]
        self.assertEqual(outcomes["COUNTS_CHANGED"], 1)
        self.assertEqual(outcomes["COUNTS_MATCHED"], 7)
        model = comparison["models"][0]
        self.assertTrue(model["decision_changed"])
        row = next(cell for cell in model["cells"] if cell["placement"] == "baseline" and cell["rounds"] == 1)
        self.assertTrue(row["matching_sampling_conditions"])
        changed = next(trial for trial in row["trials"] if trial["seed"] == 29)
        self.assertEqual(changed["status"], "COUNTS_CHANGED")
        self.assertIs(changed["counts_match"], False)
        self.assertEqual(changed["count_deltas"], {"0000": 1, "1001": -1})

    def test_unconfirmed_timeout_compares_only_reached_cells(self):
        left = _synthetic_evidence()
        _, evidence = right = _synthetic_evidence()
        report = evidence["report"]
        report.update(status="PARTIAL", local_cancellation_unconfirmed=True)
        model = report["models"]["manila"]
        model.update(status="PARTIAL", eligible_rounds=[], selected_rounds=None)
        del model["placements"][1:]
        cells = model["placements"][0]["rounds"]
        del cells["2"]
        exports = evidence["snapshots"]["manila"]["exports"]
        del exports["alternate"], exports["baseline"]["2"]
        cells["1"]["trials"] = [{
            "seed": 29, "status": "LOCAL_RESULT_WAIT_TIMED_OUT",
            "simulation": None, "local_job_cancelled": False,
        }]
        cells["1"]["evaluation"] = {
            "status": "INCOMPLETE", "verified_shots": None, "completed_shots": None, "interval": None,
        }
        with _test_directory() as directory:
            comparison = compare_placement_receipts(*_write_receipt_pair(directory, left, right))

        summary = comparison["summary"]
        self.assertEqual(summary["paired_cells"], 1)
        self.assertEqual(summary["left_only_cells"], 3)
        self.assertEqual(summary["right_only_cells"], 0)
        row = next(
            cell for cell in comparison["models"][0]["cells"]
            if cell["placement"] == "baseline" and cell["rounds"] == 1
        )
        self.assertEqual({trial["seed"]: trial["status"] for trial in row["trials"]}, {
            29: "TRIAL_STATE_CHANGED", 43: "TRIAL_STATE_CHANGED",
        })
        self.assertTrue(all(trial["counts_match"] is None for trial in row["trials"]))

    def test_resealed_invalid_decision_is_rejected_on_either_side(self):
        good = _synthetic_evidence()
        _, evidence = bad = _synthetic_evidence()
        evidence["report"]["models"]["manila"]["selected_rounds"] = 2
        with _test_directory() as directory:
            valid, invalid = _write_receipt_pair(directory, good, bad)
            for pair in ((valid, invalid), (invalid, valid)):
                with self.subTest(pair=pair), self.assertRaisesRegex(ValueError, "placement selection"):
                    compare_placement_receipts(*pair)


if __name__ == "__main__":
    unittest.main()
