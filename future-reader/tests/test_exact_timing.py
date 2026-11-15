"""Check exact timing certificates, ties, cutoff order, and independent replay."""

import unittest

from future_reader.exact_timing import Check, Controller, Decision, Delivery, Effect, Model, Route
from future_reader.exact_timing_study import reference, run_timing


class ExactTimingTests(unittest.TestCase):
    def test_joint_effects_are_needed_to_break_a_lead(self):
        model = Model((Route("A", 5), Route("B", 0)), (Check(2, 4, (Effect((0, 3)),)),) * 2, 9)
        controller = Controller(model)
        self.assertEqual(controller.advance(0).status, "WAIT")
        self.assertEqual(controller.advance(2, ((0, Delivery(2, 0)),)).status, "WAIT")
        self.assertEqual(controller.advance(4, ((1, Delivery(4, 0)),)), Decision("CERTIFIED", 4, ("B",)))

    def test_common_shifts_and_veto_conditioned_bounds(self):
        model = Model((Route("A", 1), Route("B", 0), Route("C", 99, False)),
                      (Check(2, 5, (Effect((100, 100, 100)), Effect((0, 200, 0), frozenset({"B"})))),), 8)
        self.assertEqual(Controller(model).advance(0), Decision("CERTIFIED", 0, ("A",)))
        self.assertEqual(reference(model, (Delivery(3, 1),))[:2], (("A",), 0))

    def test_cutoff_arrivals_are_atomic_and_equality_can_add_a_winner(self):
        model = Model((Route("A", 2), Route("B", 0)), (Check(2, 3, (Effect((0, 2)),)),) * 2, 5, 1, 1)
        controller = Controller(model)
        self.assertEqual(controller.advance(0).status, "WAIT")
        with self.assertRaises(ValueError):
            controller.advance(3, ((0, Delivery(3, 0)), (0, Delivery(3, 0))))
        self.assertEqual(controller.advance(3, ((0, Delivery(3, 0)),)), Decision("AT_CUTOFF", 3, ("A", "B")))
        with self.assertRaises(ValueError):
            controller.advance(3, ((1, Delivery(3, 0)),))

    def test_late_veto_no_route_and_no_safe_window(self):
        check = Check(4, 5, (Effect((0,), frozenset({"A"})),))
        self.assertEqual(Controller(Model((Route("A", 0),), (check,), 3)).advance(0).winners, ("A",))
        self.assertEqual(Controller(Model((Route("A", 0, False),), (check,), 3)).advance(0).winners, ())
        self.assertEqual(Controller(Model((Route("A", 0),), (check,), 3, 4)).result,
                         Decision("NO_SAFE_WINDOW", None, None))

    def test_compare_every_exhaustive_case_against_enumeration(self):
        report = run_timing(cases_per_seed=8)
        exhaustive = report["exhaustive"]
        self.assertEqual(exhaustive["cases"], 8_400)
        self.assertEqual(exhaustive["matched_cutoff_winner_sets"], 8_400)
        self.assertEqual(exhaustive["matched_earliest_ticks"], 8_400)
        self.assertLess(exhaustive["certified_wait_ticks"], exhaustive["fixed_wait_ticks"])
        self.assertEqual(sum(row["cases"] for row in report["generated"]), 24)
