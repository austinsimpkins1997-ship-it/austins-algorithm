"""Verify pinned fusion restarts with real local storage and mocked SDK boundaries."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_fusion_checkpoint_store as store
from austins_algorithm import snapshot_calibration_compilation_bundle as bundles
from austins_algorithm import snapshot_calibration_study_checkpoint as checkpoint
from austins_algorithm import snapshot_calibration_study_fusion_checkpoint as fusion_checkpoint
from austins_algorithm import snapshot_calibration_study_incremental as incremental
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_study_verification import verify_snapshot_calibration_study
from austins_algorithm.unknown_count_snapshot_cache import _json_bytes
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT
from test_snapshot_calibration_study_checkpoint import _GRID, _LAYOUTS
from test_snapshot_calibration_study_incremental import (
    _content_addressed, _observed, _saved_files,
)
import test_snapshot_calibration_study_incremental as incremental_fixtures


_REFERENCE_COMPLETE = "CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED"
_PREFIX = "CALIBRATION_STUDY_FUSION_CHECKPOINT_"
_KEYS = [",".join(map(str, layout)) for layout in _LAYOUTS]


class SnapshotCalibrationStudyFusionCheckpointTests(unittest.TestCase):
    setUp = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.setUp
    capture = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.capture
    timeout = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.timeout
    paired_study = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.paired_study
    simulated_runtime = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.simulated_runtime
    watched_restores = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.watched_restores
    assert_restored_programs_released = incremental_fixtures.SnapshotCalibrationStudyIncrementalTests.assert_restored_programs_released

    @contextmanager
    def runtime(self, *, candidate_failure=None, candidate_mutation=None):
        with self.simulated_runtime() as events:
            successful_capture = checkpoint._capture_restored_calibration_layout_fusion

            def candidate(prepared, reference, programs):
                layout = tuple(reference["initial_layout"])
                if candidate_failure is not None and layout == candidate_failure[0]:
                    events.append(("fusion", layout))
                    kind = candidate_failure[1]
                    if kind == "exception":
                        raise RuntimeError("simulated candidate worker failure")
                    report = _marked(self.timeout(prepared, layout, cancelled=kind == "confirmed"))
                    report["density_fusion"]["observed"] = {"earlier": {}, "later": {}}
                    return report
                report = successful_capture(prepared, reference, programs)
                if candidate_mutation is not None and layout == candidate_mutation[0]:
                    candidate_mutation[1](report)
                return report

            with (
                patch.object(checkpoint, "_capture_restored_calibration_layout_fusion", new=candidate),
                patch.object(fusion_checkpoint, "_bundle_software_versions",
                             new=incremental._bundle_software_versions),
            ):
                yield events

    def references(self, directory):
        with self.runtime() as events:
            saved = incremental.run_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory, reference_only=True, **_GRID,
            )
        self.assertEqual(saved["status"], _REFERENCE_COMPLETE)
        self.assertIsNone(saved["study"])
        self.assertEqual(_observed(events, "reference"), _LAYOUTS)
        self.assertEqual(_observed(events, "fusion"), [])
        return saved["generation_sha256"]

    def continue_fusion(self, directory, reference_pin, fusion_pin=None, *, count=None):
        return fusion_checkpoint.continue_snapshot_calibration_study_fusion_checkpoint(
            _CONSTRAINT, self.earlier, self.later, directory=directory,
            expected_reference_generation_sha256=reference_pin, expected_fusion_generation_sha256=fusion_pin,
            max_new_fusion_layouts=count,
        )

    def first_fusion(self, directory, reference_pin):
        with self.runtime() as events:
            result = self.continue_fusion(directory, reference_pin, count=1)
        self.assertEqual(result["status"], _PREFIX + "PAUSED")
        self.assertIsNone(result["study"])
        self.assertEqual(result["reference_generation_sha256"], reference_pin)
        self.assertEqual(result["attempted_layouts"], _KEYS[:1])
        self.assertEqual(result["completed_layouts"], _KEYS[:1])
        self.assertEqual(result["remaining_layouts"], _KEYS[1:])
        self.assertEqual(_observed(events, "reference"), [])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[:1])
        return result["generation_sha256"]

    def assert_valid_study(self, report):
        path = self.directory / "verified-study.json"
        raw = _json_bytes(report)
        path.write_bytes(raw)
        result = verify_snapshot_calibration_study(_CONSTRAINT, path, expected_sha256=sha256(raw).hexdigest())
        self.assertEqual(result["status"], "PAIRED_CALIBRATION_STUDY_VERIFIED")

    def test_reference_only_resume_and_saved_fusion_prefix_preserve_exact_full_study(self):
        directory = self.directory / "resumable"
        with self.runtime() as events:
            started = incremental.run_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory,
                reference_only=True, max_new_reference_layouts=1, **_GRID,
            )
        self.assertEqual(started["status"], "CALIBRATION_STUDY_INCREMENTAL_PAUSED")
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[:1])
        with self.runtime() as events:
            references = incremental.resume_snapshot_calibration_study_incremental(
                _CONSTRAINT, self.earlier, self.later, directory=directory, reference_only=True,
                expected_generation_sha256=started["generation_sha256"],
            )
        self.assertEqual(references["status"], _REFERENCE_COMPLETE)
        self.assertIsNone(references["study"])
        self.assertEqual(_observed(events, "reference"), _LAYOUTS[1:])
        self.assertEqual(_observed(events, "fusion"), [])
        reference_pin = references["generation_sha256"]
        first = self.first_fusion(directory, reference_pin)
        before = _saved_files(directory)

        with self.runtime() as events:
            completed = self.continue_fusion(directory, reference_pin, first)
        self.assertEqual(completed["status"], _PREFIX + "COMPLETED")
        self.assertEqual(completed["study"], self.paired_study())
        self.assertEqual(completed["attempted_layouts"], _KEYS)
        self.assertEqual(completed["completed_layouts"], _KEYS)
        self.assertEqual(completed["remaining_layouts"], [])
        self.assertEqual(_observed(events, "reference"), [])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:])
        final_files = _saved_files(directory)
        self.assertEqual({path: final_files[path] for path in before}, before)
        for path, raw in final_files.items():
            self.assertEqual(Path(path).stem, sha256(raw).hexdigest())
        self.assert_valid_study(completed["study"])

        with self.runtime() as events:
            reopened = self.continue_fusion(directory, reference_pin, completed["generation_sha256"])
        self.assertEqual(reopened, completed)
        self.assertEqual(_observed(events, "fusion"), [])
        self.assertEqual(_saved_files(directory), final_files)

    def test_confirmed_timeout_is_saved_and_skipped_while_later_candidates_continue(self):
        directory = self.directory / "confirmed"
        reference_pin = self.references(directory)
        with self.runtime(candidate_failure=(_LAYOUTS[0], "confirmed")) as events:
            paused = self.continue_fusion(directory, reference_pin, count=1)
        self.assertEqual(paused["status"], _PREFIX + "PAUSED")
        self.assertEqual(paused["attempted_layouts"], _KEYS[:1])
        self.assertEqual(paused["completed_layouts"], [])
        self.assertIsNone(paused["study"])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[:1])

        with self.runtime() as events:
            result = self.continue_fusion(directory, reference_pin, paused["generation_sha256"])
        self.assertEqual(result["status"], _PREFIX + "COMPLETED")
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:])
        fusion = result["study"]["fusion"]
        self.assertEqual(fusion["coverage"], {
            "requested_layouts": _KEYS, "completed_layouts": _KEYS[1:],
            "unavailable_layouts": _KEYS[:1], "unattempted_layouts": [],
        })
        self.assertTrue(fusion["captures"][_KEYS[0]]["report"]["local_job_cancelled"])
        self.assertEqual(result["study"]["planning"], self.paired_study()["planning"])
        self.assert_valid_study(result["study"])

    def test_unconfirmed_timeout_and_recognized_exception_are_terminal_on_resume(self):
        for kind in ("unconfirmed", "exception"):
            with self.subTest(kind=kind):
                directory = self.directory / kind
                reference_pin = self.references(directory)
                first = self.first_fusion(directory, reference_pin)
                with self.runtime(candidate_failure=(_LAYOUTS[1], kind)) as events:
                    stopped = self.continue_fusion(directory, reference_pin, first)
                self.assertEqual(stopped["status"], _PREFIX + "TERMINAL")
                self.assertIsNotNone(stopped["generation_sha256"])
                self.assertEqual(stopped["attempted_layouts"], _KEYS[:2])
                self.assertEqual(stopped["completed_layouts"], _KEYS[:1])
                self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
                self.assertEqual(stopped["study"]["planning"], self.paired_study()["planning"])
                self.assert_valid_study(stopped["study"])
                before = _saved_files(directory)

                with self.runtime() as events:
                    reopened = self.continue_fusion(directory, reference_pin, stopped["generation_sha256"])
                self.assertEqual(reopened, stopped)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(_observed(events, "reference"), [])
                self.assertEqual(_saved_files(directory), before)
                constraint = self.directory / "terminal-constraint.json"
                constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
                argv = [
                    "austins-algorithm", "snapshot-calibration-study-fusion-continue", str(constraint), str(directory),
                    "--expected-reference-generation-sha256", reference_pin,
                    "--expected-fusion-generation-sha256", stopped["generation_sha256"],
                ]
                for flag, archive in (("--earlier", self.earlier), ("--later", self.later)):
                    argv.extend((flag, str(archive.configuration), str(archive.properties),
                                 archive.configuration_sha256, archive.properties_sha256))
                with (
                    self.runtime() as events, patch.object(sys, "argv", argv),
                    redirect_stdout(StringIO()) as stdout, self.assertRaises(SystemExit) as terminal,
                ):
                    main()
                self.assertEqual(terminal.exception.code, 1)
                self.assertEqual(json.loads(stdout.getvalue()), stopped)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(_saved_files(directory), before)

    def test_bad_reference_pin_scope_or_readout_rejects_before_candidate_density(self):
        directory = self.directory / "reference-tamper"
        reference_pin = self.references(directory)
        document = json.loads((directory / "generations" / f"{reference_pin}.json").read_bytes())
        variants = [("absent pin", "0" * 64, r"(?i)(checkpoint|pin|SHA-256)")]
        wrong_scope = deepcopy(document)
        wrong_scope["request"]["fusion_probability_tolerance"] = .125
        variants.append(("scope", _content_addressed(directory, "generations", wrong_scope)["sha256"],
                         r"(?i)(scope|request|lineage)"))
        wrong_readout = deepcopy(document)
        report = wrong_readout["capture"]["report"]
        report["rounds"]["0"]["readout_assignment_matrices"]["earlier"]["1"] = [[.875, .125], [.125, .875]]
        wrong_readout["capture"]["report_sha256"] = _digest(report)
        variants.append(("readout", _content_addressed(directory, "generations", wrong_readout)["sha256"],
                         r"(?i)readout"))
        for label, pin, diagnostic in variants:
            with self.subTest(label=label), self.runtime() as events:
                with self.assertRaisesRegex(ValueError, diagnostic):
                    self.continue_fusion(directory, pin)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(_observed(events, "reference"), [])

    def test_late_resealed_wrong_fusion_and_wrong_reference_binding_reject_before_density(self):
        directory = self.directory / "candidate-tamper"
        reference_pin = self.references(directory)
        with self.runtime():
            paused = self.continue_fusion(directory, reference_pin, count=2)
        second = paused["generation_sha256"]
        document = json.loads((directory / "fusion-generations" / f"{second}.json").read_bytes())
        variants = [("absent pin", "f" * 64, r"(?i)(checkpoint|pin|SHA-256|generation)")]
        alternative_pin = None
        for generation in incremental._read_incremental_calibration_generations(directory, reference_pin):
            alternative = deepcopy(generation.document)
            alternative["request"]["fusion_probability_tolerance"] = .125
            alternative["previous_generation_sha256"] = alternative_pin
            alternative_pin = _content_addressed(directory, "generations", alternative)["sha256"]
        wrong_binding = {**document, "reference_generation_sha256": alternative_pin}
        variants.append(("reference binding", _content_addressed(directory, "fusion-generations", wrong_binding)["sha256"],
                         r"(?i)(reference|scope|lineage)"))
        wrong_pair = deepcopy(document)
        report = wrong_pair["capture"]["report"]
        report["rounds"]["1"]["readout_assignment_matrices"]["earlier"]["0"] = [[.75, .25], [.25, .75]]
        wrong_pair["capture"]["report_sha256"] = _digest(report)
        variants.append(("late readout", _content_addressed(directory, "fusion-generations", wrong_pair)["sha256"],
                         r"(?i)(readout|fusion|changed)"))
        for label, pin, diagnostic in variants:
            with self.subTest(label=label), self.runtime() as events:
                with self.assertRaisesRegex(ValueError, diagnostic):
                    self.continue_fusion(directory, reference_pin, pin)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assertEqual(_observed(events, "reference"), [])

    def test_marker_publication_or_candidate_validation_failure_keeps_prior_pin(self):
        def corrupt_readout(report):
            report["rounds"]["1"]["readout_assignment_matrices"]["earlier"]["0"] = [[.875, .125], [.125, .875]]

        for kind in ("publication", "validation"):
            with self.subTest(kind=kind):
                directory = self.directory / kind
                reference_pin = self.references(directory)
                first = self.first_fusion(directory, reference_pin)
                marker = directory / "fusion-generations" / f"{first}.json"
                original = marker.read_bytes()
                publish = store._publish_bundle

                def fail_marker(path, raw):
                    raise OSError("simulated fusion generation publication failure")

                mutation = (_LAYOUTS[1], corrupt_readout) if kind == "validation" else None
                publisher = fail_marker if kind == "publication" else publish
                with self.runtime(candidate_mutation=mutation) as events, patch.object(
                    store, "_publish_bundle", new=publisher,
                ):
                    result = self.continue_fusion(directory, reference_pin, first)
                self.assertEqual(result["status"], _PREFIX + "PUBLICATION_FAILED")
                self.assertEqual(result["generation_sha256"], first)
                self.assertEqual(result["failure"]["layout"], _KEYS[1])
                self.assertIsNotNone(result["failure"]["attempt"])
                self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
                self.assertEqual(list(marker.parent.iterdir()), [marker])
                self.assertEqual(marker.read_bytes(), original)
                self.assertIsNone(result["study"])

                with self.runtime() as events:
                    retried = self.continue_fusion(directory, reference_pin, first)
                self.assertEqual(retried["status"], _PREFIX + "COMPLETED")
                self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:])
                self.assertEqual(retried["study"], self.paired_study())
                self.assertEqual(marker.read_bytes(), original)

    def test_fusion_limit_rejects_bool_and_out_of_range_before_preparation(self):
        directory = self.directory / "limits"
        reference_pin = self.references(directory)
        for value in (False, True, 0, 9, 1.0, "1"):
            with self.subTest(value=value), self.runtime() as events:
                with self.assertRaisesRegex(ValueError, r"max_new_fusion_layouts"):
                    self.continue_fusion(directory, reference_pin, count=value)
                self.assertEqual(events, [])
        for value in (0, 1, None):
            with self.subTest(reference_only=value), self.runtime() as events:
                with self.assertRaisesRegex(ValueError, r"reference_only"):
                    incremental.resume_snapshot_calibration_study_incremental(
                        _CONSTRAINT, self.earlier, self.later, directory=directory,
                        expected_generation_sha256=reference_pin, reference_only=value,
                    )
                self.assertEqual(events, [])
        with self.runtime() as events:
            result = self.continue_fusion(directory, reference_pin, count=8)
        self.assertEqual(result["status"], _PREFIX + "COMPLETED")
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS)

    def test_append_reuses_prior_semantics_but_restores_references_each_invocation(self):
        directory = self.directory / "validation-reuse"
        reference_pin = self.references(directory)
        opened = []
        open_context = fusion_checkpoint._open_calibration_fusion_context

        def record_context(*args, **options):
            context = open_context(*args, **options)
            opened.append(context)
            return context

        for count, expected_validations in ((1, 1), (None, 3)):
            tip = None if not opened else paused["generation_sha256"]
            with (
                self.runtime() as events,
                patch.object(fusion_checkpoint, "_open_calibration_fusion_context", new=record_context),
                patch.object(store, "_read_complete_reference", wraps=store._read_complete_reference) as read_reference,
                patch.object(store, "_validate_reference_outcome", wraps=store._validate_reference_outcome) as validate,
            ):
                result = self.continue_fusion(directory, reference_pin, tip, count=count)
            self.assertEqual(read_reference.call_count, 1)
            self.assertEqual(validate.call_count, expected_validations)
            self.assertEqual(_observed(events, "restore"), [
                (layout, index) for layout in _LAYOUTS for index in (0, 1)
            ])
            self.assertEqual(_observed(events, "fusion"), _LAYOUTS[:1] if count == 1 else _LAYOUTS[1:])
            if count == 1:
                paused = result
                self.assertEqual(result["status"], _PREFIX + "PAUSED")
            else:
                self.assertEqual(result["status"], _PREFIX + "COMPLETED")
                self.assertEqual(result["study"], self.paired_study())
        self.assertEqual(len(opened), 2)
        self.assertIsNot(opened[0], opened[1])

        uncached = self.directory / "uncached-publication"
        self.assertEqual(self.references(uncached), reference_pin)
        tip = None
        for ordinal, layout in enumerate(_LAYOUTS, start=1):
            tip = store._publish_calibration_fusion_generation(
                uncached, reference_generation_sha256=reference_pin, previous_generation_sha256=tip,
                ordinal=ordinal, layout=layout, capture=result["study"]["fusion"]["captures"][_KEYS[ordinal - 1]],
            )["generation_sha256"]
        self.assertEqual(tip, result["generation_sha256"])
        self.assertEqual(_saved_files(uncached), _saved_files(directory))

    def test_changed_prior_bytes_during_candidate_or_verification_stop_before_marker(self):
        for kind in ("fusion", "reference"):
            for boundary in ("candidate", "verification"):
                with self.subTest(kind=kind, boundary=boundary):
                    directory = self.directory / f"tamper-{kind}-{boundary}"
                    reference_pin = self.references(directory)
                    first = self.first_fusion(directory, reference_pin)
                    group, pin = ("fusion-generations", first) if kind == "fusion" else ("generations", reference_pin)
                    prior = directory / group / f"{pin}.json"
                    original = prior.read_bytes()

                    def change_prior(_report=None):
                        prior.write_bytes(original + b" ")

                    verify = fusion_checkpoint._verify_fusion_generation

                    def verify_and_change(runtime, references, generation):
                        value = verify(runtime, references, generation)
                        if boundary == "verification" and generation.document["ordinal"] == 2:
                            change_prior()
                        return value

                    mutation = (_LAYOUTS[1], change_prior) if boundary == "candidate" else None
                    with (
                        self.runtime(candidate_mutation=mutation) as events,
                        patch.object(fusion_checkpoint, "_verify_fusion_generation", new=verify_and_change),
                    ):
                        result = self.continue_fusion(directory, reference_pin, first)
                    self.assertEqual(result["status"], _PREFIX + "PUBLICATION_FAILED")
                    self.assertEqual(result["generation_sha256"], first)
                    self.assertEqual(result["attempted_layouts"], _KEYS[:1])
                    self.assertEqual(result["failure"]["layout"], _KEYS[1])
                    self.assertIsNotNone(result["failure"]["attempt"]["capture"])
                    self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
                    self.assertEqual(list((directory / "fusion-generations").iterdir()), [
                        directory / "fusion-generations" / f"{first}.json",
                    ])
                    self.assertEqual(prior.read_bytes(), original + b" ")

                    with self.runtime() as events:
                        with self.assertRaisesRegex(ValueError, r"(?i)(SHA-256|digest|pin|changed)"):
                            self.continue_fusion(directory, reference_pin, first)
                    self.assertEqual(_observed(events, "fusion"), [])
                    self.assertEqual(_observed(events, "reference"), [])

                    prior.write_bytes(original)
                    with self.runtime() as events:
                        recovered = self.continue_fusion(directory, reference_pin, first)
                    self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:])
                    self.assertEqual(recovered["study"], self.paired_study())

    def test_failed_publication_does_not_advance_reusable_append_context(self):
        directory = self.directory / "context-publication-failure"
        reference_pin = self.references(directory)
        first = self.first_fusion(directory, reference_pin)
        publish = fusion_checkpoint._publish_calibration_fusion_generation
        calls = []

        def record_publish(*args, **options):
            calls.append((args, options.copy()))
            return publish(*args, **options)

        with (
            self.runtime() as events,
            patch.object(fusion_checkpoint, "_publish_calibration_fusion_generation", new=record_publish),
            patch.object(store, "_publish_bundle", side_effect=OSError("simulated marker publication failure")),
        ):
            result = self.continue_fusion(directory, reference_pin, first)
        self.assertEqual(result["status"], _PREFIX + "PUBLICATION_FAILED")
        self.assertEqual(result["generation_sha256"], first)
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
        self.assertEqual(len(calls), 1)
        args, options = calls[0]
        self.assertEqual(options["previous_generation_sha256"], first)
        second = publish(*args, **options)["generation_sha256"]
        self.assertNotEqual(second, first)
        with self.runtime() as events:
            completed = self.continue_fusion(directory, reference_pin, second)
        self.assertEqual(completed["status"], _PREFIX + "COMPLETED")
        self.assertEqual(completed["study"], self.paired_study())
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[2:])

    def test_cli_reference_only_and_fusion_continue_preserve_pins_on_output_failure(self):
        directory = self.directory / "cli"
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        archive_args = []
        for flag, archive in (("--earlier", self.earlier), ("--later", self.later)):
            archive_args.extend((flag, str(archive.configuration), str(archive.properties),
                                 archive.configuration_sha256, archive.properties_sha256))
        start_argv = [
            "austins-algorithm", "snapshot-calibration-study-incremental", str(constraint), str(directory),
            *archive_args, "--initial-layout", "0", "1", "--initial-layout", "0", "2",
            "--initial-layout", "1", "2", "--target-probability", ".7", "--shot-caps", "2",
            "--query-caps", "1", "--validate-trials", "3", "--fusion-probability-tolerance", "0",
            "--reference-only",
        ]
        with self.runtime() as events, patch.object(sys, "argv", start_argv), redirect_stdout(StringIO()) as stdout:
            main()
        references = json.loads(stdout.getvalue())
        self.assertEqual(references["status"], _REFERENCE_COMPLETE)
        self.assertEqual(_observed(events, "fusion"), [])
        reference_pin = references["generation_sha256"]
        destination = self.directory / "paired-study.json"
        continue_argv = [
            "austins-algorithm", "snapshot-calibration-study-fusion-continue", str(constraint), str(directory),
            *archive_args, "--expected-reference-generation-sha256", reference_pin,
        ]
        with (
            self.runtime() as events, patch.object(sys, "argv", [*continue_argv, "--max-new-fusion-layouts", "1"]),
            redirect_stdout(StringIO()) as stdout,
        ):
            main()
        paused = json.loads(stdout.getvalue())
        self.assertEqual(paused["status"], _PREFIX + "PAUSED")
        self.assertIsNone(paused["study"])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[:1])

        with (
            self.runtime() as events,
            patch.object(sys, "argv", [
                *continue_argv, "--expected-fusion-generation-sha256", paused["generation_sha256"],
                "--study-output", str(destination),
            ]),
            patch("austins_algorithm.snapshot_calibration_compilation_bundle._publish_bundle",
                  side_effect=OSError("simulated output write error")),
            redirect_stdout(StringIO()) as stdout, self.assertRaises(SystemExit) as failed,
        ):
            main()
        self.assertEqual(failed.exception.code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], _PREFIX + "OUTPUT_FAILED")
        self.assertEqual(result["reference_generation_sha256"], reference_pin)
        self.assertEqual(result["study"], self.paired_study())
        self.assertEqual(result["study_output_error"]["error_type"], "OSError")
        self.assertFalse(destination.exists())
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:])

        publish = bundles._publish_bundle

        def enforce_small_output_limit(path, raw):
            if path == destination:
                with patch.object(bundles, "_MAX_BUNDLE_BYTES", 1):
                    return publish(path, raw)
            return publish(path, raw)

        before = _saved_files(directory)
        with (
            self.runtime() as events,
            patch.object(sys, "argv", [
                *continue_argv, "--expected-fusion-generation-sha256", result["generation_sha256"],
                "--study-output", str(destination),
            ]),
            patch.object(bundles, "_publish_bundle", new=enforce_small_output_limit),
            redirect_stdout(StringIO()) as stdout, self.assertRaises(SystemExit) as oversized,
        ):
            main()
        self.assertEqual(oversized.exception.code, 1)
        limited = json.loads(stdout.getvalue())
        self.assertEqual(limited["status"], _PREFIX + "OUTPUT_FAILED")
        self.assertEqual(limited["study_output_error"]["error_type"], "ValueError")
        self.assertIn("size limit", limited["study_output_error"]["message"])
        self.assertEqual(limited["study"], result["study"])
        self.assertEqual(limited["reference_generation_sha256"], result["reference_generation_sha256"])
        self.assertEqual(limited["generation_sha256"], result["generation_sha256"])
        self.assertEqual(_observed(events, "reference"), [])
        self.assertEqual(_observed(events, "fusion"), [])
        self.assertEqual(_saved_files(directory), before)
        self.assertFalse(destination.exists())

        with (
            self.runtime() as events,
            patch.object(sys, "argv", [
                *continue_argv, "--expected-fusion-generation-sha256", result["generation_sha256"],
                "--study-output", str(destination),
            ]),
            redirect_stdout(StringIO()) as stdout,
        ):
            main()
        recovered = json.loads(stdout.getvalue())
        self.assertEqual(recovered["status"], _PREFIX + "COMPLETED")
        self.assertEqual(_observed(events, "fusion"), [])
        self.assertEqual(recovered["study"], result["study"])
        self.assertEqual(recovered["study_output"]["sha256"], sha256(destination.read_bytes()).hexdigest())
        saved = destination.read_bytes()
        with (
            self.runtime() as events,
            patch.object(sys, "argv", [*continue_argv, "--study-output", str(destination)]),
            redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as rejected,
        ):
            main()
        self.assertNotEqual(rejected.exception.code, 0)
        self.assertIn("already exists", stderr.getvalue())
        self.assertEqual(events, [])
        self.assertEqual(destination.read_bytes(), saved)

    def test_only_current_fusion_chunk_retains_programs_after_full_physical_verification(self):
        directory = self.directory / "limited-restored-lifetimes"
        reference_pin = self.references(directory)
        first = self.first_fusion(directory, reference_pin)
        before = _saved_files(directory)
        with self.runtime() as events, self.watched_restores(retained_layouts=_LAYOUTS[1:2]) as state:
            paused = self.continue_fusion(directory, reference_pin, first, count=1)
        self.assertEqual(paused["status"], _PREFIX + "PAUSED")
        self.assertEqual(paused["attempted_layouts"], _KEYS[:2])
        self.assertEqual(paused["completed_layouts"], _KEYS[:2])
        self.assertEqual(paused["remaining_layouts"], _KEYS[2:])
        self.assertIsNone(paused["study"])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
        self.assertEqual(len(state["density"]), 1)
        observed = state["density"][0]
        self.assertEqual(observed["restores"], [(layout, index) for layout in _LAYOUTS for index in (0, 1)])
        self.assertEqual(len(observed["calibrations"]), 12)
        self.assertEqual(len(observed["readout"]), 6)
        self.assertEqual(observed["live"], [(_LAYOUTS[1], 0), (_LAYOUTS[1], 1)])
        self.assertEqual(state["maximum_live"], 3)
        self.assert_restored_programs_released(state)
        after = _saved_files(directory)
        self.assertEqual({path: after[path] for path in before}, before)
        with self.runtime() as events:
            completed = self.continue_fusion(directory, reference_pin, paused["generation_sha256"])
        self.assertEqual(_observed(events, "fusion"), _LAYOUTS[2:])
        self.assertEqual(completed["study"], self.paired_study())

    def test_completed_and_terminal_tips_restore_every_round_without_retaining_programs(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                directory = self.directory / f"saved-final-{terminal}"
                reference_pin = self.references(directory)
                failure = (_LAYOUTS[0], "exception") if terminal else None
                with self.runtime(candidate_failure=failure):
                    saved = self.continue_fusion(directory, reference_pin)
                self.assertEqual(saved["status"], _PREFIX + ("TERMINAL" if terminal else "COMPLETED"))
                with self.runtime() as events, self.watched_restores(retained_layouts=()) as state:
                    reopened = self.continue_fusion(directory, reference_pin, saved["generation_sha256"])
                self.assertEqual(reopened, saved)
                self.assertEqual(state["restores"], [(layout, index) for layout in _LAYOUTS for index in (0, 1)])
                self.assertEqual(len(state["calibrations"]), 12)
                self.assertEqual(len(state["readout"]), 6)
                self.assertEqual(state["density"], [])
                self.assertEqual(state["maximum_live"], 1)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assert_restored_programs_released(state)

                with self.runtime() as events:
                    readout = checkpoint._checkpoint_readout_channels

                    def changed_readout(backend, active):
                        channels = readout(backend, active)
                        if active == _LAYOUTS[-1]:
                            channels[active[0]] = [[.875, .125], [.125, .875]]
                        return channels

                    with (
                        patch.object(checkpoint, "_checkpoint_readout_channels", new=changed_readout),
                        self.watched_restores(retained_layouts=()) as state,
                        self.assertRaisesRegex(ValueError, "(?i)readout"),
                    ):
                        self.continue_fusion(directory, reference_pin, saved["generation_sha256"])
                    self.assertEqual(len(state["restores"]), 6)
                    self.assertEqual(_observed(events, "fusion"), [])
                    self.assert_restored_programs_released(state)

    def test_skipped_completed_or_beyond_limit_programs_still_reject_corruption_before_density(self):
        directory = self.directory / "unselected-program-validation"
        reference_pin = self.references(directory)
        first = self.first_fusion(directory, reference_pin)
        for affected in (_LAYOUTS[0], _LAYOUTS[-1]):
            with self.subTest(affected=affected), self.runtime() as events:
                restore = checkpoint._restore_compiled_round

                def changed_restore(row, logical, backend, layout, seed, **options):
                    program = restore(row, logical, backend, layout, seed, **options)
                    if layout == affected and logical == 1:
                        program.report["compiled"]["depth"] += 1
                    return program

                with (
                    patch.object(checkpoint, "_restore_compiled_round", new=changed_restore),
                    self.watched_restores(retained_layouts=_LAYOUTS[1:2]) as state,
                    self.assertRaisesRegex(ValueError, "(?i)restored compilation"),
                ):
                    self.continue_fusion(directory, reference_pin, first, count=1)
                self.assertEqual(_observed(events, "fusion"), [])
                self.assert_restored_programs_released(state)

    def test_terminal_worker_and_publication_failures_release_selected_unused_programs(self):
        for kind in ("worker", "publication"):
            with self.subTest(kind=kind):
                directory = self.directory / f"selected-failure-{kind}"
                reference_pin = self.references(directory)
                first = self.first_fusion(directory, reference_pin)
                retained_error = RuntimeError("retained candidate worker exception")
                with self.runtime() as events:
                    capture = checkpoint._capture_restored_calibration_layout_fusion
                    publish = store._publish_bundle

                    def candidate(prepared, reference, programs):
                        if kind == "worker":
                            events.append(("fusion", tuple(reference["initial_layout"])))
                            raise retained_error
                        return capture(prepared, reference, programs)

                    def publisher(path, raw):
                        if kind == "publication":
                            raise OSError("selected program publication failure")
                        return publish(path, raw)

                    with (
                        patch.object(checkpoint, "_capture_restored_calibration_layout_fusion", new=candidate),
                        patch.object(store, "_publish_bundle", new=publisher),
                        self.watched_restores(retained_layouts=_LAYOUTS[1:]) as state,
                    ):
                        result = self.continue_fusion(directory, reference_pin, first, count=2)
                self.assertEqual(result["status"], _PREFIX + ("TERMINAL" if kind == "worker" else "PUBLICATION_FAILED"))
                self.assertEqual(_observed(events, "fusion"), _LAYOUTS[1:2])
                self.assertEqual(state["density"][0]["live"], [
                    (layout, index) for layout in _LAYOUTS[1:] for index in (0, 1)
                ])
                self.assert_restored_programs_released(state)

