"""Assemble one portable HTML companion with the evaluated Python fixtures."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools" / "companion"
OUTPUT = ROOT / "future-reader.html"


def script_data(value: object) -> str:
    """Keep JSON inert even when the original data contains closing tags."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def build() -> bytes:
    prompt = (ROOT / "MASTER_PROMPT.txt").read_bytes()
    fixtures = {
        name: json.loads((ROOT / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("operational", "evidence", "temporal", "release-preview", "decisions")
    }
    supplements = {}
    for key, title in (("opportunity", "Opportunity and distribution"),
                       ("runtime", "Runtime and app integration")):
        raw = (ROOT / "fixtures" / f"brief-{key}.txt").read_bytes()
        supplements[key] = {"title": title, "filename": f"future-reader-{key}-brief.txt",
                            "text": raw.decode("utf-8"), "sha256": sha256(raw).hexdigest()}
    pieces = {
        "STYLE": (SOURCE / "theme.css").read_text(encoding="utf-8"),
        "SCRIPT": (SOURCE / "app.js").read_text(encoding="utf-8"),
        "DATA": script_data({**fixtures, "prompt": prompt.decode("utf-8"),
                             "prompt_sha256": sha256(prompt).hexdigest(), "supplements": supplements}),
    }
    page = (SOURCE / "companion.html").read_text(encoding="utf-8")
    for marker, content in pieces.items():
        key = "{{" + marker + "}}"
        if page.count(key) != 1:
            raise ValueError(f"Expected exactly one {marker} marker")
        page = page.replace(key, content)
    return page.encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the generated file without rewriting it.")
    args = parser.parse_args()
    expected = build()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != expected:
            parser.exit(1, "The standalone companion differs from its inputs. Run tools/build_companion.py.\n")
        return
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".future-reader-", suffix=".html", delete=False) as file:
        file.write(expected)
    temporary = Path(file.name)
    try:
        temporary.replace(OUTPUT)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
