"""Portable project files and complete offline workbench exports."""

from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html import escape
from importlib.metadata import PackageNotFoundError, distribution
from io import StringIO
import json
import math
import os
from pathlib import Path
from pprint import pformat
import stat
from tempfile import NamedTemporaryFile
from typing import Any
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from . import __version__
from .constraints import _unique_object
from .optimization import MAX_SHOTS, SearchSession, exact_minimum, problem_from_document

PROJECT_SCHEMA_VERSION = 1
WORKBENCH_VERSION = "0.2.0-alpha"
MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
_DEFAULT_SETTINGS = {"seed": 7, "max_shots": 16, "max_queries": 64, "max_rounds": 4, "prove": True}
_PROJECT_FIELDS = {"schema_version", "problem", "settings", "run"}
_REPLAY_MODULES = (
    "__init__.py", "adapter_api.py", "classical_constraint.py", "constraints.py",
    "core.py", "grover_search.py", "local_adapter.py", "optimization.py",
)
_EXPORT_RECORDS = {
    "project.json", "problem.json", "settings.json", "run.json", "preview.json",
    "exact_baseline.json", "metadata.json", "measurements.csv", "amplitudes.csv",
    "report.html", "reproduce.py", "README.txt", "LICENSE", "NOTICE.md",
}
_CHECKOUT_TEXT_MEMBERS = {"LICENSE", "NOTICE.md", *(f"austins_algorithm/{name}" for name in _REPLAY_MODULES)}


def _matches_export_member(name: str, raw: bytes, expected: bytes) -> bool:
    if raw == expected:
        return True
    # Git may materialize these source members as LF or CRLF. The manifest
    # still verifies the exact archived bytes before equivalent text is accepted.
    if name in _CHECKOUT_TEXT_MEMBERS:
        return raw.replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n")
    return False


def _sensor_kit() -> dict[str, Any]:
    names = ("Radar sensor", "Optical sensor", "LTE uplink", "Wired uplink", "Backup power", "Local buffer")
    return {
        "name": "Weighted field sensor kit",
        "variables": [{"name": name, "cost": cost} for name, cost in zip(names, (1, 4, 1, 3, 1, 1))],
        "clauses": [[1, 2], [-1, -2], [3, 4], [-3, -4], [-1, 5], [-3, 6]],
    }


def new_project(problem: object = None) -> dict[str, Any]:
    """Make an editable project with a worked example or a caller's weighted problem."""
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "problem": problem_from_document(_sensor_kit() if problem is None else problem),
        "settings": _DEFAULT_SETTINGS.copy(),
        "run": None,
        "preview": None,
    }


