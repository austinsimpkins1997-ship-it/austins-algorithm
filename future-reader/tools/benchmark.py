"""Compare local practice workflows with equivalent results and bounded trials."""

import argparse
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import platform
import sqlite3
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter_ns


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from future_reader.adapters import austin
from future_reader.controller import Controller, execute_program, opportunity, routes, search_slice, verify_checkpoint


PROGRAM = {
    "schema": "future-reader.program/1",
    "steps": [
        {"op": "Start", "state": "S"},
        {"op": "If", "condition": "independent_check", "then": [{"op": "Move", "to": "A"}],
         "else": [{"op": "Move", "to": "B"}]},
        {"op": "Repeat", "times": 1, "steps": [{"op": "Move", "to": "G"}]},
        {"op": "Goal", "state": "G"},
    ],
}
COLD = (
    "import json; from future_reader.controller import opportunity; "
    "print(json.dumps(opportunity(), allow_nan=False))"
)


def sample(action, accept, trials):
    samples = []
    for _ in range(trials):
        start = perf_counter_ns()
        result = action()
        samples.append(perf_counter_ns() - start)
        if not accept(result):
            raise RuntimeError("Benchmark path returned a different observable result.")
    ordered = sorted(samples)
    return {"trials": trials, "p50_ms": round(median(ordered) / 1e6, 6),
            "p95_ms": round(ordered[ceil(trials * 0.95) - 1] / 1e6, 6),
            "sample_min_ms": round(ordered[0] / 1e6, 6), "sample_max_ms": round(ordered[-1] / 1e6, 6)}


def cold_opportunity():
    completed = subprocess.run([sys.executable, "-B", "-c", COLD], cwd=ROOT, capture_output=True,
                               timeout=15, check=True)
    return json.loads(completed.stdout)


def benchmark(trials, work):
    expected = opportunity()
    all_routes = routes()
    saved_frontier = json.dumps(search_slice(budget=2)["checkpoint"])
    branching_expected = [
        next(row for row in routes(check_available=available) if row["path"][1] == branch)
        for available, branch in ((True, "A"), (False, "B"))
    ]
    database = work / "resume.sqlite3"
    with Controller(database) as session:
        session.set_grant("operator", True)
        preview = session.travel("operator")
        session.commit("operator", preview["preview_id"])
        checkpoint = session.checkpoint()
    persisted_pin = checkpoint["sha256"]
    (work / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    def resume():
        bundle = json.loads((work / "checkpoint.json").read_bytes())
        with Controller(database) as session:
            return verify_checkpoint(bundle, expected_sha256=persisted_pin) and session.pinpoint() == bundle["content"]["state"]

    results = {
        "cold_process_to_opportunity": sample(cold_opportunity, lambda result: result == expected, min(trials, 30)),
        "warm_opportunity": sample(opportunity, lambda result: result == expected, trials),
        "direct_complete_frontier": sample(routes, lambda result: result == all_routes, trials),
        "resume_saved_frontier_to_completion": sample(
            lambda: search_slice(budget=16, checkpoint=json.loads(saved_frontier)),
            lambda result: result["status"] == "EXHAUSTED" and result["routes"] == all_routes, trials),
        "resume_database_and_verify_checkpoint": sample(resume, lambda result: result is True, trials),
        "branching_program_both_contexts": sample(
            lambda: [execute_program(PROGRAM, check_available=value) for value in (True, False)],
            lambda result: result == branching_expected, trials),
    }
    binding = austin()
    if binding["status"] == "LOCAL_EQUIVALENCE_VERIFIED":
        for now in range(8):
            for available in (True, False):
                direct = opportunity(now, available)["recommended"]
                adapted = austin(now=now, check_available=available)
                if direct is None and adapted["status"] == "NO_ELIGIBLE_PATH":
                    continue
                if (direct is None or adapted["status"] != "LOCAL_EQUIVALENCE_VERIFIED"
                        or direct["cost"] != adapted["direct_cost"] or direct["path"] not in adapted["paths"]):
                    raise RuntimeError("Direct and Austin adapter route choices disagree.")
        results["warm_austin_exact_adapter"] = sample(austin, lambda result: result == binding, trials)
    return {
        "schema": "future-reader.benchmark/1",
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "sqlite": sqlite3.sqlite_version},
        "inputs": {"scope": "Bundled four-state graph", "available_check": True,
                   "observed_first_useful_result": "First complete recommendation and opportunity state",
                   "controller_sha256": sha256((ROOT / "future_reader/controller.py").read_bytes()).hexdigest(),
                   "adapter_sha256": sha256((ROOT / "future_reader/adapters.py").read_bytes()).hexdigest(),
                   "frontier_cursor": json.loads(saved_frontier)["expanded"]},
        "measurement": {"clock": "perf_counter_ns", "p95": "nearest rank", "warm_trials": trials,
                        "cold_process_trials": min(trials, 30),
                        "includes": "Cold path includes child process startup, imports, JSON transport and parsing. Checkpoint path includes file read, SQLite reopen and verification.",
                        "limits": "One local host and fixed small graph. OS file cache retained. No UI latency, energy, concurrency or network measurement."},
        "austin_binding": {"status": binding["status"],
                           "equivalent_contexts": 16 if binding["status"] == "LOCAL_EQUIVALENCE_VERIFIED" else 0},
        "timings": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.trials <= 1_000:
        parser.error("Use between 1 and 1,000 trials.")
    directory = ROOT / ".build"
    directory.mkdir(exist_ok=True)
    if arguments.output is not None and not arguments.output.resolve().is_relative_to(directory.resolve()):
        parser.error("The output path must be inside this project's .build folder.")
    with tempfile.TemporaryDirectory(prefix="benchmark-", dir=directory) as temporary:
        report = benchmark(arguments.trials, Path(temporary))
    rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        with arguments.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(rendered)


if __name__ == "__main__":
    main()
