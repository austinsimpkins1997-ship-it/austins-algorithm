"""Focus preserves task state across real project save/load/export entry points."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from austins_algorithm.focus_session import basis_digest, focus_action, focus_view, start_focus, validate_focus
from austins_algorithm.workbench_project import (
    export_project, load_project, new_project, save_project, verify_export,
)
from austins_algorithm.decision_review_project import (
    export_review_project, load_review_project, new_review_project, save_review_project, verify_review_export,
)


class FocusSessionTests(unittest.TestCase):
    def test_park_interrupt_and_resume_keep_independent_decision_hold(self):
        project = new_review_project()
        before = deepcopy(project)
        focus = start_focus(project, "Review intake continuity", "Check route evidence")
        focus = focus_action(focus, "PARK", project, note="Review the chart colors later")
        self.assertEqual(focus["status"], "ACTIVE")
        focus = focus_action(focus, "INTERRUPT", project, note="Recovery evidence is disputed")
        self.assertEqual(focus["status"], "PAUSED")
        self.assertEqual(focus["checkpoint"], "Recovery evidence is disputed")
        self.assertEqual(focus["next_action"], "Check route evidence")
        focus = focus_action(focus, "RESOLVE", project, interruption_id=2)
        focus = focus_action(focus, "RESUME", project)
        self.assertEqual(focus_view(focus, project)["pending_interruptions"], 1)
        self.assertEqual(project, before)

    def test_changed_inputs_require_review_without_mutating_prior_checkpoint(self):
        project = new_project()
        focus = start_focus(project, "Inspect weighted search", "Compare two feasible choices")
        focus = focus_action(focus, "CHECKPOINT", project, note="Compared the first choice")
        before = deepcopy(focus)
        project["settings"]["seed"] += 1
        self.assertTrue(focus_view(focus, project)["inputs_changed"])
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            focus_action(focus, "RESUME", project)
        self.assertEqual(focus, before)
        resumed = focus_action(focus, "RESUME", project, reviewed_changes=True)
        self.assertFalse(focus_view(resumed, project)["inputs_changed"])
        self.assertEqual(focus["basis_sha256"], before["basis_sha256"])

    def test_both_project_formats_save_reopen_and_export_complete_focus(self):
        variants = (
            (new_project, save_project, load_project, export_project, verify_export),
            (new_review_project, save_review_project, load_review_project, export_review_project, verify_review_export),
        )
        with TemporaryDirectory() as temporary:
            for index, (create, save, load, export, verify) in enumerate(variants):
                with self.subTest(format=index):
                    project = create()
                    project["focus"] = start_focus(project, "Compare options", "Check the next input")
                    project["focus"] = focus_action(project["focus"], "CHECKPOINT", project, note="First input checked")
                    path = Path(temporary) / f"project-{index}.json"
                    archive_path = Path(temporary) / f"export-{index}.zip"
                    save(path, project)
                    self.assertEqual(load(path)["focus"], project["focus"])
                    export(archive_path, project)
                    verify(archive_path)
                    with ZipFile(archive_path) as archive:
                        self.assertEqual(json.loads(archive.read("focus.json")), project["focus"])
                        self.assertEqual(json.loads(archive.read("project.json"))["focus"], project["focus"])
                        self.assertIn("First input checked", archive.read("report.html").decode("utf-8"))
                        self.assertIn("focus.json", archive.read("manifest.json").decode("utf-8"))

    def test_bounds_and_malformed_history_fail_without_changing_session(self):
        project = new_project()
        focus = start_focus(project, "Goal", "Next")
        original = deepcopy(focus)
        for field, value in (("goal", ""), ("basis_sha256", "x" * 64), ("events", [])):
            malformed = {**focus, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_focus(malformed)
        with self.assertRaises(ValueError):
            focus_action(focus, "PARK", project, note="x" * 2001)
        with self.assertRaises(ValueError):
            focus_action(focus, "RESUME", project, reviewed_changes=1)
        with self.assertRaises(ValueError):
            focus_action(focus, "RESOLVE", project, interruption_id=True)
        for _ in range(63):
            focus = focus_action(focus, "PARK", project, note="Later")
        focus = focus_action(focus, "PARK", project, note="Last")
        with self.assertRaisesRegex(ValueError, "64 recorded"):
            focus_action(focus, "PARK", project, note="Over cap")
        self.assertEqual(len(focus["interruptions"]), 64)
        self.assertEqual(original["events"][0]["action"], "START")

    def test_focus_notes_do_not_change_input_identity_or_optimizer_report(self):
        from austins_algorithm.optimization import SearchSession

        project = new_project()
        before = basis_digest(project)
        report = SearchSession(project["problem"], **project["settings"]).run()
        project["focus"] = start_focus(project, "<script>user note</script>", "Inspect outcomes")
        self.assertEqual(basis_digest(project), before)
        self.assertEqual(SearchSession(project["problem"], **project["settings"]).run(), report)
        project["focus"] = focus_action(project["focus"], "COMPLETE", project)
        with self.assertRaisesRegex(ValueError, "complete"):
            focus_action(project["focus"], "RESUME", project)


class FocusPanelTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from austins_algorithm.workbench import Workbench

        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = Workbench(self.root)

    def test_hidden_public_controls_save_and_reopen_both_project_types(self):
        with TemporaryDirectory() as temporary:
            for page in ("Overview", "Decision Review"):
                with self.subTest(page=page):
                    self.app.show_page(page)
                    self.app.open_focus()
                    panel = self.app.focus_panel
                    self.assertEqual(panel.window.state(), "withdrawn")
                    panel.goal.set("Review one decision")
                    panel.next_action.set("Check the evidence source")
                    self.assertTrue(panel.act("START"))
                    panel.note.set("One source remains to check")
                    self.assertTrue(panel.act("CHECKPOINT"))
                    self.assertIn("PAUSED", self.app.focus_status.get())
                    path = Path(temporary) / f"{page}.json"
                    self.assertTrue(self.app.save(path))
                    self.assertTrue(self.app.open(path))
                    self.app.open_focus()
                    self.assertEqual(self.app.focus_panel.project["focus"]["checkpoint"], "One source remains to check")
                    self.assertEqual(self.root.state(), "withdrawn")

    def test_open_dialog_cannot_write_to_a_replaced_project(self):
        self.app.open_focus()
        panel = self.app.focus_panel
        panel.goal.set("Old goal")
        panel.next_action.set("Old action")
        self.app._activate_project(new_project(), None)
        self.assertFalse(panel.act("START"))
        self.assertNotIn("focus", self.app.project)
        self.assertIn("project changed", panel.status.get())

    def test_decision_edit_surfaces_changed_focus_inputs_in_footer(self):
        self.app.show_page("Decision Review")
        self.app.open_focus()
        focus = self.app.focus_panel
        focus.goal.set("Review this plan")
        focus.next_action.set("Inspect current evidence")
        self.assertTrue(focus.act("START"))
        panel = self.app.decision_review
        changed = deepcopy(panel.project)
        changed["document"]["title"] = "Revised plan context"
        panel._commit(changed, "Synthetic edit")
        self.assertIn("Inputs changed", self.app.focus_status.get())
        self.assertEqual(self.root.state(), "withdrawn")

    def test_public_decision_new_and_open_refresh_the_focus_footer(self):
        self.app.show_page("Decision Review")
        self.app.open_focus()
        focus = self.app.focus_panel
        focus.goal.set("Saved goal")
        focus.next_action.set("Saved next step")
        self.assertTrue(focus.act("START"))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "focused-review.json"
            self.assertTrue(self.app.save(path))
            self.assertIn("Saved next step", self.app.focus_status.get())
            self.app.decision_review.new()
            self.assertEqual(self.app.focus_status.get(), "")
            self.assertTrue(self.app.open(path))
            self.assertIn("Saved next step", self.app.focus_status.get())
            blank = Path(temporary) / "unfocused-review.json"
            save_review_project(blank, new_review_project())
            self.assertTrue(self.app.open(blank))
            self.assertEqual(self.app.focus_status.get(), "")
