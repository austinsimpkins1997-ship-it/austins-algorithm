"""Verify project portability, replay integrity, and complete offline exports."""

from copy import deepcopy
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from austins_algorithm import workbench_project as projects
from austins_algorithm.optimization import SearchSession


class WorkbenchProjectTests(unittest.TestCase):
    def setUp(self):
        build = PACKAGE / ".build"
        build.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(prefix="workbench-project-", dir=build)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def measured_project(self):
        project = projects.new_project({
            "name": "Example <tag> & test",
            "variables": [{"name": "High cost", "cost": 5}, {"name": "Low cost", "cost": 1}],
            "clauses": [[1, 2]],
        })
        project["settings"].update(prove=False, max_shots=4, max_queries=8)
        session = SearchSession(project["problem"], **project["settings"])
        first = session.measure(0)
        session.measure(2)
        project["run"] = session.report()
        project["preview"] = first["trace"]
        return project, session

    def test_round_trip_and_continue_exactly(self):
        project, session = self.measured_project()
        path = self.root / "project.json"
        projects.save_project(path, project)
        restored = projects.load_project(path)
        self.assertEqual(restored, project)
        resumed = projects.restore_session(restored)
        self.assertEqual(resumed.auto_step(), session.auto_step())
        self.assertEqual(resumed.run(), session.run())

    def test_legacy_optional_preview_and_strict_json(self):
        blank = projects.new_project()
        blank.pop("preview")
        path = self.root / "project.json"
        path.write_text(json.dumps(blank), encoding="utf-8")
        self.assertIsNone(projects.load_project(path)["preview"])
        malformed = (
            ('{"schema_version": 1, "schema_version": 1}', "duplicate"),
            ('{"number": NaN}', "JSON constant"),
            ('{"number": 1e9999}', "finite"),
            ("[" * 2000, "UTF-8 JSON"),
            ('{"schema_version":true,"problem":{},"settings":{},"run":null}', "Unsupported"),
            ('{"schema_version":2,"problem":{},"settings":{},"run":null}', "Unsupported"),
        )
        for payload, message in malformed:
            with self.subTest(message=message):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    projects.load_project(path)
        with patch.object(projects, "MAX_PROJECT_BYTES", 100):
            path.write_bytes(b" " * 101)
            with self.assertRaisesRegex(ValueError, "size limit"):
                projects.load_project(path)

    def test_run_and_preview_tampering_rejected(self):
        project, _ = self.measured_project()
        edited = deepcopy(project)
        edited["run"]["events"][0]["trace"]["frames"][0]["probabilities"][0] = 0.99
        with self.assertRaisesRegex(ValueError, "seeded replay"):
            projects.validate_project(edited)
        edited = deepcopy(project)
        edited["preview"]["frames"][0]["amplitudes"][0] = 0.99
        with self.assertRaisesRegex(ValueError, "Saved preview differs"):
            projects.validate_project(edited)
        edited = deepcopy(project)
        edited["settings"]["max_shots"] = 1
        with self.assertRaisesRegex(ValueError, "shot budget"):
            projects.validate_project(edited)

    def test_save_and_export_protect_existing_destination(self):
        path = self.root / "project.json"
        original = projects.new_project()
        projects.save_project(path, original)
        raw = path.read_bytes()
        edited = deepcopy(original)
        edited["problem"]["name"] = "Renamed project"
        with self.assertRaises(FileExistsError):
            projects.save_project(path, edited)
        self.assertEqual(path.read_bytes(), raw)
        with patch.object(projects.os, "replace", side_effect=PermissionError("locked destination")):
            with self.assertRaises(PermissionError):
                projects.save_project(path, edited, overwrite=True)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(self.root.iterdir()), [path])
        projects.save_project(path, edited, overwrite=True)
        self.assertEqual(projects.load_project(path), edited)

        output = self.root / "project.zip"
        output.write_bytes(b"preserve earlier report")
        with self.assertRaises(FileExistsError):
            projects.export_project(output, original)
        self.assertEqual(output.read_bytes(), b"preserve earlier report")

    def test_file_created_while_publishing_is_not_replaced(self):
        path = self.root / "created-during-save.json"

        def another_writer(stream):
            stream.write(b"new save")
            path.write_bytes(b"other writer")

        with self.assertRaisesRegex(FileExistsError, "destination changed"):
            projects._write_atomically(path, another_writer, overwrite=True)
        self.assertEqual(path.read_bytes(), b"other writer")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_zip_integrity_complete_csv_and_independent_replay(self):
        project, _ = self.measured_project()
        output = self.root / "project.zip"
        receipt = projects.export_project(output, project)
        self.assertEqual(receipt["size_bytes"], output.stat().st_size)
        extracted = self.root / "extracted"
        extracted.mkdir()
        with ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest, receipt["manifest"])
            self.assertEqual(set(archive.namelist()), {"manifest.json", *(row["path"] for row in manifest["files"])})
            for row in manifest["files"]:
                data = archive.read(row["path"])
                self.assertEqual(len(data), row["size_bytes"])
                self.assertEqual(sha256(data).hexdigest(), row["sha256"])
            self.assertEqual(json.loads(archive.read("project.json")), project)
            self.assertEqual(json.loads(archive.read("run.json")), project["run"])
            self.assertEqual(json.loads(archive.read("preview.json")), project["preview"])
            baseline = json.loads(archive.read("exact_baseline.json"))
            self.assertEqual((baseline["bits"], baseline["cost"]), ("10", 1))
            self.assertIsNone(project["run"]["reference"])
            frames = list(csv.DictReader(StringIO(archive.read("amplitudes.csv").decode("utf-8"))))
            expected = sum(len(event["trace"]["frames"]) for event in project["run"]["events"])
            self.assertEqual(len(frames), (expected + len(project["preview"]["frames"])) * 4)
            self.assertEqual(frames[0]["bits"], "00")
            self.assertTrue(any(row["stage"] == "diffusion" for row in frames))
            history = list(csv.DictReader(StringIO(archive.read("measurements.csv").decode("utf-8"))))
            self.assertEqual(len(history), 2)
            report = archive.read("report.html").decode("utf-8")
            self.assertIn("Example &lt;tag&gt; &amp; test", report)
            self.assertNotIn("Example <tag>", report)
            archive.extractall(extracted)
        result = subprocess.run(
            [sys.executable, "-B", str(extracted / "reproduce.py")],
            cwd=extracted, capture_output=True, text=True, check=True, timeout=30,
        )
        self.assertEqual(json.loads(result.stdout), project["run"])
        verified = projects.verify_export(output)
        self.assertEqual(verified["entries"], receipt["entries"])
        self.assertEqual(verified["sha256"], sha256(output.read_bytes()).hexdigest())
        self.assertEqual(verified["run_status"], project["run"]["status"])

    def test_unmeasured_export_preserves_preview_and_explains_status(self):
        project = projects.new_project()
        self.assertIn("RECORDED_ROUNDS = None", projects.generated_program(project))
        project["preview"] = SearchSession(project["problem"], **project["settings"]).preview(1)
        output = self.root / "unmeasured.zip"
        projects.export_project(output, project)
        with ZipFile(output) as archive:
            self.assertIsNone(json.loads(archive.read("run.json")))
            self.assertEqual(json.loads(archive.read("metadata.json"))["run_status"], "NOT_RUN")
            self.assertEqual(json.loads(archive.read("preview.json")), project["preview"])
            self.assertIn("kind,shot,", archive.read("amplitudes.csv").decode("utf-8"))
            self.assertIn("preview,,", archive.read("amplitudes.csv").decode("utf-8"))
        self.assertEqual(projects.verify_export(output)["run_status"], "NOT_RUN")

    def test_export_verifier_rejects_tampering_and_unsafe_members(self):
        output = self.root / "project.zip"
        projects.export_project(output, projects.new_project())
        with ZipFile(output) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}

        def write_variant(name, variant, extra=None):
            path = self.root / name
            with ZipFile(path, "w") as archive:
                for member, raw in variant.items():
                    archive.writestr(member, raw)
                if extra is not None:
                    archive.writestr(extra, b"untrusted")
            return path

        edited = files | {"report.html": b"untrusted" * 50}
        with self.assertRaisesRegex(ValueError, "size or digest"):
            projects.verify_export(write_variant("tampered.zip", edited))
        manifest = json.loads(files["manifest.json"])
        row = next(row for row in manifest["files"] if row["path"] == "report.html")
        row["size_bytes"] = len(edited["report.html"])
        row["sha256"] = sha256(edited["report.html"]).hexdigest()
        edited["manifest.json"] = json.dumps(manifest).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "differs from trusted"):
            projects.verify_export(write_variant("rehash.zip", edited))
        with self.assertRaisesRegex(ValueError, "unsafe"):
            projects.verify_export(write_variant("traversal.zip", files, "../outside.txt"))
        link = ZipInfo("report.html")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        without_report = {key: value for key, value in files.items() if key != "report.html"}
        with self.assertRaisesRegex(ValueError, "unsafe"):
            projects.verify_export(write_variant("link.zip", without_report, link))
        with patch.object(projects, "MAX_EXPANDED_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "expanded export"):
                projects.verify_export(output)

    def test_checkout_line_endings_keep_manifest_and_source_checks(self):
        output = self.root / "project.zip"
        projects.export_project(output, projects.new_project())
        with ZipFile(output) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        source_paths = ("LICENSE", "NOTICE.md", "austins_algorithm/__init__.py")
        for name in source_paths:
            data = files[name]
            files[name] = data.replace(b"\r\n", b"\n") if b"\r\n" in data else data.replace(b"\n", b"\r\n")
        def variant(name, *, rehash):
            path = self.root / name
            members = dict(files)
            if rehash:
                manifest = json.loads(members["manifest.json"])
                for row in manifest["files"]:
                    value = members[row["path"]]
                    row.update(size_bytes=len(value), sha256=sha256(value).hexdigest())
                members["manifest.json"] = json.dumps(manifest).encode("utf-8")
            with ZipFile(path, "w") as archive:
                for member, value in members.items():
                    archive.writestr(member, value)
            return path
        with self.assertRaisesRegex(ValueError, "size or digest"):
            projects.verify_export(variant("untouched-manifest.zip", rehash=False))
        self.assertEqual(projects.verify_export(variant("alternate-endings.zip", rehash=True))["run_status"], "NOT_RUN")
        files["austins_algorithm/__init__.py"] += b"\nraise RuntimeError('unexpected')\n"
        with self.assertRaisesRegex(ValueError, "differs from trusted"):
            projects.verify_export(variant("altered-source.zip", rehash=True))


if __name__ == "__main__":
    unittest.main()
