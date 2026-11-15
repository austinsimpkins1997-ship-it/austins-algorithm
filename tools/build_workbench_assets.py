"""Rasterize the pinned Lucide icons for native Tk using build-only tools."""
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageDraw
from resvg_py import svg_to_bytes


ROOT = Path(__file__).resolve().parents[1]
REVISION = "55782beff3a0133df79c417eb68f96e8f19e2404"
UPSTREAM = f"https://raw.githubusercontent.com/lucide-icons/lucide/{REVISION}"
ASSETS = ROOT / "src/austins_algorithm/workbench_assets"
ICONS = ("play", "save", "folder-open", "download", "chevron-left", "chevron-right", "boxes", "circle-help")


def download(path: str) -> bytes:
    with urlopen(f"{UPSTREAM}/{path}", timeout=20) as response:
        raw = response.read(32769)
    if len(raw) > 32768:
        raise ValueError(f"Unexpected asset size for {path}")
    return raw


def main() -> None:
    (ASSETS / "icons").mkdir(parents=True, exist_ok=True)
    (ASSETS / "licenses").mkdir(exist_ok=True)
    provenance = {"source": "https://github.com/lucide-icons/lucide", "revision": REVISION, "icons": {}}
    names = tuple("circle-question-mark" if name == "circle-help" else name for name in ICONS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(download, (f"icons/{name}.svg" for name in names)))
    license_data = download("LICENSE")
    for name, upstream_name, raw in zip(ICONS, names, sources):
        svg = raw.decode("utf-8")
        (ASSETS / "icons" / f"{name}.svg").write_bytes(raw)
        for suffix, color in (("", "#ecf6ff"), ("-dark", "#123b39")):
            rendered = svg.replace("currentColor", color).replace('width="24"', 'width="18"').replace('height="24"', 'height="18"')
            (ASSETS / "icons" / f"{name}{suffix}.png").write_bytes(svg_to_bytes(svg_string=rendered))
        provenance["icons"][name] = {"upstream_name": upstream_name, "sha256": sha256(raw).hexdigest()}
    (ASSETS / "licenses/lucide.txt").write_bytes(license_data)
    (ASSETS / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 255, 255), radius=48, fill="#10232e")
    for left, top, color in ((53, 53, "#43d8ae"), (137, 53, "#317581"),
                             (53, 137, "#317581"), (137, 137, "#bce9e0")):
        draw.rounded_rectangle((left, top, left + 66, top + 66), radius=13, fill=color)
    image.save(ASSETS / "app.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
