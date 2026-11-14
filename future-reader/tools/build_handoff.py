"""Build a bounded offline archive of public Future Reader deliverables."""

from hashlib import sha256
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from future_reader import __version__
from future_reader.codec import digest
from future_reader.releases import manifest


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".build" / f"handoff-{__version__}")
    parser.add_argument("--commit", help="Full verified source commit for a release artifact")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".build") or output == ROOT / ".build":
        parser.error("Use a new output directory inside this project's .build folder.")
    output.mkdir(parents=True, exist_ok=False)
    source = ["future-reader.html", "MASTER_PROMPT.txt", "README.md", "CHANGELOG.md", "pyproject.toml",
              "tools/reconstruct.py"]
    for directory, extension in (("fixtures", "*"), ("schemas", "*.json"), ("docs", "*.md"),
                                 ("future_reader", "*.py")):
        source.extend(path.relative_to(ROOT).as_posix() for path in (ROOT / directory).glob(extension) if path.is_file())
    blobs = {}
    for name in sorted(set(source)):
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"Invalid handoff member {name}")
        blobs[name] = path.read_bytes()
    release = manifest(blobs["future-reader.html"], __version__, commit=args.commit)
    blobs["release-manifest.json"] = (json.dumps(release, indent=2) + "\n").encode("ascii")
    (output / "release-manifest.json").write_bytes(blobs["release-manifest.json"])
    index = {"schema": "future-reader.handoff/1", "release_manifest_pin": digest(release),
             "files": {name: {"bytes": len(raw), "sha256": sha256(raw).hexdigest()} for name, raw in blobs.items()}}
    index_raw = (json.dumps(index, indent=2) + "\n").encode("ascii")
    archive = output / f"FutureReader-{__version__}.zip"
    with ZipFile(archive, "x", ZIP_DEFLATED) as bundle:
        for name, raw in blobs.items():
            bundle.writestr(name, raw)
        bundle.writestr("handoff-manifest.json", index_raw)
    with ZipFile(archive) as bundle:
        for name, stamp in index["files"].items():
            assert sha256(bundle.read(name)).hexdigest() == stamp["sha256"]
    receipt = {"version": __version__, "archive": archive.name, "bytes": archive.stat().st_size,
               "sha256": sha256(archive.read_bytes()).hexdigest(), "entries": len(blobs) + 1,
               "release_manifest_pin": digest(release), "publisher_identity": "NOT_ESTABLISHED"}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
