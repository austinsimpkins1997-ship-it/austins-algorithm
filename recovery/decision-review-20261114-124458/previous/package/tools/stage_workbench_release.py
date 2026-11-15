"""Verify a frozen Workbench and stage a commit-bound release."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path):
    return {"filename": path.name, "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def stage(build, tag, commit):
    version = runpy.run_path(str(ROOT / "src/austins_algorithm/workbench_release.py"))["DESKTOP_VERSION"]
    if tag != f"workbench-v{version}" or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Tag must match the Workbench version and identify a full commit")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if revision != commit:
        raise ValueError("Checked-out commit differs from the requested release")
    reader = ROOT / "future-reader"
    if not reader.is_dir():
        raise ValueError("A release checkout must contain the Future Reader source")
    output = ROOT / ".build" / "hosted-release"
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=False)
    smoke = output / "smoke"
    smoke.mkdir()
    executable = build / "dist" / "AustinsAlgorithmWorkbench.exe"
    companion = reader / "future-reader.html"
    receipt = json.loads((executable.parent / "build.json").read_text(encoding="utf-8"))
    if (receipt["sha256"] != fingerprint(executable)["sha256"]
            or receipt["future_reader_html_sha256"] != fingerprint(companion)["sha256"]):
        raise ValueError("Release inputs differ from the build receipt")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([str(executable), "--smoke", str(smoke)], cwd=smoke, check=True,
                   timeout=45, creationflags=flags)
    proof = json.loads((smoke / "desktop-smoke.json").read_text(encoding="utf-8"))
    if (not proof["frozen"] or proof["desktop_version"] != version
            or proof["future_reader"]["live_worker_generation"] != 2 or proof["exact_cost"] != 4):
        raise ValueError("The executable did not pass release acceptance")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    subprocess.run([sys.executable, "-B", "-m", "austins_algorithm", "workbench-export-verify",
                    str(smoke / "smoke-export.zip")], cwd=smoke, env=env, check=True, timeout=30)
    subprocess.run([sys.executable, "-B", "tools/build_handoff.py", "--commit", commit,
                    "--output", ".build/hosted-handoff"], cwd=reader, check=True, timeout=30)
    handoff = next((reader / ".build/hosted-handoff").glob("FutureReader-*.zip"))
    with ZipFile(handoff) as archive:
        if archive.testzip() is not None or archive.read("future-reader.html") != companion.read_bytes():
            raise ValueError("The portable handoff did not match the companion")
        reconstruction = output / "reconstruction"
        archive.extractall(reconstruction)
    subprocess.run([sys.executable, "-I", "-B", "tools/reconstruct.py", "."],
                   cwd=reconstruction, timeout=30, check=True)
    paths = [executable, companion, handoff]
    study = reader / "fixtures/synthetic-studies.json"
    if study.is_file():
        paths.append(study)
    for path in paths:
        shutil.copy2(path, assets / path.name)
    result = {"schema": "austin-workbench-release/1", "tag": tag, "commit": commit,
              "channel": "PRERELEASE", "executable_signature": "UNSIGNED",
              "compatibility": "Windows x64 executable. The standalone HTML opens in modern browsers.",
              "migration": "Existing weighted projects and exports use the same replay format.",
              "rollback": "Retain the previous executable and keep a portable checkpoint.",
              "verified": {"weighted_exact_cost": 4, "local_worker_replacement": True, "physical_qpu": False},
              "assets": sorted((fingerprint(path) for path in assets.iterdir()), key=lambda row: row["filename"])}
    (assets / "release-manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (output / "notes.md").write_text(
        f"# Austin's Algorithm Workbench {version}\n\n"
        "The Windows Workbench verifies local worker recovery using a finished practice job. "
        "The offline Future Reader and portable handoff include timing and pruning evidence. "
        "The IBM route supports named-profile preflight. Physical hardware evidence is pending.\n\n"
        "The Windows executable is unsigned. Verify release asset SHA-256 values against the included manifest. "
        "The manifest records the source commit and a completed local recovery check.\n",
        encoding="utf-8",
    )
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    stage(args.build.resolve(), args.tag, args.commit)
