from copy import deepcopy
import unittest

from future_reader.codec import digest, loads
from future_reader.demo_records import CUTOFF, EARLY, fixture_document, timeline
from future_reader.records import apply_append, propose_append, validate, view


class TemporalTests(unittest.TestCase):
    def test_five_views_preserve_prior_and_current_findings(self):
        views = fixture_document()["views"]
        ids = lambda mode: [row["id"] for row in views[mode]["claims"]]
        self.assertEqual(ids("at_event"), ["sensor-v1"])
        self.assertEqual(ids("as_known_then"), ["sensor-v1"])
        self.assertEqual(ids("as_understood_now"), ["sensor-v2", "conditional-route"])
        self.assertEqual(ids("possible_future"), ["conditional-route"])
        self.assertIn("archive-fragment", ids("reconstruction"))

    def test_current_access_filters_dependencies_edges_and_revision_before_view(self):
        document = timeline()
        document["claims"][1]["audience"] = ["operator"]
        visible = view(document, "as_understood_now", actor="reader", as_of=CUTOFF)
        self.assertEqual([row["id"] for row in visible["claims"]], ["sensor-v1"])
        self.assertFalse(visible["order"])
        self.assertFalse(visible["conflicts"])
        self.assertNotIn("audience", visible["claims"][0])
        self.assertNotIn("sensor-v2", str(visible))
        with self.assertRaises(PermissionError):
            propose_append(document, {}, actor="reader", as_of=CUTOFF)

    def test_clock_uncertainty_and_missing_lineage_do_not_guess(self):
        document = timeline()
        document["claims"][0]["times"]["available"]["clock"] = "other-clock"
        self.assertFalse(view(document, "as_known_then", actor="reader", as_of=EARLY)["claims"])
        view_result = view(document, "reconstruction", actor="reader", as_of=CUTOFF,
                           surviving=["sensor-v2", "archive-fragment"])
        self.assertEqual([row["id"] for row in view_result["claims"]], ["archive-fragment"])

    def test_relation_cycle_and_revision_cas(self):
        document = timeline()
        cyclic = deepcopy(document)
        cyclic["order"].append({"from": "sensor-v2", "to": "sensor-v1", "relation": "temporal", "evidence": "sensor-v2"})
        with self.assertRaisesRegex(ValueError, "Cycle"):
            validate(cyclic)
        claim = {**deepcopy(document["claims"][1]), "id": "sensor-v3", "parent": "sensor-v2"}
        before = digest(document)
        draft = propose_append(document, claim, actor="reader", as_of=CUTOFF)
        result = apply_append(document, draft, actor="reader", as_of=CUTOFF, approved_digest=draft["review_digest"])
        self.assertEqual(digest(document), before)
        self.assertIn("conditional-route", draft["impacted"])
        self.assertEqual(len(result["claims"]), 5)
        with self.assertRaises(ValueError):
            apply_append(result, draft, actor="reader", as_of=CUTOFF, approved_digest=draft["review_digest"])

    def test_conflicting_reports_stay_visible(self):
        document = timeline()
        report = {**deepcopy(document["claims"][1]), "id": "alternate", "value": {"status": "KNOWN", "value": "Rejected"}}
        report.pop("parent")
        document["claims"].append(report)
        result = view(document, "reconstruction", actor="reader", as_of=CUTOFF)
        self.assertEqual(result["conflicts"], [["sensor-v2", "alternate"]])

    def test_invalid_serialization_and_clock(self):
        for payload in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e900}'):
            with self.assertRaises(ValueError):
                loads(payload)
        with self.assertRaises(ValueError):
            view(timeline(), "at_event", actor="reader", as_of="2026-11-31T12:00:00Z")
