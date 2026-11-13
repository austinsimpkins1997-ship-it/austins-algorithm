"""Command line entry point for weighted constraint optimization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .constraints import _unique_object
from .optimization import SearchSession, exact_minimum, preview, problem_from_document


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="austins-algorithm optimize", description=__doc__)
    parser.add_argument("problem", type=Path, help="UTF-8 weighted problem JSON")
    parser.add_argument("--mode", choices=("exact", "simulate", "preview"), default="simulate")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--shots", type=int, default=16)
    parser.add_argument("--queries", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=None, help="rounds in preview or a fixed count per shot")
    parser.add_argument("--max-rounds", type=int, default=4, help="maximum rounds per shot")
    parser.add_argument("--no-proof", action="store_true", help="omit the exact reference in simulation mode")
    parser.add_argument("--incumbent", type=int, help="mark costs below this value in preview mode")
    args = parser.parse_args(argv)
    if args.mode == "exact" and args.rounds is not None:
        parser.error("--rounds is used with preview or simulation")
    try:
        with args.problem.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Weighted problem JSON exceeds 16 KiB")
        problem = problem_from_document(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object))
        if args.mode != "preview" and args.incumbent is not None:
            raise ValueError("--incumbent requires preview mode")
        if args.mode == "exact":
            report = exact_minimum(problem)
        elif args.mode == "preview":
            report = preview(problem, incumbent=args.incumbent, rounds=1 if args.rounds is None else args.rounds)
        else:
            session = SearchSession(problem, seed=args.seed, max_shots=args.shots, max_queries=args.queries,
                                    max_rounds=args.max_rounds, prove=not args.no_proof)
            if args.rounds is None:
                session.run()
            else:
                if not 0 <= args.rounds <= args.max_rounds:
                    raise ValueError("--rounds must be between zero and --max-rounds")
                while session.status == "RUNNING" and session.queries_remaining >= args.rounds:
                    session.measure(args.rounds)
            report = session.report()
            if report["status"] == "RUNNING":
                report["status"] = "BUDGET_EXHAUSTED"
                report["termination"] = "FIXED_ROUND_QUERY_LIMIT"
    except (ValueError, OSError, RuntimeError, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))
