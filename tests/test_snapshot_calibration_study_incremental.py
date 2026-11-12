"""Exercise immutable calibration generations across explicit offline restarts."""

from base64 import b64encode
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from gc import collect
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from weakref import ref


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundles
from austins_algorithm import snapshot_calibration_incremental_store as store
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm import snapshot_calibration_study_checkpoint as checkpoint
from austins_algorithm import snapshot_calibration_study_incremental as incremental
from austins_algorithm.__main__ import main
from austins_algorithm.compilation import CompiledProgram
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.unknown_count_snapshot_cache import _json_bytes
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT, _report
from test_snapshot_calibration_study_checkpoint import _GRID, _LAYOUTS, _key
import test_snapshot_calibration_study as fixtures
import test_snapshot_calibration_study_verification as verification_fixtures


_PAUSED = "CALIBRATION_STUDY_INCREMENTAL_PAUSED"
_COMPLETED = "CALIBRATION_STUDY_INCREMENTAL_COMPLETED"
_INCOMPLETE = "CALIBRATION_STUDY_INCREMENTAL_REFERENCE_INCOMPLETE"
_PUBLICATION_FAILED = "CALIBRATION_STUDY_INCREMENTAL_PUBLICATION_FAILED"
_LAYOUT_KEYS = [",".join(map(str, layout)) for layout in _LAYOUTS]


def _saved_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _content_addressed(root, group, document):
    raw = _json_bytes(document)
    pin = sha256(raw).hexdigest()
    (root / group / f"{pin}.json").write_bytes(raw)
    return {"sha256": pin, "bytes": len(raw)}


def _observed(events, phase):
    return [value for name, value in events if name == phase]


