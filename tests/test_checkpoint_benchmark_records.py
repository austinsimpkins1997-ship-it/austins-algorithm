"""Check bounded local record evidence without SDKs or external recorded paths."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import checkpoint_benchmark_records as records


def _save(path, document, *, indent=None):
    raw = json.dumps(document, indent=indent, allow_nan=False).encode("utf-8")
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def _metrics(role="baseline"):
    source = {"path": "Z:/unavailable/private/source.py", "bytes": 4, "sha256": "a" * 64}
    archive = {"configuration": "Z:/unavailable/conf.json", "properties": "Z:/unavailable/props.json",
               "configuration_sha256": "b" * 64, "properties_sha256": "c" * 64}
    memory = {"working_set_bytes": 100, "peak_working_set_bytes": 120, "private_usage_bytes": 200, "reason": None}
    return {
        "schema_version": 1, "role": role, "seconds": .25,
        "counters": dict.fromkeys(("compiles", "restores", "reference_preparations", "fusion_preparations"), 0),
        "density_preparation_limit": 8, "memory": {"before": deepcopy(memory), "after": deepcopy(memory)},
        "memory_scope": "Lifetime peak working set; endpoint private usage", "versions": {"python": "3.10", "qiskit": "recorded"},
        "inputs": {"scenario_sha256": "d" * 64, "constraint": ["Z:/unavailable/constraint.json", "e" * 64],
                   "earlier": archive, "later": deepcopy(archive), "source_files": {"module": source}},
        "error": None, "provider_job_submitted": False, "quantum_hardware_used": False,
    }


class CheckpointBenchmarkRecordsTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="benchmark-records-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "01-baseline"
        self.stage.mkdir()
        self.metrics = _metrics()
        self.result = {"status": "CALIBRATION_STUDY_COMPLETED", "study": None, "study_output": None}
        _save(self.root / "scenario.json", {"recorded_path": "Z:/unavailable/scenario.json"})
        _save(self.stage / "metrics.json", self.metrics)
        _save(self.stage / "result.json", self.result)
        (self.stage / "stdout.log").write_bytes(b"output\n")
        (self.stage / "stderr.log").write_bytes(b"")
        self.summary = {"schema_version": 1, "stages": [{
            "role": "baseline", "directory": "01-baseline", "status": "COMPLETED", "returncode": 0,
            "result": self.result, "metrics": self.metrics, "cancellation_status": None, "wall_seconds": .5,
        }]}

    def read(self):
        pin = _save(self.root / "summary.json", self.summary)
        return records._read_checkpoint_benchmark_records(self.root, expected_summary_sha256=pin)

    def test_inventory_and_semantic_binding_never_open_recorded_external_paths(self):
        pin = _save(self.root / "summary.json", self.summary)
        original = Path.open
        opened = []

        def local_only(path, *args, **kwargs):
            self.assertTrue(path.absolute().is_relative_to(self.root.absolute()), path)
            opened.append(path.name)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", local_only):
            loaded = records._read_checkpoint_benchmark_records(self.root, expected_summary_sha256=pin)
        self.assertIn("scenario.json", opened)
        self.assertEqual(loaded.storage["files"], 5)
        self.assertEqual(loaded.storage["bytes"], sum(row["bytes"] for row in loaded.storage["fingerprints"].values()))
        self.assertEqual(loaded.storage["excludes"], ["summary.json"])
        self.assertEqual(loaded.stages[0].content_binding, "PARSED_CONTENT_MATCHES_SUMMARY")
        self.assertEqual(loaded.stages[0].study, None)

    def test_raw_summary_pin_is_checked_before_parsing(self):
        (self.root / "summary.json").write_bytes(b"not JSON")
        with self.assertRaisesRegex(ValueError, "expected SHA-256"):
            records._read_checkpoint_benchmark_records(self.root, expected_summary_sha256="0" * 64)

    def test_strict_json_rejects_duplicates_nonfinite_and_nonobject(self):
        path = self.root / "strict.json"
        for raw in (b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}', b'[]'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    records._read_json(path, sha256(raw).hexdigest())

    def test_stage_raw_bytes_are_current_evidence_not_an_invented_historical_pin(self):
        loaded = self.read()
        before = loaded.stages[0].fingerprints["metrics.json"]
        _save(self.stage / "metrics.json", self.metrics, indent=2)
        loaded = self.read()
        self.assertNotEqual(loaded.stages[0].fingerprints["metrics.json"], before)
        self.assertEqual(loaded.stages[0].metrics, self.metrics)
        changed = deepcopy(self.metrics)
        changed["counters"]["compiles"] = 1
        _save(self.stage / "metrics.json", changed)
        with self.assertRaisesRegex(ValueError, "parsed content differs"):
            self.read()

    def test_missing_outputs_are_observations_for_parent_failure_validation(self):
        for path in self.stage.iterdir():
            path.unlink()
        self.summary["stages"][0].update(status="TIMED_OUT", returncode=None, result=None, metrics=None,
                                         cancellation_status="UNKNOWN")
        loaded = self.read()
        self.assertEqual(set(loaded.stages[0].observations.values()), {"MISSING"})
        self.assertEqual(loaded.stages[0].content_binding, "NOT_OBSERVED")
        (self.stage / "stdout.log").write_bytes(b"")
        (self.stage / "stderr.log").write_bytes(b"")
        self.summary["stages"][0].update(status="COMPLETED", returncode=0, cancellation_status=None)
        loaded = self.read()
        self.assertIsNone(loaded.stages[0].result)
        self.assertIsNone(loaded.stages[0].metrics)
        (self.stage / "stdout.log").unlink()
        with self.assertRaisesRegex(ValueError, "missing stdout.log"):
            self.read()

    def test_scalars_and_memory_missingness_are_validated_without_boolean_integer_coercion(self):
        changes = (
            ("counter", lambda m: m["counters"].update(compiles=True)),
            ("seconds", lambda m: m.update(seconds=float("inf"))),
            ("memory", lambda m: m["memory"]["before"].update(working_set_bytes=None)),
            ("reason", lambda m: m["memory"]["before"].update(reason="unavailable")),
            ("peak", lambda m: m["memory"]["before"].update(peak_working_set_bytes=50)),
        )
        for name, change in changes:
            with self.subTest(name=name):
                metric = _metrics()
                change(metric)
                with self.assertRaises(ValueError):
                    records._verify_metrics(metric, "baseline")
        unavailable = _metrics()
        unavailable["memory"] = {phase: {**dict.fromkeys(records._MEMORY), "reason": "unsupported platform"}
                                 for phase in ("before", "after")}
        records._verify_metrics(unavailable, "baseline")
        unavailable["processor"] = None
        records._verify_metrics(unavailable, "baseline")

    def test_preworker_failure_and_spawn_error_records_remain_inspectable(self):
        self.summary["stages"] = []
        self.assertEqual(self.read().stages, ())
        self.summary["stages"] = [{
            "role": "baseline", "directory": "01-baseline", "status": "FAILED", "returncode": None,
            "result": self.result, "metrics": self.metrics, "cancellation_status": None, "wall_seconds": .5,
            "error": {"error_type": "OSError", "message": "process could not start"},
        }]
        self.assertEqual(self.read().stages[0].record["error"]["error_type"], "OSError")

    def test_recorded_identity_drift_is_exposed_for_parent_state_machine(self):
        second = self.root / "02-fusion"
        second.mkdir()
        metric = _metrics("fusion")
        metric["versions"]["qiskit"] = "changed"
        record = deepcopy(self.summary["stages"][0])
        record.update(role="fusion", directory=second.name, metrics=metric)
        self.summary["stages"].append(record)
        _save(second / "metrics.json", metric)
        _save(second / "result.json", self.result)
        (second / "stdout.log").write_bytes(b"")
        (second / "stderr.log").write_bytes(b"")
        loaded = self.read()
        self.assertNotEqual(records._metrics_identity(loaded.stages[0].metrics),
                            records._metrics_identity(loaded.stages[1].metrics))
        metric["versions"]["qiskit"] = "recorded"
        metric["inputs"]["source_files"]["module"]["path"] = "Y:/different/recorded/path.py"
        record["metrics"] = metric
        _save(second / "metrics.json", metric)
        loaded = self.read()
        self.assertEqual(records._metrics_identity(loaded.stages[0].metrics),
                         records._metrics_identity(loaded.stages[1].metrics))

    def test_canonical_paths_and_reparse_ancestors_are_rejected(self):
        self.summary["stages"][0]["directory"] = "../01-baseline"
        with self.assertRaisesRegex(ValueError, "canonical relative name"):
            self.read()
        self.summary["stages"][0]["directory"] = "01-baseline"
        pin = _save(self.root / "summary.json", self.summary)
        original = Path.lstat

        def reparse(path, *args, **kwargs):
            if path == self.stage:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "regular checkpoint directory"):
                records._read_checkpoint_benchmark_records(self.root, expected_summary_sha256=pin)

    def test_inventory_bounds_and_unknown_files_are_not_silently_omitted(self):
        (self.root / "extra.bin").write_bytes(b"extra")
        loaded = self.read()
        self.assertIn("extra.bin", loaded.storage["fingerprints"])
        with patch.object(records, "_MAX_FILES", 1):
            with self.assertRaisesRegex(ValueError, "count exceeds"):
                self.read()
        with patch.object(records, "_MAX_TOTAL_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "total bytes"):
                self.read()


if __name__ == "__main__":
    unittest.main()
