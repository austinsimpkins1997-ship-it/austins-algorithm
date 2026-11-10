# Roadmap and evidence still needed

## What exists now

- A project-specific, three-qubit Grover circuit for `q0 == 1 and q1 != q2`, implemented in Qiskit and Cirq.
- A dependency-free CPU reference that classically enumerates small predicates and simulates ideal statevectors up to 16 qubits.
- A generic optional Qiskit circuit builder for explicitly supplied marked bitstrings, local ideal sampling, OpenQASM example, and local transpilation against a caller-supplied target.
- An experimental Python adapter contract for declared workload formats/capabilities and honest status, output, and timing records. It is not a provider connection and has no hardware plugin yet.
- A local `AustinCpuAdapter` that runs the same bounded classical simulator through that contract; it is explicitly labeled `SIMULATOR` and does not perform quantum computation.
- Tests, math notes, installation guidance, attribution, and a contribution path.

## What is needed before broader claims

1. A user-selected, non-toy decision with a meaningful error, latency or cost threshold.
2. A domain encoding and efficient reversible oracle with measured construction, input-loading, gate, ancilla and query costs.
3. A fixed, strongest relevant classical baseline evaluated on the same inputs, outputs and correctness target.
4. An unknown-solution-count procedure if `M` cannot be supplied or measured without a classical exhaustive pass.
5. Target-specific transpilation, noise/shot analysis, provider validation and an explicitly authorized QPU run if hardware evidence is sought.
6. Independent replication and an affected-user evaluation before claiming practical or societal value.
7. Separate algorithm designs and packages for domains whose problems are not Grover search (for example, chemistry simulation, QML training, tomography, error correction or communication).
8. Implement one optional adapter for a named, released backend, after its official interface, permissions, data path, costs, and cancellation behavior are reviewed. Future or unreleased hardware support remains unknown until those interfaces exist.

No quantum-hardware job, advantage, cross-domain integration, application outcome or social benefit is established by this release. A GitHub URL and public release state are intentionally not claimed until the package is reviewed and the repository is actually created/published.
