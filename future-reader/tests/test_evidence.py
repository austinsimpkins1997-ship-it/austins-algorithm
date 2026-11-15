"""Focused checks for the independently replayable evidence cases."""

from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import unittest

from future_reader.evidence import (
    CASES,
    C_M_PER_S,
    OPERATIONS,
    age_membership,
    archive_experiment,
    archive_recovery,
    cern_window,
    dispatch,
    fixture_document,
    replay_rapid_utc_extract,
    restore_archive,
    retention_probability,
    split_archive,
    timing_equivalence,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class EvidenceTests(unittest.TestCase):
    def test_timing_equivalence_is_exact_under_its_assumptions(self):
        result = timing_equivalence()
        distance = Fraction(result["path_m"])
        ratio = Fraction(result["hypothetical_speed_over_c"])
        extra = Fraction(result["extra_path_m_exact"])
        self.assertEqual((distance + extra) / C_M_PER_S, distance / (ratio * C_M_PER_S))
        self.assertEqual(extra / C_M_PER_S, Fraction(result["delay_s_exact"]))
        self.assertAlmostEqual(result["delay_ps"], 152.984, places=3)
        self.assertAlmostEqual(result["equivalent_extra_path_cm"], 4.586, places=3)
        historical_ns = Fraction(timing_equivalence("31.450")["delay_s_exact"]) * 10**9
        self.assertAlmostEqual(float(historical_ns), 0.421, places=3)
        self.assertEqual(timing_equivalence("1", "1")["delay_ps"], 0)
        for invalid in ("0", "-1", "2", "nan"):
            with self.assertRaises(ValueError):
                timing_equivalence("1", invalid)

    def test_cern_ambiguity_survives_certification_and_historical_view(self):
        pinpoint = dispatch("cern", "PINPOINT")
        self.assertEqual(pinpoint["status"], "AMBIGUOUS")
        self.assertEqual(len(pinpoint["details"]["candidates"]), 2)
        certainty = dispatch("cern", "CERTIFY")
        self.assertEqual(certainty["status"], "PROVED_IN_MODEL")
        self.assertEqual(certainty["details"]["context"]["supplied"]["status"], "NOT_REPRODUCED")
        reverse = dispatch("cern", "REVERSE")["details"]
        self.assertTrue(all(record["available_date"] <= reverse["cutoff_date"] for record in reverse["available_at_cutoff"]))
        self.assertTrue(all(record["available_date"] > reverse["cutoff_date"] for record in reverse["later_evidence"]))
        window = dispatch("cern", "TRAVEL")["details"]["hypothetical_window"]
        durations = {stage["id"]: stage["duration"] for stage in window["stages"]}
        path = durations["approval"] + max(durations["survey"], durations["calibration"]) + sum(
            durations[stage] for stage in ("queue", "measurement", "verification")
        )
        self.assertEqual(path, window["critical_path_s"])
        self.assertEqual(window["deadline_s"] - path - window["required_safety_margin_s"], window["latest_safe_start_s"])
        self.assertEqual(cern_window(prerequisites_ready_s=5)["status"], "SYNTHETIC_WINDOW_OPEN")
        self.assertEqual(cern_window(prerequisites_ready_s=6)["status"], "SYNTHETIC_FALLBACK_REQUIRED")
        self.assertEqual(cern_window(queue_s=15)["latest_safe_start_s"], 0)
        self.assertEqual(cern_window(calibration_expires_s=89)["latest_safe_start_s"], -1)
        self.assertEqual(cern_window(clock_uncertainty_s=2)["latest_safe_start_s"], 3)

    def test_geology_boundary_and_local_ambiguity(self):
        younger, older = ("93.7", "94.1"), ("100.4", "100.6")
        self.assertEqual(age_membership("100.0", younger, older), "INSIDE_FOR_ALL_BOUNDS")
        self.assertEqual(age_membership("100.4", younger, older), "BOUNDARY_AMBIGUOUS")
        self.assertEqual(age_membership("101.0", younger, older), "OUTSIDE_FOR_ALL_BOUNDS")
        with self.assertRaises(ValueError):
            age_membership("100", older, younger)
        proof = dispatch("cenomanian", "CERTIFY")["details"]
        self.assertEqual(proof["local_flooding"], "AMBIGUOUS")
        self.assertEqual(dispatch("cenomanian", "PINPOINT")["details"]["possible_local_states"], ["DRY", "FLOODED"])

    def test_hypergeometric_probability_matches_small_exhaustive_reference(self):
        for population in range(1, 8):
            for retained in range(population + 1):
                rows = list(combinations(range(population), retained))
                for decisive in range(population + 1):
                    hits = sum(any(record < decisive for record in row) for row in rows)
                    self.assertEqual(retention_probability(population, retained, decisive), Fraction(hits, len(rows)))
        for invalid in ((0, 0, 0), (10, 11, 1), (10, 1, True), (900_000, 450_000, 450_000)):
            with self.assertRaises(ValueError):
                retention_probability(*invalid)

    def test_seeded_replay_keeps_supplied_run_separate(self):
        result = archive_experiment()
        self.assertEqual(result["recoveries"], 61)
        self.assertEqual(archive_experiment(seed=20261114)["recoveries"], 68)
        lo, hi = result["wilson_95_interval"]
        self.assertLess(lo, result["analytic_probability"])
        self.assertGreater(hi, result["analytic_probability"])
        details = dispatch("archive", "CERTIFY")["details"]
        self.assertEqual(details["supplied_only"]["random_hits"], 62)
        self.assertEqual(details["supplied_only"]["original_seed"], "UNKNOWN")
        self.assertEqual(details["targeted_removal"]["recoveries"], 0)

    def test_archive_failures_are_independent_of_the_claim_meaning(self):
        summary = archive_recovery(raw_record=False, summary=True, frame=True, calibration=True, trusted_key=True)
        self.assertEqual(summary["claim_meaning"], "BOUNDED_SUMMARY")
        self.assertFalse(summary["decisive_original_bytes_available"])
        self.assertEqual(summary["underlying_evidence"], "INCOMPLETE")
        missing_key = archive_recovery(raw_record=True, summary=False, frame=True, calibration=True, trusted_key=False)
        self.assertEqual(missing_key["original_provenance"], "UNRESOLVED")
        self.assertEqual(missing_key["underlying_evidence"], "AVAILABLE")
        missing_frame = archive_recovery(raw_record=True, summary=True, frame=False, calibration=True, trusted_key=True)
        self.assertEqual(missing_frame["claim_meaning"], "AMBIGUOUS")

    def test_archive_shards_recover_any_one_erasure_in_any_order(self):
        for payload in (b"1", b"\0\xff", b"odd\0\0", b"\0" * 255, bytes(range(256))):
            expected = sha256(payload).hexdigest()
            shards = split_archive(payload)
            for pair in combinations(shards, 2):
                self.assertEqual(restore_archive(tuple(reversed(pair)), expected_sha256=expected), payload)
            self.assertEqual(restore_archive(shards, expected_sha256=expected), payload)
            with self.assertRaises(ValueError):
                restore_archive(shards[:1], expected_sha256=expected)

    def test_archive_restore_rejects_forged_payloads_and_mixed_cohorts(self):
        payload = b"correlated evidence"
        trusted = sha256(payload).hexdigest()
        shards = split_archive(payload)
        impostor = split_archive(b"impostor")
        with self.assertRaises(ValueError):
            restore_archive(impostor, expected_sha256=trusted)
        with self.assertRaises(ValueError):
            restore_archive([shards[0], impostor[1]], expected_sha256=trusted)
        with self.assertRaises(ValueError):
            restore_archive([shards[0], shards[0]], expected_sha256=trusted)
        with self.assertRaises(ValueError):
            restore_archive(shards, expected_sha256=None)
        damaged = deepcopy(shards)
        damaged[0]["data_base64"] = "!" + damaged[0]["data_base64"][1:]
        with self.assertRaises(ValueError):
            restore_archive(damaged, expected_sha256=trusted)
        # Matching self-declared shard digest is insufficient when bytes differ.
        relabelled = split_archive(b"another payload.....")
        for shard in relabelled:
            shard["original_sha256"] = trusted
        with self.assertRaises(ValueError):
            restore_archive(relabelled, expected_sha256=trusted)

    def test_bipm_excerpt_replay_requires_the_complete_cohort(self):
        fixture = json.loads((FIXTURES / "bipm-rapid-utc-extract.json").read_bytes())
        self.assertEqual(replay_rapid_utc_extract(fixture), {
            "complete_laboratories": 59,
            "any_data_laboratories": 67,
            "changes_27_to_30_jun_ns": [-0.1, 0.1, -0.3],
            "bern_wabern_27_to_30_jun_ns": [0.8, 0.8, 0.9, 0.9],
        })
        corrupt = deepcopy(fixture)
        corrupt["labs"].append(corrupt["labs"][0])
        with self.assertRaises(ValueError):
            replay_rapid_utc_extract(corrupt)
        corrupt = deepcopy(fixture)
        bern = next(row for row in corrupt["labs"] if row["id"] == "CH")
        bern["offsets_tenths_ns"][0] = None
        with self.assertRaises(ValueError):
            replay_rapid_utc_extract(corrupt)

    def test_portable_evidence_fixture_matches_runtime(self):
        artifact = json.loads((FIXTURES / "evidence.json").read_bytes())
        self.assertEqual(artifact, fixture_document())
        self.assertEqual(tuple(artifact["cases"]), CASES)
        for case in CASES:
            for operation in OPERATIONS:
                self.assertEqual(artifact["cases"][case][operation]["operation"], operation)
        self.assertEqual(dispatch("supplied-studies", "CERTIFY")["status"], "UNRESOLVED")
        with self.assertRaises(ValueError):
            dispatch([], "CERTIFY")
        with self.assertRaises(ValueError):
            dispatch("cern", "execute")
