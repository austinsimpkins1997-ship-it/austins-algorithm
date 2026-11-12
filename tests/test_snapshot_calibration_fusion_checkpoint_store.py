"""Check immutable fusion attempt lineage and publication with no simulator."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
sys.path.insert(0, str(PACKAGE / "tests"))

from austins_algorithm import snapshot_calibration_fusion_checkpoint_store as store
from austins_algorithm import snapshot_calibration_incremental_store as references
from austins_algorithm.snapshot_calibration_compilation_bundle import (
    _bundle_bytes, _bundle_software_versions, _publish_bundle,
)
from austins_algorithm.unknown_count_snapshot_profile import _digest
from test_snapshot_calibration_fusion import _marked
from test_snapshot_calibration_planning import _CONSTRAINT, _report


_LAYOUTS = [(0, 1), (1, 0)]


def _seal(report):
    return {"report_sha256": _digest(report), "report": report}


class _Cache:
    def __init__(self, key):
        self.raw = _bundle_bytes(_bundle_software_versions(), {
            key: {"qpy": "QQ==", "global_phase_hex": "0x0.0p+0", "report": {}},
        })

    def save_bundle(self, path):
        return _publish_bundle(path, self.raw)


class FusionCheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="fusion-checkpoint-store-", dir=build)
        self.assertTrue(Path(temporary.name).resolve().is_relative_to(build.resolve()))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "ledger"
        self.request = {
            "initial_layouts": [list(layout) for layout in _LAYOUTS], "required_rounds": [0, 1],
            "inputs": {"constraint_sha256": _CONSTRAINT.digest(), "search_qubits": 2, "seed_transpiler": 7},
        }
        self.captures = [_seal(_report(layout)) for layout in _LAYOUTS]
        self.reference_pins = []
        prior = None
        for index, (layout, capture) in enumerate(zip(_LAYOUTS, self.captures), start=1):
            key = str(index) * 64
            published = references._publish_incremental_calibration_generation(
                self.root, request=self.request, layout=layout, capture=capture, cache=_Cache(key),
                ordinal=index, previous_generation_sha256=prior, expected_keys=frozenset({key}),
            )
            prior = published["generation_sha256"]
            self.reference_pins.append(prior)
        self.tip = prior

    def publish(self, ordinal=1, previous=None, **options):
        capture = _seal(_marked(deepcopy(self.captures[ordinal - 1]["report"])))
        arguments = {
            "reference_generation_sha256": self.tip, "previous_generation_sha256": previous,
            "ordinal": ordinal, "layout": _LAYOUTS[ordinal - 1], "capture": capture,
        }
        arguments.update(options)
        return store._publish_calibration_fusion_generation(self.root, **arguments)

    def timeout(self, *, cancelled):
        report = _marked(deepcopy(self.captures[0]["report"]))
        for row in report["rounds"].values():
            for field in set(row) - {"compilation", "target_calibrations"}:
                del row[field]
        report.update(status="LOCAL_RESULT_WAIT_TIMED_OUT", maximum_absolute_hit_probability_difference=None,
                      blocked_snapshot="earlier", blocked_round=0, local_job_cancelled=cancelled)
        report["density_fusion"]["observed"] = {"earlier": {}, "later": {}}
        return _seal(report)

    def test_reads_explicit_tip_and_exact_collisions_without_reopening_qpy_bundles(self):
        first = self.publish()
        self.assertEqual(self.publish(), first)
        second = self.publish(2, first["generation_sha256"])
        self.assertEqual(self.publish(2, first["generation_sha256"]), second)
        with patch.object(references, "_generation_bundle", side_effect=AssertionError("QPY bundle reopened")):
            chain = store._read_calibration_fusion_generations(self.root, second["generation_sha256"])
        self.assertEqual([row.sha256 for row in chain],
                         [first["generation_sha256"], second["generation_sha256"]])
        self.assertEqual([row.document["layout"] for row in chain], [list(layout) for layout in _LAYOUTS])
        self.assertTrue(all(row.document["reference_generation_sha256"] == self.tip for row in chain))
        self.assertFalse(store._fusion_generation_terminal(chain[-1].document))
        # An unrelated marker does not change the explicitly selected history.
        (self.root / "fusion-generations" / ("f" * 64 + ".json")).write_bytes(b"unrelated")
        self.assertEqual(store._read_calibration_fusion_generations(self.root, first["generation_sha256"]),
                         chain[:1])

    def test_terminal_failures_and_unconfirmed_timeouts_cannot_have_successors(self):
        cases = (
            {"capture": None, "failure": {"error_type": "RuntimeError", "message": "candidate failed"}},
            {"capture": self.timeout(cancelled=False)},
        )
        for outcome in cases:
            with self.subTest(outcome=outcome.keys()):
                published = self.publish(**outcome)
                pin = published["generation_sha256"]
                chain = store._read_calibration_fusion_generations(self.root, pin)
                self.assertTrue(store._fusion_generation_terminal(chain[-1].document))
                with self.assertRaisesRegex(ValueError, "terminal"):
                    self.publish(2, pin)
        confirmed = self.publish(capture=self.timeout(cancelled=True))
        completed = self.publish(2, confirmed["generation_sha256"])
        self.assertEqual(len(store._read_calibration_fusion_generations(
            self.root, completed["generation_sha256"],
        )), 2)

    def test_validation_and_atomic_failure_preserve_prior_marker(self):
        first = self.publish()
        path = self.root / "fusion-generations" / f"{first['generation_sha256']}.json"
        before = path.read_bytes()
        with patch.object(store, "_publish_bundle", side_effect=OSError("marker publication failed")):
            with self.assertRaisesRegex(OSError, "marker publication"):
                self.publish(2, first["generation_sha256"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(path.parent.iterdir())), 1)
        observed = []

        def reject(generation):
            observed.append(generation.sha256)
            raise ValueError("runtime validation failed")

        with self.assertRaisesRegex(ValueError, "runtime validation"):
            self.publish(2, first["generation_sha256"], verify_generation=reject)
        self.assertEqual(len(observed), 1)
        self.assertFalse((path.parent / f"{observed[0]}.json").exists())
        self.assertEqual(path.read_bytes(), before)
        changed = _marked(deepcopy(self.captures[1]["report"]))
        changed["rounds"]["0"]["compilation"]["openqasm"]["sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "identical round"):
            self.publish(2, first["generation_sha256"], capture=_seal(changed))
        self.assertEqual(path.read_bytes(), before)

    def test_rejects_incomplete_reference_and_resealed_impossible_lineage(self):
        with self.assertRaisesRegex(ValueError, "complete reference"):
            self.publish(reference_generation_sha256=self.reference_pins[0])
        first = self.publish()
        second = self.publish(2, first["generation_sha256"])
        path = self.root / "fusion-generations" / f"{second['generation_sha256']}.json"
        document = json.loads(path.read_bytes())
        document["ordinal"] = 1
        document["layout"] = list(_LAYOUTS[0])
        raw = json.dumps(document).encode("utf-8")
        pin = sha256(raw).hexdigest()
        (path.parent / f"{pin}.json").write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "genesis"):
            store._read_calibration_fusion_generations(self.root, pin)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            store._read_calibration_fusion_generations(self.root, second["generation_sha256"])

    def test_rejects_duplicate_nonfinite_and_linked_marker_inputs(self):
        first = self.publish()
        path = self.root / "fusion-generations" / f"{first['generation_sha256']}.json"
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"schema_version":1,"bad":1e999}'):
            pin = sha256(raw).hexdigest()
            (path.parent / f"{pin}.json").write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "invalid fusion checkpoint generation JSON"):
                store._read_calibration_fusion_generations(self.root, pin)
        target = self.root / "original.json"
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError:
            target.rename(path)
            return
        with self.assertRaisesRegex(ValueError, "regular checkpoint"):
            store._read_calibration_fusion_generations(self.root, first["generation_sha256"])

    def test_invocation_context_reuses_semantics_but_standalone_reads_validate_again(self):
        with (
            patch.object(store, "_read_generation_document", wraps=store._read_generation_document) as reference,
            patch.object(store, "_read_fusion_document", wraps=store._read_fusion_document) as fusion,
            patch.object(store, "_validate_reference_outcome", wraps=store._validate_reference_outcome) as validate,
        ):
            context = store._open_calibration_fusion_context(self.root, self.tip)
            first = self.publish(_context=context)
            second = self.publish(2, first["generation_sha256"], _context=context)
            self.assertEqual((reference.call_count, fusion.call_count, validate.call_count), (2, 0, 2))
            self.assertEqual([row.sha256 for row in context.history],
                             [first["generation_sha256"], second["generation_sha256"]])
            store._read_calibration_fusion_generations(self.root, second["generation_sha256"])
            self.assertEqual((reference.call_count, fusion.call_count, validate.call_count), (4, 2, 4))
        # A context never guesses an existing branch or retains another invocation's state.
        fresh = store._open_calibration_fusion_context(self.root, self.tip)
        self.assertEqual(fresh.history, ())
        self.assertEqual(self.publish(_context=fresh), first)
        resumed = store._open_calibration_fusion_context(self.root, self.tip, first["generation_sha256"])
        self.assertEqual(self.publish(2, first["generation_sha256"], _context=resumed), second)
        with patch.object(store, "_read_complete_reference", wraps=store._read_complete_reference) as reread:
            self.assertEqual(self.publish(2, first["generation_sha256"]), second)
        self.assertEqual(reread.call_count, 1)

    def test_context_callback_and_publication_failure_do_not_advance_history(self):
        context = store._open_calibration_fusion_context(self.root, self.tip)
        first = self.publish(_context=context)
        previous = first["generation_sha256"]
        observed = []

        def mutate(generation):
            observed.append(generation)
            generation.document["layout"].reverse()

        with self.assertRaisesRegex(ValueError, "callback mutated"):
            self.publish(2, previous, _context=context, verify_generation=mutate)
        self.assertEqual(len(context.history), 1)
        self.assertFalse((self.root / "fusion-generations" / f"{observed[-1].sha256}.json").exists())

        def fail(generation):
            raise ValueError("candidate validation failed")

        with self.assertRaisesRegex(ValueError, "candidate validation failed"):
            self.publish(2, previous, _context=context, verify_generation=fail)
        with patch.object(store, "_publish_bundle", side_effect=OSError("marker publication failed")):
            with self.assertRaisesRegex(OSError, "marker publication"):
                self.publish(2, previous, _context=context)
        self.assertEqual([row.sha256 for row in context.history], [previous])
        second = self.publish(2, previous, _context=context, verify_generation=observed.append)
        observed[-1].document["layout"].reverse()
        self.assertEqual(context.history[-1].document["layout"], list(_LAYOUTS[1]))
        self.assertEqual(context.history[-1].sha256, second["generation_sha256"])
        self.assertEqual(len(store._read_calibration_fusion_generations(
            self.root, second["generation_sha256"],
        )), 2)

    def test_context_rehashes_both_ancestor_kinds_after_callback(self):
        first = self.publish()
        previous = first["generation_sha256"]
        paths = (
            self.root / "generations" / f"{self.reference_pins[0]}.json",
            self.root / "fusion-generations" / f"{previous}.json",
        )
        for path in paths:
            with self.subTest(ancestor=path.parent.name):
                context = store._open_calibration_fusion_context(self.root, self.tip, previous)
                raw = path.read_bytes()
                staged = []

                def tamper(generation):
                    staged.append(generation.sha256)
                    path.write_bytes(raw + b" ")

                try:
                    with self.assertRaisesRegex(ValueError, "ancestor.*SHA-256"):
                        self.publish(2, previous, _context=context, verify_generation=tamper)
                    self.assertEqual([row.sha256 for row in context.history], [previous])
                    self.assertFalse((self.root / "fusion-generations" / f"{staged[0]}.json").exists())
                finally:
                    path.write_bytes(raw)
        self.assertEqual(len(list((self.root / "fusion-generations").iterdir())), 1)

    def test_context_rejects_wrong_branch_and_mutated_cached_records(self):
        context = store._open_calibration_fusion_context(self.root, self.tip)
        first = self.publish(_context=context)
        previous = first["generation_sha256"]
        with self.assertRaisesRegex(ValueError, "predecessor"):
            self.publish(_context=context)
        with self.assertRaisesRegex(ValueError, "root or reference"):
            self.publish(2, previous, _context=context, reference_generation_sha256=self.reference_pins[0])
        before = deepcopy(context.history[0].document)
        context.history[0].document["layout"].reverse()
        with self.assertRaisesRegex(ValueError, "mutated validated state"):
            self.publish(2, previous, _context=context)
        context.history[0].document.clear()
        context.history[0].document.update(before)

        def mutate_cached(generation):
            context.history[0].document["layout"].reverse()

        with self.assertRaisesRegex(ValueError, "mutated validated state"):
            self.publish(2, previous, _context=context, verify_generation=mutate_cached)
        self.assertEqual(len(context.history), 1)
        self.assertEqual(len(list((self.root / "fusion-generations").iterdir())), 1)

    def test_context_rejects_symlinked_ancestor_even_with_matching_bytes(self):
        first = self.publish()
        previous = first["generation_sha256"]
        context = store._open_calibration_fusion_context(self.root, self.tip, previous)
        marker = self.root / "fusion-generations" / f"{previous}.json"
        original = self.root / "original.json"
        marker.rename(original)
        try:
            marker.symlink_to(original)
        except OSError:
            original.rename(marker)
            self.skipTest("creating symlinks is unavailable")
        with self.assertRaisesRegex(ValueError, "regular checkpoint"):
            self.publish(2, previous, _context=context)
        self.assertEqual(len(context.history), 1)

    def test_context_rejects_reentrant_append_without_advancing(self):
        context = store._open_calibration_fusion_context(self.root, self.tip)

        def reenter(generation):
            self.publish(_context=context)

        with self.assertRaisesRegex(ValueError, "append in progress"):
            self.publish(_context=context, verify_generation=reenter)
        self.assertEqual(context.history, ())
        self.assertFalse((self.root / "fusion-generations").exists())
        self.publish(_context=context)
        self.assertEqual(len(context.history), 1)


if __name__ == "__main__":
    unittest.main()
