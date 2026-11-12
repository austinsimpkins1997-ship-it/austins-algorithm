"""Caller-owned storage for verified compiled rounds on dated IBM targets."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from .compilation import CompiledProgram
from .core import _bounded_int
from .snapshot_calibration_compilation_bundle import _read_bundle, _restore_compiled_round, _write_bundle
from .unknown_count_snapshot_cache import _json_bytes

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2


class SnapshotCalibrationCompilationCache:
    """Reuse verified compilations across dated captures in one Python process.

    The byte limit counts retained OpenQASM and verification report bytes.
    The entry limit also bounds the number of retained physical circuits.
    """

    def __init__(self, *, max_entries: int = 64, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._max_entries = _bounded_int(max_entries, 1, 256, "cache max_entries")
        self._max_bytes = _bounded_int(max_bytes, 1024, 32 * 1024 * 1024, "cache max_bytes")
        self._entries: OrderedDict[str, tuple[CompiledProgram, int]] = OrderedDict()
        self._bundle = {}
        self._bundle_sha256: str | None = None
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = Lock()

    def stats(self) -> dict[str, int]:
        """Return cumulative keyed lookups, evictions, and current capacity."""
        with self._lock:
            return {
                "entries": len(self._entries), "bytes": self._bytes,
                "max_entries": self._max_entries, "max_bytes": self._max_bytes,
                "hits": self._hits, "misses": self._misses, "evictions": self._evictions,
            }

    def save_bundle(self, path: str | Path) -> str:
        """Publish the completed resident rounds to a new file and return its SHA-256."""
        destination = Path(path)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"compilation bundle already exists: {destination}")
        with self._lock:
            programs = {key: entry[0] for key, entry in self._entries.items()}
        return _write_bundle(destination, programs, self._bundle)

    @classmethod
    def load_bundle(cls, path: str | Path, *, expected_sha256: str) -> SnapshotCalibrationCompilationCache:
        """Open a trusted pinned bundle; decode its circuits on matching requests."""
        bundle = _read_bundle(Path(path), expected_sha256)
        return cls._from_verified_bundle(bundle, expected_sha256)

    @classmethod
    def _from_verified_bundle(
        cls, entries: dict[str, dict[str, Any]], expected_sha256: str,
    ) -> SnapshotCalibrationCompilationCache:
        """Use entries after their exact bundle bytes and runtime stack have been checked."""
        cache = cls(max_entries=256, max_bytes=32 * 1024 * 1024)
        cache._bundle = entries
        cache._bundle_sha256 = expected_sha256
        return cache

    def _bundle_coverage(self) -> tuple[str, frozenset[str]]:
        """Return the pinned bundle identity and saved keys without touching the resident cache."""
        if self._bundle_sha256 is None:
            raise ValueError("compilation bundle coverage requires a cache loaded from a pinned bundle")
        return self._bundle_sha256, frozenset(self._bundle)

    def _get(
        self, key: str, logical: QuantumCircuit, backend: BackendV2, layout: tuple[int, ...], seed: int, *,
        prefer_native_qasm: bool = False,
    ) -> tuple[CompiledProgram | None, bool]:
        """Look up a round and indicate whether it needs resident publication."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._hits += 1
                self._entries.move_to_end(key)
            bundled = self._bundle.get(key)
            if entry is None and bundled is None:
                self._misses += 1
                return None, True
        if entry is not None:
            # Never expose the retained circuit or mutable report to a capture.
            return deepcopy(entry[0]), False

        program = _restore_compiled_round(
            bundled, logical, backend, layout, seed, prefer_native_qasm=prefer_native_qasm,
        )
        with self._lock:
            self._hits += 1
        return program, True

    def _publish_completed(self, programs: dict[str, CompiledProgram]) -> None:
        entries = {}
        for key, program in programs.items():
            size = len(program.openqasm.encode("utf-8")) + len(_json_bytes(program.report))
            if size <= self._max_bytes:
                entries[key] = deepcopy(program), size

        with self._lock:
            for key, entry in entries.items():
                # An overlapping capture may already have published this key.
                if key in self._entries:
                    self._entries.move_to_end(key)
                    continue
                _, size = entry
                while len(self._entries) >= self._max_entries or self._bytes + size > self._max_bytes:
                    _, (_, evicted_size) = self._entries.popitem(last=False)
                    self._bytes -= evicted_size
                    self._evictions += 1
                self._entries[key] = entry
                self._bytes += size
