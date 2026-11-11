"""Bounded Grover searches over caller-supplied Qiskit phase oracles."""

from __future__ import annotations

import math
import random
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any

from .core import MAX_ITERATIONS, _bounded_int, recommended_iterations

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from qiskit import QuantumCircuit
    from qiskit.circuit import Gate
    from qiskit.quantum_info import Statevector


MAX_SEARCH_WIDTH = 12
MAX_SEARCH_SHOTS = 256
MAX_ORACLE_QUERIES = 4096
MAX_ORACLE_OPERATIONS = 256
MAX_CACHED_ROUND_CIRCUITS = 4
MAX_PREPARED_STATEVECTOR_BYTES = 1 << 18
MAX_PREPARED_PROBABILITY_BYTES = 1 << 17
MAX_AUDIT_SEARCH_WIDTH = 8
MAX_DENSE_AUDIT_WIDTH = 7  # At most 256 KiB for the complex128 matrix itself.
MIN_RECTANGULAR_AUDIT_WIDTH = 9
MAX_RECTANGULAR_AUDIT_ENTRIES = 1 << 18  # 4 MiB per complex128 column array.
MAX_RECTANGULAR_AUDIT_BLOCKS = 4  # Bounds the aggregate contraction work to four blocks.
MAX_RECTANGULAR_AUDIT_GATE_WIDTH = 7
MAX_RECTANGULAR_AUDIT_MATRIX_BYTES = 1 << 18
MAX_RECTANGULAR_AUDIT_TILE_BYTES = 1 << 18  # Combined payload of both packing arrays.
MIN_TILED_AUDIT_GATE_WIDTH = 4
MAX_RECTANGULAR_AUDIT_WORK = 1 << 26
ORACLE_AUDIT_TOLERANCE = 1e-9
DIFFUSER_METHODS = ("standard", "clean-ancilla")
IDEAL_SAMPLERS = ("statevector_sampler", "prepared_statevector", "prepared_probabilities")


@dataclass
class _LastAttempt:
    """Optional in-memory handoff for a completed measured attempt."""

    circuit: QuantumCircuit | None = None
    build_seconds: float = 0.0


def _validate_phase_oracle(phase_oracle: QuantumCircuit, search_qubits: int) -> None:
    from qiskit import QuantumCircuit
    from qiskit.circuit import Gate

    _bounded_int(search_qubits, 1, MAX_SEARCH_WIDTH, "search_qubits")
    if not isinstance(phase_oracle, QuantumCircuit):
        raise TypeError("phase_oracle must be a Qiskit QuantumCircuit")
    _bounded_int(phase_oracle.num_qubits, search_qubits, MAX_SEARCH_WIDTH, "phase_oracle width")
    if phase_oracle.num_clbits or phase_oracle.parameters:
        raise ValueError("phase_oracle must have no classical bits or unbound parameters")
    if len(phase_oracle.data) > MAX_ORACLE_OPERATIONS:
        raise ValueError(f"phase_oracle may contain at most {MAX_ORACLE_OPERATIONS} top-level gates")
    if any(not isinstance(instruction.operation, Gate) for instruction in phase_oracle.data):
        raise ValueError("phase_oracle must contain only reversible gates")


def _verify(verifier: Callable[[str], bool], bits: str) -> bool:
    result = verifier(bits)
    if not isinstance(result, bool):
        raise TypeError("verifier must return a bool")
    return result


def _prepare_diffuser(
    phase_oracle: QuantumCircuit, search_qubits: int, diffuser: str,
) -> QuantumCircuit | None:
    if diffuser not in DIFFUSER_METHODS:
        raise ValueError("diffuser must be standard or clean-ancilla")
    if diffuser == "standard":
        return None
    if search_qubits < 4 or phase_oracle.num_qubits == search_qubits:
        raise ValueError("clean-ancilla diffuser requires at least four search qubits and one work qubit")
    try:
        from qiskit.synthesis import synth_mcx_1_clean_kg24
    except ImportError as exc:
        raise ValueError("clean-ancilla diffuser requires Qiskit 2.1 or later") from exc
    return synth_mcx_1_clean_kg24(search_qubits - 1)


