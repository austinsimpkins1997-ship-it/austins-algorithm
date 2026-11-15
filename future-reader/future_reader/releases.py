"""Preview and switch independently pinned local HTML releases transactionally."""

from hashlib import sha256
import re
import sqlite3

from .codec import digest

LIMIT = 2 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\Z")
KEYS = {"schema", "name", "version", "commit", "channel", "file",
        "bytes", "sha256", "checkpoint_schema", "compatibility", "migration", "rollback"}


def manifest(raw, version, *, commit=None):
    value = {"schema": "future-reader.release/1", "name": "Future Reader",
             "version": version, "commit": commit, "channel": "LOCAL_PREVIEW",
             "file": "future-reader.html", "bytes": len(raw), "sha256": sha256(raw).hexdigest(),
             "checkpoint_schema": "future-reader.browser-checkpoint/1",
             "compatibility": "Modern browser with JavaScript and local file support",
             "migration": "Same checkpoint schema. Export a checkpoint before switching.",
             "rollback": "Switch to the previous verified artifact. Reopen its compatible checkpoint."}
    _validate(value)
    return value


def _version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError("Release version must be a bounded major.minor.patch")
    return tuple(map(int, value.split(".")))


def _validate(value):
    if (not isinstance(value, dict) or set(value) != KEYS
            or value["schema"] != "future-reader.release/1" or value["name"] != "Future Reader"
            or value["channel"] != "LOCAL_PREVIEW" or value["file"] != "future-reader.html"
            or value["checkpoint_schema"] != "future-reader.browser-checkpoint/1"):
        raise ValueError("Unsupported release manifest")
    _version(value["version"])
    if value["commit"] is not None and (not isinstance(value["commit"], str)
                                        or not re.fullmatch("[a-f0-9]{40}", value["commit"])):
        raise ValueError("Invalid release commit")
    if type(value["bytes"]) is not int or not 1 <= value["bytes"] <= LIMIT:
        raise ValueError("Invalid release byte count")
    if not isinstance(value["sha256"], str) or not HEX.fullmatch(value["sha256"]):
        raise ValueError("Invalid release artifact digest")
    for key in ("compatibility", "migration", "rollback"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 512:
            raise ValueError("Invalid release guidance")


class ReleaseStore:
    """Owns a local artifact pointer and previous bytes; it never opens the HTML."""

    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS releases (sha TEXT PRIMARY KEY, manifest BLOB NOT NULL, html BLOB NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS active_release (id INTEGER PRIMARY KEY CHECK(id=1), current TEXT, previous TEXT, generation INTEGER NOT NULL)")
        self.connection.execute("INSERT OR IGNORE INTO active_release VALUES (1,NULL,NULL,0)")
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def _pointer(self):
        return self.connection.execute("SELECT current,previous,generation FROM active_release WHERE id=1").fetchone()

    def _artifact(self, fingerprint):
        import json

        row = self.connection.execute("SELECT manifest,html FROM releases WHERE sha=?", (fingerprint,)).fetchone()
        if row is None:
            raise ValueError("Verified release artifact is missing")
        return json.loads(row[0]), row[1]

    def preview(self, value, raw, *, trusted_manifest_sha256):
        _validate(value)
        identity = digest(value)
        if identity != trusted_manifest_sha256:
            raise ValueError("Manifest does not match the independently supplied pin")
        if not isinstance(raw, bytes) or len(raw) != value["bytes"] or sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("Release artifact does not match the manifest")
        active, previous, generation = self._pointer()
        if active:
            current, _ = self._artifact(active)
            if _version(value["version"]) <= _version(current["version"]):
                raise ValueError("Use rollback for earlier versions")
        payload = {"schema": "future-reader.release-preview/1", "candidate": identity,
                   "active": active, "previous": previous, "generation": generation, "version": value["version"],
                   "trust": "CALLER_PIN_MATCH", "compatibility": value["compatibility"],
                   "migration": value["migration"], "rollback": value["rollback"]}
        return {**payload, "approval_digest": digest(payload)}

    def apply(self, value, raw, preview, *, trusted_manifest_sha256, approval_digest):
        from .codec import canonical

        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            expected = self.preview(value, raw, trusted_manifest_sha256=trusted_manifest_sha256)
            if preview != expected or approval_digest != expected["approval_digest"]:
                raise ValueError("Release preview is stale or unapproved")
            self.connection.execute("INSERT OR IGNORE INTO releases VALUES (?,?,?)",
                                    (expected["candidate"], canonical(value), raw))
            stored, staged = self._artifact(expected["candidate"])
            if stored != value or staged != raw:
                raise ValueError("Staged release did not verify")
            self.connection.execute("UPDATE active_release SET current=?,previous=?,generation=generation+1 WHERE id=1",
                                    (expected["candidate"], expected["active"]))
        return self.status()

    def status(self):
        current, previous, generation = self._pointer()
        return {"active": current, "previous": previous, "generation": generation}

    def active_bytes(self):
        current = self._pointer()[0]
        value, raw = self._artifact(current)
        _validate(value)
        if current != digest(value) or len(raw) != value["bytes"] or sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("Active release bytes or metadata are corrupted")
        return raw

    def rollback(self, *, expected_generation, compatible_checkpoint_schema):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current, previous, generation = self._pointer()
            if not previous or generation != expected_generation:
                raise ValueError("Rollback target is absent or stale")
            value, raw = self._artifact(previous)
            _validate(value)
            if (compatible_checkpoint_schema != value["checkpoint_schema"] or digest(value) != previous
                    or sha256(raw).hexdigest() != value["sha256"] or len(raw) != value["bytes"]):
                raise ValueError("Rollback release or checkpoint schema does not verify")
            self.connection.execute("UPDATE active_release SET current=?,previous=?,generation=generation+1 WHERE id=1",
                                    (previous, current))
        return self.status()
