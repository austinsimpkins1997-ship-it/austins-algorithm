"""Exercise saved-reference checkpoint integrity without launching Qiskit or Aer."""

from base64 import b64encode
from contextlib import ExitStack, contextmanager, redirect_stderr
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundles
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm import snapshot_calibration_study_checkpoint as checkpoint
from austins_algorithm.__main__ import main
from austins_algorithm.compilation import CompiledProgram
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.unknown_count_snapshot_cache import _json_bytes
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT
import test_snapshot_calibration_study as fixtures
import test_snapshot_calibration_study_verification as verification_fixtures


_LAYOUTS = [(0, 1), (0, 2), (1, 2)]
_GRID = {
    "initial_layouts": _LAYOUTS[::-1], "target_probability": .7,
    "shot_caps": [2], "query_caps": [1], "validation_trials": 3, "fusion_probability_tolerance": 0.0,
}
_MANIFEST = "checkpoint.json"
_BUNDLE = "compilation-bundle.json"


def _key(layout, rounds):
    return sha256(f"{layout}:{rounds}".encode()).hexdigest()


def _write_document(path, document):
    raw = _json_bytes(document)
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


class SnapshotCalibrationStudyCheckpointTests(unittest.TestCase):
    setUp = fixtures.SnapshotCalibrationStudyTests.setUp
    capture = fixtures.SnapshotCalibrationStudyTests.capture
    timeout = fixtures.SnapshotCalibrationStudyTests.timeout
    paired_study = verification_fixtures.SnapshotCalibrationStudyVerificationTests.fixture

    @contextmanager
    def simulated_runtime(self, *, reference_timeout=None, restore_error=None):
        prepare = study._prepare_preflighted_study
        versions = {**bundles._bundle_software_versions(), **fixtures._VERSIONS, "qiskit-qasm3-import": "0.6.0"}
        events = {"prepare": [], "reference": [], "restore": [], "fusion": []}
        entries, calibrations = {}, {}

        def prepare_runtime(*args, compilation_cache, **options):
            prepared, budget, required = prepare(*args, compilation_cache=None, **options)
            events["prepare"].append(prepared.layouts)
            compiler_versions = {**prepared.scope["versions"], "qiskit-qasm3-import": versions["qiskit-qasm3-import"]}
            scope = _json_bytes({
                "versions": compiler_versions, "native": prepared.scope.get("prefer_native_qasm", False),
            })
            return replace(prepared, compilation_cache=compilation_cache, compilation_key_scope=scope), budget, required

        def capture_reference(prepared, layout, **_options):
            events["reference"].append(layout)
            if layout == reference_timeout:
                return self.timeout(prepared, layout, cancelled=False)
            report = self.capture(prepared, layout)
            programs = {}
            for number, row in report["rounds"].items():
                rounds = int(number)
                key = _key(layout, rounds)
                calibrations[layout, rounds] = row["target_calibrations"]
                programs[key] = CompiledProgram(SimpleNamespace(rounds=rounds), "OPENQASM 3.0;", row["compilation"])
                entries[key] = {
                    "qpy": b64encode(b"synthetic QPY").decode("ascii"), "global_phase_hex": "0x0.0p+0",
                    "report": row["compilation"],
                }
            prepared.compilation_cache._publish_completed(programs)
            return report

        def save_bundle(_cache, path):
            return bundles._publish_bundle(Path(path), bundles._bundle_bytes(versions, entries))

        def restore(row, logical, _backend, layout, _seed, **_options):
            events["restore"].append((layout, logical))
            if restore_error == (layout, logical):
                raise ValueError("mocked QPY restoration failed")
            return CompiledProgram(SimpleNamespace(rounds=logical), "OPENQASM 3.0;", deepcopy(row["report"]))

        def calibration(program, backend):
            epoch = "earlier" if backend is self.backends["earlier"] else "later"
            layout = tuple(program.report["initial_logical_to_target_qubit"])
            return calibrations[layout, program.circuit.rounds][epoch]

        def capture_fusion(_prepared, reference, programs):
            layout = tuple(reference["initial_layout"])
            events["fusion"].append(layout)
            self.assertEqual(list(programs), [0, 1])
            for index, program in programs.items():
                self.assertEqual(program.report, reference["rounds"][str(index)]["compilation"])
            return _marked(deepcopy(reference))

        with ExitStack() as stack:
            for owner, name, replacement in (
                (checkpoint, "_prepare_preflighted_study", prepare_runtime),
                (checkpoint, "_logical_study_rounds", lambda _prepared, _width, required: tuple(range(required))),
                (checkpoint, "_compilation_key_grid", lambda prepared, circuits: {
                    layout: tuple(_key(layout, rounds) for rounds in circuits) for layout in prepared.layouts
                }),
                (study, "_capture_calibration_layout", capture_reference),
                (checkpoint, "_restore_compiled_round", restore),
                (checkpoint, "_program_calibration", calibration),
                (checkpoint, "_checkpoint_readout_channels", lambda _backend, active: {
                    wire: [[1, 0], [0, 1]] for wire in active
                }),
                (checkpoint, "_capture_restored_calibration_layout_fusion", capture_fusion),
                (SnapshotCalibrationCompilationCache, "save_bundle", save_bundle),
            ):
                stack.enter_context(patch.object(owner, name, new=replacement))
            stack.enter_context(patch.object(checkpoint, "_bundle_software_versions", return_value=versions))
            stack.enter_context(patch.object(
                comparison, "compile_and_verify", side_effect=AssertionError("checkpoint unexpectedly compiled a round"),
            ))
            yield events

    def save(self, destination, **overrides):
        return checkpoint.save_snapshot_calibration_study_checkpoint(
            _CONSTRAINT, self.earlier, self.later, destination=destination, **{**_GRID, **overrides},
        )

    def resume(self, directory, pin):
        return checkpoint.resume_snapshot_calibration_study_checkpoint(
            _CONSTRAINT, self.earlier, self.later, checkpoint=directory, expected_checkpoint_sha256=pin,
        )

    def test_save_references_then_resume_matches_one_shot_and_does_not_change_checkpoint(self):
        destination = self.directory / "paired-checkpoint"
        expected = self.paired_study()
        with self.simulated_runtime() as events:
            saved = self.save(destination)
            self.assertEqual(saved["status"], "CALIBRATION_STUDY_CHECKPOINT_SAVED")
            self.assertEqual(events["reference"], _LAYOUTS)
            self.assertEqual(events["restore"], [])
            self.assertEqual(events["fusion"], [])
            before = {path.name: path.read_bytes() for path in destination.iterdir()}
            self.assertEqual(set(before), {_MANIFEST, _BUNDLE})
            manifest = json.loads(before[_MANIFEST])
            self.assertEqual(manifest["phase"], "REFERENCE_CAPTURE_COMPLETED")
            self.assertEqual(manifest["reference_captures"], expected["captures"])
            pin = sha256(before[_MANIFEST]).hexdigest()
            self.assertEqual(saved["checkpoint_sha256"], pin)
            self.assertEqual(saved["checkpoint_bytes"], len(before[_MANIFEST]))
            self.assertEqual(saved["compilation_bundle"], {
                "bytes": len(before[_BUNDLE]), "sha256": sha256(before[_BUNDLE]).hexdigest(),
            })
            with self.assertRaises(FileExistsError):
                self.save(destination)
            result = self.resume(destination, pin)
        self.assertEqual(events["reference"], _LAYOUTS)
        self.assertEqual(events["restore"], [(layout, rounds) for layout in _LAYOUTS for rounds in (0, 1)])
        self.assertEqual(events["fusion"], _LAYOUTS)
        self.assertEqual(result, expected)
        self.assertEqual({path.name: path.read_bytes() for path in destination.iterdir()}, before)

    def test_incomplete_reference_never_publishes_checkpoint(self):
        destination = self.directory / "incomplete-checkpoint"
        with self.simulated_runtime(reference_timeout=_LAYOUTS[1]) as events:
            saved = self.save(destination)
        self.assertEqual(saved["status"], "CALIBRATION_STUDY_CHECKPOINT_UNAVAILABLE")
        self.assertNotIn("checkpoint_sha256", saved)
        self.assertEqual(saved["coverage"]["unattempted_layouts"], ["1,2"])
        self.assertEqual(events["reference"], _LAYOUTS[:2])
        self.assertEqual(events["fusion"], [])
        self.assertFalse((destination / _MANIFEST).exists())

    def test_manifest_publication_failure_leaves_no_usable_checkpoint(self):
        destination = self.directory / "failed-publication"
        link = os.link

        def fail_manifest(source, target):
            if Path(target).name == _MANIFEST:
                raise OSError("simulated checkpoint publication failure")
            return link(source, target)

        with (
            self.simulated_runtime() as events,
            patch.object(os, "link", new=fail_manifest),
            self.assertRaisesRegex(OSError, "publication failure"),
        ):
            self.save(destination)
        self.assertEqual(events["reference"], _LAYOUTS)
        self.assertEqual(events["fusion"], [])
        self.assertFalse((destination / _MANIFEST).exists())
        self.assertFalse(any(path.name.startswith(".compilation-bundle-") for path in destination.glob("*")))

    def test_bad_manifest_pin_fails_before_preparation_and_candidate(self):
        destination = self.directory / "bad-pin-checkpoint"
        with self.simulated_runtime() as events:
            self.save(destination)
            with self.assertRaisesRegex(ValueError, r"(?i)(SHA-256|digest|pin)"):
                self.resume(destination, "0" * 64)
        self.assertEqual(events["prepare"], [tuple(_LAYOUTS)])
        self.assertEqual(events["restore"], [])
        self.assertEqual(events["fusion"], [])

    def test_repinned_capture_scope_and_bundle_changes_never_start_candidates(self):
        mutations = ("capture seal", "capture result", "capture readout", "layout", "archive",
                     "bundle bytes", "bundle key", "bundle report", "bundle stack")
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.simulated_runtime() as events:
                destination = self.directory / mutation.replace(" ", "-")
                self.save(destination)
                manifest_path, bundle_path = destination / _MANIFEST, destination / _BUNDLE
                manifest = json.loads(manifest_path.read_bytes())
                bundle = json.loads(bundle_path.read_bytes())
                if mutation == "capture seal":
                    manifest["reference_captures"]["0,1"]["report_sha256"] = "0" * 64
                elif mutation == "capture result":
                    capture = manifest["reference_captures"]["0,1"]
                    capture["report"]["rounds"]["0"]["earlier"]["verified_hit_probability"] = .125
                    capture["report_sha256"] = _digest(capture["report"])
                elif mutation == "capture readout":
                    capture = manifest["reference_captures"]["1,2"]
                    earlier = capture["report"]["rounds"]["1"]["readout_assignment_matrices"]["earlier"]
                    earlier["1"] = [[.875, .125], [.125, .875]]
                    capture["report_sha256"] = _digest(capture["report"])
                elif mutation == "layout":
                    manifest["request"]["initial_layouts"][-1] = [1, 3]
                elif mutation == "archive":
                    manifest["request"]["snapshots"]["earlier"]["properties_sha256"] = "0" * 64
                elif mutation == "bundle bytes":
                    bundle_path.write_bytes(bundle_path.read_bytes() + b"\n")
                else:
                    if mutation == "bundle key":
                        del bundle["entries"][_key(_LAYOUTS[-1], 1)]
                    elif mutation == "bundle report":
                        bundle["entries"][_key(_LAYOUTS[0], 0)]["report"]["compiled"]["depth"] += 1
                    else:
                        bundle["software_versions"]["qiskit"] = "mismatched-checkpoint-sdk"
                    manifest["compilation_bundle"]["sha256"] = _write_document(bundle_path, bundle)
                    manifest["compilation_bundle"]["bytes"] = bundle_path.stat().st_size
                pin = _write_document(manifest_path, manifest)
                with self.assertRaises(ValueError) as rejected:
                    self.resume(destination, pin)
                self.assertEqual(events["reference"], _LAYOUTS)
                if mutation == "capture readout":
                    self.assertRegex(str(rejected.exception), r"(?i)readout")
                else:
                    self.assertEqual(events["restore"], [])
                self.assertEqual(events["fusion"], [])

    def test_qpy_restore_error_does_not_recompile_or_run_candidates(self):
        destination = self.directory / "failed-restore"
        with self.simulated_runtime(restore_error=(_LAYOUTS[-1], 1)) as events:
            self.save(destination)
            pin = sha256((destination / _MANIFEST).read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "mocked QPY restoration failed"):
                self.resume(destination, pin)
        self.assertEqual(events["reference"], _LAYOUTS)
        self.assertEqual(events["restore"], [(layout, rounds) for layout in _LAYOUTS for rounds in (0, 1)])
        self.assertEqual(events["fusion"], [])

    def test_cli_rejects_bad_pin_before_preparing_resume(self):
        destination = self.directory / "bad-cli-pin"
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        argv = ["austins-algorithm", "snapshot-calibration-study-resume", str(constraint), str(destination),
                "--expected-checkpoint-sha256", "0" * 64]
        for flag, archive in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(archive.configuration), str(archive.properties),
                         archive.configuration_sha256, archive.properties_sha256))
        with self.simulated_runtime() as events:
            self.save(destination)
            with (
                patch.object(sys, "argv", argv), redirect_stderr(StringIO()) as stderr,
                self.assertRaises(SystemExit) as rejected,
            ):
                main()
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertRegex(stderr.getvalue(), r"(?i)(SHA-256|digest|pin)")
        self.assertEqual(events["prepare"], [tuple(_LAYOUTS)])
        self.assertEqual(events["fusion"], [])
