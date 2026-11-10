"""Offline adapter that exposes the package's CPU reference through the API."""

from __future__ import annotations

import platform
from collections.abc import Callable
from dataclasses import dataclass

from .adapter_api import (
    AdapterContractError,
    BackendCapabilities,
    ExecutionResult,
    ExperimentRequest,
    check_compatibility,
)
from .core import MAX_SIMULATION_QUBITS, run_austin_search


PYTHON_PREDICATE_FORMAT = "chronorealm.python-bool-predicate/1"
DEFAULT_SHOTS = 2048
DEFAULT_SEED = 20261109


@dataclass(frozen=True)
class PythonPredicateWorkload:
    """A Python rule for the local teaching simulator; not a hardware oracle."""

    predicate: Callable[[str], bool]
    name: str = "user_python_predicate"
    iterations: int | None = None

    def __post_init__(self) -> None:
        if not callable(self.predicate):
            raise AdapterContractError("predicate must be callable.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise AdapterContractError("name must be non-empty text.")
        if self.iterations is not None and (
            isinstance(self.iterations, bool)
            or not isinstance(self.iterations, int)
            or not 0 <= self.iterations <= 256
        ):
            raise AdapterContractError("iterations must be from 0 to 256, or None.")


class AustinCpuAdapter:
    """Run the Austin reference locally through the common adapter records.

    This adapter exhaustively checks the predicate on a classical CPU before
    simulating an ideal statevector. It is not a quantum device adapter.
    """

    adapter_id = "chronorealm.austin-cpu"
    adapter_version = "1"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            supported_formats=(PYTHON_PREDICATE_FORMAT,),
            execution_modes=("SIMULATOR",),
            max_width=MAX_SIMULATION_QUBITS,
            supports_shots=True,
            supports_seed=True,
            supports_async_jobs=False,
        )

    def submit(self, request: ExperimentRequest) -> ExecutionResult:
        warnings = check_compatibility(request, self.capabilities())
        workload = request.workload.payload
        if not isinstance(workload, PythonPredicateWorkload):
            raise AdapterContractError("payload must be a PythonPredicateWorkload.")
        if request.workload.logical_width is None:
            raise AdapterContractError("logical_width is required for the CPU simulator.")
        shots = DEFAULT_SHOTS if request.shots is None else request.shots
        seed = DEFAULT_SEED if request.seed is None else request.seed
        result = run_austin_search(
            request.workload.logical_width,
            workload.predicate,
            iterations=workload.iterations,
            shots=shots,
            seed=seed,
            predicate_name=workload.name,
        )
        return ExecutionResult(
            status="COMPLETED",
            execution_mode="SIMULATOR",
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            backend_id="python-classical-statevector",
            backend_version=platform.python_version(),
            shots_requested=shots,
            shots_completed=shots,
            counts=result["sample_counts"],
            measurement_order=result["basis_order"],
            result_format="application/json",
            result_payload=result,
            timings_seconds={"total": result["runtime_seconds"]},
            timing_sources={"total": "CLIENT_MONOTONIC"},
            warnings=warnings,
        )

    def get_result(self, job_id: str) -> ExecutionResult:
        if not isinstance(job_id, str) or not job_id.strip():
            raise AdapterContractError("job_id must be non-empty text.")
        return ExecutionResult(
            status="NOT_SUPPORTED",
            execution_mode="SIMULATOR",
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            warnings=("This local adapter completes synchronously and does not create jobs.",),
        )

    def cancel(self, job_id: str) -> ExecutionResult:
        if not isinstance(job_id, str) or not job_id.strip():
            raise AdapterContractError("job_id must be non-empty text.")
        return ExecutionResult(
            status="NOT_SUPPORTED",
            execution_mode="SIMULATOR",
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            warnings=("This local adapter completes synchronously and has no job to cancel.",),
        )
