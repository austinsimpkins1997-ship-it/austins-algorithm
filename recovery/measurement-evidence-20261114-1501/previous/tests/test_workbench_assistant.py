"""Verify that local guidance reflects the actual simulation and its boundaries."""

from copy import deepcopy
import unittest

from austins_algorithm.optimization import SearchSession, exact_minimum
from austins_algorithm.workbench_assistant import assist, build_overview
from austins_algorithm.workbench_project import new_project


class WorkbenchAssistantTests(unittest.TestCase):
    def setUp(self):
        self.project = new_project()
        self.session = SearchSession(self.project["problem"], **self.project["settings"])

    def test_initial_overview_prompts_action_without_claiming_a_measurement(self):
        self.project["run"] = self.session.report()
        overview = build_overview(self.project)
        self.assertIn("Ready to explore", overview["heading"])
        self.assertIn("yet to measure", overview["outcome"])
        self.assertIn("0 of 16 shots", overview["budget"])
        self.assertIn("Choose Optimize", overview["next_step"])
        answer = assist(self.project, "What is happening right now?")
        self.assertIn(overview["heading"], answer["answer"])
        self.assertIn(overview["outcome"], answer["answer"])

    def test_current_oracle_and_selected_historical_state(self):
        self.project["preview"] = self.session.preview(1)
        before = deepcopy(self.project)
        oracle = assist(self.project, "Explain the current frame", frame_index=1)
        self.assertIn("chance of measurement stays the same", oracle["answer"])
        selected = assist(self.project, "What is this selected state?", frame_index=1, historical=True, selected_state=53)
        self.assertIn("110101", selected["answer"])
        self.assertIn("negative", selected["answer"])
        self.assertEqual(assist(self.project, "Explain the selected state.")["action"], "simulate")
        self.assertEqual(self.project, before)

    def test_exact_answer_and_budgeted_result_are_distinguished(self):
        self.project["settings"]["prove"] = False
        session = SearchSession(self.project["problem"], **self.project["settings"])
        self.project["run"] = session.run()
        overview = build_overview(self.project, exact=exact_minimum(self.project["problem"]))
        self.assertIn("shot budget", overview["heading"])
        self.assertIn("Exact minimum is 110101 at cost 4", overview["outcome"])
        self.assertIn(overview["heading"], assist(self.project, "What is happening right now?")["answer"])
        action = assist(self.project, "Optimize")
        self.assertEqual(action["action"], "optimize")
        self.assertEqual(assist(self.project, "Tell me about music")["action"], None)