def _json_bytes(value: object, *, readable: bool = False) -> bytes:
    try:
        return (json.dumps(
            value, ensure_ascii=True, sort_keys=True, allow_nan=False,
            indent=2 if readable else None, separators=None if readable else (",", ":"),
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Project data must be finite, bounded JSON.") from exc


def _replay_project(project: object) -> tuple[dict[str, Any], SearchSession]:
    if not isinstance(project, dict) or set(project) not in (_PROJECT_FIELDS, _PROJECT_FIELDS | {"preview"}):
        raise ValueError("A project requires schema_version, problem, settings and run, with an optional preview.")
    if type(project["schema_version"]) is not int or project["schema_version"] != PROJECT_SCHEMA_VERSION:
        raise ValueError("Unsupported workbench project version.")
    settings = project["settings"]
    if not isinstance(settings, dict) or set(settings) != set(_DEFAULT_SETTINGS):
        raise ValueError("Project settings must specify a seed, shot budget, query budget, round limit and proof choice.")
    session = SearchSession(project["problem"], **settings)
    stored = project["run"]
    replayed = None
    if stored is not None:
        if not isinstance(stored, dict) or type(stored.get("schema_version")) is not int or stored["schema_version"] != 1:
            raise ValueError("Unsupported saved run version.")
        events = stored.get("events")
        if not isinstance(events, list) or len(events) > min(session.settings["max_shots"], MAX_SHOTS):
            raise ValueError("Saved measurements exceed the declared shot budget.")
        for event in events:
            if not isinstance(event, dict) or "rounds" not in event:
                raise ValueError("Every saved measurement must give its round choice.")
            session.measure(event["rounds"])
        replayed = session.report()
        if _json_bytes(stored) != _json_bytes(replayed):
            raise ValueError("Saved run differs from its seeded replay. Check that it belongs to this problem and settings.")

    stored_preview = project.get("preview")
    verified_preview = None
    if stored_preview is not None:
        if not isinstance(stored_preview, dict) or "rounds" not in stored_preview:
            raise ValueError("A saved preview must give its round choice.")
        preview_bytes = _json_bytes(stored_preview)
        current = session.preview(stored_preview["rounds"])
        if _json_bytes(current) == preview_bytes:
            verified_preview = current
        elif replayed:
            for event in replayed["events"]:
                trace = event["trace"]
                if trace["rounds"] == stored_preview["rounds"] and _json_bytes(trace) == preview_bytes:
                    verified_preview = trace
                    break
        if verified_preview is None:
            raise ValueError("Saved preview differs from the current simulation and the recorded measurements.")

    return {
        "schema_version": PROJECT_SCHEMA_VERSION, "problem": session.problem,
        "settings": session.settings, "run": replayed, "preview": verified_preview,
    }, session


def validate_project(project: object) -> dict[str, Any]:
    """Validate the inputs and reproduce all saved simulation evidence."""
    return _replay_project(project)[0]


def restore_session(project: object) -> SearchSession:
    """Recreate a session at the saved shot and query counters."""
    return _replay_project(project)[1]


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Project numbers must be finite.")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid project JSON constant {value}.")


def _decode_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=lambda pairs: _unique_object(pairs, "project"),
            parse_float=_finite_float, parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("The project must contain valid UTF-8 JSON.") from exc


def _is_regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    # On Windows, Python 3.12 reports different ctime semantics for lstat and fstat.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@contextmanager
def _read_regular(path: Path, limit: int, label: str):
    initial = path.lstat()
    if not _is_regular(initial):
        raise ValueError(f"Choose a regular {label} file.")
    if initial.st_size > limit:
        raise ValueError(f"The {label} exceeds its size limit.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _is_regular(opened) or _file_state(opened) != _file_state(initial):
            raise ValueError(f"The {label} changed while opening.")
        yield stream, opened.st_size
        if _file_state(os.fstat(stream.fileno())) != _file_state(opened):
            raise ValueError(f"The {label} changed while reading.")


def load_project(path: str | Path) -> dict[str, Any]:
    """Open a bounded JSON project and verify it without executing stored code."""
    with _read_regular(Path(path), MAX_PROJECT_BYTES, "project") as (stream, _):
        raw = stream.read(MAX_PROJECT_BYTES + 1)
    if len(raw) > MAX_PROJECT_BYTES:
        raise ValueError("The project exceeds the 32 MiB size limit.")
    return validate_project(_decode_json(raw))


def _check_target(path: Path, overwrite: bool) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not _is_regular(info):
        raise ValueError("Choose a regular file as the destination.")
    if not overwrite:
        raise FileExistsError(f"File already exists: {path}")
    return _file_state(info)


def _write_atomically(path: Path, writer, *, overwrite: bool) -> None:
    """Complete and flush a same-directory temporary file before publishing it."""
    if type(overwrite) is not bool:
        raise ValueError("Overwrite must be a boolean.")
    path = path.parent.resolve(strict=True) / path.name
    initial = _check_target(path, overwrite)
    with NamedTemporaryFile(dir=path.parent, prefix=".austin-workbench-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            current = _check_target(path, overwrite)
            if current != initial:
                raise FileExistsError("The destination changed while writing. Choose a new file or review the replacement.")
            if current is not None:
                os.replace(temporary, path)
            elif os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)


def save_project(path: str | Path, project: object, *, overwrite: bool = False) -> None:
    """Save a verified project atomically, with replacement allowed explicitly."""
    raw = _json_bytes(validate_project(project))
    if len(raw) > MAX_PROJECT_BYTES:
        raise ValueError("The project exceeds the 32 MiB size limit.")
    _write_atomically(Path(path), lambda stream: stream.write(raw), overwrite=overwrite)


def _program(project: dict[str, Any]) -> str:
    run = project["run"]
    choices = [event["rounds"] for event in run["events"]] if run is not None else None
    expected = hashlib.sha256(_json_bytes(run)).hexdigest() if run is not None else None
    return (
        '"""Reproduce the weighted Grover experiment with Python 3.10 or later.\n\n'
        "Run python reproduce.py from the extracted export directory.\n"
        "With recorded choices, the program verifies the complete replay before printing it.\n"
        "A project saved before measuring runs the automatic schedule from its saved seed.\n"
        '"""\n\n'
        "import hashlib\n"
        "import json\n\n"
        "from austins_algorithm.optimization import SearchSession\n\n"
        f"PROBLEM = {pformat(project['problem'], width=88, sort_dicts=False)}\n"
        f"SETTINGS = {pformat(project['settings'], width=88, sort_dicts=False)}\n"
        f"RECORDED_ROUNDS = {pformat(choices, width=88)}\n"
        f"EXPECTED_SHA256 = {expected!r}\n\n\n"
        "def main():\n"
        "    session = SearchSession(PROBLEM, **SETTINGS)\n"
        "    if RECORDED_ROUNDS is None:\n"
        "        report = session.run()\n"
        "    else:\n"
        "        for rounds in RECORDED_ROUNDS:\n"
        "            session.measure(rounds)\n"
        "        report = session.report()\n"
        "        raw = json.dumps(report, ensure_ascii=True, sort_keys=True,\n"
        "                         separators=(',', ':'), allow_nan=False) + '\\n'\n"
        "        if hashlib.sha256(raw.encode('utf-8')).hexdigest() != EXPECTED_SHA256:\n"
        "            raise RuntimeError('Replayed report differs from the exported run.')\n"
        "    print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))\n\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )


def generated_program(project: object) -> str:
    """Print the project inputs and deterministic replay in readable Python."""
    return _program(validate_project(project))


def _csv_bytes(header, rows) -> bytes:
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _measurement_rows(events):
    for event in events:
        before = event["incumbent_before"]
        after = event["incumbent_after"]
        yield (
            event["shot"], event["rounds"], event["bits"], event["feasible"], event["cost"],
            event["accepted"], before["bits"] if before else None, before["cost"] if before else None,
            after["bits"] if after else None, after["cost"] if after else None,
            event["shots_used"], event["queries_used"],
        )


def _trace_rows(project: dict[str, Any]):
    events = project["run"]["events"] if project["run"] is not None else []
    traces = [("measurement", event["shot"], event["trace"]) for event in events]
    if project["preview"] is not None:
        traces.append(("preview", None, project["preview"]))
    for kind, shot, trace in traces:
        for frame in trace["frames"]:
            for state, amplitude, probability in zip(
                trace["states"], frame["amplitudes"], frame["probabilities"],
            ):
                yield (
                    kind, shot, trace["incumbent"], frame["stage"], frame["round"], state["bits"],
                    state["feasible"], state["marked"], state["cost"], amplitude, probability,
                    frame["marked_probability"],
                )


def _report_html(project: dict[str, Any], baseline: dict[str, Any]) -> bytes:
    problem = project["problem"]
    run = project["run"]
    events = run["events"] if run is not None else []
    trace = project["preview"] or (events[-1]["trace"] if events else None)
    status = run["status"] if run is not None else "NOT_RUN"
    best = run["best"] if run is not None else None
    variables = "".join(
        f"<tr><td>q{index}</td><td>{escape(row['name'])}</td><td>{row['cost']}</td></tr>"
        for index, row in enumerate(problem["variables"])
    )
    measurement_rows = "".join(
        f"<tr><td>{event['shot']}</td><td>{event['rounds']}</td><td>{event['bits']}</td>"
        f"<td>{event['cost']}</td><td>{'Yes' if event['feasible'] else 'No'}</td>"
        f"<td>{'Yes' if event['accepted'] else 'No'}</td><td>{event['queries_used']}</td></tr>"
        for event in events
    ) or '<tr><td colspan="7">No measurements saved.</td></tr>'
    settings = escape(json.dumps(project["settings"], indent=2))
    clauses = escape(json.dumps(problem["clauses"]))
    sampled = f"{best['bits']} · cost {best['cost']}" if best else "No verified assignment sampled"
    exact = (f"{baseline['bits']} · cost {baseline['cost']} · {len(baseline['ties'])} tied minimum assignments"
             if baseline["status"] == "OPTIMUM" else "Infeasible by exhaustive classical check")
    work = escape(json.dumps(run["work"], indent=2)) if run is not None else "No measured search work"
    chart = "<p>No preview or measured state saved.</p>"
    if trace is not None:
        frame = trace["frames"][-1]
        ordered = sorted(zip(trace["states"], frame["amplitudes"], frame["probabilities"]),
                         key=lambda row: -row[2])
        bars = "".join(
            f"<li><code>{state['bits']}</code><span><meter min=\"0\" max=\"1\" value=\"{probability:.15g}\">"
            f"{probability:.2%}</meter> {probability:.2%}</span>"
            f"<span>{amplitude:+.5f}</span><span>{'Marked' if state['marked'] else 'Unmarked'}</span></li>"
            for state, amplitude, probability in ordered[:16]
        )
        context = "Saved preview" if project["preview"] is not None else "Last measurement"
        chart = (f"<p>{context} · {frame['stage']} · round {frame['round']} · "
                 f"total marked probability {frame['marked_probability']:.2%}</p>"
                 "<p>Up to 16 most probable basis states. Columns show basis, probability, signed amplitude and marking.</p>"
                 f'<ol class="chart">{bars}</ol>')
    title = escape(problem["name"])
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title} | Austin's Algorithm</title>
<style>
:root {{color-scheme: light; color:#142432; background:#edf3f5; font:16px/1.55 system-ui,sans-serif}}
body {{max-width:1000px; margin:2rem auto; padding:0 1rem}}
h1 {{line-height:1.15}} h2 {{color:#064d47}}
section {{background:#fff; padding:1rem 1.5rem; border:1px solid #cedde1; border-radius:10px; margin:1rem 0}}
table {{border-collapse:collapse; width:100%}} th,td {{text-align:left; border-bottom:1px solid #dbe4e6; padding:.35rem}}
pre {{background:#eef5f6; padding:.75rem; white-space:pre-wrap}}
.chart {{list-style:none; padding:0}} .chart li {{display:grid; grid-template-columns:4.5rem 1fr 5.5rem 5rem; gap:.5rem}}
meter {{width:min(8rem,45%)}} a {{color:#005d71}}
@media(max-width:500px) {{section {{padding:.7rem}} .chart li {{grid-template-columns:3.5rem 1fr 4rem 4.5rem}}}}
</style>
<header><p>Austin's Algorithm · weighted constraint workbench</p><h1>{title}</h1>
<p>Project status <strong>{status}</strong>. Measurements use an ideal statevector simulated on the CPU.</p></header>
<section><h2>Results</h2><p><strong>Best verified sample</strong> {sampled}</p>
<p><strong>Exact classical baseline</strong> {exact}</p><pre>{work}</pre></section>
<section><h2>Problem and run settings</h2>
<table><thead><tr><th>Variable</th><th>Name</th><th>Enabled cost</th></tr></thead><tbody>{variables}</tbody></table>
<p>Every clause must be satisfied. A signed one-based literal selects a variable or its negation.</p>
<pre>{clauses}</pre><pre>{settings}</pre><p>Basis strings display q0 on the right.</p></section>
<section><h2>State distribution</h2>{chart}</section>
<section><h2>Measurement history</h2>
<table><thead><tr><th>Shot</th><th>Rounds</th><th>Bits</th><th>Cost</th><th>Feasible</th>
<th>Improvement</th><th>Queries used</th></tr></thead><tbody>{measurement_rows}</tbody></table></section>
<section><h2>Files and replay</h2><p><a href="project.json">Project JSON</a> · <a href="run.json">Run JSON</a> ·
<a href="exact_baseline.json">Exact baseline JSON</a> · <a href="amplitudes.csv">All phases and amplitudes CSV</a> ·
<a href="measurements.csv">Measurement CSV</a> · <a href="manifest.json">SHA-256 manifest</a></p>
<p>Extract the ZIP. Open project.json in the workbench, or run <code>python reproduce.py</code> with Python 3.10 or later.</p></section>
</html>
""".encode("utf-8")


def _license_data(module_dir: Path, asset: str, source_name: str) -> bytes:
    source_file = module_dir / "workbench_assets" / asset
    if source_file.is_file():
        return source_file.read_bytes()
    if module_dir.parent.name == "src":
        checkout_file = module_dir.parent.parent / source_name
        if checkout_file.is_file():
            return checkout_file.read_bytes()
    try:
        package = distribution("chronorealm-austins-algorithm")
    except PackageNotFoundError:
        package = None
    if package is not None:
        for file in package.files or ():
            if file.name == source_name and ".dist-info/licenses" in file.as_posix():
                return package.locate_file(file).read_bytes()
    raise FileNotFoundError(f"The installed workbench is missing {asset}.")


def _export_files(project: dict[str, Any], exported_at_utc: str) -> dict[str, bytes]:
    run = project["run"]
    events = run["events"] if run is not None else []
    baseline = exact_minimum(project["problem"])
    module_dir = Path(__file__).resolve().parent
    files = {
        "project.json": _json_bytes(project),
        "problem.json": _json_bytes(project["problem"], readable=True),
        "settings.json": _json_bytes(project["settings"], readable=True),
        "run.json": _json_bytes(run),
        "preview.json": _json_bytes(project["preview"]),
        "exact_baseline.json": _json_bytes(baseline, readable=True),
        "metadata.json": _json_bytes({
            "schema_version": 1, "application": "Austin's Algorithm",
            "application_version": WORKBENCH_VERSION, "engine_version": __version__,
            "exported_at_utc": exported_at_utc,
            "run_status": run["status"] if run is not None else "NOT_RUN",
            "basis_order": "MSB-first display, q0 is rightmost",
            "execution_mode": "IDEAL_CLASSICAL_STATEVECTOR_SIMULATION",
            "verification": "SEEDED_REPLAY" if run is not None else "INPUT_VALIDATION",
            "provider_job_submitted": False, "quantum_hardware_used": False,
        }, readable=True),
        "measurements.csv": _csv_bytes((
            "shot", "rounds", "bits", "feasible", "cost", "accepted", "incumbent_before_bits",
            "incumbent_before_cost", "incumbent_after_bits", "incumbent_after_cost", "shots_used", "queries_used",
        ), _measurement_rows(events)),
        "amplitudes.csv": _csv_bytes((
            "kind", "shot", "incumbent_cost", "stage", "round", "bits", "feasible", "marked", "cost",
            "amplitude", "probability", "marked_probability",
        ), _trace_rows(project)),
        "report.html": _report_html(project, baseline),
        "reproduce.py": _program(project).encode("utf-8"),
        "README.txt": (
            "Austin's Algorithm workbench export\n\n"
            "Open report.html for the readable results. Use project.json to reopen the project.\n"
            "run.json retains every measurement and its frames. preview.json retains the saved preview.\n"
            "exact_baseline.json is an independent exhaustive check of the problem.\n"
            "measurements.csv and amplitudes.csv contain the complete measurement and state tables.\n"
            "Import CSV basis columns as text to retain leading zeros.\n"
            "manifest.json lists sizes and SHA-256 digests for all other entries.\n"
            "The manifest verifies contents against recorded digests. Check the ZIP digest with the sender\n"
            "when you need to bind a copy to a trusted transfer.\n"
            "To replay the recorded choices and print the full result, extract the entire ZIP and run\n"
            "python reproduce.py from the extracted folder using Python 3.10 or later.\n"
            "A project exported before measuring has run.json null and reproducer uses the saved\n"
            "settings to start a deterministic automatic simulation.\n"
            "The workbench reads project JSON as data. Run bundled scripts only from trusted exports.\n"
        ).encode("utf-8"),
        "LICENSE": _license_data(module_dir, "LICENSE.txt", "LICENSE"),
        "NOTICE.md": _license_data(module_dir, "NOTICE.txt", "NOTICE.md"),
    }
    for name in _REPLAY_MODULES:
        files[f"austins_algorithm/{name}"] = (module_dir / name).read_bytes()
    return files


def export_project(path: str | Path, project: object, *, overwrite: bool = False) -> dict[str, Any]:
    """Write a complete offline ZIP with exact replay code and an integrity manifest."""
    file = Path(path)
    canonical = validate_project(project)
    files = _export_files(canonical, datetime.now(timezone.utc).isoformat())
    if len(files["project.json"]) > MAX_PROJECT_BYTES:
        raise ValueError("The project exceeds the 32 MiB size limit.")
    manifest = {
        "schema_version": 1, "application_version": WORKBENCH_VERSION,
        "engine_version": __version__, "digest_algorithm": "SHA-256",
        "files": [
            {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }
    files["manifest.json"] = _json_bytes(manifest, readable=True)

    def write_zip(stream) -> None:
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        if stream.tell() > MAX_EXPORT_BYTES:
            raise ValueError("The export exceeds its size limit.")

    _write_atomically(file, write_zip, overwrite=overwrite)
    return {"path": str(file), "size_bytes": file.stat().st_size, "entries": len(files), "manifest": manifest}


def _archive_members(archive: ZipFile) -> dict[str, Any]:
    required = _EXPORT_RECORDS | {f"austins_algorithm/{name}" for name in _REPLAY_MODULES}
    allowed = required | {"manifest.json"}
    members = {}
    total = 0
    for info in archive.infolist():
        mode = (info.external_attr >> 16) & 0xFFFF
        if (
            info.filename not in allowed or info.filename != info.orig_filename
            or info.filename in members or info.is_dir() or info.external_attr & 0x10
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
            or info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
        ):
            raise ValueError("The export contains an unexpected, duplicate, or unsafe entry.")
        total += info.file_size
        if info.file_size > MAX_PROJECT_BYTES or total > MAX_EXPANDED_BYTES:
            raise ValueError("The expanded export exceeds its size limit.")
        members[info.filename] = info
    if set(members) != allowed:
        raise ValueError("The export is missing required entries.")
    return members


def _archive_entry(archive: ZipFile, info, limit: int) -> bytes:
    if info.file_size > limit:
        raise ValueError(f"Export entry exceeds its size limit: {info.filename}")
    with archive.open(info) as stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.file_size or len(raw) > limit:
        raise ValueError(f"Export entry size differs: {info.filename}")
    return raw


def _archive_manifest(manifest: object, members: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "application_version", "engine_version", "digest_algorithm", "files"}
        or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
        or manifest["application_version"] != WORKBENCH_VERSION or manifest["engine_version"] != __version__
        or manifest["digest_algorithm"] != "SHA-256"
        or not isinstance(manifest["files"], list) or len(manifest["files"]) != len(members) - 1
    ):
        raise ValueError("Unsupported export manifest or workbench version.")
    rows = {}
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "size_bytes", "sha256"}:
            raise ValueError("Every manifest entry requires a path, byte count and SHA-256 digest.")
        name = row["path"]
        digest = row["sha256"]
        if (
            not isinstance(name, str) or name not in members or name == "manifest.json" or name in rows
            or type(row["size_bytes"]) is not int or row["size_bytes"] != members[name].file_size
            or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("An export manifest entry has an invalid path, size or digest.")
        rows[name] = row
    return rows


def verify_export(path: str | Path) -> dict[str, Any]:
    """Verify an export against current workbench code without extraction or execution."""
    file = Path(path)
    with _read_regular(file, MAX_EXPORT_BYTES, "export") as (stream, archive_size):
        try:
            with ZipFile(stream) as archive:
                members = _archive_members(archive)
                rows = _archive_manifest(
                    _decode_json(_archive_entry(archive, members["manifest.json"], 64 * 1024)), members,
                )
                project_raw = _archive_entry(archive, members["project.json"], MAX_PROJECT_BYTES)
                canonical = validate_project(_decode_json(project_raw))
                metadata_raw = _archive_entry(archive, members["metadata.json"], 16 * 1024)
                metadata = _decode_json(metadata_raw)
                timestamp = metadata.get("exported_at_utc") if isinstance(metadata, dict) else None
                if not isinstance(timestamp, str) or len(timestamp) > 40:
                    raise ValueError("The export must record an ISO UTC timestamp.")
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo != timezone.utc or parsed.isoformat() != timestamp:
                    raise ValueError("The export must record an ISO UTC timestamp.")
                expected = _export_files(canonical, timestamp)
                for name, row in rows.items():
                    if name == "project.json":
                        raw = project_raw
                    elif name == "metadata.json":
                        raw = metadata_raw
                    else:
                        raw = _archive_entry(archive, members[name], MAX_PROJECT_BYTES)
                    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                        raise ValueError(f"Export entry does not match the manifest digest: {name}")
                    if not _matches_export_member(name, raw, expected[name]):
                        raise ValueError(f"Export entry differs from trusted workbench data: {name}")
        except (BadZipFile, EOFError, NotImplementedError, RuntimeError) as exc:
            raise ValueError("The export ZIP could not be verified.") from exc
        stream.seek(0)
        archive_digest = hashlib.sha256()
        remaining = archive_size
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("The export changed while hashing.")
            archive_digest.update(chunk)
            remaining -= len(chunk)
        if stream.read(1):
            raise ValueError("The export changed while hashing.")
    run = canonical["run"]
    return {
        "path": str(file), "size_bytes": archive_size, "entries": len(members),
        "sha256": archive_digest.hexdigest(), "project_sha256": rows["project.json"]["sha256"],
        "project_schema_version": canonical["schema_version"], "application_version": WORKBENCH_VERSION,
        "engine_version": __version__, "run_status": run["status"] if run is not None else "NOT_RUN",
    }
