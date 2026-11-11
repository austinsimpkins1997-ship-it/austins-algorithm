# Austin’s Algorithm on IBM backend models

Review and local experiment date **2026-11-11**. The workload is the one-round Austin circuit for `q0 == 1 and q1 != q2`. It measures three search bits in `q2 q1 q0` order. A separate predicate verifies each returned bitstring. The direct classical solution sets the last bit and chooses the two assignments in which the other bits differ. It returns `011` and `101` deterministically.

## Which models to use

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

## Physical validation and cost

Physical measurements and provider charges are still unknown. The physical command requires a saved account, named backend, service instance, shots, and a maximum QPU execution time. The provider path uses a single explicitly selected job and checks the returned backend and measurement counts. Record the target version, transpilation settings, raw counts, verified hit rate, actual completed shots, job ID, and reported usage. A local wait deadline and provider execution cap measure different things. Provider pricing, quota, and billing come from the account's plan. [IBM Sampler API](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/executor-sampler-sampler) · [IBM workload usage](https://quantum.cloud.ibm.com/docs/en/guides/estimate-job-run-time) · [IBM job monitoring](https://quantum.cloud.ibm.com/docs/en/guides/monitor-job)

For an end-to-end comparison, match the requested answer and correctness target. Time classical solution and verification separately from circuit construction, compilation, submission, waiting, provider QPU usage, and result verification. The direct solver here is sufficient for the tiny predicate. A larger benchmark needs a named workload, its strongest applicable classical method, and data-loading and oracle-construction costs. [IBM Grover workflow](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm)
