# Changelog

## Unreleased

- Make the adaptation-use-case note self-contained so the package README does not depend on a sibling ChronoRealm project when published as its own repository.

## 0.1.0 — prepared 2026-11-09; not yet published

- Package the ChronoRealm three-qubit Grover example with a dependency-free CPU statevector reference.
- Add optional Qiskit/Cirq circuits, generic explicit-marked-state circuit construction, documentation, tests, CI, citation and license files.
- Correct the known-M first-peak estimate in the portable simulator to select the nearest iteration peak.
- License the package under Apache-2.0.
- Rewrite the README as a plain-language guide with install, run, and custom-rule examples.
- Add an ordered getting-started guide that separates the CPU teaching rule from reversible quantum oracles and gives a safe path from a toy example to a device-specific experiment.
- Add a source-reviewed GPR case study and explain why the reviewed GPR, Wi-Fi sensing, and stud-finder material does not make Grover a universal sensing or inspection method.
- Add an experimental provider-neutral adapter contract with explicit job lifecycle and labeled timing results.
- Require adapter requests to select an execution mode and reject undeclared modes before submission.
- Include guides, examples, circuits, tests, and CI in source distributions while excluding local build/cache/data artifacts.
- Remove the citation release date until a public version is actually published.
- No QPU connection, provider job, domain-specific deployment, or quantum-advantage claim is included.
