"""Bounded in-process storage for verified snapshot round profiles."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from threading import RLock
from typing import TYPE_CHECKING, Any

from .compilation import _DEFAULT_OPTIMIZATION_LEVEL
from .core import _bounded_int

if TYPE_CHECKING:
    from qiskit import QuantumCircuit
    from qiskit.providers import BackendV2


class SnapshotRoundProfileCache:
    """Share completed, detached round profiles inside one process."""

    def __init__(self, *, max_entries: int = 64, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._max_entries = _bounded_int(max_entries, 1, 256, "cache max_entries")
        self._max_bytes = _bounded_int(max_bytes, 1024, 32 * 1024 * 1024, "cache max_bytes")
        self._entries: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self._evictions = 0
        self._unconfirmed = False
        self._lock = RLock()

    def stats(self) -> dict[str, int | bool]:
        """Return current limits, retained JSON bytes, and cancellation state."""
        with self._lock:
            return {
                "entries": len(self._entries), "bytes": self._bytes, "max_entries": self._max_entries,
                "max_bytes": self._max_bytes, "evictions": self._evictions, "unconfirmed": self._unconfirmed,
            }

    @contextmanager
    def _prepare(self) -> Iterator[None]:
        with self._lock:
            if self._unconfirmed:
                raise RuntimeError("snapshot round profile cache cannot be reused after unconfirmed simulator cancellation")
            yield

    def _get(self, key: str) -> dict[str, Any] | None:
        payload = self._entries.get(key)
        if payload is None:
            return None
        self._entries.move_to_end(key)
        return json.loads(payload)

    def _publish(self, entries: dict[str, dict[str, Any]]) -> int:
        # Serialize all eligible entries before modifying the cache. A returned
        # report cannot mutate stored evidence.
        encoded = {key: _json_bytes(entry) for key, entry in entries.items()}
        published = 0
        for key, data in encoded.items():
            if len(data) > self._max_bytes:
                continue
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            while len(self._entries) >= self._max_entries or self._bytes + len(data) > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)
                self._evictions += 1
            self._entries[key] = data
            self._bytes += len(data)
            published += 1
        return published


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def _target_profile(backend: BackendV2) -> dict[str, Any]:
    target = backend.target
    instructions = {}
    for name, entries in target.items():
        operation = target.operation_from_name(name)
        kind = operation if isinstance(operation, type) else type(operation)
        parameters = [] if isinstance(operation, type) else list(map(str, operation.params))
        instructions[name] = {
            "operation": [kind.__module__, kind.__qualname__, parameters],
            "properties": {
                str(wires): None if value is None else [value.duration, value.error]
                for wires, value in (entries or {}).items()
            },
        }
    return {
        "backend_class": [type(backend).__module__, type(backend).__qualname__],
        "target": {field: getattr(target, field) for field in (
            "num_qubits", "dt", "granularity", "min_length", "pulse_alignment", "acquire_alignment",
        )},
        "qubit_properties": None if target.qubit_properties is None else [
            None if properties is None else [properties.t1, properties.t2, properties.frequency]
            for properties in target.qubit_properties
        ],
        "instructions": instructions,
    }


def _compilation_scope(
    backend: BackendV2, snapshot: dict[str, Any], versions: dict[str, str], seed: int,
) -> dict[str, Any]:
    try:
        importer = version("qiskit-qasm3-import")
    except PackageNotFoundError:
        importer = None
    return {
        "schema": 1, "snapshot": snapshot, "target": _target_profile(backend),
        "versions": {**versions, "qiskit-qasm3-import": importer},
        "compiler_seed": seed, "optimization_level": _DEFAULT_OPTIMIZATION_LEVEL,
        "prefer_native_qasm": False,
    }


def _cache_scope(backend: BackendV2, snapshot: dict[str, Any], versions: dict[str, str], seed: int) -> bytes:
    return _json_bytes({
        **_compilation_scope(backend, snapshot, versions, seed), "noise_path": "compact-density-local-readout",
    })


def _round_profile_key(circuit: QuantumCircuit, anchor: Sequence[int], scope: bytes) -> str | None:
    from qiskit import qpy
    from qiskit.circuit import Measure
    from qiskit.circuit.library import CCXGate, CCZGate, CXGate, CZGate, HGate, MCXGate, XGate, ZGate

    # QPY does not preserve arbitrary Python attributes on custom operations.
    # Cache only the stock circuit vocabulary and untouched generic MCX gates.
    stock = {gate: gate() for gate in (HGate, XGate, ZGate, CXGate, CZGate, CCXGate, CCZGate, Measure)}
    singletons = {type(gate) for gate in stock.values()}
    for item in circuit.data:
        operation = item.operation
        if type(operation) in singletons and not operation.mutable:
            continue
        reference = stock.get(type(operation))
        if type(operation) is MCXGate:
            reference = MCXGate(operation.num_ctrl_qubits)
        if reference is None or vars(operation) != vars(reference):
            return None
    payload = BytesIO()
    try:
        qpy.dump(circuit, payload)
    except (qpy.QpyError, ValueError, TypeError, RecursionError):
        return None
    fingerprint = hashlib.sha256(payload.getbuffer()).hexdigest()
    return hashlib.sha256(scope + b"\0" + _json_bytes(anchor) + b"\0" + fingerprint.encode("ascii")).hexdigest()
