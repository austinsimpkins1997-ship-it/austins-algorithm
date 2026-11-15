"""Verify the practice sequence, portable receipt and prepared review integration."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from austins_algorithm.decision_review import record_check, review_decision
from austins_algorithm.decision_review_policy import PreparedDecisions, benchmark_prepared, prepare_policy
from austins_algorithm.decision_review_practice import new_practice, practice_action, practice_view, verify_practice_bank
from austins_algorithm.decision_review_project import (
    export_review_project, load_review_project, new_review_project, project_with_document,
    record_review_decision, save_review_project, verify_review_export,
)


class ReviewIntegrationTests(unittest.TestCase):
    def test_baseline_hint_independence_transfer_and_delayed_replay(self):
        verify_practice_bank()
        now = datetime(2026, 11, 14, 15, tzinfo=timezone.utc)
        state = new_practice()

        def send(action, **kwargs):
            nonlocal state
            state = practice_action(state, action, now=kwargs.pop("now", now), **kwargs)

        send("predict", prediction=6)
        send("submit", choices=["q1", "q2"])
        self.assertEqual(practice_view(state, now=now)["evidence"]["baseline"], "needs practice")
        send("continue")
        send("hint")
        with self.assertRaises(ValueError):
            send("hint")
        send("submit", choices=["q0", "q2"])
        self.assertEqual(practice_view(state, now=now)["history"][-1]["help"], "hinted")
        self.assertEqual(practice_view(state, now=now)["evidence"]["independence"], "unknown")
        send("continue")
        send("submit", choices=["q0", "q3"])
        evidence = practice_view(state, now=now)["evidence"]
        self.assertEqual(evidence["independence"], "observed")
        self.assertEqual(evidence["acquisition"], "observed after practice")
        send("continue")
        send("submit", choices=["q0", "q2"])
        self.assertEqual(practice_view(state, now=now)["evidence"]["transfer"], "observed")
        with self.assertRaisesRegex(ValueError, "No fresh exercise"):
            send("continue")
        later = now + timedelta(days=7)
        send("continue", now=later)
        send("submit", choices=["q0", "q3"], now=later)
        self.assertEqual(practice_view(deepcopy(state), now=later)["evidence"]["retention"], "observed")

    def test_portable_decision_export_replays_and_rejects_rehashed_tampering(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            project = new_review_project()
            self.assertEqual(review_decision(project["document"])["status"], "OPTIMUM")
            project = record_review_decision(project, "accepted", "Exercise reviewer", "Reviewed synthetic result")
            saved = folder / "local.json"
            save_review_project(saved, project)
            self.assertEqual(load_review_project(saved), project)
            with self.assertRaises(FileExistsError):
                save_review_project(saved, project)
            archive_path = folder / "evidence.zip"
            receipt = export_review_project(archive_path, project)
            self.assertEqual(verify_review_export(archive_path, manifest_sha256=receipt["manifest_sha256"])["status"], "OPTIMUM")
            with self.assertRaisesRegex(ValueError, "supplied digest"):
                verify_review_export(archive_path, manifest_sha256="0" * 64)
            with ZipFile(archive_path) as archive:
                raw = {name: archive.read(name) for name in archive.namelist()}
            raw["plan-review.json"] = b'{"status":"OPTIMUM","wrong":true}'
            manifest = json.loads(raw["manifest.json"])
            manifest["files"]["plan-review.json"] = {"bytes": len(raw["plan-review.json"]),
                                                     "sha256": sha256(raw["plan-review.json"]).hexdigest()}
            raw["manifest.json"] = json.dumps(manifest).encode()
            changed = folder / "changed.zip"
            with ZipFile(changed, "w") as archive:
                for name, value in raw.items():
                    archive.writestr(name, value)
            with self.assertRaisesRegex(ValueError, "replayed"):
                verify_review_export(changed)

    def test_legacy_projects_upgrade_and_archives_verify_in_original_format(self):
        def legacy_json(value):
            return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()

        def legacy_archive(path, members):
            manifest = json.loads(members["manifest.json"])
            manifest["schema"] = "AustinDecisionExport/1"
            for name, info in manifest["files"].items():
                info.update(bytes=len(members[name]), sha256=sha256(members[name]).hexdigest())
            manifest_raw = legacy_json(manifest)
            with ZipFile(path, "w") as archive:
                for name, raw in {**members, "manifest.json": manifest_raw}.items():
                    archive.writestr(name, raw)
            return sha256(manifest_raw).hexdigest()

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            for recorded_revisions in (False, True):
                with self.subTest(recorded_revisions=recorded_revisions):
                    current = new_review_project()
                    if recorded_revisions:
                        revised = {**current["document"], "title": "Edited tabletop"}
                        current = project_with_document(current, revised)
                    export = folder / f"current-{recorded_revisions}.zip"
                    export_review_project(export, current)
                    with ZipFile(export) as archive:
                        members = {name: archive.read(name) for name in archive.namelist()}
                    self.assertEqual(json.loads(members["manifest.json"])["schema"], "AustinDecisionExport/2")
                    old_project = {**current, "schema": "AustinDecisionProject/1"}
                    if not recorded_revisions:
                        old_project.pop("revisions")
                        old_report = (b"<section><h2>Prior review snapshots</h2><pre>[]\n</pre></section>")
                        self.assertIn(old_report, members["report.html"])
                        members["report.html"] = members["report.html"].replace(old_report, b"", 1)
                    members["project.json"] = legacy_json(old_project)
                    saved = folder / f"legacy-{recorded_revisions}.json"
                    saved.write_bytes(members["project.json"])
                    self.assertEqual(load_review_project(saved), current)
                    self.assertEqual(saved.read_bytes(), members["project.json"])
                    upgraded = folder / f"upgraded-{recorded_revisions}.json"
                    save_review_project(upgraded, load_review_project(saved))
                    self.assertEqual(json.loads(upgraded.read_bytes())["schema"], "AustinDecisionProject/2")
                    old_export = folder / f"legacy-{recorded_revisions}.zip"
                    pin = legacy_archive(old_export, members)
                    self.assertTrue(verify_review_export(old_export, manifest_sha256=pin)["verified"])
                    migrated = folder / f"migrated-{recorded_revisions}.zip"
                    export_review_project(migrated, old_project)
                    with ZipFile(migrated) as archive:
                        self.assertEqual(json.loads(archive.read("project.json")), current)
                    self.assertTrue(verify_review_export(migrated)["verified"])
                    members["report.html"] += b"<p>Changed after export</p>"
                    tampered = folder / f"tampered-{recorded_revisions}.zip"
                    false_pin = legacy_archive(tampered, members)
                    with self.assertRaisesRegex(ValueError, "replayed"):
                        verify_review_export(tampered, manifest_sha256=false_pin)

    def test_held_disposition_and_prepared_case_do_not_reuse_stale_results(self):
        project = new_review_project()
        document = project["document"]
        cases = {"outage": document}
        bundle = prepare_policy(cases)
        pin = bundle["policy_sha256"]
        prepared = PreparedDecisions(bundle, pin)
        original = review_decision(document)
        args = {"input_sha256": original["input_sha256"], "policy_sha256": pin, "deadline_ns": 100}
        self.assertEqual(prepared.lookup("outage", **args, clock=iter((20, 30)).__next__)["result"], original)
        self.assertEqual(prepared.lookup("outage", **args, clock=iter((20, 100)).__next__)["status"], "HELD")
        self.assertEqual(prepared.lookup("unmodeled", **args, clock=lambda: 20)["status"], "HELD")
        check = document["handoff"]["authority_check"]
        changed = record_check(document, check, status="UNKNOWN", reference="Synthetic evidence removed.")
        self.assertEqual(review_decision(changed)["status"], "HELD")
        self.assertEqual(prepared.lookup("outage", **{**args, "input_sha256": review_decision(changed)["input_sha256"]},
                                         clock=lambda: 20)["status"], "HELD")
        project = project_with_document(project, changed)
        self.assertEqual(project["revisions"][0]["input_sha256"], original["input_sha256"])
        with self.assertRaisesRegex(ValueError, "requires a feasible"):
            record_review_decision(project, "accepted", "Exercise reviewer", "No passed checks.")
        self.assertEqual(len(record_review_decision(project, "paused", "Exercise reviewer", "Await evidence")["decision"]), 1)
        self.assertEqual(benchmark_prepared(cases, repeats=2)["matches"], 2)