def _build_circuit(
    phase_oracle: QuantumCircuit, search_qubits: int, iterations: int, clean_mcx: QuantumCircuit | None,
) -> QuantumCircuit:
    from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister

    q = QuantumRegister(phase_oracle.num_qubits, "q")
    answer = ClassicalRegister(search_qubits, "answer")
    circuit = QuantumCircuit(q, answer, name="grover_search")
    search = q[:search_qubits]
    circuit.h(search)
    for _ in range(iterations):
        circuit.compose(phase_oracle, qubits=q, inplace=True)
        # Reflect only the search register about its uniform state, up to a global phase.
        circuit.h(search)
        circuit.x(search)
        if search_qubits == 1:
            circuit.z(search[0])
        else:
            circuit.h(search[-1])
            if clean_mcx is None:
                circuit.mcx(search[:-1], search[-1])
            else:
                # The phase oracle restores its first work wire before the reflection.
                circuit.compose(clean_mcx, qubits=q[:search_qubits + 1], inplace=True)
            circuit.h(search[-1])
        circuit.x(search)
        circuit.h(search)
    circuit.measure(search, answer)
    return circuit


def build_grover_search_circuit(
    phase_oracle: QuantumCircuit, search_qubits: int, *, iterations: int, diffuser: str = "standard",
) -> QuantumCircuit:
    """Build a measured search circuit from a clean-workspace phase oracle.

    The first search_qubits wires hold x, and any remaining wires start in |0>.
    On that subspace the oracle must send |x>|0> to (-1)**f(x) |x>|0>, up
    to a single global phase. Search output is MSB-first. This function
    checks the circuit structure. Use audit_phase_oracle for a small,
    exhaustive semantic check against an independent verifier. The optional
    clean-ancilla diffuser uses the oracle's first work qubit and returns it
    to zero. It requires at least four search wires and Qiskit 2.1 or later.
    """
    _validate_phase_oracle(phase_oracle, search_qubits)
    _bounded_int(iterations, 0, MAX_ITERATIONS, "iterations")
    clean_mcx = _prepare_diffuser(phase_oracle, search_qubits, diffuser)
    return _build_circuit(phase_oracle, search_qubits, iterations, clean_mcx)


def _operator_columns_clear(
    columns: NDArray, *, start: int = 0, reference: complex | None = None, scratch: NDArray | None = None,
) -> bool:
    """Keep rounding-sensitive verdicts on the original basis-state path."""
    from numpy import copyto, einsum, fill_diagonal, minimum

    stop = start + columns.shape[1]
    diagonal = columns[start:stop].diagonal()
    if scratch is None:
        scratch = columns.copy()
    else:
        copyto(scratch, columns)
    fill_diagonal(scratch[start:stop], 0)
    # Reduce each component separately to avoid extra rectangle-sized norm temporaries.
    leakage_squared = einsum("ij,ij->j", scratch.real, scratch.real)
    leakage_squared += einsum("ij,ij->j", scratch.imag, scratch.imag)
    margin = ORACLE_AUDIT_TOLERANCE / 2
    anchor = diagonal[0] if reference is None else reference
    common_phase = minimum(abs(diagonal - anchor), abs(diagonal + anchor))
    return bool(
        (leakage_squared <= margin**2).all()
        and (abs(abs(diagonal) - 1) <= margin).all()
        and (common_phase <= margin).all()
    )


def _canonical_mcx(gate: Gate) -> bool:
    from qiskit.circuit.library import MCXGate

    return type(gate) is MCXGate and gate.definition == MCXGate(
        gate.num_ctrl_qubits, ctrl_state=gate.ctrl_state,
    ).definition


