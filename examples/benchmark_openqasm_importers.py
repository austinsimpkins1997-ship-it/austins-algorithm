"""Benchmark direct Qiskit imports of fixed, verified diffuser exports."""

from __future__ import annotations

import argparse
import math
import os
import platform
import sys
import warnings
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING, Any

from austins_algorithm.compilation import MEASUREMENT_TOLERANCE, _distance, _roundtrip_distribution
from austins_algorithm.constraint_diffuser_comparison import DIFFUSER_VARIANT_COUNT, _DiffuserExports
from austins_algorithm.constraint_replay import _parse_request, _read_request, _require_fields
from austins_algorithm.core import _bounded_int
from austins_algorithm.diffuser_benchmark import MAX_BENCHMARK_PAIRS, _verify_trial_report
from austins_algorithm.diffuser_benchmark_verification import _canonical_child
from austins_algorithm.diffuser_replay import _decode_diffuser_replay, _evaluate_diffuser_replay
from austins_algorithm.replay_receipt import _LIMITS, _json_bytes, _regular_file, _software_versions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from qiskit import QuantumCircuit


_MAX_REQUESTS = 8
_IMPORTERS = ("qiskit.qasm3.loads", "qiskit.qasm3.loads_experimental")
_WARMUP = """OPENQASM 3.0;
include "stdgates.inc";
bit[1] c;
qubit[1] q;
x q[0];
c[0] = measure q[0];
"""


@dataclass(frozen=True)
class _Export:
    placement: int
    rounds: int
    diffuser: str
    text: str
    identity: dict[str, Any]
    reference: dict[str, float]

    def verify(self, circuit: QuantumCircuit) -> float:
        active = self.identity["export_wire_to_target_qubit"]
        measured = [active.index(target) for target in self.identity["classical_bit_to_target_qubit"]]
        probabilities = _roundtrip_distribution(
            circuit, qubits=len(active), clbits=len(measured), measured=measured,
        )
        distance = _distance(self.reference, probabilities)
        if not math.isfinite(distance) or distance > MEASUREMENT_TOLERANCE:
            raise ValueError("import changed the expected measurement probabilities")
        return distance


def _variants(report: dict[str, Any]) -> Iterator[tuple[tuple[int, int, str], dict[str, Any]]]:
    for cell in report["cells"]:
        for diffuser, variant in cell["variants"].items():
            yield (cell["placement"], cell["rounds"], diffuser), variant["compilation"]


def _check_export(raw: bytes, identity: dict[str, Any], name: str) -> None:
    if len(raw) > _LIMITS["selected.qasm"]:
        raise ValueError(f"{name} exceeds its OpenQASM size limit")
    if len(raw) != identity["bytes"] or sha256(raw).hexdigest() != identity["sha256"]:
        raise ValueError(f"OpenQASM byte count or SHA-256 differs for {name}")


