"""Check all runtime bytes before building a wheel from a verified source archive."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
import warnings
from zipfile import ZipFile


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
sys.path.insert(0, str(PACKAGE / "tools"))

import build_verified_package as package_build


_NAME = "chronorealm-austins-algorithm"
_VERSION = "0.1.0"
_ARCHIVE_ROOT = "chronorealm_austins_algorithm-0.1.0"
_DIST_INFO = "chronorealm_austins_algorithm-0.1.0.dist-info"


def _metadata(*, name=_NAME, version=_VERSION):
    return f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode("utf-8")


def _fingerprint(raw):
    return {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}


class VerifiedPackageBuildTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="verified-package-test-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        (self.source / ".build").mkdir()
        self.output = self.source / ".build" / "verified"
        self.cache = self.source / ".build" / "uv-cache"
        self.cache.mkdir()
        self.source_files = {
            "pyproject.toml": (
                '[build-system]\nrequires = ["setuptools>=77"]\nbuild-backend = "setuptools.build_meta"\n'
                '[project]\nname = "chronorealm-austins-algorithm"\nversion = "0.1.0"\n'
            ).encode("utf-8"),
            "MANIFEST.in": (
                "include README.md\n"
                "include examples/benchmark_checkpoint_continuation.py examples/checkpoint_constraint.json\n"
                "include tools/build_verified_package.py tests/test_verified_package_build.py\n"
                "recursive-include src/austins_algorithm *.py *.json\nprune .build\nprune build\n"
            ).encode("utf-8"),
            "README.md": b"Controlled packaging regression fixture.\n",
            "examples/benchmark_checkpoint_continuation.py": b"# controlled benchmark fixture\n",
            "examples/checkpoint_constraint.json": b'{"schema_version":1,"qubits":1,"clauses":[[1]]}\n',
            "tools/build_verified_package.py": b"# controlled build tool fixture\n",
            "tests/test_verified_package_build.py": b"# controlled build regression fixture\n",
            "src/austins_algorithm/__init__.py": b'__version__ = "0.1.0"\n',
            "src/austins_algorithm/changed.py": b"VALUE = 'current changed module'\n",
            "src/austins_algorithm/qiskit_adapter.py": b"VALUE = 'current previously unchanged adapter'\n",
            "src/austins_algorithm/assets/oracle.json": b'{"purpose":"runtime resource coverage"}\n',
        }
        for relative, raw in self.source_files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def runtime_files(self):
        return {
            relative.removeprefix("src/"): raw for relative, raw in self.source_files.items()
            if relative.startswith("src/austins_algorithm/")
        }

    def sdist(self, path=None, *, files=None, duplicate=None, version=_VERSION):
        destination = self.directory / "package.tar.gz" if path is None else path
        entries = dict(self.source_files if files is None else files)
        entries["PKG-INFO"] = _metadata(version=version)
        with tarfile.open(destination, "w:gz") as archive:
            for relative, raw in entries.items():
                info = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{relative}")
                info.size = len(raw)
                archive.addfile(info, BytesIO(raw))
            if duplicate is not None:
                raw = entries[duplicate]
                info = tarfile.TarInfo(f"{_ARCHIVE_ROOT}/{duplicate}")
                info.size = len(raw)
                archive.addfile(info, BytesIO(raw))
        return destination

    def wheel(self, path=None, *, files=None, duplicate=None, version=_VERSION):
        destination = self.directory / f"{_ARCHIVE_ROOT}-py3-none-any.whl" if path is None else path
        entries = dict(self.runtime_files() if files is None else files)
        entries[f"{_DIST_INFO}/METADATA"] = _metadata(version=version)
        entries[f"{_DIST_INFO}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: controlled-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        entries[f"{_DIST_INFO}/RECORD"] = b""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(destination, "w") as archive:
                for relative, raw in entries.items():
                    archive.writestr(relative, raw)
                if duplicate is not None:
                    archive.writestr(duplicate, entries[duplicate])
        return destination

    def controlled_runner(self, *, stale_sdist=False, fail=False):
        calls = []

        def run(command, **options):
            calls.append(list(command))
            os.write(options["stdout"].fileno(), b"controlled package build stdout\n")
            os.write(options["stderr"].fileno(), b"controlled package build stderr\n")
            if fail:
                return subprocess.CompletedProcess(command, 7)
            output = Path(command[command.index("--out-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            input_path = Path(command[command.index("build") + 1])
            if "--sdist" in command:
                self.assertEqual(input_path, self.source)
                files = deepcopy(self.source_files)
                if stale_sdist:
                    files["src/austins_algorithm/qiskit_adapter.py"] = b"VALUE = 'stale adapter'\n"
                self.sdist(output / f"{_ARCHIVE_ROOT}.tar.gz", files=files)
            else:
                self.assertIn("--wheel", command)
                self.assertTrue(input_path.is_relative_to(self.output))
                self.assertEqual(input_path.suffixes[-2:], [".tar", ".gz"])
                verified = package_build.verify_sdist(self.source, input_path)
                self.assertEqual(verified["status"], "SDIST_VERIFIED")
                with tarfile.open(input_path, "r:gz") as archive:
                    files = {
                        member.name.split("/", 2)[-1]: archive.extractfile(member).read()
                        for member in archive.getmembers()
                        if member.isfile() and member.name.startswith(f"{_ARCHIVE_ROOT}/src/austins_algorithm/")
                    }
                self.wheel(output / f"{_ARCHIVE_ROOT}-py3-none-any.whl", files=files)
            return subprocess.CompletedProcess(command, 0)

        return run, calls

    def test_sdist_and_wheel_verify_every_runtime_file_with_identical_digests(self):
        sdist = package_build.verify_sdist(self.source, self.sdist())
        wheel = package_build.verify_wheel(self.source, self.wheel())
        expected = {relative: _fingerprint(raw) for relative, raw in self.runtime_files().items()}
        for report, status in ((sdist, "SDIST_VERIFIED"), (wheel, "WHEEL_VERIFIED")):
            with self.subTest(status=status):
                self.assertEqual(report["status"], status)
                self.assertEqual(report["project"], {"name": _NAME, "version": _VERSION})
                self.assertEqual(report["runtime_file_count"], 4)
                self.assertEqual(report["runtime_files"], expected)
        self.assertIn("examples/checkpoint_constraint.json", sdist["source_files"])
        self.assertIn("tools/build_verified_package.py", sdist["source_files"])

    def test_stale_unchanged_runtime_is_rejected_when_changed_module_matches(self):
        files = self.runtime_files()
        files["austins_algorithm/qiskit_adapter.py"] = b"VALUE = 'stale adapter'\n"
        wheel = self.wheel(files=files)
        with ZipFile(wheel) as archive:
            self.assertEqual(archive.read("austins_algorithm/changed.py"), self.source_files["src/austins_algorithm/changed.py"])
        with self.assertRaisesRegex(ValueError, r"(?i)(runtime|source|differ|mismatch|qiskit_adapter)"):
            package_build.verify_wheel(self.source, wheel)

    def test_missing_extra_and_duplicate_runtime_entries_are_rejected(self):
        for kind in ("missing", "extra", "duplicate"):
            for format_ in ("sdist", "wheel"):
                with self.subTest(kind=kind, format=format_):
                    files = deepcopy(self.source_files) if format_ == "sdist" else self.runtime_files()
                    prefix = "src/" if format_ == "sdist" else ""
                    key = prefix + "austins_algorithm/qiskit_adapter.py"
                    duplicate = None
                    if kind == "missing":
                        del files[key]
                    elif kind == "extra":
                        files[prefix + "austins_algorithm/unexpected.py"] = b"EXTRA = True\n"
                    else:
                        duplicate = key
                    archive = self.sdist(files=files, duplicate=duplicate) if format_ == "sdist" else self.wheel(files=files, duplicate=duplicate)
                    verify = package_build.verify_sdist if format_ == "sdist" else package_build.verify_wheel
                    with self.assertRaisesRegex(ValueError, r"(?i)(runtime|duplicate|missing|extra|differ|mismatch)"):
                        verify(self.source, archive)

    def test_missing_owned_source_artifact_or_wrong_metadata_is_rejected(self):
        files = deepcopy(self.source_files)
        del files["examples/checkpoint_constraint.json"]
        with self.assertRaisesRegex(ValueError, r"(?i)(source|missing|checkpoint_constraint)"):
            package_build.verify_sdist(self.source, self.sdist(files=files))
        for format_, verify in (("sdist", package_build.verify_sdist), ("wheel", package_build.verify_wheel)):
            with self.subTest(format=format_):
                archive = self.sdist(version="0.0.9") if format_ == "sdist" else self.wheel(version="0.0.9")
                with self.assertRaisesRegex(ValueError, r"(?i)(metadata|version|project)"):
                    verify(self.source, archive)

    def test_build_uses_verified_sdist_and_preserves_stale_root_build_cache(self):
        stale = self.source / "build" / "lib" / "austins_algorithm" / "qiskit_adapter.py"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"VALUE = 'stale root build cache'\n")
        runner, calls = self.controlled_runner()
        result = package_build.build_verified_package(self.source, self.output, cache_dir=self.cache, runner=runner)
        self.assertEqual(result["status"], "VERIFIED_PACKAGE_BUILT")
        self.assertEqual(len(calls), 2)
        self.assertIn("--sdist", calls[0])
        self.assertIn("--wheel", calls[1])
        expected = {relative: _fingerprint(raw) for relative, raw in self.runtime_files().items()}
        self.assertEqual(result["artifacts"]["sdist"]["runtime_files"], expected)
        self.assertEqual(result["artifacts"]["wheel"]["runtime_files"], expected)
        self.assertEqual(stale.read_bytes(), b"VALUE = 'stale root build cache'\n")
        self.assertEqual(json.loads((self.output / "build-result.json").read_bytes()), result)

    def test_sdist_mismatch_stops_before_wheel_and_keeps_evidence(self):
        runner, calls = self.controlled_runner(stale_sdist=True)
        result = package_build.build_verified_package(self.source, self.output, cache_dir=self.cache, runner=runner)
        self.assertEqual(result["status"], "PACKAGE_BUILD_FAILED")
        self.assertEqual(len(calls), 1)
        self.assertIn("--sdist", calls[0])
        self.assertTrue(list(self.output.rglob("*.tar.gz")))
        self.assertFalse(list(self.output.rglob("*.whl")))
        self.assertEqual(json.loads((self.output / "build-result.json").read_bytes()), result)
        self.assertTrue(any(path.read_bytes() for path in self.output.rglob("*.log")))

    def test_failed_build_keeps_logs_and_existing_destination_is_refused(self):
        runner, calls = self.controlled_runner(fail=True)
        result = package_build.build_verified_package(self.source, self.output, cache_dir=self.cache, runner=runner)
        self.assertEqual(result["status"], "PACKAGE_BUILD_FAILED")
        self.assertEqual(len(calls), 1)
        logs = [path.read_bytes() for path in self.output.rglob("*.log")]
        self.assertIn(b"controlled package build stdout\n", logs)
        self.assertIn(b"controlled package build stderr\n", logs)
        before = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            package_build.build_verified_package(self.source, self.output, cache_dir=self.cache, runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual({path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}, before)