def _supports_prepared_sampling(
    oracle: QuantumCircuit, search_qubits: int, *, use_probabilities: bool,
) -> bool:
    from qiskit.circuit.library import CCXGate, CCZGate, CXGate, CZGate, HGate, MCXGate
    from qiskit.circuit.library import PhaseGate, RXGate, RZGate, XGate, ZGate

    if use_probabilities:
        retained_bytes = (1 << search_qubits) * 8
        limit = MAX_PREPARED_PROBABILITY_BYTES
    else:
        retained_bytes = (1 << oracle.num_qubits) * 16
        limit = MAX_PREPARED_STATEVECTOR_BYTES
    if MAX_CACHED_ROUND_CIRCUITS * retained_bytes > limit:
        return False
    fixed_gates = (CCXGate, CCZGate, CXGate, CZGate, HGate, XGate, ZGate)
    gate_types = {type(gate()) for gate in fixed_gates}
    gate_types.update(fixed_gates)
    gate_types.update((PhaseGate, RXGate, RZGate))
    # A custom Gate can hide stochastic instructions in its definition.
    return all(
        type(item.operation) in gate_types
        or (type(item.operation) is MCXGate and _canonical_mcx(item.operation))
        for item in oracle.data
    )


def _prepare_ideal_measurement(
    circuit: QuantumCircuit, measured_qubits: list[int], *, use_probabilities: bool,
) -> Statevector | NDArray:
    from numpy import isfinite
    from qiskit.quantum_info import Statevector

    state = Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
    if not use_probabilities:
        return state
    probabilities = state.probabilities(qargs=measured_qubits)
    if (
        probabilities.shape != (1 << len(measured_qubits),)
        or probabilities.dtype.kind != "f"
        or probabilities.itemsize > 8
    ):
        raise RuntimeError("prepared measurement probabilities have an unexpected shape or dtype")
    if not isfinite(probabilities).all() or (probabilities < 0).any():
        raise ValueError("prepared measurement probabilities must be finite and nonnegative")
    return probabilities


class _FixedRoundProbabilities:
    """The plan-owned circuit and lazy weights for one exact logical search."""

    def __init__(
        self, oracle: QuantumCircuit, circuit: QuantumCircuit, search_qubits: int, rounds: int, diffuser: str,
    ) -> None:
        self.oracle = oracle
        self.circuit = circuit
        self.configuration = (search_qubits, rounds, diffuser)
        self._measured_qubits = list(range(search_qubits))
        self._lock = Lock()
        self._weights: NDArray | None = None

    def weights(self) -> NDArray:
        with self._lock:
            if self._weights is None:
                weights = _prepare_ideal_measurement(self.circuit, self._measured_qubits, use_probabilities=True)
                weights.setflags(write=False)
                self._weights = weights
            return self._weights


@dataclass(frozen=True)
class _OracleRowUpdate:
    rows: tuple[int | slice, ...]
    partner: tuple[int | slice, ...] | None


def _exact_row_update(
    gate: Gate, matrix: NDArray, axes: list[int], width: int, *, phase: bool,
) -> _OracleRowUpdate | None:
    """Recognize the exact matrix of a controlled row swap or sign flip."""
    from numpy import array_equal, count_nonzero, ones

    target_bit = 1 << (gate.num_qubits - 1)
    controls = getattr(gate, "ctrl_state", 0)
    if not isinstance(controls, int) or not 0 <= controls < target_bit:
        return None
    target_one = controls | target_bit
    dimension = 2 * target_bit
    if matrix.shape != (dimension, dimension):
        return None
    expected_diagonal = ones(dimension, dtype=matrix.dtype)
    if phase:
        expected_diagonal[target_one] = -1
    else:
        expected_diagonal[controls] = expected_diagonal[target_one] = 0
        if matrix[controls, target_one] != 1 or matrix[target_one, controls] != 1:
            return None
    if not array_equal(matrix.diagonal(), expected_diagonal) or count_nonzero(matrix) != dimension:
        return None

    selection: list[int | slice] = [slice(None)] * (width + 1)
    for index, axis in enumerate(axes[:-1]):
        selection[axis] = (controls >> index) & 1
    selection[axes[-1]] = 1
    target_one_rows = tuple(selection)
    selection[axes[-1]] = 0
    return _OracleRowUpdate(target_one_rows, None if phase else tuple(selection))


