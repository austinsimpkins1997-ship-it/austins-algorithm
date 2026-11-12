"""Verify saved paired calibration studies without reopening archives or running Aer."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.__main__ import main
from austins_algorithm.constraints import BooleanConstraint
from austins_algorithm.snapshot_calibration_study_verification import verify_snapshot_calibration_study
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT
import test_snapshot_calibration_study as fixtures


_LAYOUTS = [(0, 1), (0, 2), (1, 2)]
_KEYS = ["0,1", "0,2", "1,2"]
_VERIFIED = "PAIRED_CALIBRATION_STUDY_VERIFIED"
_REPLAY_RECORDED = "NO_OVERLAPS_TO_REPLAY"
_REPLAY_RESOLVED = "COMPILATION_BUNDLE_OVERLAP_RECEIPT_REPLAYED"


def _wrapper(report, *, resolved=False):
    replay = {
        "schema_version": 1, "status": _REPLAY_RECORDED, "evidence_scope": "RECORDED_COMPILATION_OVERLAPS_ONLY",
        "receipt_sha256": "a" * 64, "output_sha256": "b" * 64, "output_bytes": 5,
        "source_bundle_sha256s": ["c" * 64, "d" * 64],
        "request": None, "compiler_scope_sha256": None, "request_layouts_bound": False,
        "verified_conflict_count": 0, "replayed_overlaps": {}, "oracle_audit_performed": False,
        "stored_qpy_decoded": False, "snapshot_simulation_performed": False,
        "provider_job_submitted": False, "quantum_hardware_used": False,
    }
    if resolved:
        capture = report["captures"][_KEYS[0]]["report"]
        compiler_pin, overlap_key = "e" * 64, "f" * 64
        replay.update(
            status=_REPLAY_RESOLVED, compiler_scope_sha256=compiler_pin, request_layouts_bound=True,
            request={
                "inputs": deepcopy(report["inputs"]), "required_rounds": report["required_rounds"],
                "snapshots": capture["snapshots"], "versions": {**capture["versions"], "qiskit-qasm3-import": "0.6.0"},
                "compiler_scope_sha256": compiler_pin, "initial_layouts": [list(layout) for layout in _LAYOUTS],
            },
            verified_conflict_count=1, replayed_overlaps={
                overlap_key: {
                    "key": overlap_key, "status": "EQUIVALENT_PHYSICAL_CIRCUITS",
                    "initial_layout": list(_LAYOUTS[0]), "round": 0,
                },
            },
            oracle_audit_performed=True, stored_qpy_decoded=True,
        )
    return {
        "schema_version": 1, "status": "COMPILATION_OVERLAP_RECEIPT_STUDY_COMPLETED",
        "replay": replay, "study": report, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


def _reverse_objects(value):
    if isinstance(value, dict):
        return {key: _reverse_objects(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_objects(item) for item in value]
    return value


def _reseal(capture):
    capture["report_sha256"] = _digest(capture["report"])


class SnapshotCalibrationStudyVerificationTests(unittest.TestCase):
    setUp = fixtures.SnapshotCalibrationStudyTests.setUp
    capture = fixtures.SnapshotCalibrationStudyTests.capture
    timeout = fixtures.SnapshotCalibrationStudyTests.timeout

    def fixture(self, *, reference_timeout=None, fused_timeout=None, candidate_error=None):
        def reference(prepared, layout, **_options):
            if reference_timeout is not None and layout == reference_timeout[0]:
                return self.timeout(prepared, layout, cancelled=reference_timeout[1]), None
            return self.capture(prepared, layout), SimpleNamespace(layout=layout)

        def candidate(prepared, pending):
            layout = pending.layout
            if layout == candidate_error:
                raise RuntimeError("synthetic candidate failure")
            if fused_timeout is not None and layout == fused_timeout[0]:
                blocked = _marked(self.timeout(prepared, layout, cancelled=fused_timeout[1]))
                blocked["density_fusion"]["observed"] = {"earlier": {}, "later": {}}
                return blocked
            return _marked(self.capture(prepared, layout))

        with (
            patch.object(study, "_capture_calibration_layout_reference", side_effect=reference),
            patch.object(study, "_capture_calibration_layout_fusion", side_effect=candidate),
        ):
            return study.run_snapshot_calibration_study(
                _CONSTRAINT, self.earlier, self.later, initial_layouts=_LAYOUTS[::-1],
                target_probability=.7, shot_caps=[2], query_caps=[1], validation_trials=3,
                fusion_probability_tolerance=0.0,
            )

    def write_report(self, report):
        raw = report if isinstance(report, bytes) else json.dumps(report, allow_nan=False).encode("utf-8")
        path = self.directory / "saved-study.json"
        path.write_bytes(raw)
        return path, sha256(raw).hexdigest()

    def verify(self, report, *, constraint=_CONSTRAINT, expected_sha256=None):
        path, pin = self.write_report(report)
        with patch.object(comparison, "_read_archive", side_effect=AssertionError("offline verifier opened an archive")):
            return verify_snapshot_calibration_study(
                constraint, path, expected_sha256=pin if expected_sha256 is None else expected_sha256,
            )

    def assert_verified(self, result, report, *, replay_status=None):
        self.assertEqual(result["status"], _VERIFIED)
        self.assertEqual(result["study_status"], report["status"])
        self.assertEqual(result["fusion_status"], report["fusion"]["status"])
        check = report["fusion"]["check"]
        self.assertEqual(result["fusion_check_status"], None if check is None else check["status"])
        self.assertEqual(result["receipt_replay_status"], replay_status)
        self.assertEqual(result["receipt_replay_evidence"], None if replay_status is None else "RECORDED_ONLY")
        self.assertEqual(result["coverage"], report["coverage"])
        self.assertEqual(result["fusion_coverage"], report["fusion"]["coverage"])
        self.assertEqual(result["reference_plan_verified"], report["planning"] is not None)
        self.assertEqual(result["seeded_replay_verified"], report["planning"] is not None)

    def test_verifies_complete_plain_and_reserialized_receipt_wrapper(self):
        report = self.fixture()
        documents = (
            (report, None), (_reverse_objects(_wrapper(report)), _REPLAY_RECORDED),
            (_reverse_objects(_wrapper(report, resolved=True)), _REPLAY_RESOLVED),
        )
        for document, replay_status in documents:
            with self.subTest(replay_status=replay_status):
                path, pin = self.write_report(document)
                before = {file.name: file.read_bytes() for file in self.directory.iterdir()}
                checked = verify_snapshot_calibration_study(_CONSTRAINT, path, expected_sha256=pin)
                self.assert_verified(checked, report, replay_status=replay_status)
                self.assertEqual(checked["source_sha256"], pin)
                self.assertEqual(checked["source_bytes"], len(before[path.name]))
                self.assertEqual({file.name: file.read_bytes() for file in self.directory.iterdir()}, before)

    def test_verifies_supported_incomplete_coverage_and_reference_plan(self):
        cases = (
            ({"reference_timeout": (_LAYOUTS[1], False)}, _KEYS[:1], []),
            ({"reference_timeout": (_LAYOUTS[0], False)}, [], []),
            ({"fused_timeout": (_LAYOUTS[1], True)}, _KEYS, [_KEYS[0], _KEYS[2]]),
            ({"candidate_error": _LAYOUTS[1]}, _KEYS, _KEYS[:1]),
        )
        for failure, references, candidates in cases:
            with self.subTest(failure=failure):
                report = self.fixture(**failure)
                checked = self.verify(report)
                self.assert_verified(checked, report)
                self.assertIsNone(checked["fusion_check_status"])
                self.assertEqual(checked["coverage"]["completed_layouts"], references)
                self.assertEqual(checked["fusion_coverage"]["completed_layouts"], candidates)

    def test_rejects_wrong_pin_duplicate_keys_and_nonfinite_numbers(self):
        report = self.fixture()
        with self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest|pin)"):
            self.verify(report, expected_sha256="0" * 64)
        raw = json.dumps(report).encode("utf-8")
        duplicate = b'{"schema_version":1,' + raw[1:]
        nonfinite = raw.replace(b'"probability_tolerance": 0.0', b'"probability_tolerance": NaN', 1)
        overflow = nonfinite.replace(b'"probability_tolerance": NaN', b'"probability_tolerance": 1e999', 1)
        self.assertNotEqual(nonfinite, raw)
        for label, payload in (("duplicate", duplicate), ("nonfinite", nonfinite), ("overflow", overflow)):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.verify(payload)

    def test_rejects_unsupported_scope_and_mismatched_constraint_or_options(self):
        report = self.fixture()
        wrong_constraint = BooleanConstraint(2, ((1,), (2,)))
        with self.assertRaisesRegex(ValueError, r"(?i)constraint"):
            self.verify(report, constraint=wrong_constraint)
        for path, replacement in (
            (("schema_version",), 2),
            (("schema_version",), True),
            (("provider_job_submitted",), True),
            (("inputs", "shot_caps"), [1]),
            (("inputs", "seed_transpiler"), 19),
        ):
            with self.subTest(path=path, replacement=replacement):
                changed = deepcopy(report)
                parent = changed if len(path) == 1 else changed[path[0]]
                parent[path[-1]] = replacement
                with self.assertRaises(ValueError):
                    self.verify(changed)
        ordinary = deepcopy(report)
        del ordinary["fusion"]
        with self.assertRaises(ValueError):
            self.verify(ordinary)
        wrapped = _wrapper(report)
        wrapped["status"] = "COMPILATION_OVERLAP_RECEIPT_STUDY_PARTIAL"
        with self.assertRaises(ValueError):
            self.verify(wrapped)

    def test_rejects_repinning_recorded_replay_with_mismatched_study_scope(self):
        report = self.fixture()
        for field, value in (("seed_transpiler", 19), ("constraint_sha256", "0" * 64)):
            with self.subTest(field=field):
                wrapper = _wrapper(report, resolved=True)
                wrapper["replay"]["request"]["inputs"][field] = value
                with self.assertRaisesRegex(ValueError, r"recorded replay inputs"):
                    self.verify(wrapper)

    def test_rejects_independent_repinning_of_plan_seeded_replay_and_diagnostic(self):
        report = self.fixture()
        self.assertTrue(report["planning"]["validation"]["plans"])
        changes = (
            (("planning", "candidates", 0, "earlier", "found_probability"), .123),
            (("planning", "validation", "plans", 0, "earlier", "traces_sha256"), "0" * 64),
            (("fusion", "check", "maximum_total_variation_distance"), .001),
            (("fusion", "check", "decisions_match"), False),
            (("planning", "layouts", _KEYS[0], "source_capture_sha256"), "0" * 64),
            (("captures", _KEYS[0], "report_sha256"), "0" * 64),
            (("fusion", "captures", _KEYS[0], "report_sha256"), "0" * 64),
        )
        for path, replacement in changes:
            with self.subTest(path=path):
                changed = deepcopy(report)
                parent = changed
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = replacement
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_rejects_unsupported_execution_mode_even_with_recomputed_capture_links(self):
        report = self.fixture()
        key = _KEYS[0]
        capture = report["captures"][key]
        capture["report"]["execution_mode"] = "HARDWARE"
        _reseal(capture)
        report["planning"]["layouts"][key]["source_capture_sha256"] = capture["report_sha256"]
        report["fusion"]["check"]["layouts"][key]["reference_report_sha256"] = capture["report_sha256"]
        with self.assertRaises(ValueError):
            self.verify(report)

    def test_rejects_resealed_impossible_calibration_and_readout(self):
        reference = self.fixture()
        key = _KEYS[0]
        changes = (
            ("null calibration", "target_calibrations", None),
            ("nonstochastic readout", "readout_assignment_matrices", {
                "0": [[-1, 2], [0, 1]], "1": [[1, 0], [0, 1]],
            }),
            ("readout outside active wires", "readout_assignment_matrices", {
                "6": [[1, 0], [0, 1]], "1": [[1, 0], [0, 1]],
            }),
        )
        for label, field, replacement in changes:
            with self.subTest(label=label):
                report = deepcopy(reference)
                left = report["captures"][key]
                right = report["fusion"]["captures"][key]
                for capture in (left, right):
                    capture["report"]["rounds"]["0"][field]["earlier"] = replacement
                    _reseal(capture)
                report["planning"]["layouts"][key]["source_capture_sha256"] = left["report_sha256"]
                layout_check = report["fusion"]["check"]["layouts"][key]
                layout_check["reference_report_sha256"] = left["report_sha256"]
                layout_check["fused_report_sha256"] = right["report_sha256"]
                with self.assertRaisesRegex(ValueError, r"(?i)(calibration|readout)"):
                    self.verify(report)

    def test_rejects_illegal_work_after_uncancelled_timeout_with_valid_coverage_and_pins(self):
        key = _KEYS[1]
        for phase, fixture_option in (("reference", "reference_timeout"), ("fusion", "fused_timeout")):
            with self.subTest(phase=phase):
                report = self.fixture(**{fixture_option: (_LAYOUTS[1], True)})
                captures = report["captures"] if phase == "reference" else report["fusion"]["captures"]
                blocked = captures[key]
                self.assertIn(_KEYS[2], captures)
                blocked["report"]["local_job_cancelled"] = False
                _reseal(blocked)
                with self.assertRaises(ValueError):
                    self.verify(report)

    def test_rejects_fusion_after_unconfirmed_reference_timeout(self):
        report = self.fixture(reference_timeout=(_LAYOUTS[-1], False))
        fusion = report["fusion"]
        key = _KEYS[0]
        fusion["captures"][key] = self.fixture()["fusion"]["captures"][key]
        fusion["status"] = "CALIBRATION_FUSION_STUDY_PARTIAL"
        fusion["coverage"]["completed_layouts"] = [key]
        fusion["coverage"]["unattempted_layouts"] = _KEYS[1:]
        with self.assertRaises(ValueError):
            self.verify(report)

    def test_cli_verifies_pinned_report_and_rejects_wrong_pin(self):
        report = self.fixture()
        study_path, pin = self.write_report(report)
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        argv = ["austins-algorithm", "snapshot-calibration-study-verify", str(constraint), str(study_path),
                "--expected-study-sha256"]
        with patch.object(sys, "argv", [*argv, pin]), redirect_stdout(StringIO()) as stdout:
            main()
        verified = json.loads(stdout.getvalue())
        self.assert_verified(verified, report)
        self.assertEqual(verified["source_sha256"], pin)
        with (
            patch.object(sys, "argv", [*argv, "0" * 64]), redirect_stderr(StringIO()) as stderr,
            self.assertRaises(SystemExit) as rejected,
        ):
            main()
        self.assertNotEqual(rejected.exception.code, 0)
        self.assertRegex(stderr.getvalue(), r"(?i)(sha-?256|digest|pin)")
