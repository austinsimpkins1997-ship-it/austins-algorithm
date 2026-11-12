"""Check independently pinned study overlap receipts and physical replay."""

from base64 import b64decode, b64encode
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from austins_algorithm import snapshot_calibration_comparison as comparison
from austins_algorithm import snapshot_calibration_compilation_bundle as bundle_format
from austins_algorithm import snapshot_calibration_overlap as overlap
from austins_algorithm import snapshot_calibration_study as study
from austins_algorithm.__main__ import main
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_overlap_receipt import verify_snapshot_calibration_overlap_receipt
from test_snapshot_calibration_bundle_composition import _A, _B, _entry
from test_snapshot_calibration_bundle_overlap import _GRID, _argv
from test_snapshot_calibration_compilation_bundle import _test_directory, _write_report
from test_snapshot_calibration_compilation_cache import _MARK_00, _SDK_MODULES, _pinned
from test_snapshot_calibration_planning import _report


_VERIFIED = "SNAPSHOT_CALIBRATION_OVERLAP_RECEIPT_VERIFIED"
_REPLAYED = "COMPILATION_BUNDLE_OVERLAP_RECEIPT_REPLAYED"


def _source(directory, name, versions, entries):
    path = directory / f"{name}.json"
    return path, _write_report(path, {"schema_version": 1, "software_versions": versions, "entries": entries})


def _receipt_args(receipt, output, sources):
    return {
        "receipt": receipt[0], "expected_receipt_sha256": receipt[1],
        "output_bundle": output[0], "expected_output_sha256": output[1], "sources": sources,
    }


def _save_cli_receipt(directory, earlier, later, sources):
    output = directory / "composed.json"
    argv = _argv(directory, earlier, later, sources, output)
    with patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout:
        main()
    raw = stdout.getvalue().encode("utf-8")
    report = json.loads(raw)
    receipt = directory / "receipt.json"
    receipt.write_bytes(raw)
    return (receipt, sha256(raw).hexdigest()), (output, report["output"]["sha256"]), report


