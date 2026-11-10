"""Experimental, provider-neutral interface for optional quantum adapters.

This module defines data contracts only. It performs no discovery, network
access, authentication, persistence, compilation, or job submission. A caller
must explicitly invoke an adapter's ``submit`` method to start work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Protocol, runtime_checkable


EXECUTION_STATUSES = frozenset(
    {
        "NOT_SUBMITTED",
        "NOT_SUPPORTED",
        "QUEUED",
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
        "UNKNOWN",
    }
)
EXECUTION_MODES = frozenset(
    {"SIMULATOR", "EMULATOR", "QPU", "ANALOG_SIMULATOR", "OTHER", "UNKNOWN"}
)
TIMING_SOURCES = frozenset(
    {"CLIENT_MONOTONIC", "ADAPTER_REPORTED", "PROVIDER_REPORTED", "ESTIMATED", "UNKNOWN"}
)


class AdapterContractError(ValueError):
    """Raised when an adapter request or response breaks this API contract."""


def _positive_int(value: Any, label: str, *, allow_none: bool = False) -> None:
    if allow_none and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AdapterContractError(f"{label} must be a positive integer.")


def _nonempty_text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"{label} must be non-empty text.")


@dataclass(frozen=True)
class WorkloadArtifact:
    """Caller-provided program, circuit, or other workload for an adapter.

    ``format_id`` identifies how to read ``payload`` (for example,
    ``openqasm3``, ``qiskit-circuit``, or ``cirq-circuit``). The payload may be
    serialized text/bytes or a framework-native object for an in-process
    adapter. It is never serialized or stored by this package.
    """

    format_id: str
    payload: Any
    logical_width: int | None = None

    def __post_init__(self) -> None:
        _nonempty_text(self.format_id, "format_id")
        if self.format_id != self.format_id.strip():
            raise AdapterContractError("format_id cannot start or end with whitespace.")
        if len(self.format_id) > 128:
            raise AdapterContractError("format_id cannot exceed 128 characters.")
        if self.payload is None:
            raise AdapterContractError("payload cannot be None.")
        _positive_int(self.logical_width, "logical_width", allow_none=True)


@dataclass(frozen=True)
class ExperimentRequest:
    """Input to a caller-selected backend adapter."""

    workload: WorkloadArtifact
    requested_execution_mode: str
    shots: int | None = None
    seed: int | None = None

    schema: str = field(default="ChronoRealmQuantumExperimentRequest/2", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workload, WorkloadArtifact):
            raise AdapterContractError("workload must be a WorkloadArtifact.")
        if not isinstance(self.requested_execution_mode, str) or self.requested_execution_mode not in EXECUTION_MODES:
            raise AdapterContractError("requested_execution_mode must use a recognized version-1 mode.")
        if self.requested_execution_mode == "UNKNOWN":
            raise AdapterContractError("Choose a known execution mode before submitting a request.")
        _positive_int(self.shots, "shots", allow_none=True)
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise AdapterContractError("seed must be an integer or None.")


@dataclass(frozen=True)
class BackendCapabilities:
    """Capabilities reported by one explicitly selected adapter.

    A ``None`` value means that the adapter has not reported that capability;
    it never means unlimited support.
    """

    adapter_id: str
    adapter_version: str
    supported_formats: tuple[str, ...]
    execution_modes: tuple[str, ...]
    max_width: int | None = None
    supports_shots: bool | None = None
    supports_seed: bool | None = None
    supports_async_jobs: bool | None = None

    schema: str = field(default="ChronoRealmQuantumAdapterCapabilities/1", init=False)

    def __post_init__(self) -> None:
        _nonempty_text(self.adapter_id, "adapter_id")
        _nonempty_text(self.adapter_version, "adapter_version")
        if not isinstance(self.supported_formats, tuple) or not self.supported_formats or any(
            not isinstance(x, str) or not x.strip() for x in self.supported_formats
        ):
            raise AdapterContractError("supported_formats must contain non-empty format IDs.")
        if len(set(self.supported_formats)) != len(self.supported_formats):
            raise AdapterContractError("supported_formats cannot contain duplicates.")
        if not isinstance(self.execution_modes, tuple) or not self.execution_modes or any(
            not isinstance(x, str) or x not in EXECUTION_MODES for x in self.execution_modes
        ):
            raise AdapterContractError("execution_modes must contain non-empty mode names.")
        _positive_int(self.max_width, "max_width", allow_none=True)
        for name in ("supports_shots", "supports_seed", "supports_async_jobs"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise AdapterContractError(f"{name} must be True, False, or None.")


def check_compatibility(request: ExperimentRequest, capabilities: BackendCapabilities) -> tuple[str, ...]:
    """Reject declared incompatibilities and report unreported limits.

    The returned warnings preserve uncertainty so an adapter is not treated as
    unlimited just because it did not publish a limit.
    """

    if not isinstance(request, ExperimentRequest) or not isinstance(capabilities, BackendCapabilities):
        raise AdapterContractError("request and capabilities must use the version-1 adapter records.")
    if request.workload.format_id not in capabilities.supported_formats:
        raise AdapterContractError(
            f"Adapter {capabilities.adapter_id!r} does not declare support for "
            f"{request.workload.format_id!r}."
        )
    warnings: list[str] = []
    if request.requested_execution_mode not in capabilities.execution_modes:
        if "UNKNOWN" in capabilities.execution_modes:
            warnings.append("The adapter did not report all supported execution modes.")
        else:
            raise AdapterContractError(
                f"Adapter {capabilities.adapter_id!r} does not declare support for requested mode "
                f"{request.requested_execution_mode!r}."
            )
    width = request.workload.logical_width
    if width is not None:
        if capabilities.max_width is None:
            warnings.append("The adapter did not report a maximum workload width.")
        elif width > capabilities.max_width:
            raise AdapterContractError(
                f"Workload width {width} exceeds the adapter's declared limit of {capabilities.max_width}."
            )
    if request.shots is not None:
        if capabilities.supports_shots is False:
            raise AdapterContractError("The adapter declares that it does not support shots.")
        if capabilities.supports_shots is None:
            warnings.append("The adapter did not report whether shots are supported.")
    if request.seed is not None:
        if capabilities.supports_seed is False:
            raise AdapterContractError("The adapter declares that it does not support a seed.")
        if capabilities.supports_seed is None:
            warnings.append("The adapter did not report whether seeds are supported.")
    return tuple(warnings)


@dataclass(frozen=True)
class ExecutionResult:
    """Version-1 output record returned by a backend adapter."""

    status: str
    execution_mode: str
    adapter_id: str
    adapter_version: str
    backend_id: str | None = None
    backend_version: str | None = None
    target_id: str | None = None
    job_id: str | None = None
    shots_requested: int | None = None
    shots_completed: int | None = None
    counts: Mapping[str, int] | None = None
    measurement_order: str | None = None
    result_format: str | None = None
    result_payload: Any = None
    timings_seconds: Mapping[str, float] = field(default_factory=dict)
    timing_sources: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    schema: str = field(default="ChronoRealmQuantumExecutionResult/1", init=False)

    def __post_init__(self) -> None:
        _nonempty_text(self.status, "status")
        _nonempty_text(self.execution_mode, "execution_mode")
        if self.status not in EXECUTION_STATUSES:
            raise AdapterContractError("status must use a recognized version-1 status.")
        if self.execution_mode not in EXECUTION_MODES:
            raise AdapterContractError("execution_mode must use a recognized version-1 mode.")
        _nonempty_text(self.adapter_id, "adapter_id")
        _nonempty_text(self.adapter_version, "adapter_version")
        for name in ("backend_id", "backend_version", "target_id", "job_id", "measurement_order", "result_format", "error_code", "error_message"):
            value = getattr(self, name)
            if value is not None:
                _nonempty_text(value, name)
        _positive_int(self.shots_requested, "shots_requested", allow_none=True)
        if self.shots_completed is not None and (
            isinstance(self.shots_completed, bool)
            or not isinstance(self.shots_completed, int)
            or self.shots_completed < 0
        ):
            raise AdapterContractError("shots_completed must be a non-negative integer or None.")
        if self.counts is not None:
            if not isinstance(self.counts, Mapping):
                raise AdapterContractError("counts must be a mapping or None.")
            for outcome, count in self.counts.items():
                if not isinstance(outcome, str):
                    raise AdapterContractError("count outcome keys must be strings.")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise AdapterContractError("count values must be non-negative integers.")
        if not isinstance(self.timings_seconds, Mapping) or not isinstance(self.timing_sources, Mapping):
            raise AdapterContractError("timings_seconds and timing_sources must be mappings.")
        if set(self.timings_seconds) != set(self.timing_sources):
            raise AdapterContractError("Each timing must include exactly one timing source.")
        for stage, seconds in self.timings_seconds.items():
            if not isinstance(stage, str) or not stage.strip():
                raise AdapterContractError("Timing stage names must be non-empty strings.")
            if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not isfinite(seconds) or seconds < 0:
                raise AdapterContractError("Timing values must be finite, non-negative seconds.")
            source = self.timing_sources[stage]
            if not isinstance(source, str) or source not in TIMING_SOURCES:
                raise AdapterContractError("Each timing source must use a recognized version-1 value.")
        if not isinstance(self.warnings, tuple) or any(not isinstance(item, str) for item in self.warnings):
            raise AdapterContractError("warnings must contain text entries.")


@runtime_checkable
class BackendAdapter(Protocol):
    """Open plugin interface; implementations own compilation and transport."""

    def capabilities(self) -> BackendCapabilities:
        """Return local capability metadata without submitting work."""

    def submit(self, request: ExperimentRequest) -> ExecutionResult:
        """Explicitly submit one request; external effects must be documented."""

    def get_result(self, job_id: str) -> ExecutionResult:
        """Return the current state for a previously submitted job."""

    def cancel(self, job_id: str) -> ExecutionResult:
        """Request cancellation; report when cancellation is unsupported or unknown."""
