"""Exercise one-pass receipt replay followed by the ordinary calibration study."""

from base64 import b64decode, b64encode
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from importlib.util import find_spec
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundle_format
from austins_algorithm import snapshot_calibration_overlap as overlap
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm import unknown_count_snapshot_cache as key_scope
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from test_snapshot_calibration_bundle_composition import _A, _B, _entry
from test_snapshot_calibration_bundle_overlap import _argv
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_overlap_receipt import (
    _REPLAYED, _directory_bytes, _receipt_args, _save_cli_receipt, _source,
)


_LAYOUTS = [(0, 1), (0, 2), (1, 0)]
_GRID = {"initial_layouts": _LAYOUTS[::-1], "target_probability": .7, "shot_caps": [1], "query_caps": [0]}
_COMPLETED = "COMPILATION_OVERLAP_RECEIPT_STUDY_COMPLETED"
_PARTIAL = "COMPILATION_OVERLAP_RECEIPT_STUDY_PARTIAL"
_UNAVAILABLE = "COMPILATION_OVERLAP_RECEIPT_STUDY_UNAVAILABLE"


class SnapshotCalibrationOverlapReceiptStudyInputTests(unittest.TestCase):
    def test_foreign_zero_overlap_can_replay_but_combined_study_requires_current_stack(self):
        directory = _test_directory(self)
        archive = comparison.SnapshotCalibrationFiles(
            directory / "missing-config.json", directory / "missing-properties.json", "a" * 64, "b" * 64,
        )
        versions = {**bundle_format._bundle_software_versions(), "qiskit": "foreign-combined-test-version"}
        sources = [
            _source(directory, "first", versions, {_A: _entry("opaque A")}),
            _source(directory, "second", versions, {_B: _entry("opaque B")}),
        ]
        receipt, output, recorded = _save_cli_receipt(directory, archive, archive, sources)
        self.assertEqual(recorded["study_overlap"]["status"], "NOT_REQUIRED")
        args = _receipt_args(receipt, output, sources)
        argv = _argv(directory, archive, archive, [])
        before = _directory_bytes(directory)
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(comparison, "_read_archive") as read_archive,
            patch.object(overlap, "_restore_compiled_round") as restore,
        ):
            replay = study.replay_snapshot_calibration_overlap_receipt(_MARK_00, archive, archive, **args, **_GRID)
            self.assertEqual(replay["status"], "NO_OVERLAPS_TO_REPLAY")
            with self.assertRaisesRegex(ValueError, r"(?i)(software|environment|version)"):
                study.run_snapshot_calibration_study_with_overlap_receipt(_MARK_00, archive, archive, **args, **_GRID)
            with (
                patch.object(sys, "argv", [*argv, "--run-after-overlap-replay"]),
                redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as rejected,
            ):
                main()
            self.assertNotEqual(rejected.exception.code, 0)
            self.assertIn("--replay-overlap-receipt", stderr.getvalue())
        for mock in (prepare, read_archive, restore):
            mock.assert_not_called()
        self.assertEqual(_directory_bytes(directory), before)


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationOverlapReceiptStudyTests(unittest.TestCase):
    def setUp(self):
        from qiskit import qpy
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        self.directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = self.directory / "configuration.json"
        configuration.write_bytes((source / FakeJakartaV2.conf_filename).read_bytes())
        later_properties = self.directory / "later.json"
        later_properties.write_bytes((source / FakeJakartaV2.props_filename).read_bytes())
        self.later = _pinned(configuration, later_properties)
        earlier = json.loads(later_properties.read_bytes())
        earlier["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier_properties = self.directory / "earlier.json"
        earlier_properties.write_text(json.dumps(earlier), encoding="utf-8")
        self.earlier = _pinned(configuration, earlier_properties)

        cache = SnapshotCalibrationCompilationCache()
        for layout in _LAYOUTS[:2]:
            captured = comparison.compare_snapshot_calibrations(
                _MARK_00, self.earlier, self.later, initial_layout=layout, max_rounds=0, compilation_cache=cache,
            )
            self.assertEqual(captured["status"], "COMPLETED")
        first = self.directory / "first.json"
        self.first = first, cache.save_bundle(first)
        document = json.loads(first.read_bytes())
        [self.key] = [key for key, row in document["entries"].items()
                      if row["report"]["initial_logical_to_target_qubit"] == [0, 1]]
        self.versions = document["software_versions"]
        row = deepcopy(document["entries"][self.key])
        [circuit] = qpy.load(BytesIO(b64decode(row["qpy"])))
        circuit.metadata = {"receipt_fixture": "same physical circuit, different QPY bytes"}
        encoded = BytesIO()
        qpy.dump(circuit, encoded)
        row["qpy"] = b64encode(encoded.getvalue()).decode("ascii")
        self.assertNotIn(_B, document["entries"])
        second = _source(self.directory, "second", self.versions, {
            self.key: row, _B: _entry("unused opaque payload"),
        })
        self.sources = [self.first, second]
        composed = self.directory / "composed.json"
        self.recorded = study.compose_snapshot_calibration_compilation_for_study(
            _MARK_00, self.earlier, self.later, destination=composed, bundles=self.sources, **_GRID,
        )
        self.assertEqual(self.recorded["status"], "COMPILATION_BUNDLE_COMPOSED")
        self.output = composed, self.recorded["output"]["sha256"]
        receipt = self.directory / "receipt.json"
        self.receipt = receipt, _write_report(receipt, self.recorded)

    def run_combined(self, *, receipt=None, output=None, sources=None, **options):
        return study.run_snapshot_calibration_study_with_overlap_receipt(
            _MARK_00, self.earlier, self.later,
            **_receipt_args(self.receipt if receipt is None else receipt, self.output if output is None else output,
                            self.sources if sources is None else sources),
            **{**_GRID, **options},
        )

    def combined_argv(self):
        argv = _argv(self.directory, self.earlier, self.later, [])
        for layout in _LAYOUTS[1:]:
            argv.extend(("--initial-layout", *(str(wire) for wire in layout)))
        argv.extend((
            "--run-after-overlap-replay", "--replay-overlap-receipt", str(self.receipt[0]), self.receipt[1],
            "--load-compilation-bundle", str(self.output[0]), self.output[1],
        ))
        for path, pin in self.sources:
            argv.extend(("--overlap-source-bundle", str(path), pin))
        return argv

    def test_exact_standalone_report_parity_with_one_preparation_and_no_redundant_qpy_decode(self):
        from qiskit import qpy

        separate_replay = study.replay_snapshot_calibration_overlap_receipt(
            _MARK_00, self.earlier, self.later, **_receipt_args(self.receipt, self.output, self.sources), **_GRID,
        )
        cache = SnapshotCalibrationCompilationCache.load_bundle(self.output[0], expected_sha256=self.output[1])
        separate_study = study.run_snapshot_calibration_study(
            _MARK_00, self.earlier, self.later, compilation_cache=cache, **_GRID,
        )
        argv = self.combined_argv()
        before = _directory_bytes(self.directory)
        artifacts = {path for path, _ in (self.receipt, self.output, *self.sources)}
        opened = Counter()
        path_open = Path.open

        def counted_open(path, *args, **kwargs):
            if path in artifacts:
                opened[path] += 1
            return path_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", new=counted_open),
            patch.object(comparison, "_read_archive", wraps=comparison._read_archive) as read_archive,
            patch.object(comparison, "_offline_backend", wraps=comparison._offline_backend) as build_backend,
            patch.object(comparison, "audit_phase_oracle", wraps=comparison.audit_phase_oracle) as audit,
            patch.object(key_scope, "_target_profile", wraps=key_scope._target_profile) as target_profile,
            patch.object(comparison, "_round_profile_key", wraps=comparison._round_profile_key) as round_key,
            patch.object(qpy, "load", wraps=qpy.load) as decode,
            patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round,
            patch.object(comparison, "_prepare_density_snapshot", wraps=comparison._prepare_density_snapshot) as density,
        ):
            combined = self.run_combined(shot_caps=iter([1]), query_caps=iter([0]))
        self.assertEqual(combined["schema_version"], 1)
        self.assertEqual(combined["status"], _COMPLETED)
        self.assertEqual(json.dumps(combined["replay"]), json.dumps(separate_replay))
        self.assertEqual(json.dumps(combined["study"]), json.dumps(separate_study))
        self.assertEqual(opened, {path: 1 for path in artifacts})
        self.assertEqual(read_archive.call_count, 4)
        self.assertEqual(build_backend.call_count, 2)
        audit.assert_called_once()
        target_profile.assert_called_once()
        self.assertEqual(round_key.call_count, 3)
        self.assertEqual(decode.call_count, 3)
        compile_round.assert_called_once()
        self.assertEqual(tuple(compile_round.call_args.kwargs["initial_layout"]), _LAYOUTS[-1])
        self.assertEqual(density.call_count, 6)
        with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
            main()
        self.assertEqual(json.dumps(json.loads(stdout.getvalue())), json.dumps(combined))
        self.assertEqual(_directory_bytes(self.directory), before)

    def test_replay_failure_prevents_every_capture_and_publication(self):
        changed = deepcopy(self.recorded)
        for source in changed["overlap_verification"][self.key]["sources"].values():
            source["physical_signature_sha256"] = "9" * 64
        path = self.directory / "false-receipt.json"
        receipt = path, _write_report(path, changed)
        before = _directory_bytes(self.directory)
        with (
            patch.object(study, "_capture_calibration_layout") as capture,
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            patch.object(bundle_format, "_publish_bundle") as publish,
            self.assertRaisesRegex(ValueError, r"(?i)(physical|evidence|overlap)"),
        ):
            self.run_combined(receipt=receipt)
        for mock in (capture, compile_round, density, publish):
            mock.assert_not_called()
        self.assertEqual(_directory_bytes(self.directory), before)

    def test_timeouts_preserve_replay_and_follow_study_cancellation_rules(self):
        prepare_density = comparison._prepare_density_snapshot
        before = _directory_bytes(self.directory)
        for blocked_call, cancelled, status, completed, unavailable, unattempted, attempts in (
            (3, False, _PARTIAL, ["0,1"], ["0,2"], ["1,0"], 3),
            (3, True, _PARTIAL, ["0,1", "1,0"], ["0,2"], [], 5),
            (1, False, _UNAVAILABLE, [], ["0,1"], ["0,2", "1,0"], 1),
        ):
            calls = 0

            def prepare_or_timeout(*args, **options):
                nonlocal calls
                calls += 1
                if calls == blocked_call:
                    return None, {"status": "LOCAL_RESULT_WAIT_TIMED_OUT", "local_job_cancelled": cancelled}
                return prepare_density(*args, **options)

            with (
                self.subTest(blocked_call=blocked_call, cancelled=cancelled),
                patch.object(comparison, "_prepare_density_snapshot", side_effect=prepare_or_timeout),
            ):
                combined = self.run_combined()
                self.assertEqual(calls, attempts)
                self.assertEqual(combined["status"], status)
                self.assertEqual(combined["replay"]["status"], _REPLAYED)
                self.assertEqual(combined["replay"]["replayed_overlaps"], self.recorded["overlap_verification"])
                captured = combined["study"]
                self.assertEqual(captured["coverage"], {
                    "requested_layouts": ["0,1", "0,2", "1,0"], "completed_layouts": completed,
                    "unavailable_layouts": unavailable, "unattempted_layouts": unattempted,
                })
                failure = captured["captures"][unavailable[0]]["report"]
                self.assertEqual(failure["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
                self.assertIs(failure["local_job_cancelled"], cancelled)
                self.assertEqual(captured["planning"] is not None, bool(completed))
        self.assertEqual(_directory_bytes(self.directory), before)

    def test_legacy_layout_receipt_and_current_stack_zero_overlap_both_run(self):
        legacy = deepcopy(self.recorded)
        del legacy["study_overlap"]["request"]["initial_layouts"]
        path = self.directory / "legacy.json"
        result = self.run_combined(receipt=(path, _write_report(path, legacy)))
        self.assertEqual(result["status"], _COMPLETED)
        self.assertEqual(result["replay"]["status"], _REPLAYED)
        self.assertFalse(result["replay"]["request_layouts_bound"])

        sources = [self.first, _source(self.directory, "disjoint", self.versions, {_B: _entry("unused opaque payload")})]
        output = self.directory / "zero-composed.json"
        report = study.compose_snapshot_calibration_compilation_for_study(
            _MARK_00, self.earlier, self.later, destination=output, bundles=sources, **_GRID,
        )
        self.assertEqual(report["study_overlap"]["status"], "NOT_REQUIRED")
        path = self.directory / "zero-receipt.json"
        zero = self.run_combined(
            receipt=(path, _write_report(path, report)), output=(output, report["output"]["sha256"]), sources=sources,
        )
        self.assertEqual(zero["status"], _COMPLETED)
        self.assertEqual(zero["replay"]["status"], "NO_OVERLAPS_TO_REPLAY")
        self.assertFalse(zero["replay"]["request_layouts_bound"])
        self.assertEqual(json.dumps(zero["study"]), json.dumps(result["study"]))


if __name__ == "__main__":
    unittest.main()
