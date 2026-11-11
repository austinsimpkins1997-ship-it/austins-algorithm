# Roadmap and evidence still needed

## What exists now

- A project-specific, three-qubit Grover circuit for `q0 == 1 and q1 != q2`, implemented in Qiskit and Cirq.
- A dependency-free CPU reference that classically enumerates small predicates and simulates ideal statevectors up to 16 qubits.
- An optional Qiskit `StatevectorSampler` path that samples the built-in Austin circuit itself, including its two-CZ reversible oracle and diffuser; this remains an ideal classical simulation.
- A command line option, `--engine qiskit`, that runs the built-in circuit and verifies measured candidates against the Austin predicate.
- A generic optional Qiskit circuit builder for explicitly supplied marked bitstrings, local ideal sampling, OpenQASM example, and local transpilation against a caller-supplied target.
- An experimental Python adapter contract for declared workload formats/capabilities and honest status, output, and timing records. It is not a provider connection and has no hardware plugin yet.
- A local `AustinCpuAdapter` that runs the same bounded classical simulator through that contract; it is explicitly labeled `SIMULATOR` and does not perform quantum computation.
- Tests, math notes, installation guidance, attribution, and a contribution path.

## Implementation milestones

1. **Compilation and OpenQASM round trip.** Both built-in circuits have a local target workflow with exact ideal measurement comparisons and explicit wire maps.
2. **Reusable reversible oracle search.** The second constraint has a direct phase oracle and a compute/uncompute reference with one work qubit. An exhaustive audit checks both against the same verifier and checks workspace restoration where applicable.
3. **Unknown solution count.** The randomized controller verifies each sample and stops at explicit budgets. An empty solution set requires separate evidence.
4. **IBM model comparison.** Austin's circuit has an offline experiment across three published snapshots and a separately configurable physical job path. Physical results and provider costs remain unmeasured. See [IBM model research](IBM_MODEL_RESEARCH.md).
5. **Modulo-4 noise comparison.** The two variants now have a matched offline comparison with equal shot budgets and an ideal equivalence check. On the tested seeds, the direct variant had a higher verified hit rate on all three snapshots.
6. **Round selection and validation.** An IBM snapshot sweep compares zero through three rounds from the same initial placement and checks the calibration choice with a separate seed. One round halved the compiled two-qubit gates on Manila and Jakarta. Torino selected the two-round baseline. See [round selection results](IBM_MODEL_RESEARCH.md#round-selection-on-ibm-snapshots).
7. **Placement check.** The direct oracle can compare one and two rounds against a caller-set hit-rate floor on two prespecified placements. At the 40% demonstration floor, one round qualified on Jakarta and Torino. Manila remained inconclusive. See [placement results](IBM_MODEL_RESEARCH.md#hit-rate-floor-across-placements).
8. **Declarative constraint search.** The package accepts small Boolean formulas, audits reversible synthesis, and runs bounded known-count or unknown-count search. The sensor kit fixture has a measured circuit report and a direct classical reference. See [constraint workflow](CONSTRAINT_WORKFLOW.md).
9. **Factored constraint synthesis.** The sensor kit circuit went from six work qubits to one, kept the exact nine marked states, and fit within seven active wires on the Torino snapshot. See [the sensor kit comparison](CONSTRAINT_WORKFLOW.md#measured-local-run).
10. **Constraint round and placement sweep.** A matched offline Torino sweep evaluated all six combinations of two starting placements and zero through two rounds. One round on the prior layout led calibration and returned 49 of 128 verified validation shots with 222 CZ gates. See [the Torino sweep results](CONSTRAINT_WORKFLOW.md#torino-sweep-results).
11. **Clean-ancilla diffuser.** An opt-in exact circuit reuses the oracle's restored work qubit. At one round it saved 45 CZ gates on each Torino placement and gained 10 verified validation shots of 128 at each. Two rounds on the group-swap layout returned fewer valid shots despite lower compiled cost. See [the matched comparison](CONSTRAINT_WORKFLOW.md#matched-torino-diffuser-comparison).
12. **Fresh-seed diffuser comparison.** Torino and Jakarta each completed a matched comparison with two batches per circuit. The clean diffuser lowered compiled two-qubit counts in all eight cells, and the simultaneous hit-rate intervals all included zero. See [the two-snapshot results](CONSTRAINT_WORKFLOW.md#fresh-seed-results-on-torino-and-jakarta).
13. **Exact classical first witness.** The dependency-free solver applies forced assignments and returns the same lowest witness as the scan. On five local fixtures it was faster on four, and the scan led on the immediate witness. The reversible-oracle workflow can verify the propagation result against the exhaustive audit. See [the classical benchmark](CONSTRAINT_WORKFLOW.md#classical-first-witness-benchmark).
14. **Unit-reduced reversible synthesis.** The connected site interlock dropped from ten to seven active wires and from 650 to 358 CX gates on the same line target. Both oracles passed the 64-input phase and workspace audit. The reduced circuit ran locally on Jakarta and Torino snapshots. See [the interlock results](CONSTRAINT_WORKFLOW.md#shared-target-interlock-results).
15. **Factorization after unit reduction.** The interlock reaches six active wires with higher native gate counts on all three targets. The sensor kit gets both fewer wires and fewer CX gates on the shared line. See [the fixed-target comparisons](CONSTRAINT_WORKFLOW.md#factorization-after-unit-reduction).
16. **Complement phase cover.** The six-wire interlock saves 50 CX gates on the shared line, 37 CX on Jakarta, and 32 CZ on Torino. The structural selector keeps the sensor kit's direct cover. See [the complement comparison](CONSTRAINT_WORKFLOW.md#phase-cover-using-failing-assignments).
17. **Target-aware phase cover selection.** The comparator checks both exact covers and ranks their compiled costs under a caller-supplied active-wire cap. The Torino sensor kit selects different covers with automatic and shared starting layouts. See [the comparison](CONSTRAINT_WORKFLOW.md#select-a-cover-by-compiled-resources).
18. **Sampling without recompilation.** The verified phase covers now run directly against one offline snapshot at a shared starting layout. The gate-count winner led the observed hits in the Jakarta interlock and Torino sensor kit, while both rate-difference bounds included zero. See [the shared-layout sampling results](CONSTRAINT_WORKFLOW.md#shared-layout-snapshot-results).
19. **Verified witness budgets.** The snapshot workflow plans a capped batch from a conservative per-shot rate, uses fresh seeds on the retained circuits, and reports measured local costs alongside the exact classical result. At a 95% target, each of the four feasible demonstration batches returned a verified witness. See [the witness-budget results](CONSTRAINT_WORKFLOW.md#local-witness-budget-results).
20. **Repeated witness validation.** The Jakarta direct cover returned a witness in all 72 planned batches and reached the 95% lower-bound target for that fixed snapshot. The complement and the eight-batch Torino pilot remained unresolved. See [the batch results](CONSTRAINT_WORKFLOW.md#repeated-validation-at-fixed-budgets).

21. **Physical-index density-matrix sampling.** Torino accepts the original routed circuits with seven active wires and the same backend noise model. The analytical distributions agree with an independent compact representation. Four 128-shot batches took 6.81 s versus 58.16 s with MPS in the ChronoRealm run. See [the method comparison](CONSTRAINT_WORKFLOW.md#choose-the-snapshot-simulation-method).

22. **Full Torino density validation.** Each cover returned a verified witness in 111 of 112 prespecified batches. Each 97.5% one-sided lower bound was 95.126%, supporting the 95% target under the offline snapshot. Both working copies replayed the complete seeded schedule. See [the full Torino validation](CONSTRAINT_WORKFLOW.md#full-torino-validation-with-density-matrices).

23. **Compact density-matrix sampling.** The opt-in snapshot mode remaps the calibrated channels to verified active wires. The paired Torino pilot reproduced all 24 count maps and ran 4.57× faster across those sampling jobs. The production CLI replay matched the 224 validation batches and selected OpenQASM. See [the compact sampling protocol](CONSTRAINT_WORKFLOW.md#compact-density-matrix-sampling).

24. **Prepared density-matrix sampling.** A gate-only evolution runs once per cover, followed by bounded readout-only replays with the original explicit seeds. The paired Torino benchmark matched all 24 count maps and spent 0.83 s preparing and 0.026 s sampling, compared with 7.86 s across the compact jobs. See [the prepared sampling protocol](CONSTRAINT_WORKFLOW.md#prepared-density-matrix-sampling).

25. **Reduced snapshot noise startup.** The compact and prepared paths derive channels once for the active wire union. In the paired Torino benchmark, median setup fell from 3.07 s to 0.21 s. All retained channel payloads and output laws matched the full reference for the tested Manila, Jakarta, and Torino covers. See [the reduced-target results](CONSTRAINT_WORKFLOW.md#reduced-noise-model-startup).

26. **Opt-in native OpenQASM reimport.** The package retains its exact verification while allowing the native Qiskit importer, with the standard importer available after a parse rejection. Median Torino compile-and-audit wall time fell from 1.37 s to 0.69 s. The pinned compatibility audit matched 16 package exports on each of Qiskit 2.0.0 and 2.6.2. See [native reimport results](CONSTRAINT_WORKFLOW.md#native-openqasm-reimport).

27. **Bounded exhaustive oracle audit.** Eligible small oracles reuse an operator under a seven-wire cap. The original basis audit handles wider, custom, and rounding-sensitive inputs. Torino cover audits were about 9–10× faster in paired local timings, with matching audit reports and seeded CLI replay. See [oracle audit results](CONSTRAINT_WORKFLOW.md#memory-bounded-exhaustive-oracle-audit).

28. **Wider clause-flag audits.** A bounded two-buffer contraction evolves only the required work-zero input columns. The paired site-interlock audit fell from 231 ms to 26 ms and the sensor-kit audit from 215 ms to 103 ms, with matching exhaustive results. See [wider audit results](CONSTRAINT_WORKFLOW.md#wider-clause-flag-oracle-audits).

29. **Sampled-circuit handoff.** The constraint CLI compiles and samples the last completed search circuit directly. Both downstream routes receive the sampled circuit, and the build benchmark removes one synthesis, diffuser preparation, and circuit construction. See [the handoff results](CONSTRAINT_WORKFLOW.md#compile-the-actual-search-attempt).

30. **Repeated-round circuit reuse.** The ideal scheduler retains up to four measured circuits per search. In the paired 32-shot fixtures, fixed-round builds fell from 32 to one and randomized builds from 32 to ten, with exact seeded reports. See [circuit reuse results](CONSTRAINT_WORKFLOW.md#reuse-circuits-for-repeated-round-counts).

31. **Prepared ideal statevector sampling.** The opt-in ideal sampler reuses bounded deterministic states with exact tested seed replay. Fixed-round field sensor median time fell from 305 ms to 13 ms, with the measured-circuit handoff preserved. See [prepared ideal sampling](CONSTRAINT_WORKFLOW.md#prepared-ideal-statevector-sampling).

32. **Cached ideal probabilities.** The opt-in sampler keeps bounded probability tables and draws integer outcomes with the original weights and per-attempt seeds. The 12-measured-wire repeated-draw fixture fell from 191 ms with a prepared statevector to 2 ms, and all 125 tested production draws matched. See [probability cache results](CONSTRAINT_WORKFLOW.md#cached-ideal-measurement-probabilities).

33. **End-to-end first-witness cost.** A fixed 95% target and 128 paired trials compare the direct constructor, generic solvers, and ideal prepared search on the sensor kit. The local ideal run verified all 128 trials and the portable line-target export. A separate offline Torino replay retained the two covers' 111/112 batch results. See [the reproducible benchmark](CONSTRAINT_WORKFLOW.md#end-to-end-first-witness-cost).

34. **Prepared line search.** A fixed formula and compiler configuration share one verified synthetic-line export across independently seeded and verified ideal runs. The sensor-kit comparison took 1.07 s including setup versus 11.61 s for 128 fresh requests, with identical sampled attempts. See [prepared line reuse](CONSTRAINT_WORKFLOW.md#reuse-a-verified-line-compilation).

35. **Plan-owned ideal probabilities.** A prepared line plan lazily shares the exact logical measurement weights across affordable seeded runs. All 64 paired reports matched on the sensor kit and the ten-wire site interlock. Later request medians fell to 0.123 and 0.134 ms respectively. See [plan probability reuse](CONSTRAINT_WORKFLOW.md#reuse-a-plans-ideal-probability-table).

36. **Eight-bit first-witness comparison.** The connected telemetry-power fixture audits all 256 inputs. The prepared ideal run supported the 95% witness target, and the direct constructor had the lowest measured cost in both fresh and prepared comparisons. Torino compiled the 12-active-wire circuit and reported the local sampling cap. See [telemetry-power results](CONSTRAINT_WORKFLOW.md#eight-bit-telemetry-power-benchmark).

37. **Four-block oracle audit.** The eight-bit telemetry-power oracle uses four bounded blocks and two reused buffers. The isolated audit fell from 3.97 s to 0.602 s in the ChronoRealm comparison, with identical 256-input results and full basis fallback for uncertain blocks. See [four-block audit](CONSTRAINT_WORKFLOW.md#four-block-audit-at-eight-search-bits).

38. **Exact controlled-gate row updates.** The block audit uses the actual gate matrix to select exact swaps and sign flips. It retains all coefficients on the contraction path. The telemetry comparison fell from 721 to 541 ms, with identical audit and seeded workflow results. See [row updates](CONSTRAINT_WORKFLOW.md#exact-row-updates-for-controlled-gates).

39. **Audit-local dense matrix reuse.** Eligible dense steps retain read-only copies of their actual coefficients within a shared 256 KiB payload cap. Telemetry matrix builds fell from 77 to 51, and an adversarial instance override now reaches the basis reference on both accelerated paths. See [dense matrix reuse](CONSTRAINT_WORKFLOW.md#reuse-dense-matrices-within-one-audit).

40. **Bounded tiled dense contraction.** The audit packs larger local gates into two buffers sharing a 256 KiB cap. The ChronoRealm telemetry median fell from 323 to 220 ms, with the same audit verdicts and verified workflow outputs. See [tiled multiplication](CONSTRAINT_WORKFLOW.md#tiled-multiplication-for-larger-dense-gates).

41. **Direct dense views for contiguous wires.** Naturally ordered spans multiply through views of the existing buffers. The field sensor audit avoided its 256 KiB packing allocation and fell from 42.02 to 37.60 ms in the ChronoRealm comparison. See [contiguous operands](CONSTRAINT_WORKFLOW.md#direct-multiplication-for-contiguous-operands).

## Remaining evidence

- Gather more precise matched evidence on noisy hit rates if a diffuser default change is proposed.
- Run one authorized physical experiment with a named backend, record its actual job and measured results, and compare its hit rate and full cost with the local references.
- Reprofile the complete fresh and prepared workflows after the audit changes. Compare oracle synthesis, audit, compilation, and local model preparation before choosing the next bottleneck.
