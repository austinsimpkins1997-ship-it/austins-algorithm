# Changelog

## Unreleased

- Add an opt-in physical-index density-matrix method to offline cover sampling, retaining Aer’s routed active-wire checks.
- Accept prespecified repeated witness batches and report exact batch-success lower bounds, completed shots, and nominal gate costs.
- Add a bounded verified-witness shot plan and fresh validation batches, with measured local stage timings and the matched exact classical solver.
- Add opt-in offline sampling of the exact compiled phase covers at a shared starting layout, with equal shot budgets and separate gate and hit-rate rankings.
- Add `constraint-cover` to audit, compare, rank, and export exact direct and complement circuits on a selected target.
- Add opt-in `unit-factorized-complement` synthesis with exact phase cancellation on the failing assignments of a bounded component.
- Compose unit reduction with bounded component factorization in `unit-factorized` and allow a fixed synthetic line width for comparable constraint compilations.
- Add opt-in `unit-reduced` oracle synthesis, preserving every forced bit as a phase control and reducing clause flags where propagation simplifies the formula.
- Add the standard-library `constraint-classical` command with a unit-propagating exact first-witness solver, a scan reference, and a reproducible benchmark.
- Add `constraint-sweep --compare-diffusers` for matched diffuser comparisons with distinct seed batches, per-layout results, and simultaneous hit-rate difference intervals.
- Add an opt-in clean-ancilla diffuser to the Qiskit search API and the constraint commands. Reuse an oracle work qubit with Qiskit 2.1 or later.
- Add `constraint-sweep` to compare zero, one, and two Grover rounds at two fixed IBM snapshot placements with equal shot budgets and separate calibration and validation seeds.
- Factor independent components of small Boolean constraints when that reduces work qubits. Keep clause flags selectable and add an offline IBM snapshot run for the audited constraint circuit.

- Accept bounded JSON Boolean constraints, synthesize reversible phase oracles directly from clauses, and audit them against an independent interpreter before search.
- Add a placement check for the direct modulo-4 oracle with a caller-set hit-rate floor, two prespecified placements, and adjusted exact binomial intervals.
- Add an IBM snapshot round sweep for the direct modulo-4 oracle with fixed starting placement, separate calibration and validation shots, and exact compiled circuit reuse.
- Compare both modulo-4 oracles under matched IBM snapshot noise, with independent candidate verification, ideal equivalence checks, and per-variant resource and shot reports.
- Use a direct four-wire phase oracle by default for the modulo-4 command-line workflows. Keep the five-wire work-qubit variant selectable for matched compilation and search comparisons.
- Add target compilation and OpenQASM 3 round-trip verification with measurement maps and circuit resources.
- Add reusable phase-oracle search with a second constraint, independent verification, and bounded unknown-count search.
- Add an offline IBM snapshot benchmark, ideal and synthetic-noise comparisons, and an explicitly configured IBM physical job path.
- Add `--engine qiskit` to run Austin's circuit locally and report measured counts alongside candidates verified against the Austin predicate.
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