def _read_artifact(directory: Path, name: str, limit: int) -> bytes:
    path = _canonical_child(directory, name)
    _regular_file(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{name} exceeds its size limit")
    return raw


def _write_artifact(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)


def _prepare(requests: list[Path], output: Path) -> None:
    _bounded_int(len(requests), 1, _MAX_REQUESTS, "request count")
    output.mkdir()
    entries = []
    for index, source in enumerate(requests, start=1):
        raw = _read_request(source)
        document, constraint = _decode_diffuser_replay(raw)
        model = document["sampling"]["model"]
        print(f"Preparing fixed exports for {model}", file=sys.stderr, flush=True)
        exports: _DiffuserExports = {}
        started = perf_counter()
        report = _evaluate_diffuser_replay(
            "request.json", document, constraint, check_only=True, _openqasm_exports=exports,
        )
        capture_wall = perf_counter() - started
        variants = list(_variants(report))
        if (
            report["status"] != "DIFFUSER_REPLAY_VERIFIED"
            or len(variants) != DIFFUSER_VARIANT_COUNT
            or set(exports) != {key for key, _ in variants}
            or len(exports) != DIFFUSER_VARIANT_COUNT
        ):
            raise ValueError("check-only replay did not capture the complete export set")

        directory = output / f"{index:02d}"
        directory.mkdir()
        report_raw = _json_bytes(report)
        _write_artifact(directory / "request.json", raw)
        _write_artifact(directory / "report.json", report_raw)
        _verify_trial_report(directory / "report.json", document, capture_wall)
        for ordinal, (key, compilation) in enumerate(variants, start=1):
            name = f"{ordinal:02d}.qasm"
            qasm = exports[key].encode("utf-8")
            _check_export(qasm, compilation["openqasm"], name)
            _write_artifact(directory / name, qasm)
        entries.append({
            "request_sha256": sha256(raw).hexdigest(),
            "report_sha256": sha256(report_raw).hexdigest(),
            "capture_wall_seconds": capture_wall,
        })
    _write_artifact(output / "corpus.json", _json_bytes({"schema_version": 1, "requests": entries}))


def _load_corpus(root: Path) -> list[tuple[dict[str, Any], list[_Export]]]:
    root = root.resolve(strict=True)
    limit = _LIMITS["report.json"]
    manifest = _require_fields(
        _parse_request(_read_artifact(root, "corpus.json", limit), label="corpus"),
        {"schema_version", "requests"}, "corpus",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported corpus schema_version")
    entries = manifest["requests"]
    if not isinstance(entries, list):
        raise ValueError("corpus requests must be a list")
    _bounded_int(len(entries), 1, _MAX_REQUESTS, "request count")
    workloads = []
    for index, entry in enumerate(entries, start=1):
        entry = _require_fields(
            entry, {"request_sha256", "report_sha256", "capture_wall_seconds"}, "corpus entry",
        )
        directory = _canonical_child(root, f"{index:02d}")
        raw = _read_artifact(directory, "request.json", _LIMITS["request.json"])
        if sha256(raw).hexdigest() != entry["request_sha256"]:
            raise ValueError(f"request SHA-256 differs in corpus entry {index}")
        document, _ = _decode_diffuser_replay(raw)
        report_path = _canonical_child(directory, "report.json")
        _, _, _, fingerprint = _verify_trial_report(report_path, document, entry["capture_wall_seconds"])
        report_raw = _read_artifact(directory, "report.json", limit)
        if sha256(report_raw).hexdigest() != fingerprint["sha256"] or fingerprint["sha256"] != entry["report_sha256"]:
            raise ValueError(f"report SHA-256 differs in corpus entry {index}")
        report = _parse_request(report_raw, label="corpus report")
        exports = []
        for ordinal, (key, compilation) in enumerate(_variants(report), start=1):
            identity = compilation["openqasm"]
            name = f"{ordinal:02d}.qasm"
            qasm = _read_artifact(directory, name, _LIMITS["selected.qasm"])
            _check_export(qasm, identity, f"{directory.name}/{name}")
            exports.append(_Export(
                *key, qasm.decode("utf-8"), identity, compilation["verification"]["compiled_probabilities"],
            ))
        workloads.append(({
            **entry,
            "model": document["sampling"]["model"],
            "snapshot": document["expected"]["snapshot"],
            "circuits_sha256": document["expected"]["circuits_sha256"],
            "plan_sha256": document["expected"]["plan_sha256"],
            "openqasm_bytes": sum(export.identity["bytes"] for export in exports),
        }, exports))
    return workloads


def _timed_load(loader: Callable[[str], QuantumCircuit], text: str) -> tuple[QuantumCircuit, float]:
    started = perf_counter()
    circuit = loader(text)
    return circuit, perf_counter() - started


def _measure(exports: list[_Export], loaders: dict[str, Callable[[str], QuantumCircuit]], pairs: int) -> dict[str, Any]:
    from qiskit.exceptions import QiskitError

    trials = []
    for pair in range(1, pairs + 1):
        order = _IMPORTERS if pair % 2 else _IMPORTERS[::-1]
        for importer in order:
            samples = []
            for export in exports:
                try:
                    circuit, elapsed = _timed_load(loaders[importer], export.text)
                    distance = export.verify(circuit)
                except (QiskitError, ValueError, RuntimeError) as exc:
                    raise ValueError(
                        f"{importer}, placement {export.placement}, round {export.rounds}, {export.diffuser}: {exc}"
                    ) from exc
                samples.append({
                    "placement": export.placement, "rounds": export.rounds, "diffuser": export.diffuser,
                    "openqasm_sha256": export.identity["sha256"], "bytes": export.identity["bytes"],
                    "import_seconds": elapsed, "measurement_total_variation_distance": distance,
                })
            trials.append({
                "pair": pair, "importer": importer,
                "import_seconds": math.fsum(sample["import_seconds"] for sample in samples),
                "samples": samples,
            })
    timings = {}
    for importer in _IMPORTERS:
        times = [trial["import_seconds"] for trial in trials if trial["importer"] == importer]
        timings[importer] = {"min": min(times), "median": median(times), "max": max(times)}
    return {"trials": trials, "import_seconds": timings}


def _run(root: Path, output: Path, pairs: int) -> None:
    _bounded_int(pairs, 1, MAX_BENCHMARK_PAIRS, "pairs")
    workloads = _load_corpus(root)
    from qiskit import qasm3
    from qiskit.exceptions import ExperimentalWarning, QiskitError

    loaders = dict(zip(_IMPORTERS, (qasm3.loads, qasm3.loads_experimental), strict=True))
    output.mkdir()
    results = []
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=ExperimentalWarning, module=r"qiskit\.qasm3")
        for importer, loader in loaders.items():
            try:
                warmup = _roundtrip_distribution(loader(_WARMUP), qubits=1, clbits=1, measured=[0])
            except QiskitError as exc:
                raise ValueError(f"{importer} failed the warm-up circuit: {exc}") from exc
            if _distance(warmup, {"1": 1.0}) > MEASUREMENT_TOLERANCE:
                raise ValueError(f"{importer} did not pass the warm-up measurement check")
        for identity, exports in workloads:
            print(f"Timing both importers on {identity['model']} ({pairs} pairs)", file=sys.stderr, flush=True)
            results.append({**identity, **_measure(exports, loaders, pairs)})
    _write_artifact(output / "summary.json", _json_bytes({
        "schema_version": 1,
        "status": "OPENQASM_IMPORT_BENCHMARK_COMPLETED",
        "pairs": pairs,
        "timing_scope": "direct importer calls on predecoded strings; one untimed one-qubit warm-up per importer",
        "environment": {
            "software_versions": _software_versions(), "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
        },
        "measurement_tolerance": MEASUREMENT_TOLERANCE,
        "local_sampling_performed": False,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "workloads": results,
    }))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="capture and verify fixed exports from check-only requests")
    prepare.add_argument("requests", type=Path, nargs="+")
    prepare.add_argument("--output-dir", type=Path, required=True)
    run = commands.add_parser("run", help="compare direct importers using an existing corpus")
    run.add_argument("corpus", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--pairs", type=int, default=2)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            _prepare(args.requests, args.output_dir)
        else:
            _run(args.corpus, args.output_dir, args.pairs)
    except (AttributeError, ImportError, KeyError, OSError, OverflowError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, f"OpenQASM importer benchmark failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
