"""SDK-free orchestration checks for a dated calibration study."""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import snapshot_calibration_comparison, snapshot_calibration_study, unknown_count_snapshot_layout
from austins_algorithm.__main__ import main
from test_snapshot_calibration_comparison import _snapshot
from test_snapshot_calibration_planning import _CONSTRAINT, _report


_DATES = ("2021-07-26T11:47:01-04:00", "2024-05-27T14:32:24-04:00")
_HITS = ((.5, .25), (.75, .875))
_TIMEOUT = "LOCAL_RESULT_WAIT_TIMED_OUT"
_VERSIONS = {"qiskit": "2.6.2", "qiskit-aer": "0.18.2", "qiskit-ibm-runtime": "0.51.0", "numpy": "2.5.4"}


def _digest(report):
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(canonical).hexdigest()


class SnapshotCalibrationStudyTests(unittest.TestCase):
    def setUp(self):
        build = (PACKAGE / ".build").resolve()
        if not build.is_relative_to(PACKAGE):
            raise RuntimeError("calibration study test output must stay in the package")
        build.mkdir(exist_ok=True)
        directory = TemporaryDirectory(dir=build, prefix="calibration-study-test-")
        self.addCleanup(directory.cleanup)
        self.fixture_contexts = ExitStack()
        self.addCleanup(self.fixture_contexts.close)
        self.directory = Path(directory.name)
        self.earlier = _snapshot(self.directory, "earlier", _DATES[0])
        self.later = _snapshot(self.directory, "later", _DATES[1])
        self.oracle = SimpleNamespace(num_qubits=2)
        self.backends = {label: SimpleNamespace(target=object()) for label in ("earlier", "later")}
        backends_by_date = dict(zip(_DATES, self.backends.values(), strict=True))
        self.build_oracle = self.fixture_contexts.enter_context(patch.object(
            snapshot_calibration_comparison, "build_constraint_phase_oracle", return_value=self.oracle,
        ))
        self.audit_oracle = self.fixture_contexts.enter_context(patch.object(
            snapshot_calibration_comparison, "audit_phase_oracle",
            return_value={"states_checked": 4, "marked_count": 1, "marked_states": ["00"]},
        ))
        self.build_backend = self.fixture_contexts.enter_context(patch.object(
            snapshot_calibration_comparison, "_offline_backend",
            side_effect=lambda configuration, properties: backends_by_date[properties["last_update_date"]],
        ))
        self.fixture_contexts.enter_context(patch.object(
            snapshot_calibration_comparison, "version", side_effect=_VERSIONS.__getitem__,
        ))

    def study(self, **overrides):
        options = {"initial_layouts": [(0, 1)], "target_probability": .7, "shot_caps": [2], "query_caps": [1]}
        return snapshot_calibration_study.run_snapshot_calibration_study(
            _CONSTRAINT, self.earlier, self.later, **{**options, **overrides},
        )

    def capture(self, prepared, layout):
        self.assertIs(prepared.oracle, self.oracle)
        self.assertEqual(prepared.backends, self.backends)
        self.assertIn(tuple(layout), prepared.layouts)
        scope = prepared.scope
        result = _report(tuple(layout), hits=_HITS[:scope["max_rounds"] + 1])
        result.update(deepcopy(scope))
        for row in result["rounds"].values():
            row["compilation"]["seed_transpiler"] = scope["seed_transpiler"]
        return result

    def timeout(self, prepared, layout, *, cancelled):
        result = self.capture(prepared, layout)
        for row in result["rounds"].values():
            for key in set(row) - {"compilation", "target_calibrations"}:
                del row[key]
        result.update(
            status=_TIMEOUT, local_job_cancelled=cancelled, blocked_round=0, blocked_snapshot="earlier",
            maximum_absolute_hit_probability_difference=None,
        )
        return result

    def test_validates_layouts_and_budget_axes_before_any_capture(self):
        invalid = (
            {"initial_layouts": []},
            {"initial_layouts": [(0, 1), [0, 1]]},
            {"initial_layouts": [(0,)]},
            {"initial_layouts": [(0, 1), (0, 1, 2)]},
            {"initial_layouts": [(0, 0)]},
            {"initial_layouts": [(0, True)]},
            {"initial_layouts": [(0, -1)]},
            {"initial_layouts": [(0, 256)]},
            {"target_probability": float("nan")},
            {"shot_caps": []},
            {"query_caps": [-1]},
            {"max_rounds_per_shot": True},
            {"seed_transpiler": -1},
            {"validation_trials": 129},
        )
        for inputs in invalid:
            with (
                self.subTest(inputs=inputs),
                patch.object(snapshot_calibration_study, "_capture_calibration_layout") as capture,
                self.assertRaises(ValueError),
            ):
                self.study(**inputs)
            capture.assert_not_called()
        self.build_oracle.assert_not_called()
        self.audit_oracle.assert_not_called()
        self.build_backend.assert_not_called()

    def test_study_grid_limits_are_checked_before_any_capture(self):
        with (
            patch.object(unknown_count_snapshot_layout, "MAX_SELECTION_TRANSITIONS", 6),
            patch.object(snapshot_calibration_study, "_prepare_calibration_comparison") as prepare,
            self.assertRaisesRegex(ValueError, "exceeds 6 modeled transition updates"),
        ):
            self.study(initial_layouts=[(0, 1), (0, 2)])
        prepare.assert_not_called()

    def test_rejects_later_layout_outside_actual_device_before_oracle_or_backend_build(self):
        with (
            patch.object(snapshot_calibration_study, "_capture_calibration_layout") as capture,
            self.assertRaisesRegex(ValueError, r"(?i)(layout|wire)"),
        ):
            self.study(initial_layouts=[(0, 7), (0, 1)])
        capture.assert_not_called()
        self.build_oracle.assert_not_called()
        self.audit_oracle.assert_not_called()
        self.build_backend.assert_not_called()

    def test_checks_oracle_wire_count_before_auditing_or_building_backends(self):
        with (
            patch.object(snapshot_calibration_study, "_capture_calibration_layout") as capture,
            self.assertRaisesRegex(ValueError, r"(?i)(layout|oracle|wire)"),
        ):
            self.study(initial_layouts=[(0, 1, 2)])
        self.build_oracle.assert_called_once_with(_CONSTRAINT)
        capture.assert_not_called()
        self.audit_oracle.assert_not_called()
        self.build_backend.assert_not_called()

    def test_only_captures_rounds_reachable_under_schedule_and_caps(self):
        cases = (
            ({"shot_caps": [1], "query_caps": [8]}, [0]),
            ({"shot_caps": [3], "query_caps": [0]}, [0]),
            ({"shot_caps": [3], "query_caps": [1], "max_rounds_per_shot": 0}, [0]),
            ({"shot_caps": [3], "query_caps": [1]}, [0, 1]),
        )
        for options, rounds in cases:
            with (
                self.subTest(options=options),
                patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=self.capture) as capture,
            ):
                result = self.study(**options)
                capture.assert_called_once()
                self.assertEqual(capture.call_args.args[0].scope["max_rounds"], rounds[-1])
                self.assertEqual(result["required_rounds"], rounds)
                self.assertEqual(result["planning"]["required_rounds"], rounds)
                saved = result["captures"]["0,1"]
                self.assertEqual(set(saved["report"]["rounds"]), set(map(str, rounds)))
                self.assertEqual(saved["report_sha256"], _digest(saved["report"]))
                self.assertEqual(result["planning"]["layouts"]["0,1"]["source_capture_sha256"], saved["report_sha256"])

    def test_prepares_once_then_captures_each_ordered_layout_in_canonical_order(self):
        with (
            patch.object(snapshot_calibration_comparison, "_load_snapshot",
                         wraps=snapshot_calibration_comparison._load_snapshot) as load,
            patch.object(snapshot_calibration_comparison, "_read_archive",
                         wraps=snapshot_calibration_comparison._read_archive) as read,
            patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=self.capture) as capture,
        ):
            result = self.study(
                initial_layouts=[(2, 0), (0, 2), (0, 1)], seed_transpiler=19,
                shot_caps=iter([2, 1, 2]), query_caps=iter([1, 0]),
            )
        layouts = ["0,1", "0,2", "2,0"]
        expected_layouts = ((0, 1), (0, 2), (2, 0))
        self.assertEqual(tuple(tuple(call.args[1]) for call in capture.call_args_list), expected_layouts)
        prepared = capture.call_args_list[0].args[0]
        self.assertTrue(all(call.args[0] is prepared for call in capture.call_args_list))
        self.assertEqual(prepared.layouts, expected_layouts)
        self.assertEqual(prepared.scope["seed_transpiler"], 19)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(read.call_count, 4)
        self.build_oracle.assert_called_once_with(_CONSTRAINT)
        self.audit_oracle.assert_called_once_with(self.oracle, _CONSTRAINT.qubits, _CONSTRAINT.matches)
        self.assertEqual(self.build_backend.call_count, 2)
        self.assertEqual(result["status"], "CALIBRATION_STUDY_COMPLETED")
        self.assertFalse(result["provider_job_submitted"])
        self.assertFalse(result["quantum_hardware_used"])
        self.assertEqual(result["coverage"], {
            "requested_layouts": layouts, "completed_layouts": layouts,
            "unavailable_layouts": [], "unattempted_layouts": [],
        })
        self.assertEqual(list(result["captures"]), layouts)
        self.assertEqual(set(result["planning"]["layouts"]), set(layouts))
        self.assertEqual(result["planning"]["inputs"]["shot_caps"], [1, 2])
        self.assertEqual(result["planning"]["inputs"]["query_caps"], [0, 1])
        result["planning"]["snapshots"]["earlier"]["backend_version"] = "changed"
        result["planning"]["versions"]["qiskit"] = "changed"
        for capture_result in result["captures"].values():
            self.assertEqual(capture_result["report_sha256"], _digest(capture_result["report"]))

    def test_rejects_completed_capture_with_wrong_layout_or_round_range(self):
        for field, value in (("initial_layout", [0, 2]), ("max_rounds", 0)):
            def capture(prepared, layout):
                return {**self.capture(prepared, layout), field: value}

            with (
                self.subTest(field=field),
                patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=capture),
                self.assertRaisesRegex(RuntimeError, "captur.*(layout|round)"),
            ):
                self.study()

    def test_uncancelled_timeout_stops_later_captures_but_preserves_completed_work(self):
        def capture(prepared, layout):
            if tuple(layout) == (0, 2):
                return self.timeout(prepared, layout, cancelled=False)
            return self.capture(prepared, layout)

        with patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=capture) as producer:
            result = self.study(initial_layouts=[(1, 2), (0, 2), (0, 1)], validation_trials=3)
        self.assertEqual(producer.call_count, 2)
        self.assertEqual(result["status"], "CALIBRATION_STUDY_PARTIAL")
        self.assertEqual(result["coverage"], {
            "requested_layouts": ["0,1", "0,2", "1,2"], "completed_layouts": ["0,1"],
            "unavailable_layouts": ["0,2"], "unattempted_layouts": ["1,2"],
        })
        self.assertEqual(list(result["captures"]), ["0,1", "0,2"])
        blocked = result["captures"]["0,2"]
        self.assertEqual(blocked["report"]["status"], _TIMEOUT)
        self.assertFalse(blocked["report"]["local_job_cancelled"])
        self.assertEqual(blocked["report_sha256"], _digest(blocked["report"]))
        self.assertEqual(set(result["planning"]["layouts"]), {"0,1"})
        self.assertEqual(result["planning"]["validation"]["trials_per_arm"], 3)

    def test_cancelled_timeouts_allow_remaining_layouts_and_report_unavailable_if_all_fail(self):
        def cancelled(prepared, layout):
            return self.timeout(prepared, layout, cancelled=True)

        with patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=cancelled) as producer:
            result = self.study(initial_layouts=[(0, 1), (0, 2)])
        self.assertEqual(producer.call_count, 2)
        self.assertEqual(result["status"], "CALIBRATION_STUDY_UNAVAILABLE")
        self.assertEqual(result["coverage"]["completed_layouts"], [])
        self.assertEqual(result["coverage"]["unavailable_layouts"], ["0,1", "0,2"])
        self.assertEqual(result["coverage"]["unattempted_layouts"], [])
        self.assertIsNone(result["planning"])

    def test_cli_dispatches_repeated_layout_flags_and_optional_replay(self):
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
        for flag, files in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(files.configuration), str(files.properties),
                         files.configuration_sha256, files.properties_sha256))
        argv.extend((
            "--initial-layout", "2", "0", "--initial-layout", "0", "1",
            "--target-probability", ".7", "--shot-caps", "2", "--query-caps", "1", "--validate-trials", "4",
        ))
        with (
            patch.object(snapshot_calibration_study, "_capture_calibration_layout", side_effect=self.capture),
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
        ):
            main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["coverage"]["completed_layouts"], ["0,1", "2,0"])
        self.assertEqual(result["planning"]["validation"]["trials_per_arm"], 4)


if __name__ == "__main__":
    unittest.main()
