"""Opt-in review of the pinned Beyond Infinity's Bounds native ledger."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .codec import MAX_JSON, loads


BIB_LEDGER_SHA256 = "024420b996c5778047e984c2f514bdf7d7386e64711f599c58c7f626ae662d98"
BIB_BINDING = "future-reader.bib-review/1"
MAX_ENTRIES = 1_024
_RUNNER = r"""
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType

with Path(sys.argv[1]).open("rb") as stream:
    raw = stream.read(65537)
if sha256(raw).hexdigest() != sys.argv[2]:
    print(json.dumps({"error": "Native BIB implementation does not match the reviewed digest."}))
    raise SystemExit(1)

native = ModuleType("bib.ledger")
exec(compile(raw, sys.argv[1], "exec"), native.__dict__)
try:
    if sys.argv[3] == "verify":
        entries = json.load(sys.stdin)
    else:
        first = native.build_entry(sequence=0, temporal_standing="WAS",
                                   claim="A synthetic practice record was created.",
                                   times={"entry_time": "2026-11-13T12:00:00Z"})
        second = native.build_entry(sequence=1, temporal_standing="TO_BE",
                                    claim="A synthetic practice follow-up was proposed.",
                                    times={"entry_time": "2026-11-13T12:01:00Z"},
                                    previous_entry_sha256=first["entry_sha256"])
        entries = native.append_entry(native.append_entry([], first), second)
    native.verify_chain(entries)
except (ValueError, TypeError, AttributeError, UnicodeError):
    print(json.dumps({"error": "Native BIB verification rejected this ledger."}))
    raise SystemExit(1)
print(json.dumps({"entries": entries} if sys.argv[3] == "sample" else {"verified": True}, allow_nan=False))
"""


def _native(bib_source, action, *, raw=b""):
    source = Path(bib_source)
    if source.is_dir():
        source = source / "bib" / "ledger.py"
    source = source.resolve(strict=True)
    if not source.is_file() or source.stat().st_size > 64 * 1024:
        raise ValueError("Select the reviewed BIB ledger.py file or its src directory.")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", _RUNNER, str(source), BIB_LEDGER_SHA256, action],
            input=raw, capture_output=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("The native BIB check timed out.") from error
    try:
        report = loads(result.stdout, limit=MAX_JSON)
    except ValueError as error:
        raise ValueError("The native BIB check did not return a usable result.") from error
    if result.returncode:
        raise ValueError(report.get("error", "The native BIB check failed."))
    return report


def inspect_bib(ledger, *, bib_source):
    """Read a named BIB JSON chain and preserve the native record axes."""
    ledger = Path(ledger)
    if not ledger.is_file():
        raise ValueError("Select an existing BIB JSON ledger.")
    with ledger.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    entries = loads(raw)
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ENTRIES or any(type(entry) is not dict for entry in entries):
        raise ValueError("Expected a native BIB entry array with 1–1,024 records.")
    if _native(bib_source, "verify", raw=raw).get("verified") is not True:
        raise ValueError("The native BIB check did not confirm this ledger.")
    return {
        "schema": BIB_BINDING, "status": "NATIVE_CHAIN_VERIFIED",
        "mode": "CALLER_SUPPLIED_REVIEW", "native_schema": "1.0",
        "native_implementation_sha256": BIB_LEDGER_SHA256,
        "source_sha256": sha256(raw).hexdigest(), "entries": len(entries),
        "chain_head": entries[-1]["entry_sha256"],
        "records": [
            {"record_id": row["record_id"], "sequence": row["sequence"],
             "source_entry_sha256": row["entry_sha256"], "source_predecessor_sha256": row["previous_entry_sha256"],
             "native_temporal_standing": row["temporal_standing"],
             "native_evidence_status": row["evidence_status"],
             "native_validation_status": row["validation_status"],
             "claim_as_recorded": row["claim"], "evidence_refs_as_recorded": row["evidence_refs"],
             "relationships_as_recorded": row["relationships"], "times_as_recorded": row["times"],
             "uncertainty_as_recorded": row["uncertainty"]}
            for row in entries
        ],
        "limitations": [
            "Native labels stay in the BIB namespace. This view assigns no ChronoTense standing, review step or availability time.",
            "The native check verifies record shape, links and local digests. Publisher provenance and claim truth remain unresolved.",
            "The supplied record gives the controller no authority, execution grant or storage ownership.",
        ],
    }


def export_bib_fixture(destination, *, bib_source):
    """Build a native synthetic chain at a new destination and verify its bytes."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Choose a fresh destination for the synthetic BIB fixture.")
    entries = _native(bib_source, "sample")["entries"]
    raw = (json.dumps(entries, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii")
    with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=".bib-", delete=False) as stream:
        stage = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            stage.unlink(missing_ok=True)
            raise
    try:
        report = inspect_bib(stage, bib_source=bib_source)
        os.link(stage, destination)
    finally:
        stage.unlink(missing_ok=True)
    return {**report, "fixture_kind": "SYNTHETIC", "output": str(destination)}
