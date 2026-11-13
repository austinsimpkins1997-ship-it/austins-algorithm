"""Build the standalone Windows desktop executable in an app-local directory."""
from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from runpy import run_path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
NAME = "AustinsAlgorithmWorkbench"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    default_reader = ROOT / "future-reader"
    if not (default_reader / "future_reader" / "controller.py").is_file():
        default_reader = ROOT.parent / "future-reader"
    parser.add_argument("--future-reader", type=Path, default=default_reader)
    args = parser.parse_args()
    output = args.directory.resolve()
    if sys.platform != "win32" or not output.is_relative_to(ROOT / ".build"):
        parser.error("Use Windows and an output directory inside this package's .build directory.")
    output.mkdir(parents=True, exist_ok=False)
    package = ROOT / "src" / "austins_algorithm"
    notices = output / "notices"
    notices.mkdir()
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", notices / "python.txt")
    # The Windows CPython distribution carries the Tk license here.
    shutil.copy2(Path(sys.base_prefix) / "tcl" / "tk8.6" / "license.terms", notices / "tcl-tk.txt")
    # Keep build provenance and bootloader licensing with the binary.
    from importlib.metadata import distribution
    desktop_version = run_path(str(package / "workbench_release.py"))["DESKTOP_VERSION"]

    pyinstaller = distribution("pyinstaller")
    for entry in pyinstaller.files or ():
        if entry.name == "COPYING.txt" and "licenses" in entry.parts:
            shutil.copy2(pyinstaller.locate_file(entry), notices / "pyinstaller.txt")
            break
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--noupx", "--clean", "--windowed", "--onefile",
        "--name", NAME, "--paths", str(ROOT / "src"),
        "--distpath", str(output / "dist"), "--workpath", str(output / "work"), "--specpath", str(output),
        "--icon", str(package / "workbench_assets/app.ico"),
        "--add-data", f"{package / 'workbench_assets'}:austins_algorithm/workbench_assets",
        "--add-data", f"{notices}:austins_algorithm/workbench_assets/licenses/runtime",
    ]
    reader = args.future_reader.resolve()
    if not (reader / "future_reader" / "controller.py").is_file() or not (reader / "future-reader.html").is_file():
        parser.error("Provide the Future Reader source and generated offline companion with --future-reader.")
    command.extend(["--paths", str(reader), "--add-data",
                    f"{reader / 'future-reader.html'}:austins_algorithm/workbench_assets"])
    for module in ("__init__", "adapter_api", "local_adapter", "core", "constraints",
                   "classical_constraint", "grover_search", "optimization"):
        command.extend(["--add-data", f"{package / (module + '.py')}:austins_algorithm"])
    for optional in ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "numpy", "scipy", "matplotlib"):
        command.extend(["--exclude-module", optional])
    command.append(str(ROOT / "tools/workbench_entry.py"))
    with (output / "build.log").open("wb") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise SystemExit(f"Executable build failed. See {output / 'build.log'}")
    binary = output / "dist" / f"{NAME}.exe"
    with binary.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("Executable signature is missing")
    artifact = {"file": binary.name, "desktop_version": desktop_version,
                "bytes": binary.stat().st_size, "sha256": sha256(binary.read_bytes()).hexdigest(),
                "python": sys.version.split()[0], "pyinstaller": version("pyinstaller"),
                "future_reader_html_sha256": sha256((reader / "future-reader.html").read_bytes()).hexdigest()}
    (binary.parent / "build.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(binary)
    print(artifact["sha256"])


if __name__ == "__main__":
    main()
