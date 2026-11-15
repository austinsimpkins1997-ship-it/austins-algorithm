"""Receipt tests use portable synthetic data and no sibling checkouts."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from austins_algorithm.decision_review_receipts import (
    MAX_RECEIPT_BYTES,
    load_receipt,
    receipt_sha256,
    verify_receipt,
)


def _proposal(required, candidates, ties, *, exclusive, uncovered=()):
    return {
        "candidate_basis": "already eligible local fixture candidates",
        "objective": "minimum number of selected candidate records",
        "required": required,
        "candidates": candidates,
        "candidate_sha256": receipt_sha256({"required": required, "candidates": candidates, "exclusive": exclusive}),
        "action_authority": False,
        "status": "PROPOSED" if ties else "HELD",
        "selected": ties[0] if ties else [],
        "equally_small": ties,
        "uncovered": list(uncovered),
        "hold_reason": ("REQUIRED_COVERAGE_NOT_OBSERVED" if uncovered else
                        "NO_COMPATIBLE_CANDIDATE_SET" if not ties else None),
        "direct_check": None if uncovered else {
            "minimum_count": len(ties[0]) if ties else None,
            "all_minimum_choices_matched": True,
        },
        "austin": None if uncovered else {
            "version": "0.1.0", "assignments_checked": 2 ** len(candidates),
            "source_files_sha256": {"optimization.py": "d" * 64},
        },
    }


def sample_rehearsal():
    required = ["independent-stop", "restart-hold"]
    candidates = [
        {"id": "separate-stop", "covers": ["independent-stop"], "guarded_evidence_sha256": "a" * 64},
        {"id": "cascade:joint-check", "covers": required, "guarded_evidence_sha256": "b" * 64},
    ]
    return {
        "schema": "decision-containment-rehearsal-plan/1", "scope": "SYNTHETIC_REVIEW_ONLY",
        "action_authority": False, "source_comparison_sha256": "e" * 64, "cascade_comparison_sha256": "f" * 64,
        "other_fixture_cases": ["delayed-canary"],
        "rehearsal": _proposal(required, candidates, [["cascade:joint-check"]], exclusive=False),
    }


def sample_recovery():
    def option(mode, *, interlocks=(), ingress=0):
        return {
            "mode": mode, "result_mode": "SAFE_STOP" if interlocks else mode,
            "state": "HALT_AUTOMATED_FAILOVER" if interlocks else "AWAITING_OPERATOR_APPROVAL",
            "ingress_cap_rps": ingress, "verified_surviving_rps": 250,
            "interlocks": list(interlocks), "prerequisites": ["Verify independent optical path"],
            "sector_prerequisites": {"ledger": "Verify reconciliation"}, "rollback_steps": ["Set ingress to zero"],
            "preview_sha256": "a" * 64,
        }

    metered = option("METERED_ESSENTIAL_FAILOVER", ingress=180)
    regional = option("REGIONAL_ISOLATION")
    options = [option("UNRESTRICTED_FAILOVER", interlocks=("INSUFFICIENT_SURVIVING_CAPACITY",)),
               metered, regional, option("SAFE_STOP")]
    candidates = [
        {"id": metered["mode"], "covers": ["essential-service"], "preview": metered},
        {"id": regional["mode"], "covers": ["regional-isolation"], "preview": regional},
    ]
    return {
        "schema": "chronorealm-eligible-recovery-plan/1", "scope": "SYNTHETIC_REVIEW_ONLY",
        "action_authority": False, "as_of_s": 182, "tabletop_sha256": "b" * 64,
        "scenario": {"scenario": "primary-link", "status": "MODELED", "node_id": "primary",
                     "observation": "Primary path signal loss", "shared_asset": None},
        "existing_recovery": {"status": "AWAITING_OPERATOR_APPROVAL", "fresh_probe_signals": ["backup_capacity"],
                              "available_modes": [metered["mode"], regional["mode"]]},
        "existing_options": options, "review_bundles": [metered],
        "proposal": _proposal(["essential-service"], candidates, [[metered["mode"]]], exclusive=True),
    }


class ReceiptVerificationTests(unittest.TestCase):
    def test_regular_file_round_trip_and_expected_pin(self):
        rehearsal = sample_rehearsal()
        pin = receipt_sha256(rehearsal)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rehearsal.json"
            path.write_text(json.dumps(rehearsal, indent=2), encoding="utf-8-sig")
            result = load_receipt(path, expected_sha256=pin.upper(), kind="rehearsal")
            self.assertEqual(result["selected"], ["cascade:joint-check"])
            self.assertEqual(result["required"], ["independent-stop", "restart-hold"])
            self.assertEqual(result["minimum_count"], 1)
            self.assertEqual(result["other_fixture_cases"], ["delayed-canary"])
            self.assertFalse(result["action_authority"])
            self.assertTrue(result["verification"]["coverage_replay_matched"])
            rehearsal["other_fixture_cases"].append("added")
            path.write_text(json.dumps(rehearsal), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected pin"):
                load_receipt(path, expected_sha256=pin)
            original = json.dumps(sample_rehearsal())
            path.write_text(original[:-1] + ', "scope": "SYNTHETIC_REVIEW_ONLY"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
                load_receipt(path, expected_sha256=pin)
            path.write_bytes(b" " * (MAX_RECEIPT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size limit"):
                load_receipt(path, expected_sha256=pin)

    def test_rehashed_coverage_tamper_and_authority_are_rejected(self):
        rehearsal = sample_rehearsal()
        rehearsal["rehearsal"]["selected"] = ["separate-stop"]
        with self.assertRaisesRegex(ValueError, "coverage replay"):
            verify_receipt(rehearsal, expected_sha256=receipt_sha256(rehearsal))
        rehearsal = sample_rehearsal()
        rehearsal["action_authority"] = 0
        with self.assertRaisesRegex(ValueError, "read-only"):
            verify_receipt(rehearsal, expected_sha256=receipt_sha256(rehearsal))

    def test_recovery_retains_approval_interlocks_and_rollback(self):
        document = sample_recovery()
        result = verify_receipt(document, expected_sha256=receipt_sha256(document), kind="recovery")
        self.assertEqual(result["existing_options"][0]["interlocks"], ["INSUFFICIENT_SURVIVING_CAPACITY"])
        self.assertEqual(result["review_bundles"][0]["ingress_cap_rps"], 180)
        self.assertEqual(result["review_bundles"][0]["state"], "AWAITING_OPERATOR_APPROVAL")
        self.assertEqual(result["review_bundles"][0]["rollback_steps"], ["Set ingress to zero"])
        self.assertFalse(result["action_authority"])
        # A displayed option must match the owning preview, even when re-pinned.
        modified = deepcopy(document)
        modified["review_bundles"] = [deepcopy(modified["review_bundles"][0])]
        modified["review_bundles"][0]["ingress_cap_rps"] = 200
        with self.assertRaisesRegex(ValueError, "verified selection"):
            verify_receipt(modified, expected_sha256=receipt_sha256(modified))

    def test_held_recovery_retains_unknown_capacity_and_uncovered_requirement(self):
        document = sample_recovery()
        metered = document["existing_options"][1]
        metered.update(ingress_cap_rps=0, verified_surviving_rps=None, state="HALT_AUTOMATED_FAILOVER",
                       result_mode="SAFE_STOP", interlocks=["EVIDENCE_PAIR_FAILED_SHARED_ASSET"])
        document["existing_recovery"]["available_modes"].remove(metered["mode"])
        document["proposal"] = _proposal(
            ["essential-service"], document["proposal"]["candidates"][1:], [],
            exclusive=True, uncovered=["essential-service"],
        )
        document["review_bundles"] = []
        result = verify_receipt(document, expected_sha256=receipt_sha256(document))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["uncovered"], ["essential-service"])
        self.assertEqual(result["review_bundles"], [])
        self.assertIsNone(result["existing_options"][1]["verified_surviving_rps"])