def _directory_bytes(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


class SnapshotCalibrationOverlapReceiptInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = _test_directory(self)
        self.archive = comparison.SnapshotCalibrationFiles(
            self.directory / "missing-config.json", self.directory / "missing-properties.json", "a" * 64, "b" * 64,
        )
        self.versions = {key: value or "fixture-sdk" for key, value in bundle_format._bundle_software_versions().items()}
        self.versions["qiskit"] = "fixture-qiskit"

    def zero_overlap_receipt(self):
        sources = [
            _source(self.directory, "first", self.versions, {_A: _entry("opaque source A")}),
            _source(self.directory, "second", self.versions, {_B: _entry("opaque source B")}),
        ]
        receipt, output, report = _save_cli_receipt(self.directory, self.archive, self.archive, sources)
        self.assertEqual(report["study_overlap"]["status"], "NOT_REQUIRED")
        return receipt, output, sources, report

    def synthetic_overlap_receipt(self):
        fixture = _report()
        compilation = fixture["rounds"]["0"]["compilation"]
        sources = [
            _source(self.directory, name, self.versions, {
                _A: _entry(b64encode(payload).decode("ascii"), report=compilation),
            })
            for name, payload in (("first", b"recorded QPY A"), ("second", b"recorded QPY B"))
        ]
        compiler_versions = {key: self.versions[key] for key in (*fixture["versions"], "qiskit-qasm3-import")}
        prepared = SimpleNamespace(
            scope={"seed_transpiler": 7, "snapshots": fixture["snapshots"]},
            compilation_key_scope=json.dumps({"versions": compiler_versions}).encode("utf-8"),
            layouts=((0, 1),), backends={"earlier": None, "later": None},
        )
        options = {"target_probability": .7, "shot_caps": [1], "query_caps": [0], "max_rounds_per_shot": 256}

        def recorded_equivalence(key, rows, logical, **settings):
            self.assertEqual(key, _A)
            pins = sorted(rows)
            return {
                "status": "EQUIVALENT_PHYSICAL_CIRCUITS", "key": key, "source_sha256s": pins,
                "selected_source_sha256": pins[0], "differing_fields": [], "stored_qpy_decoded": True,
                "sources": {
                    pin: {"status": "RESTORED", "qpy_sha256": sha256(b64decode(rows[pin]["qpy"])).hexdigest(),
                          "physical_signature_sha256": "1" * 64, "stored_qpy_decoded": True, "error": None}
                    for pin in pins
                },
            }

        with (
            patch.object(study, "_bundle_software_versions", return_value=self.versions),
            patch.object(study, "_prepare_study", return_value=(prepared, options, 1)),
            patch.object(study, "_logical_study_rounds", return_value=(None,)),
            patch.object(study, "_compilation_key_grid", return_value={(0, 1): (_A,)}),
            patch.object(overlap, "verify_snapshot_calibration_overlap", side_effect=recorded_equivalence),
        ):
            receipt, output, report = _save_cli_receipt(self.directory, self.archive, self.archive, sources)
        return receipt, output, sources, report

    def test_zero_overlap_cli_receipt_verifies_and_replay_does_not_prepare_the_study(self):
        receipt, output, sources, _ = self.zero_overlap_receipt()
        args = _receipt_args(receipt, output, sources[::-1])
        before = _directory_bytes(self.directory)
        argv = [
            "austins-algorithm", "snapshot-calibration-overlap-receipt-verify", str(receipt[0]),
            "--expected-receipt-sha256", receipt[1], "--output-bundle", str(output[0]), output[1],
        ]
        for path, pin in sources:
            argv.extend(("--bundle", str(path), pin))
        with (
            patch.object(study, "_prepare_study") as prepare,
            patch.object(bundle_format, "_load_physical_circuit") as decode,
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
        ):
            expected = verify_snapshot_calibration_overlap_receipt(**args)
            main()
            replay = study.replay_snapshot_calibration_overlap_receipt(
                _MARK_00, self.archive, self.archive, **args, **_GRID,
            )
        prepare.assert_not_called()
        decode.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(expected["status"], _VERIFIED)
        self.assertEqual(expected["resolved_key_count"], 0)
        self.assertFalse(expected["qpy_decoded"])
        self.assertFalse(expected["physical_replay_performed"])
        self.assertEqual(replay["status"], "NO_OVERLAPS_TO_REPLAY")
        self.assertFalse(replay["request_layouts_bound"])
        self.assertEqual(_directory_bytes(self.directory), before)

    def test_receipt_output_and_all_sources_have_independent_raw_byte_pins(self):
        receipt, output, sources, _ = self.zero_overlap_receipt()
        args = _receipt_args(receipt, output, sources)
        for path, _ in (receipt, output, *sources):
            with self.subTest(artifact=path.name):
                raw = path.read_bytes()
                try:
                    changed = re.sub(rb'("schema_version"\s*:\s*)1', rb'\g<1>2', raw, count=1)
                    self.assertNotEqual(changed, raw)
                    self.assertEqual(len(changed), len(raw))
                    path.write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, r"(?i)(sha-?256|digest|pin)"):
                        verify_snapshot_calibration_overlap_receipt(**args)
                finally:
                    path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, r"(?i)(source|bundle)"):
            verify_snapshot_calibration_overlap_receipt(**{**args, "sources": sources[:1]})
        self.assertEqual(verify_snapshot_calibration_overlap_receipt(**args)["status"], _VERIFIED)

    def test_sdk_free_verification_binds_selected_row_and_typed_proof_without_decoding(self):
        receipt, output, sources, recorded = self.synthetic_overlap_receipt()
        args = _receipt_args(receipt, output, sources)
        selected_path, selected_pin = min(sources, key=lambda source: source[1])
        self.assertEqual(recorded["overlap_verification"][_A]["selected_source_sha256"], selected_pin)
        self.assertEqual(json.loads(output[0].read_bytes())["entries"][_A],
                         json.loads(selected_path.read_bytes())["entries"][_A])
        tampered = self.directory / "tampered-receipt.json"
        changes = (
            (("overlap_verification", _A, "selected_source_sha256"), max(pin for _, pin in sources)),
            (("overlap_verification", _A, "sources", selected_pin, "qpy_sha256"), "9" * 64),
            (("overlap_verification", _A, "sources", selected_pin, "physical_signature_sha256"), "9" * 64),
            (("overlap_verification", _A, "sources", selected_pin, "stored_qpy_decoded"), 1),
            (("overlap_verification", _A, "round"), 99),
        )
        with patch.object(bundle_format, "_load_physical_circuit") as decode:
            verified = verify_snapshot_calibration_overlap_receipt(**args)
            self.assertEqual(verified["status"], _VERIFIED)
            self.assertEqual(verified["resolved_keys"], [_A])
            self.assertTrue(verified["recorded_layouts_present"])
            self.assertFalse(verified["qpy_decoded"])
            self.assertFalse(verified["physical_replay_performed"])
            for fields, value in changes:
                with self.subTest(fields=fields):
                    changed = deepcopy(recorded)
                    parent = changed
                    for field in fields[:-1]:
                        parent = parent[field]
                    parent[fields[-1]] = value
                    pin = _write_report(tampered, changed)
                    with self.assertRaises(ValueError):
                        verify_snapshot_calibration_overlap_receipt(
                            **{**args, "receipt": tampered, "expected_receipt_sha256": pin},
                        )
        decode.assert_not_called()

    def test_repinning_receipt_and_output_cannot_replace_the_selected_source_row(self):
        _, _, sources, report = self.synthetic_overlap_receipt()
        other_path, _ = max(sources, key=lambda source: source[1])
        other = self.directory / "wrong-output.json"
        other.write_bytes(other_path.read_bytes())
        other_pin = sha256(other.read_bytes()).hexdigest()
        report["output"] = {"sha256": other_pin, "bytes": other.stat().st_size, "entry_count": 1}
        repinned = self.directory / "repinned-receipt.json"
        pin = _write_report(repinned, report)
        with self.assertRaisesRegex(ValueError, r"(?i)(row|selected|output|deterministic)"):
            verify_snapshot_calibration_overlap_receipt(**_receipt_args((repinned, pin), (other, other_pin), sources))


