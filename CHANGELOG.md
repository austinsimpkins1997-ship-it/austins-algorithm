# Changelog

## Unreleased

- Run the built-in Austin Qiskit circuit directly with `StatevectorSampler`, so the example samples the same two-CZ-oracle circuit it displays rather than rebuilding a second circuit from the marked-answer list.
- Place the plot's limitation note in a layout-managed figure caption so it does not collide with histogram axis labels.
- Keep the optional Qiskit and Cirq circuit examples usable in legacy Windows consoles that cannot encode Unicode box-drawing characters; clarify the current Cirq Python version requirement.
- Add regression tests that run both optional circuit examples with a simulated `cp1252` console.
- Clarify that the source repository is public on GitHub and version 0.1.0 has no release tag.
- Add an opt-in per-round ideal amplitude trace to the CPU simulator and `--trace-amplitudes` command-line option for teaching Grover's rotation.
- Add an optional Matplotlib example showing the marked/unmarked rotation, the `theta` and `2 theta` formulas, the known-solution-count first-peak estimate, and ideal probabilities beside finite-shot samples.
- Make the adaptation-use-case note self-contained so the package README does not depend on a sibling ChronoRealm project when published as its own repository.

## 0.1.0 — prepared 2026-11-09; no release tag

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
