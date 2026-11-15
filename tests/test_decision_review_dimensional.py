from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from austins_algorithm.decision_review import review_decision
from austins_algorithm.decision_review_dimensional import import_dimensional, load_dimensional


BROWSER_EXPORT = Path(__file__).with_name("fixtures") / "dimensional-browser-v1.json"


def journal_fixture():
    baseline = {"state": "Practice panels ready for inspection", "preserve": "Fixture datum",
                "reference": "Layout sketch", "owner": "Layout lead", "origin": "simulation"}
    changes = {}
    history = [{"event": 1, "command": "baseline", "change_id": None, "baseline": baseline}]

    def event(change, command, **fields):
        entry = {"event": len(history) + 1, "command": command, "change_id": change,
                 "revision": changes[change]["revision"], **fields}
        history.append(entry)
        return entry["event"]

    for key, parents in (("layout", []), ("shadow", ["layout"])):
        plan = {"title": f"Practice {key}", "owner": "Layout lead", "starting_state": "Mock panels mounted",
                "change": f"Test {key}", "expected": "Align within tolerance", "stop": "Hold on seam mismatch",
                "recovery": "Return to the original guide", "depends_on": parents,
                "interfaces": {"gap": {"reference": "Marked datum", "tolerance": "1.5 mm", "owner": "Inspector"}},
                "checks": {phase: {"phase": phase, "interface": "gap", "test": f"Check {phase}", "checker": "Inspector"}
                           for phase in ("entry", "outcome", "invariant", "recovery")}}
        stage = {"plan": plan, "revision": 1, "state": "verified", "review": "accepted", "started_event": 0,
                 "recovery_event": 0, "evidence": []}
        changes[key] = stage
        event(key, "add", plan=deepcopy(plan))
        for phase in ("entry", "invariant", "outcome"):
            if phase == "invariant":
                stage["started_event"] = event(key, "start")
            add_observation(changes, history, key, phase, "pass")
        event(key, "accept", state="verified", review="accepted")
    return {"schema": "ChronoRealmDimensionalDesign/1", "baseline": baseline, "changes": changes, "history": history}


def add_observation(changes, history, key, check, result):
    stage = changes[key]
    number = len(history) + 1
    row = {"event": number, "revision": stage["revision"], "check": check, "checker": "Inspector",
           "result": result, "origin": "simulation", "reference": "Practice log", "observation": "Observed sample"}
    stage["evidence"].append(row)
    history.append({"event": number, "command": "evidence", "change_id": key, "revision": stage["revision"],
                    "evidence": deepcopy(row)})


