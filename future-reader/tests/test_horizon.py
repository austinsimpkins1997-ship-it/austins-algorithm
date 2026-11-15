import unittest

from future_reader.horizon import MAX_TICK, slice_horizon


class HorizonTests(unittest.TestCase):
    def test_both_outcomes_get_a_turn_across_resumed_slices(self):
        first = slice_horizon(opening=5, budget=3)
        second = slice_horizon(opening=5, budget=3, checkpoint=first["checkpoint"])
        full = slice_horizon(opening=5, budget=6)
        self.assertEqual(first["observed"] + second["observed"], full["observed"])
        self.assertEqual(second["checkpoint"]["parent"], first["checkpoint"]["sha256"])
        self.assertTrue(all(row["fulfillment"] == "NOT_EXECUTED" for row in full["observed"]))
        self.assertEqual(full["status"], "PAUSED")

    def test_stale_tampered_and_representation_limit(self):
        paused = slice_horizon()["checkpoint"]
        with self.assertRaises(ValueError):
            slice_horizon(opening=1, checkpoint=paused)
        with self.assertRaises(ValueError):
            slice_horizon(checkpoint={**paused, "cursor": 0})
        boundary = slice_horizon(opening=MAX_TICK)
        self.assertEqual(boundary["status"], "REPRESENTATION_LIMIT")
        self.assertEqual(len(boundary["observed"]), 2)
