import copy
import itertools
import json
import unittest
from unittest.mock import patch

from austins_algorithm.decision_review import (
    UNASSIGNED_COST_UNIT,
    compile_scenario,
    decision_digest,
    offline_service_example,
    parse_decision_document,
    record_check,
    review_decision,
    revise_decision,
    verify_decision,
)


def rule(key, *literals):
    return {"id": key, "literals": list(literals), "reason": "Synthetic test rule.", "source": "test fixture"}


class DecisionReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = offline_service_example()

    def test_normal_outage_recovery_have_independently_checked_shared_plan(self):
        report = review_decision(self.case)
        self.assertEqual(report["origin"], "SYNTHETIC")
        self.assertEqual(report["status"], "OPTIMUM")
        result = report["result"]
        self.assertEqual(result["assignments_checked"], 64)
        self.assertEqual(result["selected"], ["paper", "escalation", "reconcile"])
        self.assertEqual(result["cost"], 9)
        self.assertEqual([item["result"]["cost"] for item in report["scenarios"]], [4, 9, 9])
        self.assertEqual([row["status"] for row in report["scenario_matrix"][0]["outcomes"]], ["PASS"] * 3)
        self.assertTrue(all(row["failures"] for row in result["cheaper_exclusions"]))
        self.assertEqual(verify_decision(self.case, report), report)
        report["result"]["cost"] = 8
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_decision(self.case, report)

    def test_required_authority_unknown_holds_before_any_solver(self):
        authority = record_check(self.case, "authority", status="UNKNOWN", reference="Approval not provided.")
        with patch("austins_algorithm.decision_review.exact_minimum", side_effect=AssertionError("must not solve")):
            report = review_decision(authority)
        self.assertEqual(report["status"], "HELD")
        self.assertEqual(report["result"]["assignments_checked"], 0)
        self.assertEqual([row["check_id"] for row in report["holds"]], ["authority"])

    def test_failed_paper_stage_can_leave_verified_alternate_available(self):
        self.case["scenarios"][-1]["checks"] = []
        document = record_check(self.case, "paper-practice", status="FAIL", reference="Fictional paper stock exhausted.")
        report = review_decision(document)
        self.assertEqual(report["status"], "OPTIMUM")
        self.assertEqual(report["result"]["cost"], 14)
        self.assertEqual(report["result"]["selected"], ["local", "spare", "escalation", "reconcile"])
        cheaper = report["result"]["cheaper_exclusions"]
        self.assertTrue(any(row["selected"] == ["paper", "escalation", "reconcile"] and row["holds"][0]["status"] == "FAIL"
                            for row in cheaper))

    def test_material_unknown_retains_partial_choice_without_qualified_minimum(self):
        self.case["scenarios"][-1]["checks"] = []
        document = record_check(self.case, "paper-practice", status="UNKNOWN", reference="Need paper exercise.")
        result = review_decision(document)["result"]
        self.assertEqual(result["status"], "HELD")
        self.assertIsNone(result["cost"])
        self.assertEqual(result["partial_best"]["cost"], 14)

    def test_no_plan_can_use_two_known_unavailable_stage_routes(self):
        self.case["scenarios"][-1]["checks"] = []
        first = record_check(self.case, "paper-practice", status="FAIL", reference="Fictional stock exhausted.")
        second = record_check(first, "offline-practice", status="FAIL", reference="Fictional queue would not open.")
        result = review_decision(second)
        self.assertEqual(result["status"], "INFEASIBLE")
        self.assertEqual({row["check_id"] for row in result["result"]["exclusion_checks"]},
                         {"paper-practice", "offline-practice"})
        self.assertEqual(result["result"]["constraint_feasible_count"], 3)

    def test_unknown_alternate_cannot_be_counted_as_known_unavailable(self):
        self.case["scenarios"][-1]["checks"] = []
        first = record_check(self.case, "paper-practice", status="FAIL", reference="Fictional stock exhausted.")
        second = record_check(first, "offline-practice", status="UNKNOWN", reference="Verify whether queue opens.")
        result = review_decision(second)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual([row["check_id"] for row in result["holds"]], ["offline-practice"])

    def test_upstream_reassessment_invalidates_dependent_check(self):
        self.case["scenarios"][-1]["checks"] = ["offline-practice"]
        changed = record_check(self.case, "restore-test", status="PASS", reference="New spare tested independently.")
        stale = review_decision(changed)
        self.assertEqual(stale["status"], "HELD")
        self.assertEqual(stale["holds"][0]["check_id"], "offline-practice")
        self.assertIn("fresh check after", stale["holds"][0]["reason"])
        refreshed = record_check(changed, "offline-practice", status="PASS", reference="Replacement queue rechecked.")
        self.assertEqual(review_decision(refreshed)["status"], "OPTIMUM")
        failed = record_check(refreshed, "restore-test", status="FAIL", reference="Spare no longer available.")
        with self.assertRaisesRegex(ValueError, "before recording this pass"):
            record_check(failed, "offline-practice", status="PASS", reference="No fresh basis yet.")

    def test_layout_revision_keeps_prior_observations_as_stale(self):
        draft = copy.deepcopy(self.case)
        draft["title"] = "Adapted office"
        draft["checks"].append({**draft["checks"][0], "id": "new-check"})
        updated = revise_decision(self.case, draft)
        self.assertEqual(updated["revision"], self.case["revision"] + 1)
        self.assertEqual(updated["checks"][0]["reference"], self.case["checks"][0]["reference"])
        self.assertEqual(updated["checks"][-1]["status"], "UNKNOWN")
        self.assertEqual(review_decision(updated)["status"], "HELD")
        self.assertNotEqual(decision_digest(updated), decision_digest(self.case))

    def test_placeholder_import_does_not_run_even_if_author_marked_pass(self):
        self.case["cost_unit"] = UNASSIGNED_COST_UNIT
        self.case["checks"].extend({**self.case["checks"][0], "id": key}
                                   for key in ("author-cost", "author-rules"))
        self.case["required_checks"] = ["author-cost", "author-rules"]
        for scenario in self.case["scenarios"]:
            scenario["rules"] = []
        with patch("austins_algorithm.decision_review.exact_minimum", side_effect=AssertionError("must not solve")):
            self.assertEqual(review_decision(self.case)["status"], "HELD")
        holds = compile_scenario(self.case)["holds"]
        self.assertEqual([item["check_id"] for item in holds], ["author-cost", "author-rules"])
        self.assertTrue(all(item["status"] == "UNKNOWN" for item in holds))

    def test_individually_feasible_scenarios_can_have_no_common_plan(self):
        self.case["scenarios"][0]["rules"].append(rule("cloud-mandatory", "cloud"))
        report = review_decision(self.case)
        self.assertEqual(report["status"], "INFEASIBLE")
        self.assertEqual([row["status"] for row in report["scenarios"]], ["OPTIMUM"] * 3)
        self.assertEqual(report["scenario_matrix"][0]["outcomes"][1]["failures"][0]["rule_id"], "isolate-cloud")

    def test_intersection_over_legacy_clause_limit_is_exact(self):
        keys = [row["id"] for row in self.case["variables"]]
        pairs = list(itertools.combinations(keys, 2))
        for index, scenario in enumerate(self.case["scenarios"]):
            chunk = pairs[index * 5:(index + 1) * 5]
            scenario["rules"] = [rule(f"pair-{seq}", left, right) for seq, (left, right) in enumerate(chunk)]
        self.assertFalse(compile_scenario(self.case)["solver_compatible"])
        result = review_decision(self.case)["result"]
        self.assertEqual(result["method"], "scenario_intersection")
        self.assertEqual(result["cost"], 13)
        self.assertEqual(result["selected"], ["paper", "spare", "escalation", "reconcile", "cloud"])

    def test_ties_and_unattested_source_provenance(self):
        self.case["variables"][1]["cost"] = 1
        result = review_decision(self.case)["result"]
        self.assertEqual([row["cost"] for row in result["ties"]], [9, 9])
        self.assertTrue(any(row["selected"] == ["paper", "escalation", "reconcile"] for row in result["ties"]))
        parsed = parse_decision_document(json.dumps(self.case, ensure_ascii=False))
        self.assertEqual(decision_digest(parsed), decision_digest(self.case))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_decision_document('{"schema":"a","schema":"b"}')

    def test_cycles_and_conflicting_handoff_check_roles_rejected(self):
        self.case["variables"][0]["depends_on"] = ["paper"]
        with self.assertRaisesRegex(ValueError, "Cycle"):
            review_decision(self.case)
        self.case["variables"][0]["depends_on"] = []
        self.case["handoff"]["consent_check"] = "authority"
        with self.assertRaisesRegex(ValueError, "separate checks"):
            review_decision(self.case)

    def test_compiler_delta_cannot_publish_unverified_result(self):
        with patch("austins_algorithm.decision_review.exact_minimum", return_value={
            "cost": 1, "ties": ["000001"], "feasible_count": 1,
        }):
            with self.assertRaisesRegex(RuntimeError, "disagrees"):
                review_decision(self.case)


if __name__ == "__main__":
    unittest.main()