def _uses_stock_matrix(gate: Gate, stock_types: set[type]) -> bool:
    # An instance conversion can vary between calls despite its stock class.
    return type(gate) in stock_types and {"to_matrix", "__array__"}.isdisjoint(vars(gate))


def _basis_offsets(wires: list[int]) -> NDArray:
    """Place each local basis index on the supplied wires in least-significant-first order."""
    from numpy import arange, intp, zeros_like

    indices = arange(1 << len(wires), dtype=intp)
    offsets = zeros_like(indices)
    for index, wire in enumerate(wires):
        offsets |= ((indices >> index) & 1) << wire
    return offsets


def _contract_dense_view(matrix: NDArray, state: NDArray, spare: NDArray, axes: list[int]) -> bool:
    """Apply an ordered contiguous wire span directly to views of the column buffers."""
    from numpy import matmul

    if axes != list(range(axes[0], axes[0] - len(axes), -1)):
        return False
    low_wires = state.ndim - 2 - axes[0]
    shape = (-1, matrix.shape[0], state.shape[-1] << low_wires)
    matmul(matrix, state.reshape(shape), out=spare.reshape(shape))
    return True


def _contract_dense_tiles(
    matrix: NDArray, state: NDArray, spare: NDArray, axes: list[int], buffers: NDArray,
) -> None:
    """Gather complete local basis groups and multiply using two bounded packing arrays."""
    from numpy import matmul, take

    width = state.ndim - 1
    columns = state.shape[-1]
    wires = [width - 1 - axis for axis in axes]
    spectators = [wire for wire in range(width) if wire not in wires]
    row_groups = _basis_offsets(spectators)[:, None] | _basis_offsets(wires)
    dimension = matrix.shape[0]
    groups_per_tile = buffers.shape[1] // (dimension * columns)
    source = state.reshape(-1, columns)
    target = spare.reshape(-1, columns)
    for start in range(0, len(row_groups), groups_per_tile):
        rows = row_groups[start:start + groups_per_tile]
        shape = (len(rows), dimension, columns)
        entries = rows.size * columns
        packed = buffers[0, :entries].reshape(shape)
        product = buffers[1, :entries].reshape(shape)
        # These indices partition all physical rows. Clip mode permits an unbuffered out.
        take(source, rows, axis=0, out=packed, mode="clip")
        matmul(matrix, packed, out=product)
        target[rows] = product


