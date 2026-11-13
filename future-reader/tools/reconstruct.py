"""Read public portable records and independently reconstruct their core claims."""

import argparse
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath


def reconstruct(root):
    root = root.resolve()
    fixtures = root / "fixtures"

    def read(name):
        with (fixtures / name).open("rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("Portable public fixture exceeds its size limit")
        return json.loads(raw)

    with (root / "handoff-manifest.json").open("rb") as stream:
        index = json.loads(stream.read(64 * 1024 + 1))
    if index["schema"] != "future-reader.handoff/1" or not 1 <= len(index["files"]) <= 128:
        raise ValueError("Unsupported portable manifest")
    for name, entry in index["files"].items():
        key = PurePosixPath(name)
        path = root.joinpath(*key.parts)
        if key.is_absolute() or ".." in key.parts or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe portable manifest path")
        with path.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or len(raw) != entry["bytes"] or sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Portable artifact differs from its manifest: {name}")
    required = {"MASTER_PROMPT.txt", "fixtures/brief-opportunity.txt", "fixtures/brief-runtime.txt",
                "fixtures/evidence.json", "fixtures/temporal.json"}
    if not required <= index["files"].keys():
        raise ValueError("The portable manifest omits core evidence")
    source = read("evidence.json")
    cases = source["cases"]
    delay = Fraction("11.42") * (1 / Fraction("0.996") - 1) / 299792458
    extra = delay * 299792458
    context = read("temporal.json")
    before = [row["id"] for row in context["views"]["as_known_then"]["claims"]]
    after = [row["id"] for row in context["views"]["as_understood_now"]["claims"]]
    if before != ["sensor-v1"] or "sensor-v2" not in after:
        raise ValueError("Portable temporal fixture lost its distinguishing revision")
    for case in ("cern", "cenomanian", "archive"):
        if not all(cases[case][operation]["limits"] for operation in ("PINPOINT", "TRAVEL", "REVERSE", "CERTIFY")):
            raise ValueError("A portable case lost its evidence limits")
    actual = {name: index["files"][name]["sha256"] for name in sorted(required)}
    return {"schema": "future-reader.reconstruction/1", "runtime": "standalone standard-library reference",
            "sources_sha256": actual, "historical_record": {"before": before, "after": after},
            "cases": {
                "cern": {"mathematical_equivalence": "EXACTLY_RECOVERED", "physical_explanation": "AMBIGUOUS",
                         "delay_seconds": str(delay), "additional_path_metres": str(extra)},
                "cenomanian": {"stage": "BOUNDED", "local_surface": "AMBIGUOUS",
                               "scope": cases["cenomanian"]["PINPOINT"]["summary"]},
                "archive": {"synthetic_replay": "BOUNDED", "missing_original_evidence": "UNRECOVERABLE_FROM_SUMMARY_ALONE",
                            "scope": cases["archive"]["CERTIFY"]["summary"]},
            }}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(reconstruct(args.root), indent=2, ensure_ascii=True))
