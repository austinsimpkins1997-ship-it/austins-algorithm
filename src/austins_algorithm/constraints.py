"""Validate and interpret a bounded JSON Boolean constraint."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .grover_search import MAX_AUDIT_SEARCH_WIDTH


MAX_CLAUSES = 12
MAX_LITERALS_PER_CLAUSE = 2 * MAX_AUDIT_SEARCH_WIDTH
MAX_CONSTRAINT_BYTES = 16 * 1024


@dataclass(frozen=True)
class BooleanConstraint:
    """Conjunctive normal form, with signed one-based literals for q0, q1, ..."""

    qubits: int
    clauses: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if type(self.qubits) is not int or not 1 <= self.qubits <= MAX_AUDIT_SEARCH_WIDTH:
            raise ValueError(f"qubits must be an integer from 1 to {MAX_AUDIT_SEARCH_WIDTH}")
        if not isinstance(self.clauses, (tuple, list)) or len(self.clauses) > MAX_CLAUSES:
            raise ValueError(f"clauses must be an array of at most {MAX_CLAUSES} clauses")
        normalized = []
        for clause in self.clauses:
            if not isinstance(clause, (tuple, list)) or len(clause) > MAX_LITERALS_PER_CLAUSE:
                raise ValueError(f"each clause must be an array of at most {MAX_LITERALS_PER_CLAUSE} literals")
            if any(type(literal) is not int or not 1 <= abs(literal) <= self.qubits for literal in clause):
                raise ValueError(f"literals must be signed nonzero integers from -{self.qubits} to {self.qubits}")
            normalized.append(tuple(clause))
        object.__setattr__(self, "clauses", tuple(normalized))

    def matches(self, bits: str) -> bool:
        """Interpret the clauses independently in displayed MSB-first order."""
        if not isinstance(bits, str) or len(bits) != self.qubits or set(bits) - {"0", "1"}:
            return False
        return all(
            any((bits[-abs(literal)] == "1") == (literal > 0) for literal in clause)
            for clause in self.clauses
        )

    def digest(self) -> str:
        """Hash the versioned formula, independent of JSON whitespace."""
        canonical = json.dumps(
            {"schema_version": 1, "qubits": self.qubits, "clauses": self.clauses},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate constraint JSON key: {key}")
        result[key] = value
    return result


def load_constraint(path: str | Path) -> BooleanConstraint:
    """Read a version-1 constraint document from a bounded UTF-8 JSON file."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_CONSTRAINT_BYTES + 1)
    if len(raw) > MAX_CONSTRAINT_BYTES:
        raise ValueError(f"constraint JSON exceeds {MAX_CONSTRAINT_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid constraint JSON: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "qubits", "clauses"}:
        raise ValueError("constraint JSON requires exactly schema_version, qubits and clauses")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported constraint schema_version")
    return BooleanConstraint(document["qubits"], document["clauses"])
