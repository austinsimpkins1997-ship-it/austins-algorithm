# Extending the package

## Domain adapter contract

Keep domain logic separate from Grover mechanics. An extension proposal should specify:

1. **Decision and users:** what concrete choice or result the system serves, who uses it, and who bears errors or costs.
2. **Encoding:** finite candidate space, bit order, input-loading cost, size, and valid/invalid state handling.
3. **Predicate:** exact Boolean definition, oracle implementation, reversible work bits, uncomputation, and how the phase is applied.
4. **Output:** measured candidate interpretation, independent classical verification, ties/no-solution handling, and uncertainty.
5. **Evaluation:** correctness tests, exact small-case reference, strongest tuned classical baseline, end-to-end latency/resource/cost, noise and shots where relevant.
6. **Governance:** data rights/privacy, access, safety and misuse review, human oversight, and a named stop condition.

For a hardware or simulator plug-in, use the experimental, provider-neutral Python contract in [`ADAPTERS.md`](ADAPTERS.md). It standardizes the request and result envelope; it does not make device formats interchangeable or connect to a provider by itself.

Do not encode an ordinary Python callback into a hardware promise. The core CPU simulator calls the predicate on every classical input. Hardware needs gates implementing a reversible function such as

\[
U_f|x\rangle|y\rangle=|x\rangle|y\oplus f(x)\rangle,
\]

or an equivalent phase oracle with valid ancilla handling. A useful oracle is often the hardest and most expensive part.

## Occam's razor in the implementation path

Apply the user's “Occom's Razer” request as Occam's razor: prefer the least complex path that meets the declared need, while retaining enough evidence to decide correctly.

- Start with a direct classical baseline and a hand-checkable exact case.
- Use `core.py` for small educational simulation without adding runtime dependencies.
- Add Qiskit or Cirq only when circuit representation or framework interoperability is needed.
- Add domain data loaders, providers, noise mitigation or persistence only when a named experiment requires them and their costs/permissions are explicit.
- Each added module must state what question it answers, its input/output and failure contract, resource boundary, tests, and removal/rollback path.
- Compare method complexity and total cost; do not treat fewer quantum oracle queries as a practical win when constructing or invoking that oracle dominates.

This is a review policy, not an automatic optimizer or guarantee. Simpler does not mean “always classical”; evidence decides whether the added complexity earns its place.

## Compatibility and result records

Keep the standard-library API stable within a minor release. Version output schemas when fields or meanings change. Preserve MSB-first display order across the Python API and framework adapters, and test it with basis-state cases. Avoid importing optional frameworks from `__init__.py` or `core.py`.

The local simulator's raw statevector is exponentially sized and capped at 16 qubits. Do not increase that cap without a resource study. Circuit construction is separately subject to the supplied backend's width, instruction set, connectivity, and error budget. Backend compilation is not job submission. Provider routes, credentials, billing, data upload and job cancellation require their own explicit integration and review.
