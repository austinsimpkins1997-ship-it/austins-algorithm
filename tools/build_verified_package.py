"""Build an offline sdist, then build and verify a wheel from that exact archive.

This package-specific release check compares every current runtime file, including
resources.  It deliberately does not install, publish, or clean build directories.
"""

from __future__ import annotations

import argparse
import email.parser
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
from typing import Callable
import zipfile


_PACKAGE = "austins_algorithm"
_CRITICAL = ("README.md", "pyproject.toml", "MANIFEST.in")


def _fingerprint(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _file_fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular source file: {path}")


def _source_root(source: str | Path) -> Path:
    root = Path(source).resolve()
    if not root.is_dir() or not (root / "src" / _PACKAGE).is_dir():
        raise ValueError("source must contain src/austins_algorithm")
    return root


def _project(root: Path) -> dict:
    path = root / "pyproject.toml"
    _regular(path)
    text = path.read_text(encoding="utf-8")
    block = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", text)
    if block is None:
        raise ValueError("pyproject.toml must have a static [project] section")
    result = {}
    for field in ("name", "version"):
        values = re.findall(rf"(?m)^{field}\s*=\s*(['\"])([^'\"\n]+)\1\s*(?:#.*)?$", block.group(1))
        if len(values) != 1:
            raise ValueError(f"pyproject.toml must declare one static project {field}")
        result[field] = values[0][1]
    return result


def _runtime(root: Path) -> dict[str, bytes]:
    directory = root / "src" / _PACKAGE
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            raise ValueError(f"runtime symlinks are not supported: {path}")
        if path.is_file():
            result[f"{_PACKAGE}/{relative.as_posix()}"] = path.read_bytes()
    if not result:
        raise ValueError("source runtime inventory is empty")
    return result


def _excluded(name: str, pruned: list[str], patterns: list[str]) -> bool:
    return any(name == prefix or name.startswith(prefix + "/") for prefix in pruned) or any(
        fnmatch.fnmatchcase(part, pattern)
        for pattern in patterns for part in PurePosixPath(name).parts
    )


def _release_sources(root: Path, runtime: dict[str, bytes]) -> tuple[dict[str, bytes], list[str], list[str]]:
    """Interpret this package's four release-rule directives, without setuptools."""
    selected = set(_CRITICAL)
    pruned: list[str] = []
    excluded: list[str] = []
    for raw in (root / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        directive, *arguments = words
        if directive == "include" and arguments:
            for pattern in arguments:
                _safe_name(pattern)
                matches = [path for path in root.glob(pattern) if path.is_file()]
                if not matches and not any(char in pattern for char in "*?["):
                    raise ValueError(f"missing MANIFEST.in include: {pattern}")
                selected.update(path.relative_to(root).as_posix() for path in matches)
        elif directive == "recursive-include" and len(arguments) >= 2:
            directory, *patterns = arguments
            _safe_name(directory)
            for path in (root / directory).rglob("*"):
                if path.is_file() and any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns):
                    selected.add(path.relative_to(root).as_posix())
        elif directive == "prune" and len(arguments) == 1:
            pruned.append(_safe_name(arguments[0]))
        elif directive == "global-exclude" and arguments:
            excluded.extend(arguments)
        else:
            raise ValueError(f"unsupported MANIFEST.in release rule: {raw}")
    selected = {name for name in selected if not _excluded(name, pruned, excluded)}
    selected.update("src/" + name for name in runtime)
    if not set(_CRITICAL).issubset(selected):
        raise ValueError("MANIFEST.in excludes a critical package source")
    result = {}
    for name in sorted(selected):
        path = root / name
        _regular(path)
        result[name] = path.read_bytes()
    return result, pruned, excluded


def _safe_name(name: str) -> str:
    value = name.rstrip("/")
    parts = PurePosixPath(value).parts
    if not value or "\\" in value or ":" in value or value.startswith("/") or any(part in (".", "..") for part in parts):
        raise ValueError(f"unsafe archive or release path: {name!r}")
    if "/".join(parts) != value:
        raise ValueError(f"noncanonical archive or release path: {name!r}")
    return value


def _metadata(data: bytes, project: dict) -> None:
    message = email.parser.BytesParser().parsebytes(data)
    for key in ("name", "version"):
        values = message.get_all(key, [])
        if len(values) != 1:
            raise ValueError(f"package metadata must contain one {key}")
        actual, expected = values[0], project[key]
        if key == "name":
            actual = re.sub(r"[-_.]+", "-", actual).lower()
            expected = re.sub(r"[-_.]+", "-", expected).lower()
        if actual != expected:
            raise ValueError(f"package metadata {key} differs from source")


def _check_runtime(actual: dict[str, object], expected: dict[str, bytes], read: Callable) -> dict:
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(f"runtime file set differs: missing={missing}, extra={extra}")
    result = {}
    for name, data in expected.items():
        if read(actual[name], len(data)) != data:
            raise ValueError(f"runtime bytes differ from source: {name}")
        result[name] = _fingerprint(data)
    return result


def verify_sdist(source: str | Path, path: str | Path) -> dict:
    root, archive = _source_root(source), Path(path).resolve()
    project, runtime = _project(root), _runtime(root)
    required, pruned, excluded = _release_sources(root, runtime)
    with tarfile.open(archive, "r:gz") as stream:
        files = {}
        names = set()
        roots = set()
        for member in stream.getmembers():
            name = _safe_name(member.name)
            if name in names:
                raise ValueError(f"duplicate sdist member: {name}")
            names.add(name)
            parts = PurePosixPath(name).parts
            roots.add(parts[0])
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2:
                raise ValueError(f"unsupported sdist member: {name}")
            relative = "/".join(parts[1:])
            if _excluded(relative, pruned, excluded):
                raise ValueError(f"excluded build/cache file in sdist: {relative}")
            files[relative] = member
        if len(roots) != 1:
            raise ValueError("sdist must have one top-level directory")

        def read(member, size=None):
            if size is not None and member.size != size:
                raise ValueError(f"sdist file size differs from source: {member.name}")
            extracted = stream.extractfile(member)
            if extracted is None:
                raise ValueError(f"unreadable sdist file: {member.name}")
            return extracted.read() if size is None else extracted.read(size + 1)

        if "PKG-INFO" not in files:
            raise ValueError("sdist has no root PKG-INFO")
        _metadata(read(files["PKG-INFO"]), project)
        actual = {name[4:]: member for name, member in files.items() if name.startswith("src/" + _PACKAGE + "/")}
        fingerprints = _check_runtime(actual, runtime, read)
        source_files = {}
        for name, data in required.items():
            if name not in files or read(files[name], len(data)) != data:
                raise ValueError(f"sdist source bytes missing or different: {name}")
            source_files[name] = _fingerprint(data)
    return {"status": "SDIST_VERIFIED", "path": str(archive), **_file_fingerprint(archive),
            "project": project, "runtime_file_count": len(fingerprints), "runtime_files": fingerprints,
            "source_file_count": len(source_files), "source_files": source_files}


def verify_wheel(source: str | Path, path: str | Path) -> dict:
    root, archive = _source_root(source), Path(path).resolve()
    project, runtime = _project(root), _runtime(root)
    with zipfile.ZipFile(archive) as stream:
        files = {}
        names = set()
        for member in stream.infolist():
            name = _safe_name(member.filename)
            if name in names:
                raise ValueError(f"duplicate wheel member: {name}")
            names.add(name)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise ValueError(f"unsupported wheel member: {name}")
            if not member.is_dir():
                files[name] = member
        metadata = [member for name, member in files.items() if len(PurePosixPath(name).parts) == 2 and name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("wheel must contain one dist-info/METADATA")
        _metadata(stream.read(metadata[0]), project)

        def read(member, size):
            if member.file_size != size:
                raise ValueError(f"wheel file size differs from source: {member.filename}")
            return stream.read(member)

        actual = {name: member for name, member in files.items() if name.startswith(_PACKAGE + "/")}
        fingerprints = _check_runtime(actual, runtime, read)
    return {"status": "WHEEL_VERIFIED", "path": str(archive), **_file_fingerprint(archive),
            "project": project, "runtime_file_count": len(fingerprints), "runtime_files": fingerprints}


def _inside_build(root: Path, path: Path, label: str) -> None:
    if not path.is_relative_to(root / ".build") or path == root / ".build":
        raise ValueError(f"{label} must be inside the source .build directory")


def build_verified_package(source: str | Path, directory: str | Path, *, cache_dir: str | Path,
                           runner: Callable = subprocess.run) -> dict:
    root, output, cache = _source_root(source), Path(directory).resolve(), Path(cache_dir).resolve()
    project = _project(root)
    _release_sources(root, _runtime(root))
    _inside_build(root, output, "output")
    _inside_build(root, cache, "cache")
    if output.exists():
        raise FileExistsError("output directory already exists")
    if not output.parent.is_dir() or not cache.is_dir():
        raise ValueError("output parent and existing cache directory are required")
    if cache == output or cache.is_relative_to(output):
        raise ValueError("cache must be separate from output")
    output.mkdir()
    identities = {name: _file_fingerprint(root / name) for name in _CRITICAL}
    tool = root / "tools" / "build_verified_package.py"
    if tool.is_file():
        identities["tools/build_verified_package.py"] = _file_fingerprint(tool)
    report = {"schema_version": 1, "status": "PACKAGE_BUILD_FAILED", "source": str(root),
              "project": project, "python": {"executable": sys.executable, "version": sys.version},
              "cache_dir": str(cache), "source_inputs": identities, "stages": [], "artifacts": {}, "error": None}
    stage = "sdist"
    try:
        for stage in ("sdist", "wheel"):
            destination = output / stage
            destination.mkdir()
            source_argument = str(root) if stage == "sdist" else report["artifacts"]["sdist"]["path"]
            if stage == "wheel" and _file_fingerprint(Path(source_argument)) != {
                key: report["artifacts"]["sdist"][key] for key in ("bytes", "sha256")
            }:
                raise ValueError("verified sdist changed before wheel build")
            command = ["uv", "build", source_argument, "--" + stage, "--offline", "--no-python-downloads", "--no-create-gitignore",
                       "--python", sys.executable, "--cache-dir", str(cache), "--out-dir", str(destination)]
            stdout_path, stderr_path = output / f"{stage}.stdout.log", output / f"{stage}.stderr.log"
            row = {"stage": stage, "command": command, "stdout": str(stdout_path), "stderr": str(stderr_path), "returncode": None}
            report["stages"].append(row)
            try:
                with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                    completed = runner(command, cwd=root, stdout=stdout, stderr=stderr, check=False,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                row["returncode"] = completed.returncode
            finally:
                row["logs"] = {name: _file_fingerprint(path) for name, path in (
                    ("stdout", stdout_path), ("stderr", stderr_path)) if path.is_file()}
                row["produced_artifacts"] = [
                    {"path": str(path), **_file_fingerprint(path)}
                    for path in sorted(destination.iterdir()) if path.is_file() and not path.is_symlink()
                ]
            if completed.returncode != 0:
                raise RuntimeError(f"uv {stage} build failed with exit code {completed.returncode}")
            artifacts = list(destination.iterdir())
            suffix = ".tar.gz" if stage == "sdist" else ".whl"
            if len(artifacts) != 1 or not artifacts[0].name.endswith(suffix) or not artifacts[0].is_file():
                raise ValueError(f"expected exactly one {suffix} artifact")
            row["artifact"] = {"path": str(artifacts[0]), **_file_fingerprint(artifacts[0])}
            report["artifacts"][stage] = (verify_sdist if stage == "sdist" else verify_wheel)(root, artifacts[0])
        for name, identity in identities.items():
            if _file_fingerprint(root / name) != identity:
                raise ValueError(f"source input changed during build: {name}")
        report["status"] = "VERIFIED_PACKAGE_BUILT"
    except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        report["error"] = {"stage": stage, "type": type(error).__name__, "message": str(error)}
    finally:
        (output / "build-result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = build_verified_package(Path(__file__).resolve().parents[1], arguments.directory, cache_dir=arguments.cache_dir)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "VERIFIED_PACKAGE_BUILT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