class SnapshotCalibrationStudyIncrementalTests(unittest.TestCase):
    setUp = fixtures.SnapshotCalibrationStudyTests.setUp
    capture = fixtures.SnapshotCalibrationStudyTests.capture
    timeout = fixtures.SnapshotCalibrationStudyTests.timeout
    paired_study = verification_fixtures.SnapshotCalibrationStudyVerificationTests.fixture

    @contextmanager
    def simulated_runtime(self, *, blocked=None, reference_error=None, restore_error=None):
        prepare = study._prepare_preflighted_study
        versions = {**bundles._bundle_software_versions(), **fixtures._VERSIONS, "qiskit-qasm3-import": "0.6.0"}
        events = []

        def prepare_runtime(*args, compilation_cache, **options):
            prepared, budget, required = prepare(*args, compilation_cache=None, **options)
            events.append(("prepare", prepared.layouts))
            compiler_versions = {**prepared.scope["versions"], "qiskit-qasm3-import": versions["qiskit-qasm3-import"]}
            scope = _json_bytes({
                "versions": compiler_versions, "native": prepared.scope.get("prefer_native_qasm", False),
            })
            return replace(prepared, compilation_cache=compilation_cache, compilation_key_scope=scope), budget, required

        def capture_reference(prepared, layout, **_options):
            events.append(("reference", layout))
            if reference_error is not None and layout == reference_error[0]:
                raise reference_error[1]
            if blocked is not None and layout == blocked[0]:
                return self.timeout(prepared, layout, cancelled=blocked[1])
            report = self.capture(prepared, layout)
            prepared.compilation_cache._publish_completed({
                _key(layout, int(index)): CompiledProgram(
                    SimpleNamespace(rounds=int(index)), "OPENQASM 3.0;", row["compilation"],
                )
                for index, row in report["rounds"].items()
            })
            return report

        def save_bundle(cache, path):
            entries = {
                key: {"qpy": b64encode(b"synthetic QPY").decode("ascii"), "global_phase_hex": "0x0.0p+0",
                      "report": program.report}
                for key, (program, _) in cache._entries.items()
            }
            return bundles._publish_bundle(Path(path), bundles._bundle_bytes(versions, entries))

        def restore(row, logical, _backend, layout, _seed, **_options):
            events.append(("restore", (layout, logical)))
            if restore_error is not None and layout == restore_error[0]:
                raise restore_error[1]
            return CompiledProgram(SimpleNamespace(rounds=logical), "OPENQASM 3.0;", deepcopy(row["report"]))

        def calibration(program, backend):
            epoch = "earlier" if backend is self.backends["earlier"] else "later"
            layout = tuple(program.report["initial_logical_to_target_qubit"])
            return _report(layout)["rounds"][str(program.circuit.rounds)]["target_calibrations"][epoch]

        def capture_fusion(_prepared, reference, programs):
            layout = tuple(reference["initial_layout"])
            events.append(("fusion", layout))
            self.assertEqual(list(programs), [0, 1])
            for index, program in programs.items():
                self.assertEqual(program.report, reference["rounds"][str(index)]["compilation"])
            return _marked(deepcopy(reference))

        with ExitStack() as stack:
            for owner, name, replacement in (
                (incremental, "_prepare_preflighted_study", prepare_runtime),
                (incremental, "_logical_study_rounds", lambda _prepared, _width, required: tuple(range(required))),
                (incremental, "_compilation_key_grid", lambda prepared, circuits: {
                    layout: tuple(_key(layout, index) for index in circuits) for layout in prepared.layouts
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
            stack.enter_context(patch.object(incremental, "_bundle_software_versions", return_value=versions))
            stack.enter_context(patch.object(
                comparison, "compile_and_verify", side_effect=AssertionError("unexpected compile fallback"),
            ))
            yield events

    def start(self, directory, *, count=1):
        return incremental.run_snapshot_calibration_study_incremental(
            _CONSTRAINT, self.earlier, self.later, directory=directory, max_new_reference_layouts=count, **_GRID,
        )

    @contextmanager
    def watched_restores(self, *, retained_layouts):
        retained = set(retained_layouts)
        state = {"probes": [], "restores": [], "calibrations": [], "readout": [], "density": [], "maximum_live": 0}
        restore = checkpoint._restore_compiled_round
        calibration = checkpoint._program_calibration
        readout = checkpoint._checkpoint_readout_channels
        capture = checkpoint._capture_restored_calibration_layout_fusion

        def check_released(*, before_layout=None):
            collect()
            for layout, _index, probe in state["probes"]:
                if layout not in retained and layout != before_layout:
                    self.assertIsNone(probe(), f"unselected restored program remains alive for {layout}")

        def restored(row, logical, backend, layout, seed, **options):
            check_released(before_layout=layout)
            program = restore(row, logical, backend, layout, seed, **options)
            state["restores"].append((layout, logical))
            state["probes"].append((layout, logical, ref(program)))
            live = sum(probe() is not None for _layout, _index, probe in state["probes"])
            state["maximum_live"] = max(state["maximum_live"], live)
            # These fixtures have two rounds per layout, with one temporary restored program allowed.
            self.assertLessEqual(live, 2 * len(retained) + 1)
            return program

        def calibrated(program, backend):
            layout = tuple(program.report["initial_logical_to_target_qubit"])
            label = "earlier" if backend is self.backends["earlier"] else "later"
            state["calibrations"].append((layout, program.circuit.rounds, label))
            return calibration(program, backend)

        def readout_channels(backend, active):
            label = "earlier" if backend is self.backends["earlier"] else "later"
            state["readout"].append((active, label))
            return readout(backend, active)

        def density(prepared, reference, programs):
            check_released()
            state["density"].append({
                "layout": tuple(reference["initial_layout"]),
                "live": [(layout, index) for layout, index, probe in state["probes"] if probe() is not None],
                "restores": list(state["restores"]), "calibrations": list(state["calibrations"]),
                "readout": list(state["readout"]),
            })
            return capture(prepared, reference, programs)

        # Plain wrappers record weak references and scalar evidence only.
        with (
            patch.object(checkpoint, "_restore_compiled_round", new=restored),
            patch.object(checkpoint, "_program_calibration", new=calibrated),
            patch.object(checkpoint, "_checkpoint_readout_channels", new=readout_channels),
            patch.object(checkpoint, "_capture_restored_calibration_layout_fusion", new=density),
        ):
            yield state

    def assert_restored_programs_released(self, state):
        collect()
        self.assertTrue(state["probes"])
        self.assertTrue(all(probe() is None for _layout, _index, probe in state["probes"]))

    def resume(self, directory, pin, *, count=None):
        return incremental.resume_snapshot_calibration_study_incremental(
            _CONSTRAINT, self.earlier, self.later, directory=directory,
            expected_generation_sha256=pin, max_new_reference_layouts=count,
        )

    def first_generation(self, directory):
        with self.simulated_runtime() as events:
            result = self.start(directory)
        self.assertEqual(result["status"], _PAUSED)
        self.assertIsNone(result["study"])
        self.assertEqual(result["completed_layouts"], _LAYOUT_KEYS[:1])
        self.assertEqual(result["remaining_layouts"], _LAYOUT_KEYS[1:])
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[:1])
        self.assertEqual(_observed(events, "fusion"), [])
        return result["generation_sha256"]

    def test_explicit_pauses_preserve_prefix_and_resume_matches_one_shot(self):
        directory = self.directory / "incremental"
        expected = self.paired_study()
        first = self.first_generation(directory)
        first_files = _saved_files(directory)
        with self.simulated_runtime() as events:
            paused = self.resume(directory, first, count=1)
        self.assertEqual(paused["status"], _PAUSED)
        self.assertIsNone(paused["study"])
        self.assertEqual(paused["completed_layouts"], _LAYOUT_KEYS[:2])
        self.assertEqual(paused["remaining_layouts"], _LAYOUT_KEYS[2:])
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
        self.assertEqual(_observed(events, "fusion"), [])
        self.assertLess(events.index(("restore", (_LAYOUTS[0], 1))), events.index(("reference", _LAYOUTS[1])))
        second = paused["generation_sha256"]
        second_files = _saved_files(directory)
        self.assertEqual({path: second_files[path] for path in first_files}, first_files)

        with self.simulated_runtime() as events:
            completed = self.resume(directory, second)
        self.assertEqual(completed["status"], _COMPLETED)
        self.assertEqual(completed["completed_layouts"], _LAYOUT_KEYS)
        self.assertEqual(completed["remaining_layouts"], [])
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[2:])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS)
        self.assertEqual(completed["study"], expected)
        third = completed["generation_sha256"]
        final_files = _saved_files(directory)
        self.assertEqual({path: final_files[path] for path in second_files}, second_files)
        self.assertEqual(len(final_files), 2 * len(_LAYOUTS))
        for path, raw in final_files.items():
            self.assertEqual(Path(path).stem, sha256(raw).hexdigest())
        for ordinal, (pin, previous) in enumerate(((first, None), (second, first), (third, second)), start=1):
            document = json.loads(final_files[f"generations/{pin}.json"])
            self.assertEqual(document["ordinal"], ordinal)
            self.assertEqual(document["previous_generation_sha256"], previous)
            self.assertEqual(document["layout"], list(_LAYOUTS[ordinal - 1]))
            bundle = document["compilation_bundle"]
            self.assertEqual(bundle["bytes"], len(final_files[f"bundles/{bundle['sha256']}.json"]))

        with self.simulated_runtime() as events:
            reopened = self.resume(directory, third)
        self.assertEqual(reopened, completed)
        self.assertEqual(_observed(events, "reference"), [])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS)
        self.assertEqual(_saved_files(directory), final_files)

    def test_repinned_chain_layout_compilation_and_readout_fail_before_new_density(self):
        directory = self.directory / "tamper"
        first = self.first_generation(directory)
        with self.simulated_runtime():
            second = self.resume(directory, first, count=1)["generation_sha256"]
        old = json.loads((directory / "generations" / f"{first}.json").read_bytes())
        child = json.loads((directory / "generations" / f"{second}.json").read_bytes())
        wrong_pin = "f" * 64
        (directory / "generations" / f"{wrong_pin}.json").write_bytes(_json_bytes(old))
        variants = [
            ("absent tip", "0" * 64, r"(?i)expected a regular checkpoint file"),
            ("wrong pin", wrong_pin, r"(?i)expected SHA-256"),
            ("wrong chain", _content_addressed(directory, "generations", {
                **child, "previous_generation_sha256": None,
            })["sha256"], r"(?i)(chain|generation|ordinal|prefix|predecessor)"),
            ("wrong layout", _content_addressed(directory, "generations", {
                **old, "layout": list(_LAYOUTS[1]),
            })["sha256"], r"(?i)(layout|prefix|generation)"),
        ]

        altered = deepcopy(old)
        pin = old["compilation_bundle"]["sha256"]
        bundle = json.loads((directory / "bundles" / f"{pin}.json").read_bytes())
        bundle["entries"][_key(_LAYOUTS[0], 0)]["report"]["compiled"]["depth"] += 1
        altered["compilation_bundle"] = _content_addressed(directory, "bundles", bundle)
        variants.append(("compiled report", _content_addressed(directory, "generations", altered)["sha256"],
                         r"(?i)(compile|compilation|bundle report)"))

        altered = deepcopy(old)
        capture = altered["capture"]
        readout = capture["report"]["rounds"]["1"]["readout_assignment_matrices"]["earlier"]
        readout["0"] = [[.875, .125], [.125, .875]]
        capture["report_sha256"] = _digest(capture["report"])
        variants.append(("readout", _content_addressed(directory, "generations", altered)["sha256"], r"(?i)readout"))

        for label, pin, error in variants:
            with self.subTest(label=label), self.simulated_runtime() as events:
                with self.assertRaisesRegex(ValueError, error):
                    self.resume(directory, pin)
                self.assertEqual(_observed(events, "reference"), [])
                self.assertEqual(_observed(events, "fusion"), [])

    def test_incomplete_reference_retains_predecessor_and_stops_even_when_cancelled(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                directory = self.directory / f"incomplete-{cancelled}"
                first = self.first_generation(directory)
                before = _saved_files(directory)
                with self.simulated_runtime(blocked=(_LAYOUTS[1], cancelled)) as events:
                    result = self.resume(directory, first)
                self.assertEqual(result["status"], _INCOMPLETE)
                self.assertEqual(result["generation_sha256"], first)
                self.assertIsNone(result["study"])
                self.assertEqual(result["completed_layouts"], _LAYOUT_KEYS[:1])
                self.assertEqual(result["remaining_layouts"], _LAYOUT_KEYS[1:])
                self.assertEqual(result["failure"]["layout"], _LAYOUT_KEYS[1])
                self.assertIs(result["failure"]["capture"]["report"]["local_job_cancelled"], cancelled)
                self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(_saved_files(directory), before)

    def test_atomic_generation_publication_failure_keeps_predecessor_usable(self):
        directory = self.directory / "publication-failure"
        first = self.first_generation(directory)
        before = _saved_files(directory)
        publish = store._publish_bundle

        def fail_generation(path, raw):
            if path.parent.name == "generations":
                raise OSError("simulated atomic generation publication failure")
            return publish(path, raw)

        with self.simulated_runtime() as events, patch.object(store, "_publish_bundle", new=fail_generation):
            result = self.resume(directory, first, count=1)
        self.assertEqual(result["status"], _PUBLICATION_FAILED)
        self.assertEqual(result["generation_sha256"], first)
        self.assertIsNone(result["study"])
        self.assertEqual(result["failure"]["error_type"], "OSError")
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
        self.assertEqual(_observed(events, "fusion"), [])
        after = _saved_files(directory)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(list((directory / "generations").iterdir()), [directory / "generations" / f"{first}.json"])

        with self.simulated_runtime() as events:
            retried = self.resume(directory, first, count=1)
        self.assertEqual(retried["status"], _PAUSED)
        self.assertNotEqual(retried["generation_sha256"], first)
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
        self.assertEqual(_observed(events, "fusion"), [])

    def test_capture_or_prepublication_restore_error_keeps_prior_generation_resumable(self):
        for boundary, error, expected_status in (
            ("reference_error", RuntimeError("simulated reference capture error"), _INCOMPLETE),
            ("restore_error", ValueError("simulated physical restore error"), _PUBLICATION_FAILED),
        ):
            with self.subTest(boundary=boundary):
                directory = self.directory / boundary
                first = self.first_generation(directory)
                marker = directory / "generations" / f"{first}.json"
                original = marker.read_bytes()

                with self.simulated_runtime(**{boundary: (_LAYOUTS[1], error)}) as events:
                    failed = self.resume(directory, first)

                self.assertEqual(failed["status"], expected_status)
                self.assertEqual(failed["generation_sha256"], first)
                self.assertEqual(failed["completed_layouts"], _LAYOUT_KEYS[:1])
                self.assertEqual(failed["remaining_layouts"], _LAYOUT_KEYS[1:])
                self.assertIsNone(failed["study"])
                self.assertEqual(failed["failure"]["layout"], _LAYOUT_KEYS[1])
                self.assertEqual(failed["failure"]["error_type"], type(error).__name__)
                self.assertIn(str(error), failed["failure"]["message"])
                self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(list(marker.parent.iterdir()), [marker])
                self.assertEqual(marker.read_bytes(), original)

                with self.simulated_runtime() as events:
                    completed = self.resume(directory, first)
                self.assertEqual(completed["status"], _COMPLETED)
                self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:])
                self.assertEqual(_observed(events, "fusion"), _LAYOUTS)
                self.assertEqual(marker.read_bytes(), original)

    def test_cli_study_output_write_error_preserves_completed_study_and_pin(self):
        directory = self.directory / "cli"
        first = self.first_generation(directory)
        expected = self.paired_study()
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        destination = self.directory / "paired-study.json"
        argv = [
            "austins-algorithm", "snapshot-calibration-study-incremental-resume", str(constraint), str(directory),
            "--expected-generation-sha256", first, "--study-output", str(destination),
        ]
        for flag, archive in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(archive.configuration), str(archive.properties),
                         archive.configuration_sha256, archive.properties_sha256))
        publish = bundles._publish_bundle

        def fail_study_output(path, raw):
            if path == destination:
                raise OSError("simulated study output write error")
            return publish(path, raw)

        with (
            self.simulated_runtime() as events, patch.object(bundles, "_publish_bundle", new=fail_study_output),
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
            self.assertRaises(SystemExit) as rejected,
        ):
            main()

        self.assertEqual(rejected.exception.code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "CALIBRATION_STUDY_INCREMENTAL_OUTPUT_FAILED")
        self.assertEqual(result["study"], expected)
        self.assertEqual(result["study_output_error"], {
            "path": str(destination), "error_type": "OSError", "message": "simulated study output write error",
        })
        self.assertFalse(destination.exists())
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:])
        tip = result["generation_sha256"]
        lineage = store._read_incremental_calibration_generations(directory, tip)
        self.assertEqual([generation.document["layout"] for generation in lineage], list(map(list, _LAYOUTS)))
        self.assertEqual(lineage[0].sha256, first)

    def test_reference_only_and_paused_calls_release_restored_programs_between_layouts(self):
        directory = self.directory / "reference-lifetimes"
        first = self.first_generation(directory)
        with self.simulated_runtime() as events, self.watched_restores(retained_layouts=()) as state:
            paused = self.resume(directory, first, count=1)
        self.assertEqual(paused["status"], _PAUSED)
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:2])
        self.assertEqual(state["restores"], [(layout, index) for layout in _LAYOUTS[:2] for index in (0, 1)])
        self.assertEqual(state["density"], [])
        self.assertEqual(state["maximum_live"], 1)
        self.assert_restored_programs_released(state)

        with self.simulated_runtime() as events, self.watched_restores(retained_layouts=()) as state:
            completed = incremental.resume_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory,
                expected_generation_sha256=paused["generation_sha256"], reference_only=True,
            )
        self.assertEqual(completed["status"], "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED")
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[2:])
        self.assertEqual(state["restores"], [(layout, index) for layout in _LAYOUTS for index in (0, 1)])
        self.assertEqual(state["density"], [])
        self.assert_restored_programs_released(state)
        before = _saved_files(directory)
        with self.simulated_runtime(), self.watched_restores(retained_layouts=()) as state:
            reopened = incremental.resume_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory,
                expected_generation_sha256=completed["generation_sha256"], reference_only=True,
            )
        self.assertEqual(reopened, completed)
        self.assertEqual(_saved_files(directory), before)
        self.assertEqual(len(state["restores"]), 6)
        self.assertEqual(len(state["calibrations"]), 12)
        self.assertEqual(len(state["readout"]), 6)
        self.assertEqual(state["maximum_live"], 1)
        self.assert_restored_programs_released(state)

    def test_reference_only_still_rejects_late_restored_program_and_readout_changes(self):
        directory = self.directory / "reference-only-verification"
        with self.simulated_runtime():
            completed = incremental.run_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory, reference_only=True, **_GRID,
            )
        for kind in ("program", "readout"):
            with self.subTest(kind=kind), self.simulated_runtime() as events:
                restore = checkpoint._restore_compiled_round
                readout = checkpoint._checkpoint_readout_channels

                def changed_restore(row, logical, backend, layout, seed, **options):
                    program = restore(row, logical, backend, layout, seed, **options)
                    if kind == "program" and layout == _LAYOUTS[-1] and logical == 1:
                        program.report["compiled"]["depth"] += 1
                    return program

                def changed_readout(backend, active):
                    channels = readout(backend, active)
                    if kind == "readout" and active == _LAYOUTS[-1]:
                        channels[active[0]] = [[.875, .125], [.125, .875]]
                    return channels

                with (
                    patch.object(checkpoint, "_restore_compiled_round", new=changed_restore),
                    patch.object(checkpoint, "_checkpoint_readout_channels", new=changed_readout),
                    self.watched_restores(retained_layouts=()) as state,
                    self.assertRaisesRegex(ValueError, "(?i)(restored compilation|readout)"),
                ):
                    incremental.resume_snapshot_calibration_study_incremental(
                        _CONSTRAINT, self.earlier, self.later, directory=directory,
                        expected_generation_sha256=completed["generation_sha256"], reference_only=True,
                    )
                self.assertEqual(len(state["restores"]), 6)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assert_restored_programs_released(state)
