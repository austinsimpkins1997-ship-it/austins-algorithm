"""Public Tk controls for local Decision Review workflows."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class DecisionReviewPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import tkinter as tk
        except ImportError as error:
            self.skipTest(f"Tk unavailable: {error}")
        from austins_algorithm.workbench import Workbench

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root = root
        self.addCleanup(root.destroy)
        root.withdraw()
        self.app = Workbench(root)
        root.geometry("1000x720")
        self.app.show_page("Decision Review")
        root.update()
        self.assertEqual(root.state(), "withdrawn")
        self.panel = self.app.decision_review

    def test_review_preserves_weighted_and_checkpoints_one_project(self) -> None:
        from austins_algorithm.decision_review_project import load_review_project, verify_review_export

        weighted = self.app.session.problem
        with TemporaryDirectory() as folder:
            saved = Path(folder) / "review.decision.json"
            exported = Path(folder) / "review.zip"
            self.assertTrue(self.app.save(saved))
            self.assertTrue(self.panel.optimize())
            self.assertEqual(self.panel.report["result"]["cost"], 9)
            self.assertIn("Check each candidate across all scenarios", self.panel.plan_text.get("1.0", "end"))
            self.panel.actor.set("Fixture reviewer")
            self.panel.reason.set("Synthetic round-trip fixture")
            self.assertTrue(self.panel.record_disposition())
            self.assertFalse(self.panel.dirty)
            self.assertEqual(load_review_project(saved)["decision"][0]["actor"], "Fixture reviewer")
            receipt = self.app.export(exported)
            self.assertIsNotNone(receipt)
            self.assertTrue(verify_review_export(exported)["verified"])
            self.assertTrue(self.app.open(saved))
            self.assertEqual(len(self.panel.project["decision"]), 1)
            self.app.show_page("Overview")
            self.assertEqual(self.app.session.problem, weighted)
            self.assertEqual(self.app.project_title.get(), weighted["name"])

    def test_practice_and_replay_controls(self) -> None:
        self.assertEqual(self.panel.current_practice["kind"], "baseline")
        self.panel.prediction.set("7")
        self.panel.predict_button.invoke()
        self.assertEqual(self.panel.current_practice["prediction"], 7)
        for choice in ("q0", "q2"):
            self.panel.practice_options[choice].set(True)
        self.panel.submit_button.invoke()
        self.assertTrue(self.panel.current_practice["history"][-1]["passed"])
        self.panel.continue_button.invoke()
        self.assertEqual(self.panel.current_practice["kind"], "practice")
        self.panel.hint_button.invoke()
        self.assertTrue(self.panel.current_practice["hint"])
        for _ in range(3):
            self.panel.next_button.invoke()
        self.assertEqual(self.panel.stasis_index.get(), "Step 4 of 4")
        self.assertIn("Legacy  RUN", self.panel.stasis_text.get("1.0", "end"))
        self.assertIn("Lost 1", self.panel.containment_text.get("1.0", "end"))
        self.assertIn("Duplicated 1", self.panel.containment_text.get("1.0", "end"))
        self.assertIn("Pending 5", self.panel.containment_text.get("1.0", "end"))
        self.assertIn("Recovery complete  Yes", self.panel.containment_text.get("1.0", "end"))
        with patch("austins_algorithm.decision_review_panel.messagebox.showinfo") as explain:
            self.app.open_assistant()
            self.assertIn("Check & Optimize", explain.call_args.args[1])

    def test_changed_file_blocks_autosave_and_keeps_local_update(self) -> None:
        with TemporaryDirectory() as folder:
            saved = Path(folder) / "review.decision.json"
            second = Path(folder) / "review-copy.decision.json"
            self.assertTrue(self.app.save(saved))
            saved.write_text("external change", encoding="utf-8")
            self.panel.prediction.set("7")
            self.panel.predict_button.invoke()
            self.assertTrue(self.panel.dirty)
            self.assertIn("changed on disk", self.app.message.get())
            self.assertEqual(saved.read_text(encoding="utf-8"), "external change")
            self.assertTrue(self.app.save(second))
            self.assertFalse(self.panel.dirty)
            self.assertTrue(self.app.open(second))
            self.assertEqual(self.panel.current_practice["kind"], "baseline")
            self.assertEqual(self.panel.current_practice["prediction"], 7)