@unittest.skipUnless(all(find_spec(name) is not None for name in _SDK_MODULES), "optional IBM tools unavailable")
class SnapshotCalibrationOverlapReceiptReplayTests(unittest.TestCase):
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
        capture = comparison.compare_snapshot_calibrations(
            _MARK_00, self.earlier, self.later, initial_layout=(0, 1), max_rounds=0, compilation_cache=cache,
        )
        self.assertEqual(capture["status"], "COMPLETED")
        first = self.directory / "first.json"
        pin = cache.save_bundle(first)
        document = json.loads(first.read_bytes())
        [self.key] = document["entries"]
        row = document["entries"][self.key]
        [circuit] = qpy.load(BytesIO(b64decode(row["qpy"])))
        circuit.metadata = {"different_QPY": "same physical program"}
        encoded = BytesIO()
        qpy.dump(circuit, encoded)
        row["qpy"] = b64encode(encoded.getvalue()).decode("ascii")
        second = self.directory / "second.json"
        self.sources = [(first, pin), (second, _write_report(second, document))]
        self.receipt, self.output, self.recorded = _save_cli_receipt(
            self.directory, self.earlier, self.later, self.sources,
        )

    def replay(self, *, receipt=None, earlier=None, **overrides):
        return study.replay_snapshot_calibration_overlap_receipt(
            _MARK_00, self.earlier if earlier is None else earlier, self.later,
            **_receipt_args(self.receipt if receipt is None else receipt, self.output, self.sources),
            **{**_GRID, **overrides},
        )

    def replay_argv(self):
        argv = _argv(self.directory, self.earlier, self.later, [])
        argv.extend((
            "--replay-overlap-receipt", str(self.receipt[0]), self.receipt[1],
            "--load-compilation-bundle", str(self.output[0]), self.output[1],
        ))
        for path, pin in self.sources:
            argv.extend(("--overlap-source-bundle", str(path), pin))
        return argv

    def test_sdk_replay_and_cli_reproduce_physical_evidence_without_compile_simulate_or_write(self):
        argv = self.replay_argv()
        before = _directory_bytes(self.directory)
        with (
            patch.object(overlap, "_restore_compiled_round", wraps=overlap._restore_compiled_round) as restore,
            patch.object(comparison, "compile_and_verify") as compile_round,
            patch.object(comparison, "_prepare_density_snapshot") as density,
            patch.object(bundle_format, "_publish_bundle") as publish,
            patch.object(sys, "argv", argv), redirect_stdout(StringIO()) as stdout,
        ):
            verified = verify_snapshot_calibration_overlap_receipt(
                **_receipt_args(self.receipt, self.output, self.sources),
            )
            restore.assert_not_called()
            expected = self.replay()
            main()
        self.assertEqual(verified["status"], _VERIFIED)
        self.assertEqual(expected["status"], _REPLAYED)
        self.assertEqual(expected["verified_conflict_count"], 1)
        self.assertTrue(expected["request_layouts_bound"])
        self.assertEqual(expected["replayed_overlaps"], self.recorded["overlap_verification"])
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(restore.call_count, 4)
        for mock in (compile_round, density, publish):
            mock.assert_not_called()
        self.assertEqual(_directory_bytes(self.directory), before)

    def test_new_receipts_bind_all_requested_layouts_and_legacy_receipts_still_replay(self):
        self.assertEqual(self.recorded["study_overlap"]["request"]["initial_layouts"], [[0, 1]])
        repinned_earlier = self.directory / "repinned-earlier.json"
        repinned_earlier.write_bytes(Path(self.earlier.properties).read_bytes() + b"\n")
        for changes in (
            {"initial_layouts": [(1, 0), (0, 1)]}, {"seed_transpiler": 29},
            {"shot_caps": [2], "query_caps": [1]},
            {"earlier": _pinned(Path(self.earlier.configuration), repinned_earlier)},
        ):
            with (
                self.subTest(changes=changes), patch.object(overlap, "_restore_compiled_round") as restore,
                self.assertRaisesRegex(ValueError, r"(?i)(request|scope|layout)"),
            ):
                self.replay(**changes)
            restore.assert_not_called()
        legacy = deepcopy(self.recorded)
        del legacy["study_overlap"]["request"]["initial_layouts"]
        path = self.directory / "legacy.json"
        pin = _write_report(path, legacy)
        replayed = self.replay(receipt=(path, pin), initial_layouts=[(1, 0), (0, 1)])
        self.assertEqual(replayed["status"], _REPLAYED)
        self.assertFalse(replayed["request_layouts_bound"])
        self.assertEqual(replayed["replayed_overlaps"], self.recorded["overlap_verification"])

    def test_uniformly_resealed_physical_signature_requires_sdk_replay_to_disprove(self):
        changed = deepcopy(self.recorded)
        for row in changed["overlap_verification"][self.key]["sources"].values():
            row["physical_signature_sha256"] = "9" * 64
        path = self.directory / "false-physical-proof.json"
        pin = _write_report(path, changed)
        receipt = (path, pin)
        self.assertEqual(verify_snapshot_calibration_overlap_receipt(
            **_receipt_args(receipt, self.output, self.sources),
        )["status"], _VERIFIED)
        before = _directory_bytes(self.directory)
        with self.assertRaisesRegex(ValueError, r"(?i)(physical|evidence|overlap)"):
            self.replay(receipt=receipt)
        self.assertEqual(_directory_bytes(self.directory), before)


if __name__ == "__main__":
    unittest.main()
