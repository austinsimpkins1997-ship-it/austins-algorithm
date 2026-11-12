# Austin’s Algorithm on IBM backend models

Review and local experiment date **2026-11-11**. The workload is the one-round Austin circuit for `q0 == 1 and q1 != q2`. It measures three search bits in `q2 q1 q0` order. A separate predicate verifies each returned bitstring. The direct classical solution sets the last bit and chooses the two assignments in which the other bits differ. It returns `011` and `101` deterministically.

## Which models to use

The calibration comparison command accepts a pair of pinned local IBM archives and uses one compiled program per Grover round for both. The Jakarta experiment uses the July 2021 and May 2024 calibration times recorded in the properties JSON. Their reported versions are 1.0.11 and 1.4.8. The same-device check preserves the version difference in the report. See the [matched-circuit workflow](CONSTRAINT_WORKFLOW.md#compare-two-dated-ibm-snapshots-on-the-same-circuits) and the [Qiskit fake-backend update](https://github.com/Qiskit/qiskit-ibm-runtime/pull/1710).

IBM’s fake backends provide versioned snapshots of gate sets, coupling maps, qubit properties, and errors. This package loads snapshots already distributed with the installed SDK. Historical calibration dates are part of the result because error rates and device availability can change. IBM’s SDK also provides a live refresh path, which requires account access. [IBM Fake Provider](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

Manila and Jakarta exercise small historical CX targets. Torino adds a 133-qubit Heron snapshot whose compiled two-qubit operations for this circuit are CZ. IBM documents separate Heron revisions and the grid topology of Nighthawk. For a future device run, select the actual backend and use its current target, version, calibration, and connectivity. Circuit resources can change with the layout, supported instructions, and compiler settings. [IBM processor families](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/processor-types) · [IBM backend details](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/qpu-information)

## Method and local results

Run `python -m austins_algorithm compare --ibm-model all --shots 1024 --seed 7` in an environment with the `ibm` extra. This experiment used Python 3.12.15, Qiskit 2.6.2, IBM Runtime 0.51.0, Aer 0.18.2, and the OpenQASM importer 0.6.0. Target transpilation used optimization level 1 and seed 7. Model construction, compilation, sampling, verification, and elapsed time are reported where measured.

| Condition | Calibration date in snapshot | Target width | Compiled depth | Compiled two-qubit gates | Verified shots |
|---|---|---:|---:|---:|---:|
| Ideal logical circuit | — | 3 | 11 | 2 CZ plus one three-qubit CCX | 1,024 / 1,024 |
| Synthetic 2% depolarization per logical gate | — | 3 | 11 | 2 CZ plus one three-qubit CCX | 795 / 1,024 |
| Manila snapshot, density matrix | 2024-05-27 | 5 | 33 | 14 CX | 777 / 1,024 |
| Jakarta snapshot, density matrix | 2024-05-27 | 7 | 33 | 14 CX | 814 / 1,024 |
| Torino snapshot, matrix product state | 2025-02-26 | 133 | 53 | 14 CZ | 922 / 1,024 |

The logical depth and gate counts describe the input circuit before target compilation. Three physical wires were active on each snapshot for this seed. The ideal distribution remained 50/50 over the two valid answers after compilation and after reimport of the portable OpenQASM. The export records how compact wires map to the target and how measurements map to classical bits. That check covers this circuit's initialized measurement distribution. The separate oracle audit covers the phase rule on each basis input. [Qiskit compiler](https://quantum.cloud.ibm.com/docs/en/api/qiskit/compiler) · [Qiskit OpenQASM 3](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3)

The synthetic run uses a user-set depolarization probability on logical gates. The snapshot runs use Aer’s backend-derived noise model, which uses available gate, relaxation, and readout properties. Torino uses a matrix product state to keep memory bounded on the wider target. Each row is one seeded local experiment. Calibration age, model approximations, finite-shot variation, and different routed circuits limit comparisons between snapshots. [IBM local testing mode](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/local-testing-mode) · [Aer noise model](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.NoiseModel.html) · [Aer simulator methods](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

The local snapshot command limits runs to 4,096 shots and seven active target wires. The Torino run used a maximum bond dimension of eight. Wilson 95% intervals for the three simulated shot proportions were 73.16–78.40% (Manila), 76.91–81.85% (Jakarta), and 88.05–91.73% (Torino). These intervals describe sampling variation within the chosen models. Device drift and model error are outside those intervals.

One ChronoRealm run recorded about 4 µs for the direct solve and verification, 15.4 ms for the combined ideal and synthetic comparison, and 0.30 s, 0.12 s, and 4.05 s for the Manila, Jakarta, and Torino stages. The snapshot timings include compilation, export verification, model setup, sampling, and result checking. The program records per-stage timings. These single-run local wall times depend on the host and warm-up state. QPU access, queue time, and billing are unmeasured.

## Search and stopping

The reusable search takes a phase oracle, the width of its search register, and a separate Boolean verifier. Additional wires start at zero and must return to zero after the oracle. The second problem searches four-bit integers in `[8, 16)` with remainder 1 modulo 4. The exhaustive audit checks all 16 states and finds `1001` and `1101`. The two-round ideal success probability is 0.9453125.

The direct oracle flips the phase when `q3=1`, `q1=0`, and `q0=1`. The fifth wire from the compute/uncompute reference is unnecessary because a controlled-controlled-Z gate can apply that phase on the search register. Both oracles passed the full basis audit. Their ideal output distributions agreed after separate OpenQASM round trips on the same line target, with total variation distance below `1e-12`.

| Target | Reference depth | Direct depth | Reference CX | Direct CX |
|---|---:|---:|---:|---:|
| Synthetic five-qubit line | 284 | 126 | 207 | 76 |
| Manila snapshot | 284 | 126 | 207 | 76 |
| Jakarta snapshot | 251 | 110 | 171 | 58 |

These are two-round compilation measurements with seed 7, optimization level 1, and Qiskit 2.6.2. Each row holds the target fixed. The line case uses about 63% fewer CX gates.

For an unknown marked count, the local controller samples a round count from a growing window, verifies each measured candidate, and applies shot and query budgets. With seed 7, it found `1101` in three shots and one logical oracle query. This is one seeded result. Budget exhaustion leaves existence unresolved. The separate exhaustive audit can establish an empty marked set for small inputs. [Boyer, Brassard, Høyer and Tapp, §4](https://arxiv.org/html/quant-ph/9605034#S4)

## Matched modulo-4 noise comparison

Measured locally on **2026-11-11** with the same software versions and IBM snapshots listed above. Each comparison holds the predicate, two Grover rounds, backend, optimization level 1, simulator method, shot budget, and compile and sample seeds fixed across the variants. The compiler can choose different physical wires. Both variants passed the full 16-input oracle audit and preserved the same ideal output distribution after compilation and OpenQASM reimport. Every simulated shot was checked against the independent modulo-4 predicate.

| Snapshot | Work qubit, verified out of 1,024 | Direct, verified out of 1,024 | Hit-rate change | Two-qubit gates, work qubit → direct |
|---|---:|---:|---:|---:|
| Manila | 207 (20.21%) | 459 (44.82%) | +24.61 pp | 207 → 76 CX |
| Jakarta | 248 (24.22%) | 536 (52.34%) | +28.13 pp | 171 → 58 CX |
| Torino | 463 (45.21%) | 721 (70.41%) | +25.20 pp | 207 → 76 CZ |

The table uses seed 7. A sensitivity run with seed 19 and 512 shots per variant also favored the direct circuit on Manila (100 → 227), Jakarta (154 → 287), and Torino (261 → 330). Torino's routed layouts changed at the second seed, and the improvement fell to 13.48 percentage points. These results measure the combined effect of the oracle implementation, compilation, and the snapshot noise on the routed circuit. Isolating the gate-count effect would require controlling physical layout as well. Wilson intervals in the report describe per-variant simulated shot variation. They exclude model uncertainty and device drift. The 30-second local wait limit can yield a partial comparison at higher shot counts.

Reproduce with `python -m austins_algorithm compare --problem mod4-range --ibm-model all --shots 1024 --seed 7`. The report records the raw counts, physical wire maps, circuit resources, and local stage timings.

## Round selection on IBM snapshots

The direct oracle was tested on **2026-11-11** for zero through three Grover rounds, with 1,024 shots per circuit per stage. The ideal probabilities are 12.50%, 78.13%, 94.53%, and 33.01%, respectively. For each snapshot the compiler first found the two-round baseline's starting placement, then reused that placement for all four candidates. Routing and final measurement wires could still differ. Each candidate was compiled and verified once after fixing the placement. Calibration used seed 7 and selected the highest verified count, with ties going to fewer rounds. Validation used seed 19 on the exact compiled selection and baseline. A selection of two rounds shares one validation batch.

| Snapshot | Calibration hits for 0, 1, 2, 3 rounds | Selected rounds | Validation hits, selected vs. two rounds | Two-qubit gates, selected vs. two rounds |
|---|---|---:|---|---|
| Manila | 123, 506, 459, 218 | 1 | 495 vs. 458 | 38 vs. 76 CX |
| Jakarta | 132, 542, 536, 231 | 1 | 574 vs. 544 | 29 vs. 58 CX |
| Torino | 135, 693, 721, 255 | 2 | 722, shared batch | 76 CZ, same circuit |

The one-round circuits halved two-qubit gate counts on Manila and Jakarta. The observed validation gains were 3.61 and 2.93 percentage points, respectively. Their individual Wilson intervals overlap. These results establish the compiler resource savings and record a seeded hit-rate comparison. A conclusion about hit-rate improvement or equivalence would need a prespecified margin or comparison method and additional evidence. All candidates in a stage use its common simulator seed for reproducibility. The different validation seed tests the selection on fresh shots from the same historical snapshot. It leaves model fidelity and hardware drift unmeasured. Jakarta's one- and two-round circuits used different final measurement maps despite their shared initial placement.

Reproduce with `python -m austins_algorithm compare --problem mod4-range --ibm-model all --round-sweep --shots 1024 --seed 7 --validation-seed 19 --transpiler-seed 7`. Each model uses at most four calibration batches and two validation batches. If a local result wait expires and cancellation cannot be confirmed, the sweep returns a partial report and launches no further jobs.

## Hit-rate floor across placements

On **2026-11-11**, the placement check evaluated the direct oracle at one and two rounds. Before sampling, the demonstration floor was set to 40% verified shots. Each snapshot used its seed-7 two-round compiler anchor and a topology-selected alternate. The alternate moves one logical wire to an unused physical wire, retains a connected four-wire footprint, and ranks candidates by distance, logical wire index, then physical wire index. Each circuit and placement received 512 shots at each of two fresh seeds, 29 and 43.

Shots are pooled only within a circuit and placement. There are 12 prespecified cells across the three snapshots. The check uses two-sided exact binomial intervals with a Bonferroni adjustment for 95% simultaneous coverage under the simulator shot model. A circuit meets the floor when its lower bound reaches 40% at both placements. If a cell's upper bound is below 40%, that cell is below target. An interval spanning 40% is inconclusive. The policy selects the qualifying round count with the fewest summed compiled two-qubit gates. [SciPy exact binomial intervals](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html)

| Snapshot | Initial placement | Verified shots of 1,024, one vs. two rounds | Cell decisions, one vs. two rounds | Two-qubit gates, one vs. two rounds |
|---|---|---:|---|---:|
| Manila | `[2,4,1,3]` | 470 vs. 439 | Meets / inconclusive | 38 vs. 76 CX |
| Manila | `[2,0,1,3]` | 423 vs. 374 | Inconclusive / inconclusive | 47 vs. 85 CX |
| Jakarta | `[0,3,2,1]` | 566 vs. 516 | Meets / meets | 29 vs. 58 CX |
| Jakarta | `[5,3,2,1]` | 456 vs. 418 | Meets / inconclusive | 44 vs. 82 CX |
| Torino | `[92,98,80,99]` | 685 vs. 716 | Meets / meets | 38 vs. 76 CZ |
| Torino | `[92,100,80,99]` | 666 vs. 691 | Meets / meets | 38 vs. 76 CZ |

Manila had no qualifying round count. Jakarta selected one round, with a weakest lower bound of 40.07%. Torino also selected one round for this floor. Its two-round runs had higher raw hit rates, while one round met the same requirement with half the two-qubit gates. The 40% floor is a demonstration criterion. These decisions apply to the tested placements and the historical snapshot model. They do not measure calibration drift or predict current physical hardware performance.

Reproduce with `python -m austins_algorithm compare --problem mod4-range --ibm-model all --placement-check --minimum-hit-rate 0.40 --shots 512 --evaluation-seeds 29 43`. The report includes raw batches, adjusted intervals, routed measurement maps, and compilation digests. An incomplete cell cannot qualify a circuit.

## Choose an OpenQASM importer for offline comparisons

The `ibm` install extra includes the standard OpenQASM importer. Add `--native-qasm` to any offline `compare --ibm-model` command to try Qiskit's experimental native importer. This covers Austin, both modulo-4 oracle variants, the modulo-4 round sweep, and the placement check. The Python runners accept the same opt-in through `prefer_native_qasm=True`. [Qiskit OpenQASM importer documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3#experimental-import-interface)

```powershell
python -m austins_algorithm compare --ibm-model all --shots 32 --seed 7 --native-qasm
python -m austins_algorithm compare --problem mod4-range --ibm-model all --shots 32 --seed 7 --native-qasm
python -m austins_algorithm compare --problem mod4-range --ibm-model all --round-sweep --shots 32 --seed 7 --validation-seed 19 --transpiler-seed 7 --native-qasm
python -m austins_algorithm compare --problem mod4-range --ibm-model all --placement-check --shots 32 --minimum-hit-rate 0.20 --evaluation-seeds 29 43 --transpiler-seed 7 --native-qasm
```

The preference applies to the verified compiles of sampled circuits. Each reported compilation records the actual importer and the native preference when requested. A native parse rejection falls back to the standard importer when available. The compiler checks the imported circuit's wire widths, measurement mapping, and ideal probabilities before the workflow samples the compiled circuit. The `compare` flag requires a bundled IBM snapshot selected through `--ibm-model`.

On 2026-11-11, both copies ran all four modes on Manila, Jakarta, and Torino with CPython 3.12.15, Qiskit 2.6.2, IBM Runtime 0.51.0, Aer 0.18.2, and the standard importer 0.6.0. Each mode ran once per importer and copy at 32 shots per batch. The Austin batch count includes the ideal reference. The placement floor of 0.20 was fixed for this check.

| Offline comparison | Compiler calls per importer and copy | Seeded batches per importer and copy |
| --- | ---: | ---: |
| Austin | 3 | 4 |
| Modulo-4 oracles | 6 | 6 |
| Modulo-4 round sweep | 15 | 18 |
| Modulo-4 placement check | 15 | 24 |

The original audit recorded 39 verified compiler calls per importer and copy, including six automatic layout compiles. The optimization below reduces that workload. Every call used the requested importer. Circuit hashes, measurement maps, ideal distributions, all 52 seeded histograms, and selection decisions matched after excluding importer fields and elapsed timings. The semantic report digests matched across both copies. These are local results under the recorded snapshots, seeds, and software. Physical execution remains unverified. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

## Select the starting layout without an export round trip

The round sweep and placement check use a normal seeded, level-1 Qiskit transpilation to discover the starting layout. They read the ordered initial indices with ancillas filtered. Each sampled circuit then compiles at the chosen explicit placement and passes the complete OpenQASM and ideal measurement verification before sampling. The importer option applies to those verified exports. [Qiskit initial layout API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.transpiler.TranspileLayout#initial_index_layout)

On 2026-11-11, both copies profiled the two-round direct modulo-4 circuit at transpiler seed 7 on the same Windows host with an Intel Core i5-13490F. They used the pinned stack above. Each path was warmed once per snapshot, then timed three times in rotating order. Backend creation and sampling were outside the timer. Each layout-only call returned the same initial mapping as the corresponding former full verification call.

| Snapshot | ChronoRealm median ms, former standard / former native / layout only | E: median ms, former standard / former native / layout only |
| --- | ---: | ---: |
| Manila | 374.56 / 13.67 / 4.54 | 354.98 / 13.75 / 4.31 |
| Jakarta | 329.65 / 14.07 / 4.55 | 320.68 / 12.57 / 4.31 |
| Torino | 829.81 / 24.33 / 8.83 | 729.96 / 20.22 / 7.37 |

Both copies also checked transpiler seed 23. Automatic and fixed-layout routing produced different export hashes on Manila and Torino even though they began at the same placement. The explicit sampled compiles preserve the intended circuit and comparison.

Each mode now performs 12 verified exports across the three snapshots, down from 15. The replays repeated the 32-shot runs above with both importers. All 18 round-sweep histograms and all 24 placement-check histograms matched their respective saved baselines, as did the sampled circuit hashes, ideal distributions, measurement maps, and decisions. The local profiles and replay summaries are retained under `.build/layout-anchor-study` in each copy. The measured times apply to the layout selection step for these circuits and versions.

## Profile repeated placement sampling

The placement check constructs one Aer simulator per snapshot and uses each verified circuit for both seed jobs. Aer serializes its noise model for every job. In one instrumented 32-shot run in each copy, Torino serialized 965 error records with median times of 0.483 s in ChronoRealm and 0.518 s on E:. Manila and Jakarta serialized 33 and 47 error records. These measurements describe individual runs. [Aer simulator documentation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

Both copies tested a noise model restricted to the active physical wires while keeping the verified physical circuit and original simulation method. The test used compiler seeds 7 and 23, simulator seeds 29 and 43, and 32 shots. Manila and Jakarta reproduced all 16 histograms per model and copy. Torino's MPS histograms differed in all 16 cases per copy. Exact seeded replay on the tested Torino circuits uses the full noise model. The local profiles and projection comparisons are saved under `.build/placement-prep-study`.

## Reuse the calibrated simulator between oracle variants

The modulo-4 oracle comparison now builds one full calibrated Aer simulator per snapshot and shares it across the two variants. Initialization waits for the first variant that fits the active-wire limit. Each variant has its own verified circuit and seeded job. When a timeout leaves cancellation unconfirmed, the report retains the attempted variant, identifies omitted variants or snapshots, and stops the comparison. Ideal and sampled comparison metrics require the corresponding pair of results. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

On 2026-11-11, both copies benchmarked 16 shots per variant with seed 23 and the native importer on the pinned Windows stack above. Each model had one warm-up followed by two alternating pairs. The control constructed a fresh simulator for each variant. The shared path constructed one. The timed interval covered the complete local comparison, and all paired compiled evidence, histograms, and hit-rate comparisons matched.

| Snapshot | ChronoRealm median seconds, fresh per variant / shared | E: median seconds, fresh per variant / shared |
| --- | ---: | ---: |
| Manila | 0.231 / 0.175 | 0.216 / 0.182 |
| Jakarta | 0.286 / 0.196 | 0.245 / 0.183 |
| Torino | 6.815 / 4.724 | 6.827 / 4.735 |

Both copies also matched their saved 32-shot, seed-7 reports on all three snapshots using each importer. For each parser, the workflow reproduced six histograms and built three simulators across the three snapshots. Local raw reports and timing samples are in `.build/mod4-reuse-study`. These observations apply to the bundled snapshots and recorded software and seeds. Physical execution remains unverified.

## Reuse verified circuits across placement checks

Use `PreparedMod4PlacementCheck` when several local requests share the same snapshot selection, compiler seed, and importer preference. The plan prepares the reached layouts and circuits on its first request. Later requests reuse those verified inputs and build fresh calibrated simulators. Each request can set its own hit-rate floor, shot count, and two evaluation seeds. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

```python
from austins_algorithm.snapshot_placement import PreparedMod4PlacementCheck

plan = PreparedMod4PlacementCheck(model="jakarta", transpiler_seed=7)
first = plan.run(minimum_hit_rate=0.20, shots=32)
second = plan.run(minimum_hit_rate=0.35, shots=16, evaluation_seeds=(31, 47))
```

Install the `ibm` extra to run this example. The one-shot CLI creates a fresh plan for each invocation. Calls on one reusable plan are serialized and return detached reports. An unconfirmed cancellation ends the request with partial evidence and makes that plan unavailable for later requests.

On 2026-11-11, both copies compared two requests across Manila, Jakarta, and Torino against the archived one-shot implementation. The requests used the settings in the example and the standard importer on the pinned Windows stack above. ChronoRealm ran the fresh arm first. E: ran the prepared arm first. Each copy performed one paired comparison. The table uses the workflow's recorded wall time, including preparation in the first request.

| Copy | First request seconds, fresh / prepared | Second request seconds, fresh / prepared | Two-request total seconds, fresh / prepared |
| --- | ---: | ---: | ---: |
| ChronoRealm | 17.65 / 19.00 | 19.04 / 13.98 | 36.69 / 32.99 |
| E: drive | 18.68 / 18.82 | 18.52 / 13.20 | 37.20 / 32.02 |

The first prepared request verified 12 circuits. The second reused them and built three fresh simulators. The 48 seeded histograms, circuit hashes, measurement maps, intervals, and decisions matched the fresh runs in each copy. Both copies also ran two prepared requests with the native importer and matched all 48 histograms against the saved native reference. Local reports and timing evidence are under `.build/prepared-placement-study`. These observations cover the named fixtures and versions.

## Save and verify a placement receipt

Add `--receipt-dir` to the local placement check, or pass `receipt_dir` to `PreparedMod4PlacementCheck.run`. The path must be new. The workflow reserves it before sampling and writes the receipt manifest after its evidence files are present.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
python -m austins_algorithm compare --problem mod4-range --ibm-model all `
  --placement-check --minimum-hit-rate 0.20 --shots 32 `
  --receipt-dir .build/placement-receipt
python -m austins_algorithm placement-receipt-verify .build/placement-receipt
```

The manifest binds the bytes of `request.json` and `report.json` by SHA-256. The report envelope contains the original placement report, canonical snapshot configuration and properties, the effective coupling map, and the exact OpenQASM text for each reached circuit. The whole folder can be moved. A receipt from an unconfirmed timeout retains the observed prefix and skipped snapshot list.

The verifier uses the standard library. It recomputes the calibration digest, checks the alternative placement against the recorded coupling map, and validates wire mappings and gate counts in the static OpenQASM emitted for these snapshots. It verifies the seed schedule and predicate counts, then recomputes the Bonferroni-adjusted Clopperâ€“Pearson intervals. Interval endpoints must agree within `2e-11` absolute error. The validated endpoints drive the original floor comparisons, including equality at the lower endpoint. Round selection uses the verified two-qubit gate totals and the original fewer-rounds tie break. [NIST exact binomial confidence limits](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm)

A successful check establishes consistency of the supplied artifacts and selection. The recorded ideal probability evidence comes from the original verified compiler run. Receipt provenance depends on the source that supplied the bundle.

On 2026-11-11, ChronoRealm captured an all-snapshot receipt with the native importer and E: captured one with the standard importer. Both verified with site packages disabled, checked 12 exports and 768 recorded shots, and selected one round on all three snapshots. Both relocated receipts verified, and a changed selection failed even with recomputed manifest hashes. An injected timeout test verified that receipt capture on a reused plan includes only the reached circuits. Local evidence is in `.build/placement-receipt-study`.

## Compare two placement receipts

Use the offline comparison to check two saved runs together. The command independently verifies each receipt before it compares any results. It aligns reached cells by snapshot, baseline or alternate placement label, and round count. Within each cell it pairs trials by the numeric seed. Both sides' physical wire maps are visible when a placement label refers to a different circuit.

```powershell
python -m austins_algorithm placement-receipt-compare .build/placement-baseline .build/placement-candidate
```

The result shows changes in calibration, effective coupling, physical layouts, circuit hashes, shots, and recorded software. Importer provenance appears alongside the compiled circuit. The requested seed order is retained, and a seed shared by the two schedules is paired by value. Hit-rate floor and confidence changes appear with the decision context.

For two completed trials, the report shows both histograms and whether the raw counts match. Matching recorded sampling conditions allow a signed per-bitstring difference, calculated as right minus left. Unpaired seeds, unattempted trials, timeouts, and compile-only results have explicit states. The report includes eligible and selected rounds for each snapshot, with a decision change evaluated when both snapshot runs completed. These comparisons describe observed differences under the recorded conditions.

On 2026-11-11, both copies exercised the following cases with site packages disabled during comparison.

| Case | Verified outcome |
| --- | --- |
| Native and standard all-snapshot receipts | All 12 circuits and 24 histograms matched. Importer records differed. |
| Manila seeds 29/43 and 29/47 | All four seed-29 histograms matched. Each receipt had four trials under its exclusive seed. |
| Torino compiler seeds 7 and 23 | All four paired export hashes and eight histograms differed. The calibration, starting layouts, and selected round matched. |
| Manila 32-shot and 4-shot trials | Eight completed pairs had different shot budgets. The model scope and confidence family also differed. |
| Synthetic calibration marker and timeout cases | The comparison surfaced changed calibration despite identical counts, and distinguished a timed-out trial from planned work that was never attempted. |

The saved results are under `.build/placement-receipt-compare-study` in each copy. A test with independently recomputed counts also checked signed histogram differences and a changed selection. Both inputs must pass receipt verification, including when someone has recomputed the file checksums after altering the evidence.

## Replay a placement receipt

Use the optional IBM dependencies to rebuild a verified receipt against the installed fake backends. The preflight checks every recorded snapshot and reached circuit before any noisy simulator is constructed. It compares the calibration fingerprint, directed effective coupling, anchor layout, exact OpenQASM bytes, physical wire maps, and compiled resources. The sampling phase uses those same compiled objects.

```powershell
python -m austins_algorithm placement-receipt-replay .build/placement-baseline --check-only
python -m austins_algorithm placement-receipt-replay .build/placement-baseline
```

The first command reports verified identities with zero samples. The second reuses the original shots and seeds for each completed reference trial, compares per-seed histograms, and recomputes the interval evaluations and placement selections. Both commands read the receipt in place and print JSON to standard output. Identity mismatches exit before sampling. The report records both software environments and any importer differences. A complete replay under changed recorded sampling or decision software cannot qualify as an exact match, even when the counts agree.

Replay of a partial reference covers only its completed trials. Historical timeouts, compile-only results, and unattempted seeds stay visible. An unconfirmed cancellation in the fresh replay stops the remaining submissions. The CLI exits with status 1 for incomplete or differing results or for matches under changed sampling or decision software. The result describes the installed local simulation and exact recorded conditions. Physical execution and cross-host reproducibility remain unverified.

Python callers can use `austins_algorithm.placement_replay.replay_placement_receipt(directory, check_only=False)`. The SDK-free verifier and comparator still work without simulator dependencies.

### Packaged Manila placement smoke check

The source distribution includes `examples/placement-smoke-regression/baseline` with the request, the report, and their byte fingerprints. The baseline uses Manila, the standard OpenQASM importer, compiler seed 7, evaluation seeds 29 and 43, four shots per trial, and a 20% demonstration floor. Its four circuits use 32 total shots. Keep the three JSON files together with LF line endings so their checksums survive a Windows checkout.

The [Windows smoke workflow](CONSTRAINT_WORKFLOW.md#run-the-manila-smoke-regressions-in-the-windows-workflow) installs the built source distribution using the shared smoke requirements. The placement step requires a completed offline verification with 32 recorded shots, four rebuilt exports with zero samples in check-only mode, and eight exact histogram matches in the full replay. Each phase writes separate JSON, stderr, and exit-code files to `manila-placement-smoke-evidence` for upload. The fixture measures local simulation reproducibility under the recorded toolchain. Hosted Windows execution and artifact upload require a separate observation.

### Manila smoke environment evidence

Each Windows smoke run captures `environment.json` after installing the packaged source distribution. The record identifies that archive and the packaged requirements by byte count and SHA-256. It lists the installed distribution names and versions, compares each exact pin with its installed version, and fingerprints the sorted name/version mapping. Use the dependency fingerprint to spot inventory changes and the package list to identify the changed versions.

The record includes the Python implementation, compiler and bitness, the Windows build and architecture, uv and PowerShell versions, and available GitHub runner and image identifiers. Runner fields are null when the expected values are unavailable. The capture reads only those runner fields and distribution names and versions. A pin mismatch leaves a structured report and a failing capture step. The four replay lanes can still produce evidence after a capture failure when installation succeeded.

`manila-smoke-environment` is a separate artifact beside the replay results. The installed inventory identifies observed metadata. It does not establish exact wheel bytes, a reproducible reinstall, or physical-device behavior. Compare archive fingerprints only when checking the exact built artifact. Independent archives can contain identical source files with different archive metadata. [GitHub runner variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables) · [GitHub runner image metadata](https://github.com/actions/runner-images/discussions/7661)

### Compare saved smoke environments

```powershell
python -m austins_algorithm smoke-environment-compare baseline/environment.json candidate/environment.json
```

The command reads two version 1 reports and checks their shape, dependency fingerprints, and pin outcomes. It lists added, removed, and changed distribution versions and pin records. It also shows changed input fingerprints and recorded Python, host, tool, and runner fields. Observation times, run IDs, and commit identifiers are reported separately so a new capture alone does not count as an environment change. An unavailable field is reported for the side that lacks it.

The comparison exits successfully for any two valid reports, including a pin mismatch or observed drift. Invalid evidence produces a failing exit and identifies the affected side. The comparator needs only the two JSON files and the standard library. It does not open the recorded input artifacts. A different source archive fingerprint can reflect packaging metadata, and matching dependency versions do not establish matching installed binaries.

### Verify a Manila smoke run index

The Windows smoke job writes `run-index.json` after the five evidence upload steps when checkout succeeded and the run is still active. The index preserves the workflow step outcomes, available upload artifact IDs, individual command exit codes, reported phase statuses, and byte counts and SHA-256 hashes for the known evidence paths. These observations are separate. A failed step can have valid evidence, and an upload step can report success without returning an artifact ID. Later phases with no output are recorded as unobserved. [GitHub step outcomes](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#steps-context) [Pinned upload action](https://raw.githubusercontent.com/actions/upload-artifact/ea165f8d65b6e75b540449e92b4886f43607fa02/action.yml)

Extract each downloaded bundle under a directory named for its artifact, such as `downloads/manila-placement-smoke-evidence`. Pass the index from the separate `manila-smoke-run-index` artifact and the parent of the extracted bundles. For a local runner tree, use `--runner-layout` to use the original directory names.

```powershell
python -m austins_algorithm smoke-run-index-verify run-index.json downloads
python -m austins_algorithm smoke-run-index-verify run-index.json .build/local-run --runner-layout
python -m austins_algorithm smoke-run-index .build/local-run .build/local-run-index.json
```

Verification checks the supplied files and rederives phase observations from their indexed exit and status files. Unprovided bundles and unreadable evidence produce partial verification. A missing expected file in a supplied bundle, a changed fingerprint, or a changed phase observation fails verification. Both partial and mismatched verification return exit code 1 and a JSON report. A verified index can describe a failed or incomplete smoke run because verification concerns the evidence that was recorded. The index covers the known workflow outputs and does not authenticate the supplied index, establish current GitHub artifact availability, or verify the simulation independently.

### Compare two Manila smoke run indexes

```powershell
python -m austins_algorithm smoke-run-index-compare baseline/run-index.json baseline candidate/run-index.json candidate
```

Pass each index and the parent directory of its extracted evidence bundles. Use `--left-runner-layout` or `--right-runner-layout` on either side that uses the original runner directory names. The command verifies each side locally and reports changes in recorded step outcomes, upload metadata, and run identifiers. It compares phase exit codes and statuses where each side's evidence is available. A known transition from an observed result to an unrecorded phase appears separately from a phase whose evidence was withheld or unreadable.

The report lists fingerprint changes separately from phase changes. Timing or local paths in a report can change its bytes without changing the replay outcome. The environment comparison uses the same bytes the verifier hashed. It checks each report's schema and pin results and reports conflicts between the index and environment run identifiers before comparing dependency and runner details.

A fully comparable pair exits 0, including runs with failed replay phases. Partial comparisons return exit 1 with the independently verified observations. A file or phase verification mismatch returns exit 1 and withholds the comparison deltas. The report describes the supplied evidence and does not establish the cause of a replay change or authenticate the original run.

## Physical validation and cost

Physical measurements and provider charges are still unknown. The physical command requires a saved account, named backend, service instance, shots, and a maximum QPU execution time. The provider path uses a single explicitly selected job and checks the returned backend and measurement counts. Record the target version, transpilation settings, raw counts, verified hit rate, actual completed shots, job ID, and reported usage. A local wait deadline and provider execution cap measure different things. Provider pricing, quota, and billing come from the account's plan. [IBM Sampler API](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/executor-sampler-sampler) · [IBM workload usage](https://quantum.cloud.ibm.com/docs/en/guides/estimate-job-run-time) · [IBM job monitoring](https://quantum.cloud.ibm.com/docs/en/guides/monitor-job)

For an end-to-end comparison, match the requested answer and correctness target. Time classical solution and verification separately from circuit construction, compilation, submission, waiting, provider QPU usage, and result verification. The direct solver here is sufficient for the tiny predicate. A larger benchmark needs a named workload, its strongest applicable classical method, and data-loading and oracle-construction costs. [IBM Grover workflow](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm)
