"""Compare the fixed Austin circuit with local models and an explicit IBM job."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from pathlib import Path
from time import perf_counter
from typing import Any

from .compilation import compile_and_verify
from .core import austin_predicate
from .qiskit_adapter import MAX_LOCAL_SHOTS, build_austin_circuit


MAX_HARDWARE_SHOTS = 4_096
MAX_QPU_EXECUTION_SECONDS = 10_800
MAX_WAIT_TIMEOUT_SECONDS = 3_600


def _bounded_int(name: str, value: int, lower: int, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer from {lower} to {upper}")


@dataclass(frozen=True)
class IBMHardwareRun:
    """Select a saved account, service instance, backend and QPU time budget."""

    backend_name: str
    account_name: str
    instance: str
    max_execution_seconds: int
    wait_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        for name in ("backend_name", "account_name", "instance"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be an explicit nonempty name")
        if self.instance.lower() == "auto":
            raise ValueError("instance must identify an IBM Quantum service instance")
        _bounded_int("max_execution_seconds", self.max_execution_seconds, 1, MAX_QPU_EXECUTION_SECONDS)
        _bounded_int("wait_timeout_seconds", self.wait_timeout_seconds, 1, MAX_WAIT_TIMEOUT_SECONDS)


def summarize_austin_counts(
    counts: Mapping[str, int], *, requested_shots: int
) -> dict[str, Any]:
    """Verify the fixed three-bit predicate over measured MSB-first bitstrings."""
    return summarize_search_counts(counts, requested_shots=requested_shots, width=3, verifier=austin_predicate)


def summarize_search_counts(
    counts: Mapping[str, int], *, requested_shots: int, width: int, verifier: Callable[[str], bool]
) -> dict[str, Any]:
    """Validate shot counts and check each distinct MSB-first candidate."""
    _bounded_int("requested_shots", requested_shots, 1, MAX_LOCAL_SHOTS)
    _bounded_int("width", width, 1, 16)
    if not callable(verifier):
        raise TypeError("verifier must be callable")
    if not isinstance(counts, Mapping):
        raise ValueError("counts must map bitstrings to positive integers")
    validated_counts = {}
    for bits, count in counts.items():
        if (
            not isinstance(bits, str)
            or len(bits) != width
            or (bits.strip("01") if type(bits) is str else any(bit not in "01" for bit in bits))
        ):
            raise ValueError(f"measurement must be a {width}-bit MSB-first binary string")
        _bounded_int("count", count, 1, requested_shots)
        validated_counts[bits] = count
    completed_shots = sum(validated_counts.values())
    if not 1 <= completed_shots <= requested_shots:
        raise ValueError("completed shots must be positive and no greater than requested shots")
    ordered_bits = sorted(validated_counts)
    verified_candidates = []
    for bits in ordered_bits:
        accepted = verifier(bits)
        if not isinstance(accepted, bool):
            raise TypeError("verifier must return a bool")
        if accepted:
            verified_candidates.append(bits)
    verified_shots = sum(validated_counts[bits] for bits in verified_candidates)
    return {
        "counts": {bits: validated_counts[bits] for bits in ordered_bits},
        "requested_shots": requested_shots,
        "completed_shots": completed_shots,
        "verified_shots": verified_shots,
        "verified_candidates": verified_candidates,
        "verified_hit_rate": verified_shots / completed_shots,
    }


def austin_classical_baseline() -> dict[str, Any]:
    """Solve q0 = 1 and q1 != q2 directly, then check the two answers."""
    started = perf_counter()
    solutions = [f"{q2}{1 - q2}1" for q2 in range(2)]
    solved_at = perf_counter()
    verified = [bits for bits in solutions if austin_predicate(bits)]
    finished = perf_counter()
    return {
        "method": "direct_boolean_constraint",
        "solutions": verified,
        "predicate_verifications": len(solutions),
        "solve_seconds": solved_at - started,
        "verify_seconds": finished - solved_at,
        "wall_seconds": finished - started,
    }


def _circuit_resources(circuit: Any) -> dict[str, Any]:
    return {
        "qubits": circuit.num_qubits,
        "depth": circuit.depth(),
        "two_qubit_gates": sum(
            len(instruction.qubits) == 2 and instruction.operation.name != "barrier"
            for instruction in circuit.data
        ),
        "operation_counts": dict(circuit.count_ops()),
    }


def _local_report(counts: Mapping[str, int], shots: int) -> dict[str, Any]:
    report = summarize_austin_counts(counts, requested_shots=shots)
    if report["completed_shots"] != shots:
        raise RuntimeError("local simulator returned an incomplete shot count")
    return {
        "execution_mode": "SIMULATOR",
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
        **report,
    }


def _ideal_simulation(circuit: Any, *, shots: int, seed: int) -> dict[str, Any]:
    from qiskit.primitives import StatevectorSampler

    started = perf_counter()
    counts = StatevectorSampler(seed=seed).run([circuit], shots=shots).result()[0].data.answer.get_counts()
    sampled_at = perf_counter()
    result = _local_report(counts, shots)
    finished = perf_counter()
    return {
        "method": "qiskit_statevector_sampler",
        **result,
        "sample_seconds": sampled_at - started,
        "verify_seconds": finished - sampled_at,
        "wall_seconds": finished - started,
    }


def _depolarizing_channel(width: int, probability: float) -> Any:
    from qiskit.quantum_info import Kraus, Pauli

    # (1 - p) rho + p I / 2**width is the mixture with uniformly random Paulis.
    base_weight = probability / (4**width)
    operators = []
    for letters in product("IXYZ", repeat=width):
        label = "".join(letters)
        weight = 1.0 - probability + base_weight if label == "I" * width else base_weight
        operators.append(math.sqrt(weight) * Pauli(label).to_matrix())
    return Kraus(operators)


def _noisy_simulation(circuit: Any, *, shots: int, seed: int, probability: float) -> dict[str, Any]:
    from qiskit.quantum_info import DensityMatrix

    started = perf_counter()
    unitary = circuit.remove_final_measurements(inplace=False)
    density = DensityMatrix.from_label("000")
    channels = {}
    for instruction in unitary.data:
        indices = [unitary.find_bit(bit).index for bit in instruction.qubits]
        density = density.evolve(instruction.operation, qargs=indices)
        if probability:
            width = len(indices)
            if width not in channels:
                channels[width] = _depolarizing_channel(width, probability)
            density = density.evolve(channels[width], qargs=indices)
    distribution = {bits: float(value) for bits, value in sorted(density.probabilities_dict().items())}
    weights = list(distribution.values())
    if any(not math.isfinite(value) or value < 0 for value in weights) or not math.isclose(sum(weights), 1.0):
        raise RuntimeError("synthetic noise model produced an invalid probability distribution")
    modelled_at = perf_counter()
    counts = Counter(random.Random(seed).choices(list(distribution), weights=weights, k=shots))
    sampled_at = perf_counter()
    result = _local_report(counts, shots)
    finished = perf_counter()
    return {
        "method": "qiskit_density_matrix_with_synthetic_depolarization",
        "noise_probability_per_logical_gate": probability,
        "calibration": "NONE_SYNTHETIC",
        "model_probabilities": distribution,
        "model_hit_probability": sum(
            value for bits, value in distribution.items() if austin_predicate(bits)
        ),
        **result,
        "model_seconds": modelled_at - started,
        "sample_seconds": sampled_at - modelled_at,
        "verify_seconds": finished - sampled_at,
        "wall_seconds": finished - started,
    }


def _load_ibm_runtime() -> tuple[Any, ...]:
    from qiskit_ibm_runtime import IBMBackend, QiskitRuntimeService
    from qiskit_ibm_runtime.accounts.exceptions import AccountNotFoundError
    from qiskit_ibm_runtime.exceptions import (
        RuntimeInvalidStateError,
        RuntimeJobFailureError,
        RuntimeJobMaxTimeoutError,
        RuntimeJobTimeoutError,
    )
    from qiskit_ibm_runtime.executor_sampler import Sampler

    return (
        QiskitRuntimeService, IBMBackend, Sampler,
        RuntimeJobTimeoutError, RuntimeJobMaxTimeoutError, RuntimeJobFailureError,
        RuntimeInvalidStateError, AccountNotFoundError,
    )


def _ibm_account_file() -> Path:
    return Path.home() / ".qiskit" / "qiskit-ibm.json"


def _is_named_qpu(backend: Any, backend_type: type, name: str) -> bool:
    return (
        isinstance(backend, backend_type)
        and backend.name == name
        and backend.is_mock is False
        and backend.simulator is False
        and backend.local is False
    )


def _seconds_between(timestamps: Mapping[str, Any], before: str, after: str) -> float | None:
    try:
        start = datetime.fromisoformat(timestamps[before].replace("Z", "+00:00"))
        end = datetime.fromisoformat(timestamps[after].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or end < start:
            return None
        return (end - start).total_seconds()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _provider_metrics(job: Any) -> dict[str, Any]:
    metrics = job.metrics()
    usage = metrics.get("usage", {})
    timestamps = metrics.get("timestamps", {})
    quantum_seconds = usage.get("qpu_charge_time_seconds", usage.get("quantum_seconds"))
    if (
        usage.get("status") == "pending"
        or isinstance(quantum_seconds, bool)
        or not isinstance(quantum_seconds, (float, int))
        or not math.isfinite(quantum_seconds)
        or quantum_seconds < 0
    ):
        quantum_seconds = None
    return {
        "provider_reported_qpu_seconds": quantum_seconds,
        "provider_reported_queue_seconds": _seconds_between(timestamps, "created", "running"),
        "provider_reported_running_seconds": _seconds_between(timestamps, "running", "finished"),
    }


def _run_ibm_hardware(
    circuit: Any, hardware: IBMHardwareRun, shots: int, seed: int, *, preflight_only: bool,
) -> dict[str, Any]:
    started = perf_counter()
    report: dict[str, Any] = {
        "status": "NOT_SUBMITTED",
        "provider": "IBM Quantum",
        "requested_execution_mode": "QPU",
        "preflight_only": preflight_only,
        "requested_backend": hardware.backend_name,
        "verified_target": None,
        "actual_backend": None,
        "job_id": None,
        "requested_shots": shots,
        "completed_shots": None,
        "max_qpu_execution_seconds": hardware.max_execution_seconds,
        "wait_timeout_seconds": hardware.wait_timeout_seconds,
        "provider_job_submitted": False,
        "quantum_hardware_used": False,
        "provider_reported_qpu_seconds": None,
        "provider_reported_queue_seconds": None,
        "provider_reported_running_seconds": None,
        "provider_reported_monetary_cost": None,
        "transpiler_seed": seed,
        "optimization_level": 1,
        "timing_seconds": {},
    }
    timings = report["timing_seconds"]
    stage = "load_runtime"
    stage_started = started
    try:
        (service_type, backend_type, sampler_type, wait_error, limit_error,
         job_error, state_error, account_error) = _load_ibm_runtime()
        stage = "load_account"
        stage_started = perf_counter()
        account_file = _ibm_account_file()
        if not account_file.is_file():
            report["status"] = "IBM_SAVED_PROFILE_UNAVAILABLE"
            return report
        try:
            service = service_type(
                filename=str(account_file), name=hardware.account_name, instance=hardware.instance,
            )
        except account_error:
            report["status"] = "IBM_SAVED_PROFILE_UNAVAILABLE"
            return report
        if service.is_local is not False:
            report["status"] = "LOCAL_SERVICE_REJECTED"
            return report
        timings["load_account"] = perf_counter() - stage_started
        stage = "select_backend"
        stage_started = perf_counter()
        backend = service.backend(hardware.backend_name, instance=hardware.instance)
        timings["select_backend"] = perf_counter() - stage_started
        if not _is_named_qpu(backend, backend_type, hardware.backend_name):
            report["status"] = "NON_QPU_OR_WRONG_BACKEND"
            return report
        stage = "backend_status"
        stage_started = perf_counter()
        if backend.status().operational is not True:
            report["status"] = "BACKEND_NOT_OPERATIONAL"
            return report
        timings["backend_status"] = perf_counter() - stage_started
        stage = "compile_and_verify"
        stage_started = perf_counter()
        compiled = compile_and_verify(circuit, backend=backend, optimization_level=1, seed=seed)
        report["verified_target"] = backend.name
        report["compiled_circuit"] = _circuit_resources(compiled.circuit)
        report["compilation"] = compiled.report
        timings["compile_and_verify"] = perf_counter() - stage_started
        timings["transpile"] = compiled.timing_seconds["transpile"]
        if preflight_only:
            report["status"] = "TARGET_COMPILED_AND_VERIFIED"
            return report
        stage = "configure_sampler"
        stage_started = perf_counter()
        sampler = sampler_type(
            mode=backend,
            options={
                "max_execution_time": hardware.max_execution_seconds,
                "twirling": {"enable_gates": False, "enable_measure": False},
            },
        )
        if sampler.options.max_execution_time != hardware.max_execution_seconds:
            report["status"] = "QPU_BUDGET_NOT_CONFIGURED"
            return report
        timings["configure_sampler"] = perf_counter() - stage_started
        stage = "submit"
        stage_started = perf_counter()
        # A request may be accepted even if the client loses the response.
        report.update(provider_job_submitted=None, quantum_hardware_used=None)
        job = sampler.run([compiled.circuit], shots=shots, dry_run=False)
        report.update(provider_job_submitted=True, job_id=job.job_id())
        timings["submit"] = perf_counter() - stage_started
        stage = "wait_and_fetch"
        stage_started = perf_counter()
        try:
            result = job.result(timeout=hardware.wait_timeout_seconds)
        except (wait_error, TimeoutError):
            report["status"] = "WAIT_TIMED_OUT"
            return report
        except limit_error:
            report["status"] = "QPU_EXECUTION_LIMIT_EXCEEDED"
            return report
        except job_error:
            report["status"] = "PROVIDER_JOB_FAILED"
            return report
        except state_error:
            report["status"] = "PROVIDER_JOB_UNAVAILABLE"
            return report
        timings["wait_and_fetch"] = perf_counter() - stage_started
        stage = "verify_results"
        stage_started = perf_counter()
        executed_backend = job.backend(timeout=hardware.wait_timeout_seconds)
        if not _is_named_qpu(executed_backend, backend_type, hardware.backend_name):
            report["status"] = "EXECUTED_BACKEND_UNVERIFIED"
            return report
        report.update(
            actual_backend=executed_backend.name,
            execution_mode="QPU",
            quantum_hardware_used=True,
        )
        measurements = summarize_austin_counts(
            result[0].data.answer.get_counts(), requested_shots=shots
        )
        report.update(
            status="COMPLETED" if measurements["completed_shots"] == shots else "PARTIAL_RESULTS",
            **measurements,
        )
        timings["verify_results"] = perf_counter() - stage_started
        stage = "fetch_usage"
        stage_started = perf_counter()
        try:
            report.update(_provider_metrics(job))
        except Exception as exc:
            # Provider exceptions may embed account details. Record only their class.
            report["usage_error_type"] = type(exc).__name__
        return report
    except Exception as exc:
        if stage == "load_runtime" and isinstance(exc, ImportError):
            report["status"] = "IBM_RUNTIME_UNAVAILABLE"
        elif stage == "submit" and report["provider_job_submitted"] is None:
            report["status"] = "SUBMISSION_UNKNOWN"
        else:
            report["status"] = "PROVIDER_STAGE_FAILED"
        report.update(failed_stage=stage, error_type=type(exc).__name__)
        return report
    finally:
        finished = perf_counter()
        timings.setdefault(stage, finished - stage_started)
        timings["wall"] = finished - started


def run_austin_device_comparison(
    *,
    shots: int = 1024,
    seed: int = 7,
    noise_probability: float = 0.02,
    hardware: IBMHardwareRun | None = None,
    qpu_preflight_only: bool = False,
) -> dict[str, Any]:
    """Compare the same logical Austin circuit and direct classical solver.

    Noise is synthetic depolarization applied to each logical gate. The seed
    controls local sampling and target compilation. A supplied hardware
    configuration explicitly requests one physical job with the given caps
    unless qpu_preflight_only is true, which only checks the selected target.
    """
    if hardware is not None and not isinstance(hardware, IBMHardwareRun):
        raise TypeError("hardware must be an IBMHardwareRun")
    if type(qpu_preflight_only) is not bool or (qpu_preflight_only and hardware is None):
        raise ValueError("qpu_preflight_only requires an explicit hardware configuration")
    _bounded_int("shots", shots, 1, MAX_HARDWARE_SHOTS if hardware else MAX_LOCAL_SHOTS)
    _bounded_int("seed", seed, 0, 2**32 - 1)
    if (
        isinstance(noise_probability, bool)
        or not isinstance(noise_probability, (float, int))
        or not 0 <= noise_probability <= 1
    ):
        raise ValueError("noise_probability must be a number from 0 to 1")

    from qiskit import __version__ as qiskit_version

    started = perf_counter()
    circuit = build_austin_circuit()
    built_at = perf_counter()
    classical = austin_classical_baseline()
    ideal = _ideal_simulation(circuit, shots=shots, seed=seed)
    noisy = _noisy_simulation(circuit, shots=shots, seed=seed, probability=float(noise_probability))
    physical = (
        _run_ibm_hardware(circuit, hardware, shots, seed, preflight_only=qpu_preflight_only)
        if hardware is not None
        else {"status": "NOT_REQUESTED", "provider_job_submitted": False, "quantum_hardware_used": False}
    )
    return {
        "circuit": "AUSTIN_3Q_TWO_CZ_CONSTRAINT_SEARCH",
        "basis_order": "q2 q1 q0",
        "qiskit_version": qiskit_version,
        "seed": seed,
        "logical_circuit": _circuit_resources(circuit),
        "build_seconds": built_at - started,
        "classical": classical,
        "ideal": ideal,
        "noisy": noisy,
        "hardware": physical,
        "wall_seconds": perf_counter() - started,
    }
