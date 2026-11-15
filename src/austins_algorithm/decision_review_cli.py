"""Inspect a Decision Review case or verify its portable evidence."""

import argparse
import json
from pathlib import Path

from .decision_review import load_decision_document, offline_service_example, review_decision
from .decision_review_policy import PreparedDecisions, benchmark_prepared, prepare_policy
from .decision_review_project import (
    MAX_FILE, _read_json, export_review_project, load_review_project,
    new_review_project, verify_review_export,
)
from .decision_review_receipts import load_receipt


def main(argv=None):
    parser = argparse.ArgumentParser(prog="austins-algorithm decision-review", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("example", help="Print an editable synthetic case")
    review = commands.add_parser("review", help="Review the example or a chosen case")
    review.add_argument("case", nargs="?", type=Path)
    export = commands.add_parser("export", help="Package the example or a saved review project")
    export.add_argument("archive", type=Path)
    export.add_argument("--project", type=Path)
    verify = commands.add_parser("verify", help="Replay a ZIP against the installed verifier")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--manifest-sha256")
    prepare = commands.add_parser("prepare", help="Prepare replay-verified cases for repeated decisions")
    prepare.add_argument("case", nargs="*", type=Path)
    policy = commands.add_parser("verify-policy", help="Replay a prepared bundle against an expected digest")
    policy.add_argument("file", type=Path)
    policy.add_argument("--policy-sha256", required=True)
    bench = commands.add_parser("benchmark", help="Compare prepared and fresh local decisions")
    bench.add_argument("--repeats", type=int, default=32)
    receipts = commands.add_parser("receipts", help="Review a pair of pinned synthetic pilot receipts")
    receipts.add_argument("rehearsal", type=Path)
    receipts.add_argument("recovery", type=Path)
    receipts.add_argument("--rehearsal-sha256", required=True, help="expected pin from the trusted rehearsal producer")
    receipts.add_argument("--recovery-sha256", required=True, help="expected pin from the trusted recovery producer")
    args = parser.parse_args(argv)
    try:
        if args.command == "example":
            result = offline_service_example()
        elif args.command == "review":
            result = review_decision(load_decision_document(args.case) if args.case else offline_service_example())
        elif args.command == "export":
            project = load_review_project(args.project) if args.project else new_review_project()
            result = export_review_project(args.archive, project)
        elif args.command == "verify":
            result = verify_review_export(args.archive, manifest_sha256=args.manifest_sha256)
        elif args.command == "prepare":
            cases = {str(path): load_decision_document(path) for path in args.case}
            if len(cases) != len(args.case):
                raise ValueError("Use distinct paths for policy cases.")
            result = prepare_policy(cases or {"offline service": offline_service_example()})
        elif args.command == "verify-policy":
            bundle = _read_json(args.file, MAX_FILE, "prepared policy")
            checked = PreparedDecisions(bundle, args.policy_sha256)
            result = {"verified": True, "policy_sha256": checked.policy_sha256, "cases": len(bundle["cases"])}
        elif args.command == "receipts":
            result = {
                "scope": "SYNTHETIC_REVIEW_ONLY",
                "rehearsal": load_receipt(args.rehearsal, expected_sha256=args.rehearsal_sha256, kind="rehearsal"),
                "recovery": load_receipt(args.recovery, expected_sha256=args.recovery_sha256, kind="recovery"),
                "action_authority": False,
            }
        else:
            result = benchmark_prepared({"offline service": offline_service_example()}, repeats=args.repeats)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0
