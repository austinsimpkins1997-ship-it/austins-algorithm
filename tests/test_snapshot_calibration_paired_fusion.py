"""Exercise paired dated studies while keeping the reference plan authoritative."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from gc import collect
from importlib.util import find_spec
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
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.__main__ import main
from austins_algorithm.compilation import CompiledProgram
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_fusion import compare_snapshot_calibration_fusion_budgets
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_compilation_bundle import _test_directory
from test_snapshot_calibration_compilation_cache import _SDK_MODULES, _pinned
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT
import test_snapshot_calibration_study as fixtures


_LAYOUTS = [(0, 1), (0, 2), (1, 2)]
_KEYS = ["0,1", "0,2", "1,2"]
_BUDGET = {"target_probability": .7, "shot_caps": [2], "query_caps": [1], "validation_trials": 3}
_REFERENCE_COMPLETED = "CALIBRATION_STUDY_COMPLETED"
_CHECKED = "CALIBRATION_FUSION_STUDY_CHECKED"
_PARTIAL = "CALIBRATION_FUSION_STUDY_PARTIAL"
_UNAVAILABLE = "CALIBRATION_FUSION_STUDY_UNAVAILABLE"


class _LifetimeResource:
    pass


class SnapshotCalibrationPairedFusionInputTests(unittest.TestCase):
    setUp = fixtures.SnapshotCalibrationStudyTests.setUp
    capture = fixtures.SnapshotCalibrationStudyTests.capture
    timeout = fixtures.SnapshotCalibrationStudyTests.timeout

    def run_study(self, **options):
        return study.run_snapshot_calibration_study(
            _CONSTRAINT, self.earlier, self.later,
            **{"initial_layouts": _LAYOUTS[::-1], **_BUDGET, **options},
        )

    def mock_captures(self, *, reference_timeout=None, candidate_failure=None):
        events = []

        def reference(prepared, layout, **_options):
            events.append(("reference", layout))
            if reference_timeout == layout:
                return self.timeout(prepared, layout, cancelled=False), None
            return self.capture(prepared, layout), SimpleNamespace(layout=layout)

        def fusion(prepared, pending):
            layout = pending.layout
            events.append(("fusion", layout))
            if candidate_failure is not None and layout == candidate_failure[0]:
                kind = candidate_failure[1]
                if kind == "exception":
                    raise RuntimeError("candidate preparation failed")
                return _marked(self.timeout(prepared, layout, cancelled=kind == "confirmed"))
            return _marked(self.capture(prepared, layout))

        self.fixture_contexts.enter_context(patch.object(
            study, "_capture_calibration_layout_reference", side_effect=reference,
        ))
        self.fixture_contexts.enter_context(patch.object(
            study, "_capture_calibration_layout_fusion", side_effect=fusion,
        ))
        return events

    def assert_reference_complete(self, result):
        self.assertEqual(result["status"], _REFERENCE_COMPLETED)
        self.assertEqual(result["coverage"]["completed_layouts"], _KEYS)
        self.assertEqual(list(result["planning"]["layouts"]), _KEYS)
        for key, capture in result["captures"].items():
            self.assertEqual(capture["report_sha256"], _digest(capture["report"]))
            self.assertEqual(result["planning"]["layouts"][key]["source_capture_sha256"], capture["report_sha256"])

    def test_paired_study_retains_exact_ordinary_study_and_independent_diagnostic(self):
        events = self.mock_captures()
        with patch.object(study, "_capture_calibration_layout", side_effect=self.capture):
            reference = self.run_study()
        self.assertNotIn("fusion", reference)
        self.assertEqual(events, [])

        result = self.run_study(fusion_probability_tolerance=0.0)
        self.assertEqual(events, [(stage, layout) for stage in ("reference", "fusion") for layout in _LAYOUTS])
        self.assertEqual(json.dumps({key: value for key, value in result.items() if key != "fusion"}),
                         json.dumps(reference))
        fusion = result["fusion"]
        self.assertEqual(fusion["status"], _CHECKED)
        self.assertEqual(fusion["coverage"], {
            "requested_layouts": _KEYS, "completed_layouts": _KEYS, "unavailable_layouts": [],
            "unattempted_layouts": [],
        })
        self.assertFalse(fusion["failures"])
        left = [row["report"] for row in result["captures"].values()]
        right = [row["report"] for row in fusion["captures"].values()]
        expected = compare_snapshot_calibration_fusion_budgets(
            left, right, _CONSTRAINT, probability_tolerance=0.0, **_BUDGET,
        )
        self.assertEqual(fusion["check"], expected["fusion_check"])
        self.assertEqual(fusion["check"]["status"], "MATCHED")
        for key, capture in fusion["captures"].items():
            self.assertEqual(capture["report_sha256"], _digest(capture["report"]))
            self.assertEqual(fusion["check"]["layouts"][key]["fused_report_sha256"], capture["report_sha256"])

    def test_reference_uncancelled_timeout_stops_all_candidates_and_later_references(self):
        events = self.mock_captures(reference_timeout=_LAYOUTS[1])
        result = self.run_study(fusion_probability_tolerance=1e-12)
        self.assertEqual(events, [("reference", layout) for layout in _LAYOUTS[:2]])
        self.assertEqual(result["status"], "CALIBRATION_STUDY_PARTIAL")
        self.assertEqual(result["coverage"]["completed_layouts"], _KEYS[:1])
        self.assertEqual(result["coverage"]["unattempted_layouts"], _KEYS[2:])
        self.assertEqual(list(result["planning"]["layouts"]), _KEYS[:1])
        fusion = result["fusion"]
        self.assertEqual(fusion["status"], _UNAVAILABLE)
        self.assertEqual(fusion["coverage"]["completed_layouts"], [])
        self.assertEqual(fusion["coverage"]["unattempted_layouts"], _KEYS)
        self.assertEqual(fusion["captures"], {})
        self.assertIsNone(fusion["check"])

    def test_uncancelled_candidate_timeout_stops_later_work_and_keeps_reference_plan(self):
        events = self.mock_captures(candidate_failure=(_LAYOUTS[1], "unconfirmed"))
        result = self.run_study(fusion_probability_tolerance=1e-12)
        self.assertEqual(events, [
            *[("reference", layout) for layout in _LAYOUTS], *[("fusion", layout) for layout in _LAYOUTS[:2]],
        ])
        self.assert_reference_complete(result)
        fusion = result["fusion"]
        self.assertEqual(fusion["status"], _PARTIAL)
        self.assertEqual(fusion["coverage"], {
            "requested_layouts": _KEYS, "completed_layouts": _KEYS[:1], "unavailable_layouts": _KEYS[1:2],
            "unattempted_layouts": _KEYS[2:],
        })
        blocked = fusion["captures"][_KEYS[1]]
        self.assertEqual(blocked["report"]["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
        self.assertFalse(blocked["report"]["local_job_cancelled"])
        self.assertEqual(blocked["report_sha256"], _digest(blocked["report"]))
        self.assertIsNone(fusion["check"])

    def test_candidate_exception_preserves_reference_and_stops_later_candidates(self):
        events = self.mock_captures(candidate_failure=(_LAYOUTS[0], "exception"))
        result = self.run_study(fusion_probability_tolerance=1e-12)
        self.assertEqual(events, [*[("reference", layout) for layout in _LAYOUTS], ("fusion", _LAYOUTS[0])])
        self.assert_reference_complete(result)
        fusion = result["fusion"]
        self.assertEqual(fusion["status"], _UNAVAILABLE)
        self.assertEqual(fusion["coverage"]["unavailable_layouts"], _KEYS[:1])
        self.assertEqual(fusion["coverage"]["unattempted_layouts"], _KEYS[1:])
        self.assertEqual(fusion["failures"], {
            _KEYS[0]: {"error_type": "RuntimeError", "message": "candidate preparation failed"},
        })
        self.assertIsNone(fusion["check"])

    def test_confirmed_candidate_cancellation_allows_later_candidate(self):
        events = self.mock_captures(candidate_failure=(_LAYOUTS[1], "confirmed"))
        result = self.run_study(fusion_probability_tolerance=1e-12)
        self.assertEqual(events, [(stage, layout) for stage in ("reference", "fusion") for layout in _LAYOUTS])
        self.assert_reference_complete(result)
        fusion = result["fusion"]
        self.assertEqual(fusion["status"], _PARTIAL)
        self.assertEqual(fusion["coverage"]["completed_layouts"], [_KEYS[0], _KEYS[2]])
        self.assertEqual(fusion["coverage"]["unavailable_layouts"], _KEYS[1:2])
        self.assertEqual(fusion["coverage"]["unattempted_layouts"], [])
        self.assertIsNone(fusion["check"])

    def test_invalid_tolerances_do_not_start_preparation(self):
        with patch.object(study, "_prepare_study") as prepare:
            for tolerance in (-1, 2, True, float("nan"), float("inf")):
                with self.subTest(tolerance=tolerance), self.assertRaisesRegex(ValueError, "probability_tolerance"):
                    self.run_study(fusion_probability_tolerance=tolerance)
        prepare.assert_not_called()

    def test_reference_timeout_does_not_publish_rounds_or_prepare_fusion(self):
        cache = SnapshotCalibrationCompilationCache()
        prepared = SimpleNamespace(compilation_cache=cache)
        report = {"status": "PENDING"}
        compiled = {"round": object()}

        def timeout(_prepared, capture, _programs, **_options):
            capture.update(status="LOCAL_RESULT_WAIT_TIMED_OUT", local_job_cancelled=False)

        with (
            patch.object(comparison, "_compile_calibration_layout", return_value=(report, {}, compiled)),
            patch.object(comparison, "_simulate_calibration_layout", side_effect=timeout),
            patch.object(cache, "_publish_completed") as publish,
        ):
            captured, pending = comparison._capture_calibration_layout_reference(prepared, _LAYOUTS[0])
        self.assertIsNone(pending)
        self.assertEqual(captured["status"], "LOCAL_RESULT_WAIT_TIMED_OUT")
        publish.assert_not_called()
        self.assertEqual(cache.stats()["entries"], 0)

    def test_reference_releases_full_compilation_and_fusion_consumes_compact_setup(self):
        prepared = SimpleNamespace(compilation_cache=None, backends={"earlier": None, "later": None})
        compile_refs, compact_refs = [], []

        def compile_layout(*_args, **_options):
            circuit = _LifetimeResource()
            program = CompiledProgram(circuit, "OPENQASM 3.0;", {})
            compile_refs.extend((ref(program), ref(circuit)))
            return {"status": "PENDING"}, {0: program}, {"round": program}

        def simulate(_prepared, report, programs, *, compact_by_date):
            if "density_fusion" in report:
                self.assertIsNone(programs)
                self.assertTrue(all(probe() is not None for probe in compact_refs))
            else:
                self.assertIs(programs[0], compile_refs[0]())
                for label in prepared.backends:
                    circuit, simulator = _LifetimeResource(), _LifetimeResource()
                    compact_by_date[label] = {0: (circuit, simulator)}
                    compact_refs.extend((ref(circuit), ref(simulator)))
            report["status"] = "COMPLETED"

        # Record only weak references so test instrumentation cannot keep the resources alive.
        with (
            patch.object(comparison, "_compile_calibration_layout", new=compile_layout),
            patch.object(comparison, "_simulate_calibration_layout", new=simulate),
        ):
            reference, pending = comparison._capture_calibration_layout_reference(prepared, _LAYOUTS[0])
            self.assertEqual(reference["status"], "COMPLETED")
            self.assertIsNotNone(pending)
            collect()
            self.assertEqual(len(compile_refs), 2)
            self.assertTrue(all(probe() is None for probe in compile_refs))
            self.assertEqual(len(compact_refs), 4)
            self.assertTrue(all(probe() is not None for probe in compact_refs))
            candidate = comparison._capture_calibration_layout_fusion(prepared, pending)
        self.assertEqual(candidate["status"], "COMPLETED")
        collect()
        self.assertTrue(all(probe() is None for probe in compact_refs))

    def test_compact_setup_is_released_before_next_candidate_and_reference_planning(self):
        compare = study.compare_snapshot_calibration_fusion_budgets
        plan = study.plan_snapshot_calibration_budgets
        cases = (
            ("complete", _LAYOUTS, "comparison"),
            ("confirmed candidate timeout", _LAYOUTS, "reference"),
            ("unconfirmed candidate timeout", _LAYOUTS[:2], "reference"),
            ("candidate exception", _LAYOUTS[:2], "reference"),
            ("unconfirmed reference timeout", [], "reference"),
        )
        for outcome, expected_candidates, expected_planner in cases:
            with self.subTest(outcome=outcome):
                setup_refs, attempted, planning = {}, [], []
                failure = RuntimeError("candidate preparation failed")

                def reference(prepared, layout, **_options):
                    if outcome == "unconfirmed reference timeout" and layout == _LAYOUTS[1]:
                        return self.timeout(prepared, layout, cancelled=False), None
                    setup = _LifetimeResource()
                    setup_refs[layout] = ref(setup)
                    return self.capture(prepared, layout), SimpleNamespace(layout=layout, setup=setup)

                def candidate(prepared, pending):
                    collect()
                    self.assertTrue(all(setup_refs[layout]() is None for layout in attempted))
                    layout = pending.layout
                    self.assertIs(setup_refs[layout](), pending.setup)
                    attempted.append(layout)
                    if layout == _LAYOUTS[1]:
                        if outcome == "candidate exception":
                            raise failure
                        if outcome.endswith("candidate timeout"):
                            return _marked(self.timeout(prepared, layout, cancelled=outcome.startswith("confirmed")))
                    return _marked(self.capture(prepared, layout))

                def check_planning(function, name):
                    def checked(*args, **kwargs):
                        collect()
                        self.assertTrue(all(probe() is None for probe in setup_refs.values()))
                        planning.append(name)
                        return function(*args, **kwargs)
                    return checked

                with (
                    patch.object(study, "_capture_calibration_layout_reference", new=reference),
                    patch.object(study, "_capture_calibration_layout_fusion", new=candidate),
                    patch.object(study, "compare_snapshot_calibration_fusion_budgets",
                                 new=check_planning(compare, "comparison")),
                    patch.object(study, "plan_snapshot_calibration_budgets",
                                 new=check_planning(plan, "reference")),
                ):
                    self.run_study(fusion_probability_tolerance=1e-12)
                self.assertEqual(attempted, expected_candidates)
                self.assertEqual(planning, [expected_planner])

    def cli_argv(self):
        constraint = self.directory / "constraint.json"
        constraint.write_text('{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}', encoding="utf-8")
        argv = ["austins-algorithm", "snapshot-calibration-study", str(constraint)]
        for flag, archive in (("--earlier", self.earlier), ("--later", self.later)):
            argv.extend((flag, str(archive.configuration), str(archive.properties),
                         archive.configuration_sha256, archive.properties_sha256))
        return [*argv, "--initial-layout", "0", "1", "--target-probability", ".7",
                "--shot-caps", "2", "--query-caps", "1", "--fusion-probability-tolerance", "0"]

    def test_cli_dispatches_zero_tolerance_to_executing_studies(self):
        args = self.cli_argv()
        receipt = [
            "--replay-overlap-receipt", "receipt.json", "a" * 64,
            "--load-compilation-bundle", "output.json", "b" * 64,
            "--overlap-source-bundle", "first.json", "c" * 64,
            "--overlap-source-bundle", "second.json", "d" * 64, "--run-after-overlap-replay",
        ]
        for route, extra in (("run_snapshot_calibration_study", []),
                             ("run_snapshot_calibration_study_with_overlap_receipt", receipt)):
            with (
                self.subTest(route=route),
                patch.object(sys, "argv", [*args, *extra]),
                patch.object(study, route, return_value={"status": "mocked"}) as execute,
                redirect_stdout(StringIO()),
            ):
                main()
            execute.assert_called_once()
            self.assertEqual(execute.call_args.kwargs["fusion_probability_tolerance"], 0.0)

    def test_cli_rejects_fusion_option_on_nonexecuting_paths(self):
        args = self.cli_argv()
        modes = (
            ["--load-compilation-bundle", "unused.json", "a" * 64, "--check-compilation-bundle"],
            ["--replay-overlap-receipt", "unused.json", "a" * 64,
             "--load-compilation-bundle", "output.json", "b" * 64,
             "--overlap-source-bundle", "source.json", "c" * 64],
        )
        with patch.object(SnapshotCalibrationCompilationCache, "load_bundle") as load:
            for mode in modes:
                with (
                    self.subTest(mode=mode), patch.object(sys, "argv", [*args, *mode]),
                    redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit),
                ):
                    main()
                self.assertIn("--fusion-probability-tolerance", stderr.getvalue())
        load.assert_not_called()


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationPairedFusionRuntimeTests(unittest.TestCase):
    def test_one_compile_and_compact_pass_per_round_and_date_with_detached_reports(self):
        from qiskit_ibm_runtime.fake_provider import FakeJakartaV2

        directory = _test_directory(self)
        source = Path(FakeJakartaV2.dirname)
        configuration = source / FakeJakartaV2.conf_filename
        properties = source / FakeJakartaV2.props_filename
        later = _pinned(configuration, properties)
        earlier_properties = json.loads(properties.read_bytes())
        earlier_properties["last_update_date"] = "2021-07-26T11:47:01-04:00"
        earlier_path = directory / "earlier.json"
        earlier_path.write_text(json.dumps(earlier_properties), encoding="utf-8")
        earlier = _pinned(configuration, earlier_path)
        cache = SnapshotCalibrationCompilationCache()
        options = {
            "initial_layouts": [(0, 1)], "prefer_native_qasm": True, "batch_density": True,
            "compilation_cache": cache, "fusion_probability_tolerance": 1e-12, **_BUDGET,
        }
        with (
            patch.object(comparison, "compile_and_verify", wraps=comparison.compile_and_verify) as compile_round,
            patch.object(comparison, "_round_profile_key", wraps=comparison._round_profile_key) as key_round,
            patch.object(comparison, "_compact_snapshot_inputs", wraps=comparison._compact_snapshot_inputs) as compact,
            patch.object(comparison, "_prepare_density_snapshot_batch",
                         wraps=comparison._prepare_density_snapshot_batch) as prepare,
        ):
            result = study.run_snapshot_calibration_study(_CONSTRAINT, earlier, later, **options)
            self.assertEqual((compile_round.call_count, key_round.call_count, compact.call_count), (2, 2, 2))
            self.assertEqual([len(call.args[0]) for call in prepare.call_args_list], [2, 2, 2, 2])
            self.assertEqual([call.kwargs.get("density_fusion", False) for call in prepare.call_args_list],
                             [False, False, True, True])
            self.assertEqual(cache.stats()["entries"], 2)
            self.assertEqual(cache.stats()["misses"], 2)
            self.assertEqual(cache.stats()["hits"], 0)
            self.assertEqual(result["status"], _REFERENCE_COMPLETED)
            self.assertEqual(result["fusion"]["status"], _CHECKED)
            snapshot = deepcopy(result)
            reference = result["captures"]["0,1"]
            fused = result["fusion"]["captures"]["0,1"]
            fused["report"]["rounds"]["0"]["compilation"]["compiled"]["depth"] += 1
            self.assertEqual(reference["report_sha256"], _digest(reference["report"]))

            compile_round.reset_mock()
            key_round.reset_mock()
            compact.reset_mock()
            reused = study.run_snapshot_calibration_study(_CONSTRAINT, earlier, later, **options)
            compile_round.assert_not_called()
            self.assertEqual((key_round.call_count, compact.call_count), (2, 2))
            self.assertEqual(cache.stats()["hits"], 2)
            self.assertEqual(reused, snapshot)
