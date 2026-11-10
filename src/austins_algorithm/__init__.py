"""Austin's Algorithm: a ChronoRealm Grover-search teaching package.

The core module is standard-library-only and classically simulates ideal
statevectors. Optional Qiskit/Cirq modules create quantum circuits. The
experimental adapter API does not add provider access; AustinCpuAdapter wraps
the same classical simulator.
"""

from .core import (
    ALGORITHM_NAME,
    MAX_SIMULATION_QUBITS,
    AustinAlgorithmError,
    austin_predicate,
    recommended_iterations,
    run_austin_search,
    theoretical_success_probability,
    validate_marked_states,
)
from .adapter_api import (
    AdapterContractError,
    BackendAdapter,
    BackendCapabilities,
    ExecutionResult,
    ExperimentRequest,
    WorkloadArtifact,
    check_compatibility,
)
from .local_adapter import AustinCpuAdapter, PythonPredicateWorkload

__version__ = "0.1.0"

__all__ = [
    "ALGORITHM_NAME",
    "MAX_SIMULATION_QUBITS",
    "AustinAlgorithmError",
    "austin_predicate",
    "recommended_iterations",
    "run_austin_search",
    "theoretical_success_probability",
    "validate_marked_states",
    "AdapterContractError",
    "BackendAdapter",
    "BackendCapabilities",
    "ExecutionResult",
    "ExperimentRequest",
    "WorkloadArtifact",
    "check_compatibility",
    "AustinCpuAdapter",
    "PythonPredicateWorkload",
]
