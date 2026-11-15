"""Measure local end-to-end calls to exact ranking on fixed synthetic inputs."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
from random import Random
from statistics import median
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from future_reader.codec import digest
from future_reader.exact_pruning import POLICIES, _sample, rank


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.output.resolve() if args.output else None
    if path and (not path.is_relative_to((ROOT / ".build").resolve()) or path.exists() or not path.parent.is_dir()):
        parser.error("Use a new filename in an existing .build directory.")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cells = []
    for count in (8, 32, 128, 1024):
        rng = Random(20261115 ^ count)
        raw = [_sample(rng, count, adversarial=bool(i % 2), trial=i) for i in range(8)]
        for slack in (0, 30, 200):
            rows = [[{**route, "bound_slack": slack} for route in row] for row in raw]
            for case in rows:
                choices = [(r["score"], r["winners"]) for p in POLICIES
                           for r in (rank(case, deadline=20, policy=p),)]
                if any(choice != choices[0] for choice in choices):
                    raise RuntimeError("Benchmarked choice differs from exhaustive ranking.")
            samples = {p: [] for p in POLICIES}
            for i in range(32):
                order = POLICIES[i % 3:] + POLICIES[:i % 3]
                for policy in order:
                    before = perf_counter_ns()
                    rank(rows[i % len(rows)], deadline=20, policy=policy)
                    samples[policy].append(perf_counter_ns() - before)
            cells.append({"routes": count, "slack": slack, "inputs_sha256": digest(rows),
                          "matched_cases": len(rows),
                          "policies": {name: {"p50_ns": median(values), "p95_ns": sorted(values)[30],
                                             "samples_ns": values}
                                       for name, values in samples.items()}})
    source_names = ("tools/benchmark_exact_ranking.py", "future_reader/exact_pruning.py", "future_reader/codec.py")
    report = {"schema": "future-reader.exact-ranking-timing/1", "mode": "LOCAL_SYNTHETIC",
              "cases_per_cell": 8, "samples_per_policy": 32, "p95_rule": "nearest rank",
              "included": "rank() validation, sorting where applicable, score evaluation, and result construction",
              "excluded": "interpreter startup, workload generation, disk I/O and independent oracle replay",
              "machine": {"python": platform.python_version(), "os": platform.platform(),
                          "timer": "perf_counter_ns", "cpu_architecture": platform.machine()},
              "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "source_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in source_names},
              "cells": cells}
    contents = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if path:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
    else:
        print(contents, end="")


if __name__ == "__main__":
    main()
