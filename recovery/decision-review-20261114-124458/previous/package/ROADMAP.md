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

42. **Workflow reprofile and native constraint export.** Warm stage timings identify the telemetry audit, sensor export verification, and Torino density preparation as the largest costs in their respective workflows. The new `constraint --native-qasm` preference reduced the measured telemetry Torino compile and verify time from 594 to 238 ms with matching evidence. See [workflow profile](CONSTRAINT_WORKFLOW.md#workflow-profile-after-the-audit-changes).

43. **Opt-in prepared density fusion.** Aer gate-only fusion reduced the warm Torino preparation median from 0.983 to 0.530 s. The largest saved matrix difference was `1.67e-16`, and all 228 seeded count maps matched. See [density fusion](CONSTRAINT_WORKFLOW.md#opt-in-aer-fusion-for-prepared-density).

44. **Reproducible density fusion comparison.** The packaged benchmark checks raw matrices and seeded reports on the Jakarta interlock and the Torino sensor kit. Jakarta's warm preparation median moved from 0.248 to 0.227 s, with all 148 archived count maps and both witness decisions preserved. See [the paired benchmark](CONSTRAINT_WORKFLOW.md#opt-in-aer-fusion-for-prepared-density).

45. **Bounded stock-gate ideal verification.** The separate original, compiled, and reimported checks use a local matrix path for eligible small stock-gate circuits. In paired offline runs, the complete workflow fell from 0.922 to 0.800 s on Jakarta and 2.200 to 2.006 s on Torino with identical verified evidence. See [stock-gate verification](CONSTRAINT_WORKFLOW.md#faster-ideal-verification-for-stock-gates).

46. **Prepared offline snapshot compilation.** A caller-owned plan reuses the audited programs for repeated requests on one fixed snapshot and layout. In the ChronoRealm comparison, setup-inclusive reuse led in all four pairs by request two on both snapshots, with identical paired reports and exports. See [snapshot compilation reuse](CONSTRAINT_WORKFLOW.md#reuse-a-verified-snapshot-compilation).

47. **Plan-owned density reuse.** Prepared snapshot plans can retain both validated pre-readout matrices per fusion setting and reconstruct fresh readout samplers. ChronoRealm second-request medians fell from 0.685 to 0.206 s on Jakarta and 1.020 to 0.340 s on Torino, with exact report and export parity. See [density reuse](CONSTRAINT_WORKFLOW.md#reuse-pre-readout-density-across-requests).

48. **Profiled holdout verification.** Snapshot batches expose separate submission, wait, count formatting, and verification timings. A prepared plan's exhaustive classical lookup and validated count snapshot lowered measured holdout count verification to about 5 ms on both protocols, with exact seeded result parity. See [holdout verification](CONSTRAINT_WORKFLOW.md#profile-and-reduce-holdout-verification-work).

49. **Fixed-schedule holdout futility.** An opt-in stop reports every unsubmitted seed and the best attainable bound once the prespecified target is unreachable. Archived Jakarta and Torino replays skipped 39, 24, and 32 jobs across three schedules while preserving every executed result. See [holdout futility](CONSTRAINT_WORKFLOW.md#stop-a-holdout-whose-target-is-unreachable).

50. **Pre-run witness schedule sizing.** The CLI and Python planner compute the minimum per-cover batches and interleaved seed count for a target and miss allowance. Replays crossed the 95% target at the calculated 72- and 110-batch boundaries. See [schedule sizing](CONSTRAINT_WORKFLOW.md#size-a-holdout-before-sampling).

51. **Proposed seed schedule checks.** `witness-schedule` shares runtime seed validation and reports the sizing result for a supplied ordered list. The accepted Jakarta and Torino lists replayed with exact archived evidence. See [seed preflight](CONSTRAINT_WORKFLOW.md#check-a-proposed-seed-list-before-sampling).

52. **Saved offline witness replay.** Versioned requests embed the checked formula and seed lists, pin the snapshot content and compiled wire maps, and replay through the existing simulator workflow. Jakarta and Torino reproduced the explicit commands and archived evidence. See [saved replay](CONSTRAINT_WORKFLOW.md#save-and-run-a-checked-offline-replay).

53. **Inspection-only offline replay.** A saved request can be checked against the installed snapshot and compiled exports before any Aer job. The report includes the compiled sampling limits and the hypothetical seed assessment. See [replay inspection](CONSTRAINT_WORKFLOW.md#inspect-a-saved-replay-without-sampling).

54. **Exact-byte replay receipts.** An optional bundle records the consumed request, observed report, selected OpenQASM, and software versions. Both offline IBM snapshot replays verified with the Python standard library alone. See [replay receipts](CONSTRAINT_WORKFLOW.md#save-and-verify-a-replay-receipt).

55. **Offline replay receipt comparison.** The CLI locates the first changed field in each verified JSON section, checks the selected OpenQASM and recorded software versions, and skips measured report durations. Independent Jakarta and Torino replays matched across drives by model, and a changed Jakarta seed was traced through the request and observed report. See [receipt comparison](CONSTRAINT_WORKFLOW.md#compare-two-verified-replay-receipts).

56. **Bounded offline regression suites.** Named baseline receipts can be replayed in sequential workers with case and suite shot limits and per-worker timeouts. Jakarta and Torino matched their baselines. The aggregate-limit and timeout runs exercised the stop and continuation paths. See [offline regression suites](CONSTRAINT_WORKFLOW.md#rerun-a-named-set-of-offline-baselines).

57. **Packaged offline smoke baseline.** The source fixture runs a verified two-bit XOR replay against FakeManila under a 160-shot ceiling and records its exact requirements. Scoped checkout attributes preserve the bytes covered by the receipt. See [Manila smoke baseline](CONSTRAINT_WORKFLOW.md#run-the-packaged-manila-smoke-baseline).

58. **Automated packaged Manila smoke regression.** A Windows test job builds the source archive, installs its pinned smoke environment, and replays the packaged baseline. The regression exit status gates the job and replay evidence is available as a workflow artifact. See [the Manila smoke baseline](CONSTRAINT_WORKFLOW.md#run-the-packaged-manila-smoke-baseline).

59. **Bounded diffuser seed schedules.** The comparison accepts up to 16 alternating evaluation seeds and keeps its original four-seed entry point. Doubling the batches per variant narrowed all eight simultaneous Jakarta and Torino hit-rate intervals by 26.8–32.0%. All eight intervals included zero. See [the longer seed schedule results](CONSTRAINT_WORKFLOW.md#longer-fixed-seed-schedules).

60. **Prepared diffuser density reuse.** An opt-in simulator mode prepares each compiled variant once. Both copies matched physical-index density matrices, readout channels, and classical output probabilities for all 16 tested Jakarta and Torino variants. Torino wall time fell from 61.7–65.2 s to about 8.2 s in the paired runs. See [prepared diffuser states](CONSTRAINT_WORKFLOW.md#reuse-prepared-diffuser-states).

61. **Modeled diffuser probabilities.** The offline comparison can report the full bit-ordered distribution, verified hit probability, and clean-minus-standard difference with zero measurement batches. Both copies checked all 1,024 Jakarta and Torino outcome probabilities against the physical-index density reference. See [modeled diffuser hit probabilities](CONSTRAINT_WORKFLOW.md#calculate-modeled-diffuser-hit-probabilities).

62. **Modeled diffuser witness budgets.** The offline comparison sizes the smallest fixed batch for a caller's modeled witness target and reports nominal two-qubit gate executions. At 95%, both copies verified 6–18 required shots and 22.5–35.2% lower nominal gate totals for clean-ancilla across eight paired cases. See [modeled witness budgeting](CONSTRAINT_WORKFLOW.md#plan-witness-shots-from-a-modeled-diffuser-rate).

63. **Modeled diffuser witness validation.** Fixed fresh seed schedules sample each feasible plan on its prepared circuit and report one-sided simultaneous witness bounds. Both copies reproduced the 1,600-batch Jakarta and Torino run. Two variants supported the 95% target. See [witness validation](CONSTRAINT_WORKFLOW.md#validate-modeled-witness-budgets-on-fresh-seeds).

64. **Diffuser holdout preflight.** The witness schedule command sizes or checks an alternating seed list for the eight-variant family, reports its cap and shortfall, and runs without the quantum SDK. Both copies confirmed the 99-batch zero-miss boundary and the 128-batch cap failure with one tolerated miss. See [diffuser holdout preflight](CONSTRAINT_WORKFLOW.md#check-a-diffuser-holdout-before-sampling).

65. **Diffuser holdout futility.** An opt-in stop records the best attainable bound and exact unsubmitted seeds per variant. Both copies reproduced the Jakarta and Torino schedules with 994 fewer batches, preserving every executed outcome and both supported targets. See [diffuser holdout futility](CONSTRAINT_WORKFLOW.md#stop-a-diffuser-holdout-whose-target-is-unreachable).

66. **Diffuser validation power.** An optional planner estimates the chance that a fixed schedule supports the target under a supplied batch success probability. It checks eligible sizes for the best probability or smallest schedule meeting requested power. Both copies verified six reference grids and 16 archived per-variant forecasts. See [validation power](CONSTRAINT_WORKFLOW.md#estimate-diffuser-validation-power-before-sampling).

67. **Modeled diffuser holdout preview.** Plan-only sweeps report per-circuit confidence power and prospective workload using the same compiled circuits and modeled batch rates as validation. Both copies matched all 16 default forecasts and four archived workloads with zero measurement jobs. See [holdout preview](CONSTRAINT_WORKFLOW.md#preview-modeled-diffuser-holdout-costs-and-power).

68. **Power-targeted diffuser shot budgets.** Preview and validation can size the smallest capped batch for both the witness target and a requested holdout power. Both copies matched independent minima and actual local workloads on Jakarta and Torino, including power failures at a 20-shot cap. See [power-targeted shot budgets](CONSTRAINT_WORKFLOW.md#size-witness-shots-for-a-validation-power-target).

69. **Joint diffuser schedule and shot search.** A planning sweep compares eligible batch counts and power-targeted shot minima, then selects the lowest shot and nominal gate costs across full-coverage schedules. Both copies verified all 30 eligible sizes on Jakarta and Torino and replayed the 99-batch minima. See [schedule cost search](CONSTRAINT_WORKFLOW.md#compare-diffuser-holdout-schedules-before-assigning-seeds).

70. **Checked diffuser holdout replay.** Save a fully feasible selected schedule with explicit seed bindings, snapshot metadata, circuit identity, and workload. Replay recomputes the checks before measurement. Both copies reproduced the 1,584 Jakarta and Torino seeded outcomes and rejected altered requests before sampling. See [diffuser holdout replay](CONSTRAINT_WORKFLOW.md#save-and-verify-a-diffuser-holdout-request).

71. **Diffuser execution receipts.** Checked replays can save exact request and report bytes. The offline verifier checks the bindings and derives observed workload from the batch records, including futility and timeout accounting. Both copies verified the two 792-batch IBM snapshot receipts and rejected a rehashed workload disagreement. See [diffuser execution receipts](CONSTRAINT_WORKFLOW.md#record-and-verify-a-diffuser-execution-receipt).

72. **Diffuser receipt comparison.** The offline comparator aligns physical circuit variants and seeds, shows workload deltas, and distinguishes exact count changes from differences in execution conditions. Both copies matched the 792 completed batches per snapshot and checked added seeds, changed budgets, reordered layouts, and partial execution states. See [diffuser receipt comparison](CONSTRAINT_WORKFLOW.md#compare-diffuser-receipts-by-circuit-and-seed).

73. **Bounded diffuser replay regression.** One local command verifies and pins a baseline receipt, enforces a full-plan shot limit, runs a timed worker, and compares verified outcomes. Both copies matched the Jakarta and Torino baselines at their exact limits and verified rejection, timeout, and difference paths. See [bounded diffuser regression](CONSTRAINT_WORKFLOW.md#run-a-diffuser-regression-within-a-declared-limit).

74. **Diffuser regression suites.** A manifest names multiple local baselines and declares per-case and combined shot limits. Both copies matched the Jakarta and Torino suite at 37,620 shots and rejected lower limits before launching any worker. See [diffuser regression suites](CONSTRAINT_WORKFLOW.md#run-named-diffuser-regressions-from-a-manifest).

75. **Packaged Manila diffuser smoke baseline.** A five-wire fixture covers all eight diffuser variants in 16 batches and 112 shots. Both copies verified the receipt, reproduced the baseline from checkout, and replayed the packaged fixture using an installed wheel. See [Manila diffuser smoke](CONSTRAINT_WORKFLOW.md#run-the-packaged-manila-diffuser-smoke-baseline).

76. **Combined Manila Windows smoke workflow.** One packaged installation runs both regressions and configures separate evidence uploads. Both copies passed the exact local command blocks and verified that either cap failure still allows the other replay to finish. See [Windows smoke workflow](CONSTRAINT_WORKFLOW.md#run-the-manila-smoke-regressions-in-the-windows-workflow).

77. **Concurrent Manila smoke replays.** Local profiling identified replay time as the largest part of the three workflow command blocks. Concurrent CLI trials cut median replay wall time by 32–37% across the two copies. Both copies verified the exact updated PowerShell workflow, including a one-shot reduction of each cap in turn. See [local replay timings](CONSTRAINT_WORKFLOW.md#local-replay-timings).

78. **Optional native OpenQASM importer for diffuser replays.** Diffuser comparisons can prefer the native importer and save that choice in a schema 2 request. Both copies reproduced the original schema 1 fixture and verified a new native Manila receipt with all 16 seeded histograms and 112 shots. See [native diffuser import](CONSTRAINT_WORKFLOW.md#prefer-native-openqasm-import-for-a-diffuser-holdout).

79. **Packaged native Manila Windows smoke baseline.** The Windows job replays the phase-cover, original diffuser, and native diffuser receipts from one installed source distribution. Both copies matched all three and checked each one-shot cap failure with the other two replays completing. See [Windows smoke verification](CONSTRAINT_WORKFLOW.md#run-the-manila-smoke-regressions-in-the-windows-workflow).

80. **Diffuser compilation timing diagnostics.** Sampled comparisons, check-only runs, and validation receipts report separate transpilation, export, import, and verification times. Both copies reproduced the two Manila request schemas byte for byte and matched their replay fingerprints and seeded histograms. See [diffuser compilation timings](CONSTRAINT_WORKFLOW.md#inspect-diffuser-compilation-timings).

81. **Offline diffuser timing comparison.** An opt-in receipt comparison aligns recorded compiler times by physical layout, round count, and diffuser, with importer conditions and missing observations visible. Both copies verified the timed and archived Manila receipts without the quantum SDK. See [compare recorded compilation timings](CONSTRAINT_WORKFLOW.md#compare-recorded-diffuser-compilation-timings).

82. **Matched diffuser compiler benchmark.** An installed command benchmarks checked requests in fresh processes with alternating order, bounded time, and verified compiler timings. Both copies verified four Manila pairs and independently recomputed the timing summaries. See [benchmark matched requests](CONSTRAINT_WORKFLOW.md#benchmark-matched-diffuser-requests).

83. **Offline diffuser benchmark verification.** An installed command verifies saved benchmark requests, trial order and reports, and recomputes paired timing statistics. New runs bind report and log bytes with SHA-256, and both copies verified completed and timed-out bundles under both schemas. See [verify saved benchmark evidence](CONSTRAINT_WORKFLOW.md#verify-saved-diffuser-benchmark-evidence).

84. **Cross-run diffuser benchmark comparison.** An installed command verifies both saved benchmarks and reports their sample sizes, timing spread, median differences, and recorded workload, importer, and environment changes. Both copies verified complete, reversed-arm, and incomplete comparisons. See [compare saved benchmarks](CONSTRAINT_WORKFLOW.md#compare-saved-diffuser-benchmarks).

85. **Three-snapshot diffuser compiler benchmark.** A reproducible example runs matched OpenQASM importer requests on Manila, Jakarta, and Torino and records compiled sizes and verified timing summaries. Both copies verified all three snapshots with two pairs each. See [benchmark importers across IBM snapshots](CONSTRAINT_WORKFLOW.md#benchmark-diffuser-importers-across-ibm-snapshots).

86. **Importer-only benchmark on fixed OpenQASM.** A two-stage example captures verified diffuser exports and times both direct parsers on identical inputs. Both copies checked all three IBM snapshots, verified 192 imports in total, and rejected an edited export before parsing. See [benchmark importers on fixed exports](CONSTRAINT_WORKFLOW.md#benchmark-openqasm-importers-on-fixed-exports).

87. **Compile and export against a named IBM snapshot.** The built-in and declarative CLI compile paths can target bundled Manila, Jakarta, or Torino without a separate noisy simulation. Both copies checked matching standard and native exports for four circuits, including work-qubit cases, and verified the custom-gate fallback. See [compile for an offline IBM snapshot](CONSTRAINT_WORKFLOW.md#compile-for-an-offline-ibm-snapshot).

88. **Native import for offline IBM comparisons.** All four snapshot modes gained an option for Qiskit's native parser. The initial verification included the automatic layout compiles. The IBM install extra includes the standard importer. Both copies verified the same 52 seeded histograms per importer, with matching circuit hashes, measurement maps, and selection decisions across all three snapshots. See [matched importer verification](IBM_MODEL_RESEARCH.md#choose-an-openqasm-importer-for-offline-comparisons).

89. **Faster starting layout selection.** Round sweeps and placement checks discover layouts through the same seeded transpilation while keeping the verified fixed-layout compiles for sampling. Both copies profiled all three snapshots and matched saved circuit and seeded sampling evidence with both parsers. See [layout selection profile](IBM_MODEL_RESEARCH.md#select-the-starting-layout-without-an-export-round-trip).

90. **Reuse the calibrated simulator across oracle variants.** The modulo-4 comparison builds one full simulator per snapshot and halts after an unconfirmed cancellation. Both copies matched seeded results and measured lower local comparison times across all three models. The placement profile also recorded where noise projection changes exact seeded results. See [simulator reuse and verification](IBM_MODEL_RESEARCH.md#reuse-the-calibrated-simulator-between-oracle-variants).

91. **Reusable placement checks.** A Python plan retains verified placements and circuits while each request builds fresh simulators and returns detached evidence. Both copies matched seeded outcomes under both parsers and measured lower two-request totals with the standard parser. Unconfirmed cancellation stops the request and invalidates the plan. See [reusable placement checks](IBM_MODEL_RESEARCH.md#reuse-verified-circuits-across-placement-checks).

92. **Offline placement receipts.** A new receipt preserves calibration, exact OpenQASM, seed results, and placement decisions. Both copies verified all three snapshots with site packages disabled and rejected altered selections even with updated manifest checksums. See [save and verify a placement receipt](IBM_MODEL_RESEARCH.md#save-and-verify-a-placement-receipt).

93. **Compare verified placement receipts.** The offline command aligns cells and seeds and reports observed histogram and decision changes alongside differences in calibration, physical circuits, shot budgets, and software. Both copies verified complete, changed-condition, and timeout cases. See [compare two placement receipts](IBM_MODEL_RESEARCH.md#compare-two-placement-receipts).

94. **Replay verified placement receipts.** The local command rebuilds all recorded circuit identities before sampling, then checks seeded histograms and placement decisions. It supports check-only preflight and measured trials from partial references. See [replay a placement receipt](IBM_MODEL_RESEARCH.md#replay-a-placement-receipt).

95. **Packaged Manila placement smoke replay.** The source distribution carries a 32-shot sealed baseline. The Windows smoke job verifies its receipt, rebuilds all four circuit identities, and requires eight exact histogram matches with separate phase evidence. See [packaged placement smoke check](IBM_MODEL_RESEARCH.md#packaged-manila-placement-smoke-check).

96. **Manila smoke environment evidence.** Capture the installed versions, pin comparisons, exact input fingerprints, and available runner context before replay. The environment artifact and exit diagnostics are separate from the four replay lanes. See [Manila smoke environment evidence](IBM_MODEL_RESEARCH.md#manila-smoke-environment-evidence).

97. **Offline smoke environment comparison.** Compare valid saved reports and isolate dependency, pin, input, and runner changes from capture identity. See [compare saved smoke environments](IBM_MODEL_RESEARCH.md#compare-saved-smoke-environments).

98. **Verifiable Manila smoke run index.** Link environment and replay bundles with observed step, upload, and command outcomes. Verify the recorded file evidence after downloading or relocating the bundles. See [verify a Manila smoke run index](IBM_MODEL_RESEARCH.md#verify-a-manila-smoke-run-index).

99. **Offline Manila smoke run comparison.** Verify each index against local evidence and compare phase outcomes, file fingerprints, and bound environment records. See [compare two Manila smoke run indexes](IBM_MODEL_RESEARCH.md#compare-two-manila-smoke-run-indexes).

100. **Unknown-count search benchmark.** Compare exact classical witnesses, bounded ideal searches, and the empty-proof path on fixed six-bit formulas. The report checks every sample and separates deterministic evidence from local timings. See [unknown-count search across solution densities](CONSTRAINT_WORKFLOW.md#benchmark-unknown-count-search-across-solution-densities).

101. **Affordable-round continuation.** An opt-in policy redraws unaffordable rounds within the remaining allowance. A matched seed panel reports rescued witnesses, additional work, and preserved default traces. The adjusted attempt was verified on the local Manila snapshot. See [continue with affordable rounds](CONSTRAINT_WORKFLOW.md#continue-with-affordable-rounds).

102. **Ideal unknown-count budget planner.** Forecast first-witness probability, budget stops, and expected logical work for both policies without an SDK. Compare the model with the fixed panel and verify it against exact small enumerations. See [model the unknown-count budgets](CONSTRAINT_WORKFLOW.md#model-the-unknown-count-budgets-before-sampling).

103. **Unknown-count budget selection.** Evaluate explicit shot and query caps under both policies and all declared solution counts. Report feasible expected-work tradeoffs and the best attainable probability when the grid misses the target. See [select budgets for a modeled witness target](CONSTRAINT_WORKFLOW.md#select-budgets-for-a-modeled-witness-target).

104. **Selected-budget holdout benchmark.** Run preselected plans on disjoint seed blocks and audited threshold oracles. Record trial outcomes, modeled expectations, and finite-sample intervals at both declared and held-out counts. See [validate selected budgets on fresh seeds and counts](CONSTRAINT_WORKFLOW.md#validate-selected-budgets-on-fresh-seeds-and-counts).

105. **Adaptive selected-budget snapshot comparison.** Compile and audit each possible round circuit once. Compare complete ideal and calibrated-noise searches with matched seeds, independent stopping, and separate compilation, sampling, and logical work evidence. See [compare complete searches under IBM snapshot noise](CONSTRAINT_WORKFLOW.md#compare-complete-searches-under-ibm-snapshot-noise).

106. **Snapshot-aware budget selection.** Reuse verified saved round profiles without an SDK. Rank budgets by worst-case expected shots, queries, or nominal compiled two-qubit gates, and replay the selected plans on held-out cases. See [select budgets from saved snapshot distributions](CONSTRAINT_WORKFLOW.md#select-budgets-from-saved-snapshot-distributions).

107. **Bounded snapshot round profile reuse.** Share completed round probabilities and compile evidence across matching requests. Verify circuit, layout, and calibration invalidation and reproduce the archived Jakarta and Manila panels with warm profiles. See [reuse verified snapshot round profiles](CONSTRAINT_WORKFLOW.md#reuse-verified-snapshot-round-profiles).

108. **Portable snapshot profile bundles.** Export the retained round and seed evidence, verify the bundle on its own or against the full panel, and reproduce offline budget selection and holdout replay. See [export and verify a portable snapshot profile](CONSTRAINT_WORKFLOW.md#export-and-verify-a-portable-snapshot-profile).

109. **Matched snapshot profile comparison.** Compare the same grid across two verified bundles, rank plans that meet the target on both, and replay their recommendations on matched cases. Report calibration, layout, circuit, and distribution differences alongside the decision. See [compare snapshot profiles on a common grid](CONSTRAINT_WORKFLOW.md#compare-snapshot-profiles-on-a-common-grid).

110. **Joint snapshot layout and budget planning.** Capture fixed-layout profiles on one named snapshot, rank layout-budget pairs against declared cases, and replay frozen budgets across matched held-out profiles. See [plan a starting layout and search budget together](CONSTRAINT_WORKFLOW.md#plan-a-starting-layout-and-search-budget-together).

111. **Round-probability sensitivity.** Rank layout-budget pairs under bounded round hit probabilities, report nominal and endpoint forecasts, and replay frozen selections on the saved distributions. See [check layout and budget sensitivity](CONSTRAINT_WORKFLOW.md#check-layout-and-budget-sensitivity-to-round-probabilities).

112. **Radius sweep and fixed-plan tolerance.** Compare robust recommendations across a bounded radius grid and bracket the probability tolerance of each fixed plan. See [compare several radii](CONSTRAINT_WORKFLOW.md#compare-several-radii-and-find-each-fixed-plans-tolerance).

113. **Dated calibration comparison.** Simulate a single verified circuit per round against two SHA-256-pinned archives of the same IBM device. Report target support, probabilities, readout channels, and signed differences. See [compare two dated IBM snapshots](CONSTRAINT_WORKFLOW.md#compare-two-dated-ibm-snapshots-on-the-same-circuits).

114. **Shared budgets across dated calibrations.** Rank fixed caps by success and worst expected work over the two archived models. See [plan fixed search budgets across the two dates](CONSTRAINT_WORKFLOW.md#plan-fixed-search-budgets-across-the-two-dates).

115. **Matched replay across dated calibrations.** Replay unique frozen recommendations on both archived models and a distinct-candidate classical reference under matched seed blocks. See [replay selected budgets with matched seeds](CONSTRAINT_WORKFLOW.md#replay-selected-budgets-with-matched-seeds).

116. **Multi-layout calibration study.** Capture only the rounds reachable under the budget grid and plan or replay completed layouts in one command. Verified on the original two-bit constraint and the four-bit diffuser smoke constraint. See [capture and plan a multi-layout study](CONSTRAINT_WORKFLOW.md#capture-and-plan-a-multi-layout-study-in-one-command).

117. **Shared calibration study preparation.** Verify and decode the pinned archive pair and audit the oracle once per study, then preflight every layout before compilation. See [capture and plan a multi-layout study](CONSTRAINT_WORKFLOW.md#capture-and-plan-a-multi-layout-study-in-one-command).

118. **Shared static calibration noise channels.** Derive channels once per dated target and matching active-wire union within a study, then build each layout's models with read-only gate channels and independent readout buffers. See [capture and plan a multi-layout study](CONSTRAINT_WORKFLOW.md#capture-and-plan-a-multi-layout-study-in-one-command).

119. **Subset projection for static calibration noise.** Project channels from a covering union on the same dated target, preserving directed physical gate arguments and the compact target's calibration checks. See [capture and plan a multi-layout study](CONSTRAINT_WORKFLOW.md#capture-and-plan-a-multi-layout-study-in-one-command).

120. **Verified compiled-round reuse across dated studies.** Reuse an exact compiled round across direct comparisons and studies in the same Python process. Verify both archive pins and rerun both density lanes on every request. See [reuse compiled rounds across dated studies](CONSTRAINT_WORKFLOW.md#reuse-compiled-rounds-across-dated-studies).

121. **Portable compiled rounds for dated studies.** Save completed physical circuits to a pinned local bundle, recheck their verification evidence in a separate process, and rerun both noise simulations. Extend a saved study to new round ranges while preserving the original bundle. See [carry compiled rounds into a separate process](CONSTRAINT_WORKFLOW.md#carry-compiled-rounds-into-a-separate-process).

122. **Portable compilation coverage preflight.** Inspect a pinned bundle without the SDK and check which keys cover the exact requested layout and round grid before running a dated study. See [check bundle coverage before a study](CONSTRAINT_WORKFLOW.md#check-bundle-coverage-before-a-study).

123. **Compare coverage across pinned compilation bundles.** Prepare the requested grid once, rank candidates by matched round cells, and retain ties and software incompatibilities in the report. See [compare bundle coverage for one study](CONSTRAINT_WORKFLOW.md#compare-bundle-coverage-for-one-study).

124. **Compose complementary compilation bundles.** Copy exact saved circuit rows into a new pinned bundle without the SDK. Deduplicate identical rows and report conflicting evidence before publication. See [compose complementary compilation bundles](CONSTRAINT_WORKFLOW.md#compose-complementary-compilation-bundles).

125. **Verify overlapping saved physical circuits.** Check QPY-only conflicts against one pinned request, compare decoded physical evidence, and retain the selected source's exact row. See [verify differing QPY rows before composition](CONSTRAINT_WORKFLOW.md#verify-differing-qpy-rows-before-composition).

126. **Verify portable overlap receipts.** Check pinned source and output bytes without the SDK, then optionally replay the recorded physical checks against the exact study request. See [verify or replay an overlap receipt](CONSTRAINT_WORKFLOW.md#verify-or-replay-an-overlap-receipt).

127. **Share preparation across receipt replay and study.** Run receipt verification and the dated study together, reusing the pinned preparation and verified winning programs while retaining both reports. See [replay a receipt and run the study together](CONSTRAINT_WORKFLOW.md#replay-a-receipt-and-run-the-study-together).

128. **Verify and profile the receipt-backed workflow on Manila.** Match eight programs across the 2021 and 2024 archives, check their modeled distributions against full-wire Aer, and measure fresh compilation and paired replay-and-study stages. See [profile a receipt-backed study on archived Manila](CONSTRAINT_WORKFLOW.md#profile-a-receipt-backed-study-on-archived-manila).

129. **Native OpenQASM for dated calibration.** Prefer the experimental importer through comparison, study, bundle coverage, and receipt replay. The pinned Manila and Jakarta studies preserve exact physical, probability, and budget evidence. See [prefer native OpenQASM for a dated calibration study](CONSTRAINT_WORKFLOW.md#prefer-native-openqasm-for-a-dated-calibration-study).

130. **Batch dated calibration density preparation.** Group up to three consecutive rounds on the same compact simulator and validate each Aer result before publishing a layout. Manila and Jakarta used eight jobs for 16 circuits with matching density and probability evidence. See [batch density preparation for dated calibration](CONSTRAINT_WORKFLOW.md#batch-density-preparation-for-dated-calibration).

131. **Profile wider dated density workers and prune unused gate noise.** Time worker assembly, serialization, controller execution, and formatting on six- and seven-wire Jakarta circuits. The dated path keeps only applicable stock gate channels where the compact simulation can prove their scope. See [profile density workers on wider Jakarta circuits](CONSTRAINT_WORKFLOW.md#profile-density-workers-on-wider-jakarta-circuits).

132. **Capture opt-in dated density fusion and audit budget decisions.** Record Aer fusion settings and observations by round and date. Compare matched, pinned captures using an explicit tolerance and keep the budget plan and seeded replay tied to the default reference. See [capture density fusion and check it against a pinned reference](CONSTRAINT_WORKFLOW.md#capture-density-fusion-and-check-it-against-a-pinned-reference).

133. **Run both dated density policies in a single study.** Reuse verified circuits and dated compact simulators, preserve the default plan through candidate failures, and audit matched results against an explicit tolerance. See [run both density policies in one study](CONSTRAINT_WORKFLOW.md#run-both-density-policies-in-one-study).

134. **Verify a saved paired study offline.** Bind both capture sets to the constraint and round request, verify coverage, and recompute the default plan, seeded replay, and complete fusion diagnostic. See [verify a saved paired calibration study offline](CONSTRAINT_WORKFLOW.md#verify-a-saved-paired-calibration-study-offline).

135. **Release paired simulator setup as each layout finishes.** Drop full programs after reference capture and release compact setup before planning, including after cancellation or error. Measure live objects and process memory and verify the resulting reports in fresh processes. See [release simulator setup and carry evidence across runs](CONSTRAINT_WORKFLOW.md#release-simulator-setup-and-carry-evidence-across-runs).

136. **Restart a paired study from a pinned reference checkpoint.** Save completed default captures and verified compiled rounds before fusion, validate their request and scope after restart, and rebuild the compact simulators to complete the comparison. See [restart a paired study from completed reference captures](CONSTRAINT_WORKFLOW.md#restart-a-paired-study-from-completed-reference-captures).

137. **Resume between completed reference layouts.** Save one immutable capture and compiled bundle per layout, verify an explicit generation chain after interruption, and continue the remaining references before fusion. See [resume between reference layouts](CONSTRAINT_WORKFLOW.md#resume-between-reference-layouts).

138. **Resume between published fusion attempts.** Bind immutable diagnostic outcomes to a complete reference tip, verify both chains before new jobs, skip saved attempts, and retain terminal cancellation and failure boundaries. Export the ordinary paired report for offline verification. See [resume between fusion attempts](CONSTRAINT_WORKFLOW.md#resume-between-fusion-attempts).

139. **Reuse fusion validation within one invocation.** Validate saved history once, retain explicit branch identity and cached-state integrity, and rehash every ancestor after the callback before publication. Verify exact generation/report parity and preserve standalone validation. See [reuse verified checkpoint context within one invocation](CONSTRAINT_WORKFLOW.md#reuse-verified-checkpoint-context-within-one-invocation).

140. **Exercise eight-layout continuation and verify full package contents.** Add a bounded fresh-process benchmark with exact report checks, resource observations and preserved failure evidence. Run the full eight-layout request on dated Jakarta archives in both copies. Build wheels from a verified source archive and compare every runtime file to prevent stale build-cache contents from entering an artifact. See [benchmark checkpoint continuation at eight layouts](CONSTRAINT_WORKFLOW.md#benchmark-checkpoint-continuation-at-eight-layouts) and [the package build procedure](CONTRIBUTING.md#build-and-check-release-artifacts).

141. **Retain restored programs only for eligible work.** Verify every saved physical round while keeping callback-owned programs only for the fusion chunk that can run now. Discard setup for complete, terminal, reference-only and reference-pause paths; preserve exact reports, pins and late-corruption rejection. See [retain only the current restoration chunk](CONSTRAINT_WORKFLOW.md#retain-only-the-current-restoration-chunk).

142. **Audit saved checkpoint benchmark evidence offline.** Check a pinned summary against the local stage files, immutable chains, available studies, logical operation bounds and recorded totals without the SDK. Keep audit success separate from the original run outcome and resource observations. See [verify saved checkpoint benchmark records](CONSTRAINT_WORKFLOW.md#verify-saved-checkpoint-benchmark-records).

143. **Keep controlled worker tests portable.** Scope the benchmark subprocess replacement to its own module, preserve independent host-metadata calls and exercise that separation explicitly on Windows and Linux. Inspect all run #9 matrix failures and retain the pre-fix reproduction. See [saved benchmark verification](CONSTRAINT_WORKFLOW.md#verify-saved-checkpoint-benchmark-records).

144. **Minimum-cost search and desktop Workbench.** Add bounded integer objectives, an independent exact minimum, budgeted Grover simulation, and an audited reversible cost-threshold circuit. Deliver a native Windows executable with interactive stage inspection, saved projects, generated replay code, and full export. See [the Workbench guide](WORKBENCH.md).

## Remaining evidence

- Extend regression coverage to additional supported simulator/OS combinations when a named environment is available. Keep checkpoint records and package artifacts bound to their exact revisions; local checks do not replace hosted verification or physical-device evidence.
- Run one authorized physical experiment with a named backend, record its actual job and measured results, and compare its hit rate and full cost with the local references.
- Keep hosted verification tied to the committed revision. [Run #10](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions/runs/43300577324) passed all ten jobs for the #143 CI repair at `33eab1190c31188c322951a78cc6b7f4e620c0fa`. Desktop #144 adds local Windows executable evidence. A managed organizational deployment requires its own signing, distribution, and accessibility validation.