class DimensionalImportTests(unittest.TestCase):
    def test_actual_browser_download_preserves_hold_failure_reversion_and_source(self):
        raw = BROWSER_EXPORT.read_bytes()
        document = load_dimensional(BROWSER_EXPORT)
        report = review_decision(document)
        checks = {row["id"]: row for row in document["checks"]}
        self.assertEqual(document["title"], "Truck stripe rehearsal")
        self.assertEqual(document["revision"], 21)
        self.assertEqual(document["origin"], "SYNTHETIC")
        self.assertEqual(document["variables"][1]["depends_on"], ["stage-seat-template"])
        self.assertEqual(checks["admission-seat-template"]["status"], "PASS")
        self.assertIn("14 verified/accepted", checks["admission-seat-template"]["reference"])
        self.assertEqual(checks["admission-another-pair"]["status"], "FAIL")
        self.assertIn("17 failed/pending", checks["admission-another-pair"]["reference"])
        self.assertIn("21 reverted/reverted", checks["admission-another-pair"]["reference"])
        observations = {row["assessment"]: row for row in document["checks"] if row["id"].startswith("check-")}
        self.assertEqual([observations[number]["status"] for number in (17, 19, 20)], ["FAIL", "PASS", "PASS"])
        self.assertIn("source Bounded browser fixture", observations[17]["reference"])
        self.assertIn("Observed 2026-11-14T17:00:00Z", observations[17]["reference"])
        self.assertIn("actor Bench reviewer", observations[17]["reference"])
        self.assertIn(hashlib.sha256(raw).hexdigest(), checks["source-review"]["reference"])
        self.assertEqual(document["scenarios"][0]["rules"], [])
        self.assertEqual(report["status"], "HELD")
        self.assertEqual(report["result"]["assignments_checked"], 0)

    def test_browser_export_needs_valid_state_transitions_and_matching_projection(self):
        data = json.loads(BROWSER_EXPORT.read_bytes())
        for label, mutate, expected in (
            ("display state", lambda d: d["project"]["changes"][1].update(state="verified"), "exported view"),
            ("check failed before revert", lambda d: d["events"][18]["payload"].update(result="fail"),
             "reversion needs restoration evidence"),
            ("acceptance actor", lambda d: d["events"][13].update(actor="Someone else"), "assigned checker"),
            ("duplicate request", lambda d: d["events"][1].update(request_id=d["events"][0]["request_id"]),
             "event identifiers"),
            ("conflicting revision type", lambda d: d["project"]["changes"][0].update(revision=True), "exported view"),
        ):
            with self.subTest(label=label):
                candidate = deepcopy(data)
                mutate(candidate)
                with self.assertRaisesRegex(ValueError, expected):
                    import_dimensional(candidate)

    def test_imported_plan_uses_explicit_holds_instead_of_implying_cost_or_scope(self):
        native = journal_fixture()
        before = deepcopy(native)
        draft = import_dimensional(native)
        report = review_decision(draft)
        self.assertEqual(report["status"], "HELD")
        self.assertEqual(report["result"]["assignments_checked"], 0)
        self.assertEqual(draft["origin"], "SYNTHETIC")
        self.assertIn("placeholders", draft["cost_unit"])
        self.assertIn("author-cost", draft["required_checks"])
        self.assertIn("handoff-consent", {entry["check_id"] for entry in report["holds"]})
        self.assertEqual(draft["variables"][1]["depends_on"], ["stage-layout"])
        self.assertEqual(draft["variables"][0]["interfaces"][0]["tolerance"], "1.5 mm")
        self.assertEqual([entry["holds"] for entry in report["stages"]], [[], []])
        self.assertEqual(native, before)

    def test_changed_prerequisite_requires_fresh_downstream_acceptance(self):
        native = journal_fixture()
        stage = native["changes"]["layout"]
        add_observation(native["changes"], native["history"], "layout", "invariant", "pass")
        native["history"].append({"event": len(native["history"]) + 1, "command": "accept", "change_id": "layout",
                                   "revision": stage["revision"], "state": "verified", "review": "accepted"})
        report = review_decision(import_dimensional(native))
        self.assertEqual(report["stages"][0]["holds"], [])
        self.assertIn("fresh acceptance after its prerequisite changed", report["stages"][1]["holds"][0]["reference"])

    def test_revision_filters_old_evidence_and_recovery_is_separate(self):
        native = journal_fixture()
        stage = native["changes"]["layout"]
        last = len(native["history"]) + 1
        stage.update(revision=2, started_event=0, state="unknown", review="revised")
        native["history"].append({"event": last, "command": "revise", "change_id": "layout", "revision": 2,
                                   "plan": deepcopy(stage["plan"])})
        draft = import_dimensional(native)
        checks = {row["id"]: row for row in draft["checks"]}
        self.assertEqual(checks["check-layout-outcome"]["status"], "UNKNOWN")
        self.assertEqual(checks["admission-layout"]["status"], "UNKNOWN")
        self.assertNotIn("check-layout-recovery", draft["variables"][0]["checks"])

    def test_explicit_mapping_preserves_values_while_review_stays_held(self):
        scenarios = [{"id": "normal", "title": "Normal conditions", "checks": [],
                      "rules": [{"id": "finish", "literals": ["stage-shadow"], "reason": "Finish the layout",
                                 "source": "Reviewed exercise requirements"}]}]
        doc = import_dimensional(journal_fixture(), mapping={"cost_unit": "Trial labor minutes",
                                 "costs": {"stage-layout": 2, "stage-shadow": 4}, "scenarios": scenarios})
        self.assertEqual([row["cost"] for row in doc["variables"]], [2, 4])
        self.assertEqual(doc["scenarios"], scenarios)
        self.assertEqual(doc["required_checks"], ["source-review"])
        self.assertEqual(review_decision(doc)["status"], "HELD")

    def test_rejects_unlogged_evidence_and_loads_demo_envelope_with_file_pin(self):
        native = journal_fixture()
        broken = deepcopy(native)
        broken["changes"]["layout"]["evidence"][0]["result"] = "fail"
        with self.assertRaisesRegex(ValueError, "evidence records disagree"):
            import_dimensional(broken)
        raw = json.dumps({"export": native, "evidence_origin": "simulation"}).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_bytes(raw)
            imported = load_dimensional(path)
        review = next(row for row in imported["checks"] if row["id"] == "source-review")
        self.assertIn(path.name, review["reference"])
        self.assertIn(hashlib.sha256(raw).hexdigest(), review["reference"])
