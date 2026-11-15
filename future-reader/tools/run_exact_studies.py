"""Run or replay the bounded Future Reader exact-refinement studies."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
sys.path.insert(0, str(ROOT))

from future_reader.codec import digest, read_json
from future_reader.exact_grid import run_grid
from future_reader.exact_pruning import run_scale_study
from future_reader.exact_timing_study import run_timing


SCHEMA = "future-reader.exact-studies/1"
FILES = ("future_reader/codec.py", "future_reader/studies.py", "future_reader/exact_grid.py",
         "future_reader/exact_timing.py", "future_reader/exact_timing_study.py",
         "future_reader/exact_pruning.py", "tools/run_exact_studies.py")


def _checked_path(path):
    resolved = path.resolve()
    if resolved == BUILD.resolve() or not resolved.is_relative_to(BUILD.resolve()):
        raise ValueError("Use a report file inside this Future Reader copy's .build folder.")
    return resolved


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parameters(cases_per_seed, ranking_trials):
    if type(cases_per_seed) is not int or not 4 <= cases_per_seed <= 10_000 or cases_per_seed % 4:
        raise ValueError("Use 4 through 10,000 timing cases per seed, in a multiple of four.")
    if type(ranking_trials) is not int or not 1 <= ranking_trials <= 64:
        raise ValueError("Use 1 through 64 ranking trials per cell.")
    return {"cases_per_seed": cases_per_seed, "ranking_trials": ranking_trials}


def _compute(parameters):
    return {"schema": SCHEMA, "parameters": parameters, "original_grid": run_grid(),
            "adaptive_timing": run_timing(cases_per_seed=parameters["cases_per_seed"]),
            "exact_pruning": run_scale_study(trials=parameters["ranking_trials"])}


def _sources():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


def _record(parameters):
    started = _utc()
    content = _compute(parameters)
    return {"content": content, "content_sha256": digest(content), "source_sha256": _sources(),
            "environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "started_utc": started, "finished_utc": _utc()}}


def _verify(path):
    report = read_json(path)
    if (not isinstance(report, dict) or not isinstance(report.get("content"), dict)
            or report["content"].get("schema") != SCHEMA):
        raise ValueError("Not an exact-studies receipt.")
    content = report["content"]
    if report.get("content_sha256") != digest(content):
        raise ValueError("Receipt content digest does not match.")
    if report.get("source_sha256") != _sources():
        raise ValueError("Receipt source differs from this copy; replay using the pinned implementation.")
    raw_parameters = content.get("parameters")
    if not isinstance(raw_parameters, dict) or set(raw_parameters) != {"cases_per_seed", "ranking_trials"}:
        raise ValueError("Invalid study parameters.")
    parameters = _parameters(**raw_parameters)
    if digest(_compute(parameters)) != report["content_sha256"]:
        raise ValueError("Receipt results do not match independently checked replay.")
    return {"status": "VERIFIED_REPLAY", "content_sha256": report["content_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-per-seed", type=int, help="Generated timing cases per seed (default: 2048)")
    parser.add_argument("--ranking-trials", type=int, help="Ranking cases per study cell (default: 8)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--output", type=Path)
    action.add_argument("--verify", type=Path)
    args = parser.parse_args()
    try:
        if args.verify is not None:
            if args.cases_per_seed is not None or args.ranking_trials is not None:
                parser.error("Verification uses the recorded parameters.")
            print(json.dumps(_verify(_checked_path(args.verify))))
        else:
            path = None if args.output is None else _checked_path(args.output)
            if path is not None and (path.exists() or not path.parent.is_dir()):
                raise ValueError("Use a new filename in an existing .build directory.")
            parameters = _parameters(2048 if args.cases_per_seed is None else args.cases_per_seed,
                                     8 if args.ranking_trials is None else args.ranking_trials)
            report = _record(parameters)
            rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
            if path is None:
                print(rendered, end="")
            else:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(rendered)
                print(json.dumps({"status": "RECORDED", "path": str(path),
                                  "content_sha256": report["content_sha256"]}))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
