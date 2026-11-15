"""Run the bounded timing horizon and pruning experiments."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from future_reader.studies import run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=125)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--pruning-trials", type=int, default=10_000)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    build = ROOT / ".build"
    if arguments.output is not None and not arguments.output.resolve().is_relative_to(build.resolve()):
        parser.error("Place the output inside the local .build folder.")
    if (not 1 <= arguments.repetitions <= 500 or not 0 <= arguments.seed < 2**64
            or not 1 <= arguments.pruning_trials <= 100_000):
        parser.error("Use 1–500 repetitions, an unsigned 64-bit seed, and 1–100,000 pruning trials.")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = run_study(repetitions=arguments.repetitions, seed=arguments.seed, pruning_trials=arguments.pruning_trials)
    report["receipt"] = {
        "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(), "platform": platform.platform(),
        "source_sha256": sha256((ROOT / "future_reader/studies.py").read_bytes()).hexdigest(),
        "parameters": {"repetitions": arguments.repetitions, "seed": arguments.seed, "pruning_trials": arguments.pruning_trials},
    }
    rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        with arguments.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(rendered)


if __name__ == "__main__":
    main()
