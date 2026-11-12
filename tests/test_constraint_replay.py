"""Check bounded offline replay files and their execution guards."""

import json
import sys
import unittest
from contextlib import redirect_stderr
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from austins_algorithm.constraint_cover_comparison import run_constraint_cover_snapshot  # noqa: E402
from austins_algorithm.constraint_replay import (  # noqa: E402
    MAX_REPLAY_BYTES, check_constraint_replay, run_constraint_replay, save_constraint_replay,
)
from austins_algorithm.constraints import BooleanConstraint, _constraint_document  # noqa: E402
from austins_algorithm.witness_budget import WitnessBudgetRequest  # noqa: E402


COVER = "austins_algorithm.constraint_cover_comparison"
IBM_TOOLS = all(find_spec(module) is not None for module in (
    "qiskit", "qiskit_aer", "qiskit_ibm_runtime", "qiskit_qasm3_import", "scipy",
))
FORMULA = BooleanConstraint(2, ((1, 2), (-1, -2)))
WITNESS = WitnessBudgetRequest(0.8, validation_seeds=(41, 43))
OPTIONS = {
    "model": "manila", "initial_layout": (0, 1), "rounds": 2, "seed": 7, "max_active_qubits": 2,
    "shots": 32, "evaluation_seeds": (11, 17, 23, 31), "simulation_method": "prepared_density_matrix",
    "prefer_native_qasm": True, "density_fusion": False,
}


def _without_timings(value):
    if isinstance(value, dict):
        timings = {"timing_seconds", "wall_seconds", "aer_experiment_seconds"}
        return {key: _without_timings(item) for key, item in value.items() if key not in timings}
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


class ConstraintReplayTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE_ROOT / ".build"
        build.mkdir(exist_ok=True)
        temp = TemporaryDirectory(prefix="constraint-replay-", dir=build)
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "request.json"

    def _document(self):
        return {
            "schema_version": 1,
            "formula": _constraint_document(FORMULA),
            "formula_sha256": FORMULA.digest(),
            "snapshot": {
                "backend_name": "fake_manila", "backend_version": "0.0.0", "last_update_date": None,
                "configuration_properties_sha256": "0" * 64,
            },
            "sampling": OPTIONS,
            "witness": {
                "target_probability": 0.8, "max_shots": 256, "validation_seeds": [41, 43],
                "stop_on_futility": False, "tolerated_misses": 0,
            },
            "exports": {
                "direct": {
                    "sha256": "1" * 64, "initial_logical_to_target_qubit": [0, 1],
                    "export_wire_to_target_qubit": [0, 1], "classical_bit_to_target_qubit": [0, 1],
                },
            },
        }

    def _write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def test_rejects_invalid_json_and_schema_before_backend(self):
        valid = self._document()
        for data, message in (
            (b" " * (MAX_REPLAY_BYTES + 1), "exceeds"),
            (b'{"schema_version":1,"schema_version":2}', "duplicate replay request JSON key"),
            (b'{"schema_version":NaN}', "invalid replay request JSON number"),
            (b'{"schema_version":', "invalid replay request JSON"),
            (json.dumps({**valid, "schema_version": True}).encode(), "unsupported replay schema_version"),
            (json.dumps({**valid, "qasm_output": "other.qasm"}).encode(), "replay request requires exactly"),
            (json.dumps({**valid, "formula_sha256": "0" * 64}).encode(), "formula digest differs"),
            (json.dumps({**valid, "sampling": {**valid["sampling"], "density_fusion": 1}}).encode(),
             "density_fusion must be a boolean"),
            (json.dumps({**valid, "witness": {**valid["witness"], "stop_on_futility": "false"}}).encode(),
             "stop_on_futility must be a boolean"),
        ):
            self.path.write_bytes(data)
            with (
                self.subTest(message=message),
                patch(f"{COVER}._snapshot_specs") as specs,
                self.assertRaisesRegex(ValueError, message),
            ):
                run_constraint_replay(self.path)
            specs.assert_not_called()

    def test_rejects_overlapping_seeds_before_backend(self):
        document = self._document()
        document["witness"]["validation_seeds"] = [41, 31]
        self._write(document)
        with (
            patch(f"{COVER}._snapshot_specs") as specs,
            self.assertRaisesRegex(ValueError, "differ from all calibration seeds"),
        ):
            run_constraint_replay(self.path)
        specs.assert_not_called()

    def test_save_preserves_an_existing_request_before_backend(self):
        self.path.write_text("original", encoding="utf-8")
        with (
            patch(f"{COVER}._snapshot_specs") as specs,
            self.assertRaisesRegex(FileExistsError, "already exists"),
        ):
            save_constraint_replay(self.path, FORMULA, **OPTIONS, witness_budget=WITNESS)
        specs.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_replay_cli_has_no_backend_override_and_checks_output_collision(self):
        from austins_algorithm.workflows_cli import main

        output = self.path.with_suffix(".qasm")
        output.write_text("original", encoding="utf-8")
        for extra, message, code in (
            (["--backend", "some-device"], "unrecognized arguments", 2),
            (["--qasm-output", str(output)], "OpenQASM file already exists", 1),
            (["--check-only", "--qasm-output", str(output)], "not allowed with argument", 2),
        ):
            error = StringIO()
            with (
                self.subTest(extra=extra),
                patch("austins_algorithm.constraint_replay.run_constraint_replay") as run,
                patch("austins_algorithm.constraint_replay.check_constraint_replay") as check,
                redirect_stderr(error),
                self.assertRaises(SystemExit) as exit_status,
            ):
                main(["constraint-replay", str(self.path), *extra])
            self.assertEqual(exit_status.exception.code, code)
            self.assertIn(message, error.getvalue())
            run.assert_not_called()
            check.assert_not_called()
        self.assertEqual(output.read_text(encoding="utf-8"), "original")

    @unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
    def test_saved_replay_matches_explicit_run_without_sampling_during_save(self):
        from qiskit_aer import AerSimulator

        with patch.object(AerSimulator, "run", side_effect=AssertionError("save launched an Aer job")) as run:
            saved = save_constraint_replay(self.path, FORMULA, **OPTIONS, witness_budget=WITNESS)
        run.assert_not_called()
        self.assertEqual(saved["status"], "REPLAY_REQUEST_SAVED")
        self.assertEqual(saved["schedule_check"]["proposed_schedule"]["status"], "SCHEDULE_TOO_SHORT")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["formula_sha256"], FORMULA.digest())
        self.assertEqual(document["sampling"]["evaluation_seeds"], list(OPTIONS["evaluation_seeds"]))
        self.assertEqual(document["witness"]["validation_seeds"], [41, 43])

        direct = run_constraint_cover_snapshot(FORMULA, **OPTIONS, witness_budget=WITNESS)
        replayed = run_constraint_replay(self.path)
        self.assertEqual(_without_timings(replayed.report), _without_timings(direct.report))
        self.assertEqual(replayed.selected.openqasm, direct.selected.openqasm)

    @unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
    def test_check_only_verifies_saved_evidence_without_sampling_or_editing_the_file(self):
        from qiskit_aer import AerSimulator

        saved = save_constraint_replay(self.path, FORMULA, **OPTIONS, witness_budget=WITNESS)
        original = self.path.read_bytes()
        with (
            patch(f"{COVER}._sample_snapshot_covers") as sample,
            patch.object(AerSimulator, "run", side_effect=AssertionError("check launched Aer")) as run,
        ):
            report = check_constraint_replay(self.path)
        sample.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(report["status"], "REPLAY_VERIFIED")
        self.assertEqual(report["execution_mode"], "COMPILE_ONLY")
        self.assertFalse(report["local_sampling_performed"])
        self.assertFalse(report["provider_job_submitted"])
        self.assertFalse(report["quantum_hardware_used"])
        self.assertEqual(report["formula_sha256"], saved["formula_sha256"])
        self.assertEqual(report["snapshot"], {"model": "manila", **saved["snapshot"]})
        self.assertEqual(report["exports"], saved["exports"])
        self.assertEqual(report["compiled_sampling_status"], "COMPILED_COVERS_ELIGIBLE")
        self.assertEqual(report["schedule_check"], saved["schedule_check"])
        self.assertEqual(report["schedule_check"]["proposed_schedule"]["status"], "SCHEDULE_TOO_SHORT")
        self.assertNotIn("witness_budget", report)

    @unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
    def test_check_reports_the_same_compiled_sampling_limits_as_replay(self):
        for formula, cap, expected in (
            (FORMULA, 1, "COMPILE_ONLY_ACTIVE_QUBIT_LIMIT"),
            (BooleanConstraint(2, ((1,),)), 2, "NO_ALTERNATE_COVER"),
        ):
            path = self.path.with_name(f"{expected}.json")
            save_constraint_replay(path, formula, **{**OPTIONS, "max_active_qubits": cap}, witness_budget=WITNESS)
            with (
                self.subTest(expected=expected),
                patch(f"{COVER}._sample_snapshot") as sample,
                patch(f"{COVER}._snapshot_simulator") as simulator,
                patch(f"{COVER}._compact_snapshot_inputs") as compact,
            ):
                checked = check_constraint_replay(path)
                replayed = run_constraint_replay(path)
                self.assertEqual(checked["status"], "REPLAY_VERIFIED")
                self.assertEqual(checked["compiled_sampling_status"], expected)
                self.assertEqual(replayed.report["snapshot"]["status"], expected)
            sample.assert_not_called()
            simulator.assert_not_called()
            compact.assert_not_called()

    @unittest.skipUnless(IBM_TOOLS, "optional IBM snapshot tools are unavailable")
    def test_changed_snapshot_and_compilation_stop_before_sampling(self):
        save_constraint_replay(self.path, FORMULA, **OPTIONS, witness_budget=WITNESS)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        changed = {**document, "snapshot": {**document["snapshot"], "configuration_properties_sha256": "0" * 64}}
        self._write(changed)
        for evaluate in (check_constraint_replay, run_constraint_replay):
            with (
                self.subTest(mode=evaluate.__name__),
                patch(f"{COVER}.compare_constraint_phase_covers") as compile_covers,
                patch(f"{COVER}._sample_snapshot_covers") as sample,
                self.assertRaisesRegex(ValueError, "snapshot identity differs"),
            ):
                evaluate(self.path)
            compile_covers.assert_not_called()
            sample.assert_not_called()

        for changed in ({"sha256": "0" * 64}, {"classical_bit_to_target_qubit": [1, 0]}):
            exports = {**document["exports"], "direct": {**document["exports"]["direct"], **changed}}
            self._write({**document, "exports": exports})
            for evaluate in (check_constraint_replay, run_constraint_replay):
                with (
                    self.subTest(changed=changed, mode=evaluate.__name__),
                    patch(f"{COVER}._sample_snapshot_covers") as sample,
                    self.assertRaisesRegex(ValueError, "compiled OpenQASM or wire maps differ"),
                ):
                    evaluate(self.path)
                sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()

