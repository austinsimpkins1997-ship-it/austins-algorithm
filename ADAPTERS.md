# Hardware adapters: an open connection point

**Status:** experimental Python interface, not a hardware connection.

## Why the 16-qubit limit exists

The limit applies only to this package's regular-computer teaching simulator. It stores the amplitudes for every possible answer. Adding one qubit doubles the number of stored values, so memory and work grow quickly. The limit helps keep this small example from using too many resources on an ordinary computer. It is not a limit on quantum hardware.

## One algorithm cannot solve every quantum problem

Grover's method is for search: it helps find items that pass a yes/no rule. Chemistry simulation, machine learning, error correction, and quantum communication ask different questions. They may need different algorithms, circuit types, results, and hardware. The adapter interface below can help move a supported workload to a device; it does not turn Grover's search into a universal algorithm.

## The input-to-output path

```text
problem and circuit
        ↓
WorkloadArtifact (format + payload + optional width)
        ↓
ExperimentRequest (chosen execution mode, optional shots and seed)
        ↓
the caller-selected adapter checks its declared capabilities
        ↓
the caller explicitly submits, checks status, or requests cancellation
        ↓
ExecutionResult (status + device/mode + results + labeled timings)
```

This package defines the records and Python `BackendAdapter` interface in `src/austins_algorithm/adapter_api.py`. The version-2 `ExperimentRequest` requires the caller to select an execution mode (`SIMULATOR`, `EMULATOR`, `QPU`, `ANALOG_SIMULATOR`, or `OTHER`); `UNKNOWN` cannot be submitted. A workload can be text, bytes, or a framework object. Its `format_id` tells the adapter how to read it. Examples include `openqasm3`, `qiskit-circuit`, and `cirq-circuit`; an adapter must name every format it actually supports.

The package also includes one real, offline adapter for its local simulator:

```python
from austins_algorithm import (
    AustinCpuAdapter,
    ExperimentRequest,
    PythonPredicateWorkload,
    WorkloadArtifact,
)

workload = PythonPredicateWorkload(lambda bits: bits.startswith("11"), "starts_with_11")
request = ExperimentRequest(
    WorkloadArtifact("chronorealm.python-bool-predicate/1", workload, logical_width=4),
    requested_execution_mode="SIMULATOR",
    shots=32,
    seed=7,
)
result = AustinCpuAdapter().submit(request)
print(result.status, result.execution_mode, result.counts)
```

This produces a completed **classical simulation** result. The Python rule is checked on the CPU for every possible input. Do not pass this callable to a hardware adapter; hardware needs a circuit in a format that the target supports.

```python
from austins_algorithm import ExperimentRequest, WorkloadArtifact, check_compatibility

request = ExperimentRequest(
    workload=WorkloadArtifact(
        format_id="openqasm3",
        payload="OPENQASM 3.0; ...",  # Put the complete circuit text here.
        logical_width=3,
    ),
    requested_execution_mode="QPU",
    shots=1024,
    seed=7,
)

# A real adapter supplies this record. This example only shows its shape.
capabilities = adapter.capabilities()
warnings = check_compatibility(request, capabilities)
print(warnings)  # Unknown limits stay visible.

# This explicit call may contact a provider or use paid hardware, depending
# on the adapter. This package does not make it for you.
receipt = adapter.submit(request)
print(receipt.status, receipt.job_id)

# If status is QUEUED or RUNNING, the caller can ask again:
# result = adapter.get_result(receipt.job_id)
# Or explicitly request cancellation:
# result = adapter.cancel(receipt.job_id)
```

The snippet is an interface example, not a runnable device setup: `adapter` must be provided by a separately installed and reviewed plugin. A request must name the execution mode it wants; compatibility checking rejects an adapter that declares a different mode and warns when the adapter reports its modes as `UNKNOWN`. The adapter must return the mode it actually ran and must not silently switch a QPU request to a simulator. `check_compatibility` also rejects declared format, width, shot, and seed mismatches. When a limit is `None`, it reports a warning; `None` means **not reported**, not unlimited.

## What each adapter must report

- **Capabilities:** adapter name/version, accepted input formats, execution mode, and any known width, shot, seed, or asynchronous-job limits.
- **Status:** `NOT_SUBMITTED`, `NOT_SUPPORTED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, or `UNKNOWN`.
- **Execution mode:** `SIMULATOR`, `EMULATOR`, `QPU`, `ANALOG_SIMULATOR`, `OTHER`, or `UNKNOWN`. A simulator result must not be labeled as a QPU result.
- **Output:** raw measurement counts when available, their bit order when known, and an optional result payload with its format. If the adapter does not know the bit order or result meaning, leave it unknown.
- **Timing:** separate stages such as compilation, queue wait, device run, transfer, and result checking. Each time must say whether it was measured locally, reported by the adapter/provider, estimated, or unknown. Do not combine queue delay with device run time without saying so.
- **Problems:** warnings and errors with credentials removed. Unknown provider statuses map to `UNKNOWN`, not `COMPLETED`.

The request has no credential field. Adapter authors must obtain credentials using their provider's approved method, keep them out of requests/results/logs, explain any data transfer and cost, and provide explicit cancellation/status behavior. Do not save circuits or results unless a separate, reviewed feature asks to do so.

## What is connected today

- The standard-library core runs a small ideal simulation on a regular computer. `AustinCpuAdapter` exposes this same local simulator through the shared input/output records; it does not connect to a device.
- The optional Qiskit and Cirq modules build circuits and run local ideal simulators.
- Qiskit can compile a circuit for a target supplied by the caller. Compilation is local and does not submit a job.
- There is **no built-in provider adapter** in this package. No hardware account, credentials, network request, or QPU job is included or tested.

The new interface is a place for a separately reviewed adapter to plug in. It is not itself a driver for today's devices. A real adapter must be built and tested against a named, released device and its current official SDK. For hardware that has not been released, its format, limits, SDK, and behavior are not known yet. This interface can make a later adapter easier to add; it cannot make or verify that adapter ahead of those facts.

## Keeping it small, honest, and quick

This uses Occam's razor as an engineering habit: keep the main algorithm and its no-dependency simulator small, and add an optional adapter only when it answers a real question. The package does not search for plugins, install SDKs, log in, or submit a request on its own.

When measuring speed, time the whole path and label each part: circuit building, compile, upload, queue, device work, download, and answer checking. Use a local monotonic clock for local measurements and preserve provider-reported times separately. Report latency only from real runs with the adapter, device, workload, settings, and date recorded. Do not call a timeout a successful run or treat a short queue as fast device execution.

## Adding a real adapter

1. Pick a released device and name the exact decision it should help with.
2. Read its current official documentation and choose a circuit/workload format it supports.
3. Build an optional adapter package that implements `BackendAdapter`; do not add provider dependencies to the core.
4. Test the adapter with a fake backend first. Check unsupported inputs, width/shot/seed limits, failures, timeouts, cancellation, result order, and credential redaction.
5. Compare against a strong regular-computer method with the same inputs, outputs, and total work.
6. Only run a real job through a separately authorized caller that explains account use, data transfer, cost, and stop conditions.
7. Record the device, target, adapter and SDK versions, date, job ID, settings, results, and limits. A passing local test is not hardware evidence.

See [`EXTENDING.md`](EXTENDING.md) for domain rules and [`ROADMAP.md`](ROADMAP.md) for what has not been built.