def _rectangular_oracle_blocks(oracle: QuantumCircuit, search_qubits: int) -> Iterator[tuple[NDArray, NDArray]]:
    """Yield reusable work-zero column blocks and their guard scratch buffer."""
    from numpy import arange, complex128, copyto, einsum, empty, empty_like, exp, negative
    from qiskit.circuit.library import CCXGate, CXGate, CZGate, HGate, MCXGate, XGate, ZGate
    from qiskit.quantum_info import Operator

    width = oracle.num_qubits
    row_count = 1 << width
    input_count = 1 << search_qubits
    block_size = min(input_count, MAX_RECTANGULAR_AUDIT_ENTRIES // row_count)
    if search_qubits < 4 or input_count > block_size * MAX_RECTANGULAR_AUDIT_BLOCKS:
        return

    fixed_gates = (CCXGate, CXGate, CZGate, HGate, XGate, ZGate)
    stock_types = {MCXGate, *fixed_gates, *(type(gate()) for gate in fixed_gates)}
    operations = list(oracle.data)
    if any(
        not _uses_stock_matrix(item.operation, stock_types)
        or item.operation.num_qubits > MAX_RECTANGULAR_AUDIT_GATE_WIDTH
        for item in operations
    ):
        return
    work = sum(1 << item.operation.num_qubits for item in operations)
    if row_count * block_size * work > MAX_RECTANGULAR_AUDIT_WORK:
        return
    if any(item.operation.base_class is MCXGate and not _canonical_mcx(item.operation) for item in operations):
        return

    steps = []
    remaining_bytes = MAX_RECTANGULAR_AUDIT_MATRIX_BYTES
    for item in operations:
        gate = item.operation
        axes = [width - 1 - oracle.find_bit(bit).index for bit in item.qubits]
        update = None
        candidate = None
        if gate.base_class is not HGate:
            candidate = Operator(gate).data
            update = _exact_row_update(gate, candidate, axes, width, phase=gate.base_class in (CZGate, ZGate))

        retained = None
        if update is None and gate.base_class in (HGate, MCXGate) and remaining_bytes:
            if candidate is None:
                candidate = Operator(gate).data
            if candidate.nbytes <= remaining_bytes:
                retained = candidate.copy()
                retained.setflags(write=False)
                remaining_bytes -= retained.nbytes
        steps.append((gate, axes, update, retained))
        del candidate

    shape = (2,) * width + (block_size,)
    state = empty(shape, dtype=complex128)
    spare = empty_like(state)
    tile_entries = min(state.size, MAX_RECTANGULAR_AUDIT_TILE_BYTES // (2 * state.itemsize))
    tile_buffers = None
    offsets = arange(block_size)
    phase = exp(1j * float(oracle.global_phase)) if oracle.global_phase else 1.0
    labels = list(range(width + 1))
    for start in range(0, input_count, block_size):
        count = min(block_size, input_count - start)
        state.fill(0)
        state.reshape(row_count, block_size)[start + offsets[:count], offsets[:count]] = phase
        for gate, axes, update, retained in steps:
            if update is not None:
                rows = update.rows
                partner = update.partner
                if partner is None:
                    negative(state[rows], out=state[rows])
                else:
                    copyto(spare[rows], state[rows])
                    copyto(state[rows], state[partner])
                    copyto(state[partner], spare[rows])
                continue

            matrix = Operator(gate).data if retained is None else retained
            dimension = 1 << gate.num_qubits
            if (
                gate.num_qubits >= MIN_TILED_AUDIT_GATE_WIDTH
                and tile_entries >= dimension * block_size
                and matrix.shape == (dimension, dimension)
                and matrix.dtype == state.dtype
            ):
                if not _contract_dense_view(matrix, state, spare, axes):
                    if tile_buffers is None:
                        tile_buffers = empty((2, tile_entries), dtype=state.dtype)
                    _contract_dense_tiles(matrix, state, spare, axes, tile_buffers)
            else:
                # Qiskit's last operand is the most significant local matrix bit.
                acted = list(reversed(axes))
                contracted = list(range(width + 1, width + 1 + gate.num_qubits))
                state_labels = labels.copy()
                for axis, label in zip(acted, contracted):
                    state_labels[axis] = label
                matrix = matrix.reshape((2,) * (2 * gate.num_qubits))
                einsum(matrix, acted + contracted, state, state_labels, labels, out=spare)
            del matrix
            state, spare = spare, state
        yield state.reshape(row_count, block_size)[:, :count], spare.reshape(row_count, block_size)[:, :count]


def _rectangular_amplitudes(oracle: QuantumCircuit, search_qubits: int) -> NDArray | None:
    """Keep only the checked diagonals and release both buffers before returning."""
    from numpy import complex128, empty

    amplitudes = empty(1 << search_qubits, dtype=complex128)
    start = 0
    for columns, scratch in _rectangular_oracle_blocks(oracle, search_qubits):
        reference = amplitudes[0] if start else None
        if not _operator_columns_clear(columns, start=start, reference=reference, scratch=scratch):
            return None
        stop = start + columns.shape[1]
        amplitudes[start:stop] = columns[start:stop].diagonal()
        start = stop
    return amplitudes if start == len(amplitudes) else None


def _audit_outputs(oracle: QuantumCircuit, search_qubits: int) -> Iterator[tuple[complex, bool]]:
    """Yield each input's diagonal amplitude and clean-workspace verdict in order."""
    from numpy.linalg import norm
    from qiskit.circuit.library import (
        CCXGate, CCZGate, CXGate, CZGate, HGate, MCXGate, PhaseGate, RXGate, RZGate, XGate, ZGate,
    )
    from qiskit.exceptions import QiskitError
    from qiskit.quantum_info import Operator, Statevector

    width = oracle.num_qubits
    input_count = 1 << search_qubits
    try:
        if 3 <= search_qubits <= width <= MAX_DENSE_AUDIT_WIDTH and width - search_qubits <= 2:
            fixed_gates = (CCXGate, CCZGate, CXGate, CZGate, HGate, XGate, ZGate)
            dense_types = {MCXGate, PhaseGate, RXGate, RZGate, *fixed_gates, *(type(gate()) for gate in fixed_gates)}
            if all(_uses_stock_matrix(item.operation, dense_types) for item in oracle.data):
                columns = Operator(oracle).data[:, :input_count]
                if _operator_columns_clear(columns):
                    yield from ((amplitude, True) for amplitude in columns.diagonal())
                    return
        elif width >= MIN_RECTANGULAR_AUDIT_WIDTH:
            amplitudes = _rectangular_amplitudes(oracle, search_qubits)
            if amplitudes is not None:
                # Publish only after every block clears the same global phase guard.
                yield from ((amplitude, True) for amplitude in amplitudes)
                return
    except QiskitError:
        # Keep the original evolution when Qiskit rejects matrix construction.
        pass

    dims = (2,) * width
    tolerance = ORACLE_AUDIT_TOLERANCE
    for index in range(input_count):
        output = Statevector.from_int(index, dims=dims).evolve(oracle).data
        amplitude = output[index]
        residual = output.copy()
        residual[index] = 0
        clear = norm(residual) <= tolerance and math.isclose(abs(amplitude), 1.0, abs_tol=tolerance, rel_tol=0)
        yield amplitude, clear


def audit_phase_oracle(
    phase_oracle: QuantumCircuit, search_qubits: int, verifier: Callable[[str], bool],
) -> dict[str, Any]:
    """Check the phase, search bits and zero work register on every small input."""
    _validate_phase_oracle(phase_oracle, search_qubits)
    _bounded_int(search_qubits, 1, MAX_AUDIT_SEARCH_WIDTH, "audit search_qubits")
    if not callable(verifier):
        raise TypeError("verifier must be callable")
    outputs = _audit_outputs(phase_oracle.copy(), search_qubits)
    reference_phase = None
    marked = []
    tolerance = ORACLE_AUDIT_TOLERANCE
    for index in range(1 << search_qubits):
        bits = format(index, f"0{search_qubits}b")
        is_marked = _verify(verifier, bits)
        amplitude, clear = next(outputs)
        if not clear:
            raise ValueError(f"oracle changes search bits or leaves work bits dirty for {bits}")
        signed_phase = -amplitude if is_marked else amplitude
        if reference_phase is None:
            reference_phase = signed_phase
        if not abs(signed_phase - reference_phase) <= tolerance:
            raise ValueError(f"oracle phase disagrees with the verifier for {bits}")
        if is_marked:
            marked.append(bits)
    return {"states_checked": 1 << search_qubits, "marked_count": len(marked), "marked_states": marked}


def run_grover_search(
    phase_oracle: QuantumCircuit,
    search_qubits: int,
    verifier: Callable[[str], bool],
    *,
    marked_count: int | None = None,
    max_shots: int = 64,
    max_oracle_queries: int = 256,
    max_rounds_per_shot: int = MAX_ITERATIONS,
    seed: int = 20261109,
    diffuser: str = "standard",
    ideal_sampler: str = "statevector_sampler",
    _last_attempt: _LastAttempt | None = None,
    _fixed_probabilities: _FixedRoundProbabilities | None = None,
) -> dict[str, Any]:
    """Find one independently verified candidate on an ideal local simulator.

    A supplied marked_count schedules rounds near the first Grover peak.
    Omitting it uses a bounded Boyer–Brassard–Høyer–Tapp-style schedule.
    Each attempt samples once and verifies the result. The optional prepared
    samplers reuse a deterministic ideal state or its measurement probabilities.
    Every attempt gets its own sampler seed. The budget counts logical
    phase-oracle invocations for every sample. No
    candidate within the budget yields BUDGET_EXHAUSTED, even when a caller
    supplied zero marked states. Use audit_phase_oracle for an exhaustive
    reference on a small instance. The diffuser options use the same
    clean-workspace contract as build_grover_search_circuit.
    """
    _validate_phase_oracle(phase_oracle, search_qubits)
    if not callable(verifier):
        raise TypeError("verifier must be callable")
    if ideal_sampler not in IDEAL_SAMPLERS:
        raise ValueError(f"ideal_sampler must be one of {', '.join(IDEAL_SAMPLERS)}")
    _bounded_int(max_shots, 1, MAX_SEARCH_SHOTS, "max_shots")
    _bounded_int(max_oracle_queries, 0, MAX_ORACLE_QUERIES, "max_oracle_queries")
    _bounded_int(max_rounds_per_shot, 0, MAX_ITERATIONS, "max_rounds_per_shot")
    _bounded_int(seed, 0, (1 << 32) - 1, "seed")
    fixed_rounds = None
    if marked_count is not None:
        fixed_rounds = min(recommended_iterations(search_qubits, marked_count), max_rounds_per_shot)

    use_probabilities = ideal_sampler == "prepared_probabilities"
    shared = _fixed_probabilities
    if (
        not use_probabilities
        or shared is None
        or _last_attempt is not None
        or shared.oracle is not phase_oracle
        or shared.configuration != (search_qubits, fixed_rounds, diffuser)
    ):
        shared = None
    clean_mcx = _prepare_diffuser(phase_oracle, search_qubits, diffuser) if shared is None else None

    from numpy.random import default_rng
    from qiskit.primitives import StatevectorSampler

    oracle = phase_oracle.copy() if shared is None else phase_oracle
    prepare_samples = False if ideal_sampler == "statevector_sampler" else None
    measured_qubits = list(range(search_qubits))
    schedule_random = random.Random(seed)
    sample_random = random.Random(seed ^ 0xA5A5A5A5)
    window = 1.0
    window_limit = min(math.sqrt(1 << search_qubits), max_rounds_per_shot + 1)
    counts: Counter[str] = Counter()
    attempts = []
    circuits: OrderedDict[int, tuple[QuantumCircuit, Statevector | NDArray | None]] = OrderedDict()
    oracle_queries = 0
    candidate = None
    exhaustion_reason = "SHOT_BUDGET"
    for _ in range(max_shots):
        rounds = fixed_rounds if fixed_rounds is not None else schedule_random.randrange(math.ceil(window))
        if oracle_queries + rounds > max_oracle_queries:
            exhaustion_reason = "ORACLE_QUERY_BUDGET"
            break
        cached = circuits.get(rounds)
        build_seconds = 0.0
        if cached is None:
            if len(circuits) == MAX_CACHED_ROUND_CIRCUITS:
                circuits.popitem(last=False)
            if shared is not None:
                circuit = shared.circuit
                prepared = shared.weights()
                prepare_samples = True
            else:
                build_started = perf_counter() if _last_attempt is not None else 0.0
                circuit = _build_circuit(oracle, search_qubits, rounds, clean_mcx)
                build_seconds = perf_counter() - build_started if _last_attempt is not None else 0.0
                if prepare_samples is None:
                    prepare_samples = _supports_prepared_sampling(
                        oracle, search_qubits, use_probabilities=use_probabilities,
                    )
                prepared = None
                if prepare_samples:
                    prepared = _prepare_ideal_measurement(circuit, measured_qubits, use_probabilities=use_probabilities)
            circuits[rounds] = (circuit, prepared)
        else:
            circuit, prepared = cached
            circuits.move_to_end(rounds)

        sample_seed = sample_random.getrandbits(32)
        if prepared is None:
            sampler = StatevectorSampler(seed=sample_seed)
            result = sampler.run([circuit], shots=1).result()[0].data.answer.get_counts()
            if len(result) != 1 or sum(result.values()) != 1:
                raise RuntimeError("sampler must return exactly one shot per attempt")
            bits = next(iter(result))
        elif use_probabilities:
            outcome = default_rng(sample_seed).choice(prepared.size, size=1, p=prepared)[0]
            bits = format(int(outcome), f"0{search_qubits}b")
        else:
            prepared.seed(sample_seed)
            bits = str(prepared.sample_memory(shots=1, qargs=measured_qubits)[0])
        if len(bits) != search_qubits or any(bit not in "01" for bit in bits):
            raise RuntimeError("sampler returned an invalid search bitstring")
        oracle_queries += rounds
        counts[bits] += 1
        verified = _verify(verifier, bits)
        attempts.append({"rounds": rounds, "sample": bits, "verified": verified})
        if _last_attempt is not None:
            _last_attempt.circuit = circuit
            _last_attempt.build_seconds = build_seconds
        if verified:
            candidate = bits
            exhaustion_reason = None
            break
        window = min(window * 1.2, window_limit)

    used_sampler = ideal_sampler if prepare_samples and attempts else "statevector_sampler"
    simulators = {
        "statevector_sampler": "Qiskit StatevectorSampler (ideal)",
        "prepared_statevector": "Qiskit Statevector (prepared ideal)",
        "prepared_probabilities": "Qiskit Statevector + NumPy (prepared probabilities)",
    }
    report = {
        "status": "FOUND" if candidate is not None else "BUDGET_EXHAUSTED",
        "execution_mode": "SIMULATOR",
        "simulator": simulators[used_sampler],
        "diffuser": diffuser,
        "strategy": "KNOWN_MARKED_COUNT" if marked_count is not None else "BBHT_BOUNDED",
        "candidate": candidate,
        "exhaustion_reason": exhaustion_reason,
        "search_qubits": search_qubits,
        "work_qubits": oracle.num_qubits - search_qubits,
        "basis_order": " ".join(f"q{i}" for i in reversed(range(search_qubits))),
        "marked_count": marked_count,
        "scheduled_rounds": fixed_rounds,
        "shots_used": len(attempts),
        "oracle_queries": oracle_queries,
        "verifier_calls": len(attempts),
        "max_shots": max_shots,
        "max_oracle_queries": max_oracle_queries,
        "max_rounds_per_shot": max_rounds_per_shot,
        "seed": seed,
        "counts": dict(sorted(counts.items())),
        "attempts": attempts,
        "quantum_hardware_used": False,
        "provider_job_submitted": False,
    }
    if ideal_sampler != "statevector_sampler":
        report["ideal_sampler"] = {"requested": ideal_sampler, "used": used_sampler if attempts else None}
    return report


def build_austin_phase_oracle() -> QuantumCircuit:
    """Return the two-CZ phase oracle for q0 == 1 and q1 != q2."""
    from qiskit import QuantumCircuit

    oracle = QuantumCircuit(3, name="austin_phase_oracle")
    oracle.cz(0, 1)
    oracle.cz(0, 2)
    return oracle


def build_mod4_range_oracle() -> QuantumCircuit:
    """Mark four-bit x in [8, 16) with x % 4 == 1, using one work flag."""
    from qiskit import QuantumCircuit

    oracle = QuantumCircuit(5, name="mod4_range_oracle")
    # x >= 8 sets q3; x % 4 == 1 sets q0 and clears q1.
    oracle.x(1)
    oracle.mcx([0, 1, 3], 4)
    oracle.z(4)
    oracle.mcx([0, 1, 3], 4)
    oracle.x(1)
    return oracle


def build_mod4_range_direct_oracle() -> QuantumCircuit:
    """Mark the range and remainder constraint directly on the four search wires."""
    from qiskit import QuantumCircuit

    oracle = QuantumCircuit(4, name="mod4_range_direct_oracle")
    # q3 and q0 must be one, q1 must be zero, and q2 may be either value.
    oracle.x(1)
    oracle.ccz(0, 1, 3)
    oracle.x(1)
    return oracle


def mod4_range_predicate(bits: str) -> bool:
    """Independently check the four-bit unsigned range and remainder."""
    if len(bits) != 4 or any(bit not in "01" for bit in bits):
        return False
    value = int(bits, 2)
    return 8 <= value < 16 and value % 4 == 1
