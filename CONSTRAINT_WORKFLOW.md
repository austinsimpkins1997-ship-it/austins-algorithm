# Search a Boolean constraint

The `constraint` command reads a small conjunctive normal form (CNF) formula, builds a reversible phase oracle, and audits it against an independent clause interpreter. Each clause is an OR of signed literals. All clauses must hold. Literal `1` means `q0=1`, literal `-1` means `q0=0`, and the numbering continues upward. Displayed answers put `q0` at the right.

```json
{"schema_version": 1, "qubits": 2, "clauses": [[1, -2], [-1, 2]]}
```

This formula requires the two bits to agree. An empty clause is false. A formula with no clauses is true. Input accepts 1 to 8 search bits, up to 12 clauses, up to 16 literals per clause, and up to 16 KiB of UTF-8 JSON. The generated circuit must fit the 12-wire search limit. Repeated literals and clauses are allowed. Both synthesis paths omit duplicates and tautologies, apply the phase, and restore all work wires. The report hashes the validated ordered formula. JSON whitespace and object key order leave that hash unchanged.

- `--synthesis auto` uses factorization when there are at least two independent components of up to four variables each and the resulting circuit uses fewer work wires. The report records the method selected and the local analysis.
- `--synthesis clause-flags` uses unit clauses as controls and one flag per distinct non-unit clause.
- `--synthesis unit-reduced` applies root unit propagation before allocating clause flags. The circuit retains every forced bit as a phase control on its original wire.
- `--synthesis unit-factorized` applies the same reduction, then factorizes the remaining formula. It retains forced bits as controls and rejects a residual component above the four-variable factorization limit.
- `--synthesis unit-factorized-complement` compares the direct phase cover with a cover of failing assignments for the final multi-cube component. It keeps the direct cover on a tie.
- `--synthesis factorized` explicitly requests bounded component synthesis, including for a single component. The compiler enumerates each local truth table and merges compatible assignments into disjoint partial assignments. Components with one partial assignment become direct controls. Of the components with multiple partial assignments, all but the last use a flag. The last applies the phase directly. An oversized component can prevent this method from compiling.

The exhaustive audit checks phase and restored work wires for every small input. When it proves the marked set empty, the report returns `PROVEN_EMPTY`. The sampler receives the audited solution count in known-count mode. Unknown-count mode withholds it from the scheduler. If the sampler reaches a budget on a nonempty problem, the report returns `BUDGET_EXHAUSTED`. The audit and classical reference are separate from the simulator search timing.

## Solve the constraint with standard-library Python

Use `constraint-classical` to get the smallest unsigned witness in the same bit order. It uses unit propagation by default. The solver applies forced bits, prunes contradictory branches, and tries zero first at the highest unresolved bit. Free bits are zero. It checks the resulting witness with the independent clause interpreter. If all branches are contradictory, the command returns `PROVEN_EMPTY`. [Davis, Logemann, and Loveland’s original paper](https://cs.uwaterloo.ca/~david/cl/original-dpll.pdf)

With the package installed, run either method against the same formula.

```powershell
python -m austins_algorithm constraint-classical examples/field_sensor_kit.json
python -m austins_algorithm constraint-classical examples/field_sensor_kit.json --method scan
```

For a reversible-oracle run, add `--classical-method propagation` to `constraint`. The report compares the selected classical reference with the first marked state in the exhaustive oracle audit. The scan is the default reference for that command.

### Classical first-witness benchmark

Run `python examples/benchmark_classical_constraints.py` from the package directory. The benchmark checks the expected witness and scan count for each of five fixed formulas. It times full solver calls on preloaded formulas, including propagation setup, verification, and report construction. Both methods warm up with 64 calls, then alternate order across nine batches of 64 calls each. The table reports the median microseconds per call from the ChronoRealm package on Windows AMD64 with CPython 3.12.15.

| Fixture | Lowest witness | Scan assignments | Propagation nodes | Scan (µs) | Propagation (µs) |
|---|---|---:|---:|---:|---:|
| Immediate witness | `00000000` | 1 | 3 | 2.11 | 4.80 |
| Field sensor kit | `001010` | 11 | 3 | 24.29 | 11.08 |
| Forced chain | `11111111` | 256 | 1 | 399.80 | 14.41 |
| Late witness | `10000010` | 131 | 4 | 185.40 | 6.66 |
| Contradictory triples | Unsatisfiable | 256 | 7 | 660.44 | 8.37 |

The scan was faster when the first candidate satisfied the formula. Propagation was faster on the other four fixtures in this run. A scan assignment is a complete candidate. A propagation node is a partial search state, including the root. The benchmark reports forced assignments and pruned conflicts separately. The field kit’s direct constructor also provides a workload-specific first-witness baseline.

## Reduce the oracle with forced literals

The synthetic [site interlock](examples/site_interlock.json) connects six bits. Site approval requires certified power. Certified power excludes maintenance mode. Either maintenance mode or an uplink must be available. Maintenance mode requires a diagnostic buffer.

Unit propagation forces literals `1, 2, -3`, leaving the uplink clause `(4, 5)`. The diagnostic buffer bit is free because maintenance mode is off. Keeping the three forced literals as singleton clauses preserves the phase on inputs that violate those conditions. If root propagation proves a contradiction, the circuit is the identity and the exhaustive audit checks that the original formula has no marked states.

```powershell
python -m austins_algorithm constraint examples/site_interlock.json --strategy known --synthesis unit-reduced --compile --ibm-model jakarta --snapshot-shots 128
```

The report includes the forced signed literals and the residual clause count. It hashes and audits the original formula. On 2026-11-11, both synthesis paths marked exactly six of 64 inputs and restored their work wires on every input.

### Shared-target interlock results

Both two-round circuits used the same synthetic ten-wire line target, compiler seed 7, and Qiskit 2.6.2. The two exported ideal measurement distributions agreed to about `8.52e-16` total variation distance.

| Synthesis | Work qubits | Active target wires | Compiled depth | CX gates |
|---|---:|---:|---:|---:|
| Clause flags | 4 | 10 | 714 | 650 |
| Unit reduced | 1 | 7 | 462 | 358 |

To reproduce the shared-target comparison in Python, use:

```python
from austins_algorithm.compilation import compile_and_verify, line_target
from austins_algorithm.constraint_oracle import synthesize_constraint_oracle
from austins_algorithm.constraints import load_constraint
from austins_algorithm.grover_search import audit_phase_oracle, build_grover_search_circuit

constraint = load_constraint("examples/site_interlock.json")
target = line_target(10, seed=7)
for method in ("clause-flags", "unit-reduced"):
    oracle, _ = synthesize_constraint_oracle(constraint, synthesis=method)
    audit_phase_oracle(oracle, constraint.qubits, constraint.matches)
    circuit = build_grover_search_circuit(oracle, constraint.qubits, iterations=2)
    report = compile_and_verify(circuit, target=target, seed=7).report
    print(method, report["compiled"])
```

The reduced circuit also ran within seven active wires on the offline Jakarta and Torino snapshots with Aer 0.18.2 and Qiskit Runtime 0.51.0. Jakarta compiled 295 CX gates and verified 16 of 128 shots. Torino compiled 295 CZ gates and verified 11 of 64 shots. Both used seed 7. The ten-wire clause-flag circuit exceeded Jakarta’s target width, so the noisy runs verify the reduced circuit’s execution only.

### Factorization after unit reduction

Use the same `--line-width`, seed, strategy, and diffuser to compare compilations. This keeps the line target fixed when the input circuits have different logical widths. The reports retain the original constraint digest, the forced-literal record, and the full oracle audit. The factorized report also identifies its bounded components.

```powershell
python -m austins_algorithm constraint examples/site_interlock.json --strategy known --synthesis unit-reduced --compile --line-width 10
python -m austins_algorithm constraint examples/site_interlock.json --strategy known --synthesis unit-factorized --compile --line-width 10
```

These 2026-11-11 runs used two Grover rounds with the standard diffuser, compiler seed 7, and Qiskit 2.6.2. The 64-input audits preserved all six interlock witnesses and all nine sensor kit witnesses. The exported ideal distributions agreed within `1.05e-14` total variation distance for each line comparison.

| Workload and shared target | Active wires, flags → factorized | Depth, flags → factorized | Native two-qubit gates, flags → factorized |
|---|---:|---:|---:|
| Site interlock, ten-wire line | 7 → 6 | 462 → 743 | 358 → 546 CX |
| Site interlock, Jakarta snapshot | 7 → 6 | 418 → 672 | 295 → 477 CX |
| Site interlock, Torino snapshot | 7 → 6 | 956 → 1361 | 295 → 483 CZ |
| Sensor kit, twelve-wire line | 12 → 7 | 828 → 718 | 826 → 504 CX |

For the interlock, the factored circuit meets a six-active-wire budget. The seven-wire circuit uses fewer two-qubit gates on each tested target. The six-wire circuit uses the standard diffuser because clean-ancilla diffusion needs an existing work qubit. The sensor kit has no root-forced bits. Its two independent components are what reduce width and gate count.

The interlock also completed seeded local sampling on both IBM snapshots. Jakarta verified 16 of 128 shots with flags and 15 of 128 with factorization. Torino verified 11 of 64 with flags and 16 of 64 with factorization. Each variant used its own seed-7 automatic layout, so these counts describe the two routed circuits under each offline model.

### Phase cover using failing assignments

Let `P` denote all fixed controls and computed component flags, and `f` the final component's condition. The phase exponent `P AND f` equals `P XOR (P AND NOT f)`. The alternate circuit applies a phase under `P`, then cancels it for each disjoint failing cube under the same controls. An empty prefix contributes an exact global π phase. Earlier flags are restored by reversing their computation.

The selector sorts the nonzero phase term widths from largest to smallest. It chooses the alternate when the first differing width is smaller, or when its list ends earlier after an equal prefix. The report records both lists and the chosen form. This is a structural estimate. Native gate costs depend on compilation.

```powershell
python -m austins_algorithm constraint examples/site_interlock.json --strategy known --synthesis unit-factorized-complement --compile --line-width 10
```

In the 2026-11-11 run, the interlock's phase widths changed from `[5, 4]` to `[5, 3]`. Each version preserved all six marked inputs in the exhaustive 64-input audit. The table uses two standard Grover rounds and compiler seed 7 with Qiskit 2.6.2.

| Target | Active wires | Depth, direct → complement | Two-qubit gates, direct → complement |
|---|---:|---:|---:|
| Shared ten-wire line | 6 | 743 → 687 | 546 → 496 CX |
| Jakarta snapshot | 6 | 672 → 653 | 477 → 440 CX |
| Torino snapshot | 6 | 1361 → 1267 | 483 → 451 CZ |

The line OpenQASM distributions agreed to about `7.96e-16` total variation distance. The sensor kit kept its direct cover and produced a byte-identical OpenQASM export on the twelve-wire line. Exact regression checks include the global phase and restored work flags.

The interlock's offline Jakarta run verified 15 of 128 shots with the direct cover and 9 of 128 with the complement. Torino verified 16 of 64 and 17 of 64 respectively. The variants used seed-7 automatic layouts. These are descriptive counts for the compiled snapshot circuits.

### Select a cover by compiled resources

Use `constraint-cover` to compile the direct cover and the complement cover on a synthetic line or an offline IBM snapshot target. It applies unit reduction once and audits each available oracle against the original formula. Both candidates use the same standard diffuser, Grover round count, and compiler seed. Their full ideal distributions must agree after OpenQASM reimport.

```powershell
python -m austins_algorithm constraint-cover examples/site_interlock.json --line-width 10 --max-active-qubits 6 --qasm-output interlock-selected.qasm
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99
```

The report applies the requested cap to the number of active routed wires. It ranks eligible candidates by fewest native two-qubit gates, then smaller depth, fewer active wires, and finally the direct cover. With no alternative terminal cover, the report gives `DIRECT_ONLY` if the direct circuit fits. If all candidates exceed the active-wire cap, it reports `NO_COVER_FITS` and leaves the export path unused. The command writes the exact verified OpenQASM from the selected candidate using an exclusive file create.

With a shared `--initial-layout`, each candidate starts with the same logical-to-physical map. When Qiskit chooses layouts separately, the reported costs include each candidate's routing. The report preserves both maps.

The 2026-11-11 checks used Qiskit 2.6.2, seed 7, and two rounds. All five comparisons passed the 64-input audits and agreed within `2.86e-15` total variation distance. Each row reports direct and forced-complement costs.

| Workload and target | Layout | Active wires | Direct | Complement | Selected |
|---|---|---:|---:|---:|---|
| Interlock, ten-wire line | Automatic | 6 | 546 CX | 496 CX | Complement |
| Interlock, Jakarta snapshot | Automatic | 6 | 477 CX | 440 CX | Complement |
| Sensor kit, twelve-wire line | Automatic | 7 | 504 CX | 516 CX | Direct |
| Sensor kit, Torino snapshot | Automatic | 7 | 459 CZ | 450 CZ | Complement |
| Sensor kit, Torino snapshot | Shared starting layout | 7 | 459 CZ | 474 CZ | Direct |

These commands measure compilation resources. Add sampling explicitly for a local noisy comparison.

### Sample both verified covers on a shared layout

Use `--snapshot-shots` with a named `--ibm-model` and a shared `--initial-layout`. Both covers keep their exact compiled physical circuits, including the routed measurement map. The same backend instance supplies the noise model. Each cover runs two equal-budget batches of up to 256 shots. Supply four distinct evaluation seeds in direct, complement, direct, complement order.

```powershell
python -m austins_algorithm constraint-cover examples/site_interlock.json --ibm-model jakarta --max-active-qubits 6 --initial-layout 1 2 0 6 3 5 --snapshot-shots 128 --evaluation-seeds 211 223 227 229
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 191 193 197 199
```

The report keeps the compilation selection and the observed hit leader as separate fields. Counts are verified against the original formula. Each sampling record identifies its verified compilation by the portable OpenQASM digest. When requested, the export still contains the gate-count winner's verified OpenQASM. The two exact 97.5% rate intervals give a conservative 95% bound for their difference under the fixed-model binomial shot assumption. This bound describes sampling variability in the snapshot simulator.

Both covers must fit the caller's cap and the seven-active-wire simulator limit to run a matched comparison. The snapshot reports `NO_ALTERNATE_COVER` when there is only a direct circuit and `COMPILE_ONLY_ACTIVE_QUBIT_LIMIT` when either cover exceeds the cap. A sampling interruption keeps completed batches. An unconfirmed cancellation stops further sampling.

#### Shared-layout snapshot results

On 2026-11-11, both fixtures used two standard Grover rounds and compiler seed 7. The runs used Qiskit 2.6.2, Aer 0.18.2, Runtime 0.51.0, and SciPy 1.18.1. Each cover completed two 128-shot batches.

| Workload and snapshot | Active wires | Native gates, direct / complement | Verified hits out of 256, direct / complement | Complement minus direct, percentage points |
|---|---:|---:|---:|---:|
| Interlock, Jakarta (2024-05-27) | 6 | 492 / 443 CX | 24 / 27 | +1.2 [−7.6, +9.9] |
| Sensor kit, Torino (2025-02-26) | 7 | 459 / 474 CZ | 62 / 57 | −2.0 [−14.1, +10.2] |

The gate-count winner also led the observed hits in each run. Both difference bounds include zero. Each pair shared its starting layout and preserved its own routed measurement map. The five-wire interlock cap returned a compile-only sampling status, with zero submitted local batches. A replay with the same seeds checks reproducibility.

#### Choose the snapshot simulation method

`--snapshot-method density_matrix` runs a density-matrix simulation using the selected backend’s physical-index noise model. The default method follows the snapshot, including matrix product state on Torino. Both methods pass the same verified physical-index circuit to Aer. With truncation enabled, the report checks that Aer used the exact routed active-wire set and keeps the compiled classical-bit map. The seven-active-wire, 128 MiB, one-thread, and per-job timeout limits still apply. [Aer simulation methods and truncation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html) · [Aer backend noise models](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.NoiseModel.html)

```powershell
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 1301 1303 1307 1319 --snapshot-method density_matrix
```

Use `--snapshot-method matrix_product_state` with the same arguments to repeat the reference timing. The report records the actual method. The two simulation algorithms can produce different seeded counts from the same circuit and noise model. Compare the shot estimates with their sampling uncertainty.

The 2026-11-11 comparison used the seven active Torino wires `80, 81, 82, 92, 98, 99, 100`, Qiskit 2.6.2, Aer 0.18.2, Runtime 0.51.0, and the same compiled circuits for both methods. The physical noise tables agreed after excluding generated error IDs. An independent check rebuilt the model on compact indices and verified each used directed gate channel and readout matrix. The full analytical distributions agreed before and after readout for both covers. Both representations had the same raw probability mass drift below `4.7e-7` per cover, and the check normalized them before comparing. [Aer probability snapshots](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.save_probabilities.html)

The modeled verified hit rates were 24.633% for direct and 23.793% for complement. Each observed rate interval included its modeled rate. The full sampled distributions are compared with the model by total variation distance (TVD).

| Method | Direct hits / 256 | Complement hits / 256 | TVD from model, direct / complement | Four-batch sampling (s) | Full process wall (s) |
|---|---:|---:|---:|---:|---:|
| Matrix product state | 52 | 71 | 0.264 / 0.247 | 58.16 | 68.34 |
| Density matrix | 62 | 59 | 0.215 / 0.191 | 6.81 | 16.44 |

The timing comparison is one local run with two 128-shot batches per cover. The full process includes setup, compilation, and output. The sampled histograms carry finite-shot variation. The analytical check tests the noise representation for these routed circuits.



### Plan and check a verified witness budget

Add `--witness-probability` to a shared-layout snapshot comparison. The target is strictly between zero and one. The planner takes each cover’s lower calibrated hit-rate bound, `p`, and finds the smallest integer batch whose probability `1 - (1 - p)^n` reaches the target under the fixed-model independent-shot assumption. It compares the supplied rates as exact fractions. Displayed probabilities are rounded to floating point.

The calibration interval’s confidence and the target probability answer different questions. A zero lower bound gives no positive reliability estimate. A cap that cannot reach the target produces `BUDGET_EXCEEDED` for that cover. The command runs fresh batches for each feasible cover, using the exact already compiled circuit. The default is one seed per cover. All validation seeds must differ from each other and from the four calibration seeds. A completed fresh batch records its verified witness, if present. The grouped counts do not contain the order of individual shots.

```powershell
python -m austins_algorithm constraint-cover examples/site_interlock.json --ibm-model jakarta --max-active-qubits 6 --initial-layout 1 2 0 6 3 5 --snapshot-shots 128 --evaluation-seeds 211 223 227 229 --witness-probability 0.95 --witness-max-shots 256 --witness-seeds 241 251
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 191 193 197 199 --witness-probability 0.95 --witness-max-shots 256 --witness-seeds 233 239
```

The report includes the exact classical propagation result on the same formula and checks it against the exhaustive oracle audit. It measures formula loading, the classical solve and verification, backend setup, compilation and audit, noise-model setup, calibration, planning, and fresh validation separately. The workflow wall clock covers the in-process comparison after formula loading and before JSON or OpenQASM output. Each validation batch also records sampling and verification time. The reported nominal two-qubit operations are compiled gates per circuit multiplied by the planned shots.

#### Local witness-budget results

The 2026-11-11 run used a 95% target, two 128-shot calibration batches per cover, compiler seed 7, and the shared layouts above. Each planned batch found at least one verified answer with its fresh seed.

| Workload and cover | Planned shots | Nominal two-qubit operations | Verified hits in fresh batch | Sample and verify (s) |
|---|---:|---:|---:|---:|
| Jakarta interlock, direct | 51 | 25,092 CX | 3 | 0.117 |
| Jakarta interlock, complement | 44 | 19,492 CX | 8 | 0.087 |
| Torino sensor kit, direct | 15 | 6,885 CZ | 2 | 3.588 |
| Torino sensor kit, complement | 17 | 8,058 CZ | 3 | 3.258 |

The exact classical solver returned `001011` for the interlock in about 37 µs and `001010` for the sensor kit in about 35 µs, including verification. The snapshot workflow took 3.69 s for Jakarta and 78.12 s for Torino. Compilation and audit took 0.89 s and 1.74 s respectively. Calibration took 0.86 s and 64.23 s. These are single-run local measurements with the same parsed formulas. Input loading was measured separately.

The one-shot interlock cap produced `NO_FEASIBLE_BUDGET` and launched no validation batches. The observed fresh outcomes are individual batch results. The 95% target comes from the calibrated model and requires repeated independent batches to assess empirically.

#### Repeated validation at fixed budgets

Pass an even number of values to `--witness-seed-schedule` in alternating direct and complement order. A single pair runs one batch per cover. Up to 128 pairs can be supplied. Every seed must be distinct and disjoint from the four calibration seeds. The complete seed schedule is recorded before validation, and each cover keeps the same compiled circuit and planned shots throughout.

A batch succeeds if it returns at least one verified witness. The report gives the completed batch count, success fraction, completed shots, nominal two-qubit operations, and local validation time. A completed batch with zero verified hits is a miss. Interrupted or unstarted batches contribute no outcome. When any planned batch is incomplete, the report withholds that cover's inferential bound.

For a complete cover, the one-sided 97.5% Clopper–Pearson lower bound applies to the probability of a successful batch. The two bounds give at least 95% simultaneous coverage under the fixed-model independent-batch assumption. The report gives `TARGET_SUPPORTED` when a lower bound reaches the caller's target, and `UNRESOLVED` otherwise. At a 95% target, the first all-success result that can support it is 72 of 72 batches. [SciPy exact binomial intervals](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binomtest.html) · [NIST simultaneous intervals](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm)

```powershell
$jakartaSeeds = 1001..1144
python -m austins_algorithm constraint-cover examples/site_interlock.json --ibm-model jakarta --max-active-qubits 6 --initial-layout 1 2 0 6 3 5 --snapshot-shots 128 --evaluation-seeds 211 223 227 229 --witness-probability 0.95 --witness-seed-schedule @jakartaSeeds

$torinoSeeds = 1201..1216
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 191 193 197 199 --witness-probability 0.95 --witness-seed-schedule @torinoSeeds
```

The 2026-11-11 comparison kept the earlier calibration seeds and planned budgets. The table reports fresh batch outcomes. Lower bounds are one-sided at 97.5% per cover.

| Snapshot and cover | Shots per batch | Successful batches | Lower success bound | 95% target | Completed shots | Nominal two-qubit operations |
|---|---:|---:|---:|---|---:|---:|
| Jakarta interlock, direct | 51 | 72 / 72 | 95.006% | Supported | 3,672 | 1,806,624 CX |
| Jakarta interlock, complement | 44 | 71 / 72 | 92.503% | Unresolved | 3,168 | 1,403,424 CX |
| Torino sensor kit, direct | 15 | 8 / 8 | 63.058% | Unresolved | 120 | 55,080 CZ |
| Torino sensor kit, complement | 17 | 8 / 8 | 63.058% | Unresolved | 136 | 64,464 CZ |

The ChronoRealm run spent 11.94 s on Jakarta validation and 51.36 s on the Torino pilot. The summed time spent verifying the returned counts was about 28 ms and 2.3 ms respectively. The complete local workflows took 16.09 s and 116.57 s. These measurements include both covers at each snapshot. The lower bounds apply to these fixed compiled circuits under the offline noise models.

#### Full Torino validation with density matrices

The Torino density-matrix follow-up fixed 112 batches per cover before sampling. With the one-sided 97.5% confidence level per cover, zero or one miss in 112 batches supports the 95% target for that cover. The budget remains fixed throughout validation.

The run reused the archived density-matrix calibration protocol of two 128-shot batches per cover and seeds `1301, 1303, 1307, 1319`. It used two standard Grover rounds, compiler seed 7, and the same starting layout and compiled circuits as the method comparison. This calibration planned 15 shots per direct batch and 16 per complement batch. The fresh schedule assigned odd seeds `2001` through `2223` to direct and even seeds `2002` through `2224` to complement.

```powershell
$torinoFullSeeds = 2001..2224
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --snapshot-method density_matrix --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 1301 1303 1307 1319 --witness-probability 0.95 --witness-seed-schedule @torinoFullSeeds
```

| Cover | Shots per batch | Verified batches | One-sided 97.5% lower bound | 95% target | Validation shots | Nominal CZ operations |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Direct | 15 | 111 / 112 | 95.126% | Supported | 1,680 | 771,120 |
| Complement | 16 | 111 / 112 | 95.126% | Supported | 1,792 | 849,408 |

Both covers returned the classically verified witness `001010`. The exact binomial tail at a 95% batch success probability was 0.022058 per cover, below the prespecified 0.025 threshold. Applying that threshold to each cover gives at least 95% simultaneous confidence for the two lower bounds. Both working copies reproduced the per-seed counts and selected OpenQASM. This replay checks reproducibility and contributes no additional statistical trials.

| Local working copy | Calibration | Holdout validation | Complete workflow |
| --- | ---: | ---: | ---: |
| ChronoRealm | 8.42 s | 342.39 s | 358.85 s |
| E: | 8.38 s | 338.17 s | 354.53 s |

The two runs shared one machine and ran concurrently. Count verification took about 34 ms per copy summed across the holdout batches. The earlier MPS pilot had eight batches per cover and 15 and 17 shots per batch, so its elapsed time covers a different workload. These results apply to the fixed routed circuits, shot budgets, and bundled Torino calibration snapshot dated 2025-02-26. Physical-device reliability remains unmeasured.










#### Compact density-matrix sampling

Add `--snapshot-method compact_density_matrix` to use the active wires explicitly. It is available on the offline named snapshots for the verified static circuits in this command. The compilation, physical measurement map, and portable OpenQASM stay tied to the original target. The snapshot reports `method: density_matrix` and `wire_representation: compact`. The physical simulation methods report `wire_representation: physical`.

This mode prepares one compact simulator per distinct active wire map. Aer derives gate and single-wire readout channels once for the combined active wires. Each cover keeps its own ordered gate operands and classical output order. Circuits must use bound static gates and terminal measurements. Delay relaxation, resets, dynamic control flow, and arbitrary user noise models are outside this mode. Each calibration or validation batch still gets its own explicit Aer seed and bounded job. [Aer device-noise functions](https://qiskit.github.io/qiskit-aer/apidocs/aer_noise.html#device-noise-parameters)

```powershell
$torinoFullSeeds = 2001..2224
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --snapshot-method compact_density_matrix --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 1301 1303 1307 1319 --witness-probability 0.95 --witness-seed-schedule @torinoFullSeeds
```

The 2026-11-11 paired benchmark held the routed circuits, shot budgets, explicit seeds, snapshot and software fixed. It alternated which representation ran first. Both covers used the same seven active Torino wires. Their 47 compact channels matched the ordered projection of the 965-entry physical model after excluding generated error IDs. The analytical check of the two representations gave zero total variation distance in the normalized output laws before and after readout for these circuits.

| Cover | Shots per batch | Exact count matches | Median physical wall per batch | Median compact wall per batch |
| --- | ---: | ---: | ---: | ---: |
| Direct | 15 | 12 / 12 | 1.208 s | 0.251 s |
| Complement | 16 | 12 / 12 | 1.227 s | 0.265 s |

Across the 24 paired batches, sampling wall time went from 29.34 s to 6.41 s, a 4.57× speedup. Those timings exclude compilation, noise-model construction, and the channel-table audit. Both full CLI replays matched all four calibration and 224 validation count maps, both witness bounds, and the selected OpenQASM from their earlier physical-index runs. The ChronoRealm replay spent 60.32 s validating and 67.55 s end to end. The E: replay spent 75.71 s validating and 83.91 s end to end. The replays ran under different local loads. Seed replay is observed for these circuits, seeds, and the tested Qiskit 2.6.2, Aer 0.18.2, and Runtime 0.51.0 versions. Snapshot results characterize the local model.

#### Prepared density-matrix sampling

Use `--snapshot-method prepared_density_matrix` to prepare the noisy gate evolution once per compact cover, then reuse the resulting density matrix for calibration and holdout batches. The state is held in memory for that run. Each replay loads the full matrix, including any unmeasured work wires, and applies only the snapshot's local readout noise with the original measurement order, classical destinations, batch seeds, and shot counts. The physical compilation and exported OpenQASM remain the source for nominal gate counts. Those counts describe repeated execution of the compiled circuit. Simulator time separately measures preparation and sampling.

This mode uses the verified static compact circuit contract. The gate-only preparation has no measurements. Per-wire terminal measurements commute past gates and their local noise on disjoint wires. Aer validates the saved matrix before reload, and the mode keeps the raw trace. A preparation timeout is recorded before the workflow stops scheduling new jobs. The existing seven-wire, 128 MiB, one-thread, and 30-second per-job wait limits also apply to preparation. [Aer density-matrix save API](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.SaveDensityMatrix.html) · [Aer density-matrix load API](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.SetDensityMatrix.html)

```powershell
$torinoFullSeeds = 2001..2224
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --snapshot-method prepared_density_matrix --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 1301 1303 1307 1319 --witness-probability 0.95 --witness-seed-schedule @torinoFullSeeds
```

The paired production benchmark used the same two Torino circuits and shot budgets as the compact run. It alternated run order within each cover and matched all 24 full count maps, including four archived and eight fresh seeds per cover.

| Cover | Shots per batch | Exact count matches | Median compact batch wall | Median prepared batch wall |
| --- | ---: | ---: | ---: | ---: |
| Direct | 15 | 12 / 12 | 319.36 ms | 1.07 ms |
| Complement | 16 | 12 / 12 | 330.38 ms | 1.19 ms |

The compact arm spent 7.86 s across the 24 sampling jobs. The prepared arm spent 0.83 s preparing two states and 0.026 s sampling. These timings exclude their shared compilation and compact noise-model setup. Both seven-wire density matrices held 256 KiB of raw complex values. The trace deficits were below `4.7e-7`, and Aer accepted both matrices without normalization. The full matrix round trip was element-for-element identical for both covers. All 64 output probabilities agreed before and after applying the same readout channels.

Both full CLI replays matched all four calibration and 224 validation count maps and the selected OpenQASM from their earlier compact runs.

| Local working copy | State preparation | Holdout sampling and verification | Complete workflow |
| --- | ---: | ---: | ---: |
| ChronoRealm | 0.95 s | 0.19 s | 8.64 s |
| E: | 0.90 s | 0.18 s | 8.57 s |

Both covers returned a verified witness in 111 of 112 batches, with the same 95.126% lower bound for each cover. These measurements apply to the tested Torino snapshot and Qiskit 2.6.2, Aer 0.18.2, and Runtime 0.51.0.

#### Reduced noise-model startup

The compact and prepared density-matrix paths build their static noise channels from a public Qiskit target projected onto the union of the two covers' active wires. The target retains the calibrated qubit and instruction properties and ordered operand tuples needed by Aer's device-error functions. If the union covers the full device, the workflow uses the original target. Each compact simulator receives the subset for its own wire map, and covers with identical maps share a simulator. The routed programs, measurement maps, and OpenQASM still use the original backend target. [Qiskit Target API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.transpiler.Target) · [Aer device-noise functions](https://qiskit.github.io/qiskit-aer/apidocs/aer_noise.html#device-noise-parameters)

The 2026-11-11 local benchmark alternated five paired setup runs per snapshot on the same already-compiled covers. It compared the archived full-target implementation and the production reduced-target implementation after warming both. These times include compact circuit and simulator setup. Backend loading, compilation, the separate channel audit, and shot sampling are outside the timings.

| Snapshot and fixture | Retained ordered channels per cover | Full-target setup median | Reduced-target setup median |
| --- | ---: | ---: | ---: |
| Manila, two-bit Boolean constraint | 12 | 53.91 ms | 18.02 ms |
| Jakarta, site interlock | 40 | 144.07 ms | 98.20 ms |
| Torino, field sensor kit | 47 | 3,073.51 ms | 206.27 ms |

All retained channel payloads matched the projected full-model values after ignoring generated error IDs. The exact density-matrix audit found zero total variation distance before and after readout on both covers of all three fixtures, and two seeded 64-shot batches per cover matched the full physical-index reference. A separate regression fixture checked distinct overlapping wire maps, directed gate channels, and asymmetric readout.

The Torino prepared replay took 0.34 s for model setup and 5.68 s end to end in ChronoRealm. The E: copy took 0.18 s for setup and 5.16 s end to end. Both copies reproduced all four calibration and 224 validation count maps, the witness bounds, and the selected OpenQASM from their earlier prepared runs. These are local measurements under Qiskit 2.6.2, Aer 0.18.2, and Runtime 0.51.0.

#### Native OpenQASM reimport

Add `--native-qasm` to `compile` or `constraint-cover` to prefer Qiskit's experimental native importer for the generated OpenQASM. The Python compiler and cover APIs accept `prefer_native_qasm=True`. Reports record the preference in `openqasm.preferred_importer` and the successful importer in `openqasm.importer`.

The existing default prefers the standard importer when installed. A native parse rejection uses the installed standard importer. A successful reimport proceeds through the same verification, and a verification failure ends the workflow. Both paths check the reimported circuit type, declared quantum and classical widths, and instruction and statevector work limits before allocating the statevector. Verification checks unique terminal measurements, their classical mapping, finite normalized probabilities, and all three full-distribution comparisons. The scope is the measured distribution from the initialized input. [Qiskit OpenQASM importer documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3)

```powershell
python -m austins_algorithm compile --problem austin --native-qasm
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --initial-layout 92 98 81 80 100 82 99 --max-active-qubits 7 --native-qasm
```

The 2026-11-11 compatibility audit compared both importers on 16 package exports each under Qiskit 2.0.0 and 2.6.2, using standard importer 0.6.0. The tested ordered instructions, parameters, wire maps, and gate matrices matched. Total variation distance was zero for each full measurement distribution. On the selected Torino export, median parse time fell from 347 ms to 13.23 ms with Qiskit 2.0.0 and from 321 ms to 9.75 ms with Qiskit 2.6.2. Native rejected some broader constructs, including an emitted custom gate that the standard importer successfully reimported and verified.

Six paired production Torino compile-and-audit runs alternated the parser order after warming both. Median wall time fell from 1.37 s to 0.69 s for the two covers together, with identical OpenQASM, measurement maps, and verification results in every pair. This timing includes compilation and the full checks and excludes backend initialization and shot sampling. Both prepared-snapshot CLI replays matched all four calibration and 224 holdout count maps, the witness bounds, and the archived OpenQASM. Compile-and-audit time was 0.78 s in ChronoRealm and 0.77 s on E:. The full offline runs took 3.95 s and 4.27 s respectively.

#### Memory-bounded exhaustive oracle audit

The audit can build one Qiskit operator and reuse its columns for eligible oracles. It uses the dense path for 3–7 search wires, at most seven total wires, at most two work wires, and the checked built-in gate families. Only the first `2**search_qubits` input columns are audited, which initializes the higher-index work wires to zero. The raw complex128 operator is at most 256 KiB. Scratch arrays and Qiskit construction temporaries add to that storage. [Qiskit Operator API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.quantum_info.Operator)

Both paths call the verifier once per examined input in ascending basis order. They check the full off-diagonal residual, unit diagonal magnitude, and agreement with the verifier up to one common phase. The absolute audit tolerance remains `1e-9`. The dense path checks for phase-oracle structure with half that tolerance before using the matrix, so rounding-sensitive cases run through the original per-input Statevector audit. Wider, work-heavy, and custom-gate circuits also use that path. If Qiskit rejects a gate definition during operator construction, the original audit still runs.

The 2026-11-11 paired production benchmark compared the archived per-input implementation with the new audit. Six paired timings per oracle alternated execution order after warming both paths. The verifier, phase and work-wire checks, and audit reports matched in each pair. These timings cover the audit only.

| Oracle | Search / total wires | Per-input median | Bounded operator median |
| --- | ---: | ---: | ---: |
| Austin | 3 / 3 | 0.78 ms | 0.21 ms |
| Modulo-4 direct | 4 / 4 | 1.80 ms | 0.29 ms |
| Modulo-4 with work wire | 4 / 5 | 16.13 ms | 2.03 ms |
| Site interlock direct | 6 / 6 | 114.66 ms | 5.40 ms |
| Site interlock complement | 6 / 6 | 104.72 ms | 5.22 ms |
| Field sensor kit direct | 6 / 7 | 127.26 ms | 13.98 ms |
| Field sensor kit complement | 6 / 7 | 145.78 ms | 14.74 ms |

The separate crossover experiment found that an unrestricted dense audit could slow down when few search inputs shared a wide matrix. A synthetic seven-wire, three-search-wire case was slightly slower with the full matrix. At ten wires, the 16 MiB matrix increased observed process peak working set by about 68 MiB. These Windows high-water measurements describe the test process and workloads.

Both prepared Torino CLI replays reproduced the previous per-cover audits, selected OpenQASM, all four calibration and 224 validation count maps, and the witness bounds. Compilation and audit took about 0.45 s per copy. The full offline workflows took 3.42 s in ChronoRealm and 3.60 s on E:. The benchmark used Qiskit 2.6.2. The focused oracle tests also exercised Qiskit 2.0.0.

#### Wider clause-flag oracle audits

The wider audit evolves only the work-zero input columns. It applies each local Qiskit gate in the circuit's operand order, using a second buffer for contractions and row swaps. Both the global phase and the local coefficients are retained. Each block uses its global input offsets and the first input's phase as the common reference. The numerical guard requires workspace leakage and phase variation within half the public tolerance before the verifier sees any accelerated output. If any block is uncertain, or if a bound, gate check, or Qiskit matrix construction fails, the full audit uses the original per-input path. Verifier calls remain ordered and occur once per checked input. [Qiskit Operator API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.quantum_info.Operator) · [NumPy contraction API](https://numpy.org/doc/stable/reference/generated/numpy.einsum.html)

This path covers 9–12 total wires and at least four search wires with the checked clause-flag gate families. Each complex128 column buffer may contain at most `2**18` entries (4 MiB), and each local gate matrix may span at most seven wires (256 KiB). The audit permits up to four blocks, each with at most `2**26` estimated contraction terms. The estimate is one block's array size multiplied by the sum of `2**gate_width` across the circuit, so the aggregate estimate is bounded by `2**28` terms. The two buffers are reused across all blocks, including guard scratch. Gate matrices, compact diagonals, and library temporaries are separate from these column buffers.

```powershell
python -m austins_algorithm constraint examples/field_sensor_kit.json --synthesis clause-flags --strategy known --max-shots 4 --max-oracle-queries 64 --classical-method propagation --seed 7
```

The original 2026-11-11 single-block benchmark timed the archived per-input and wider audits in six alternating pairs per bundled fixture. Every exhaustive audit result matched. Both paths received the same synthesized oracle and independent verifier.

| Fixture | Search / total wires | Per-input median | Wider median | Observed traced allocation peak |
| --- | ---: | ---: | ---: | ---: |
| Site interlock, clause flags | 6 / 10 | 230.83 ms | 25.79 ms | 2.07 MiB |
| Field sensor kit, clause flags | 6 / 12 | 214.83 ms | 102.93 ms | 8.20 MiB |

The peak figures come from Python allocation tracing and omit untracked native allocations. Seven eligible synthetic clause-flag cases covering four to eight search wires had paired median speedups from 1.75× to 5.22×. Three other synthetic cases exercised the separate gate-width, work, and array-size fallbacks. All returned the same audit verdict as the reference. The benchmark used the local Qiskit 2.6.2 environment.

Both CLI copies produced the same complete audits and seeded search results as the archived basis runs. The 10-wire interlock returned `110011`, and the 12-wire sensor kit returned `111001`. These wide searches used the ideal local simulator. A separate seven-wire prepared Torino regression reproduced all four calibration and 224 holdout count maps, both witness bounds, and the selected OpenQASM in each copy.

#### Four-block audit at eight search bits

The 12-wire telemetry-power oracle now audits all 256 inputs in four 64-column blocks. It uses two explicit 4 MiB buffers and retains at most 4 KiB of diagonals for the final phase checks. The benchmark instrumented the two buffer addresses across all four blocks. Its 239,075,328 estimated terms fit the aggregate bound. The fast path matched all 96 witnesses from the archived basis audit.

| ChronoRealm isolated audit | Archived median | Current median |
| --- | ---: | ---: |
| Telemetry power, 8 search / 12 total wires | 3.97 s | 0.602 s |
| Site interlock, 6 search / 10 total wires | 39.6 ms | 46.5 ms |
| Field sensor kit, 6 search / 12 total wires | 169.2 ms | 141.5 ms |

The measurement alternated six pairs with the same synthesized oracle and verifier in a warmed Python 3.12.15 process using Qiskit 2.6.2 and NumPy 2.5.4. The six-bit fixtures each use a single block and serve as regression checks. These samples establish the telemetry speedup under the tested conditions. They leave a smaller timing change on the site interlock unresolved. Regression tests exercise sparse operand order and global phase across block boundaries, wrong phases, work leakage at input `10000000`, and whole-audit fallback for a later phase near the acceptance tolerance.

#### Exact row updates for controlled gates

The block audit recognizes an exact controlled swap or sign flip from the actual Qiskit matrix. It compares the diagonal and the relevant off-diagonal entries and checks the total number of nonzero coefficients. The declared control bits select tensor axes in operand order, including open controls. Exact X-family swaps use slices of the two existing buffers. Exact Z-family gates negate only the selected slice. Each H or gate with additional nonzero coefficients uses the full local contraction. The two-buffer and block limits still use the conservative dense work estimate. No full-size row index maps are allocated. [NumPy copy API](https://numpy.org/doc/stable/reference/generated/numpy.copyto.html)

The ChronoRealm run alternated six pairs with the archived block audit on the same clause-flag oracles. All phase and workspace results matched. The telemetry oracle used exact row updates for 44 of its 51 gates. The remaining gates used their actual coefficients. Tests cover open CX, CCX, MCX, and CZ controls on reordered operands, a nonzero global phase, and an altered MCX whose small additional phase accumulates across 256 uses.

| ChronoRealm isolated audit | Archived block contraction | Exact row updates |
| --- | ---: | ---: |
| Telemetry power | 721.2 ms | 540.8 ms |
| Site interlock | 46.0 ms | 32.3 ms |
| Field sensor kit | 157.1 ms | 87.2 ms |

The paired medians use Python 3.12.15, Qiskit 2.6.2, and NumPy 2.5.4. The benchmark observed the same two buffer addresses per audit. Timings cover local validation on these fixtures.

#### Reuse dense matrices within one audit

The blocked audit can retain read-only copies of the actual Hadamard and multi-controlled X matrices for dense contraction. When sparse recognition has already built a matrix, the audit can keep those same coefficients. Each instruction has its own entry. Entries live only for that audit and share a 256 KiB matrix payload cap, separate from the two column buffers. A step that exceeds the remaining budget reconstructs its matrix when needed. This cap describes retained arrays. Source matrices, defensive copies during planning, and SDK and NumPy internals can add temporary allocations.

Both accelerated audit paths check concrete stock gate types. An instance-level matrix conversion override or a nonstock subclass goes through per-input evolution. The regression circuit returns a stock swap during early conversions and a different phase on later calls. The reference catches its incorrect phase at input `0001` on both four-wire and nine-wire circuits. Another test verifies zero-cap, partial, and full matrix retention across two blocks.

| ChronoRealm isolated audit | Matrix builds before / after | Retained matrix bytes | Median before / after |
| --- | ---: | ---: | ---: |
| Telemetry power | 77 / 51 | 45,184 | 298.3 / 269.9 ms |
| Site interlock | 42 / 39 | 24,704 | 17.35 / 13.87 ms |
| Field sensor kit | 52 / 51 | 65,664 | 55.40 / 52.65 ms |

These are six alternating pairs per fixture with the archived exact-row audit on Python 3.12.15, Qiskit 2.6.2, and NumPy 2.5.4. The benchmark excludes oracle synthesis. It observed the same two reusable column buffers and the same phase and workspace outcomes in both arms. The local matrix reuse removes repeated setup work. Dense contractions still dominate the measured telemetry path.

#### Tiled multiplication for larger dense gates

The audit can apply a dense complex128 gate spanning four or more wires by packing complete local basis groups, multiplying each batch by the actual Qiskit matrix, and scattering the results into the existing output buffer. The row order follows the gate operands, including nonadjacent and reordered wires. The audit allocates the two packing arrays once, with a combined payload of at most 256 KiB. The row-index table is separate and has at most one native integer per physical basis row. NumPy and BLAS can allocate their own temporaries. The smaller-gate and over-budget paths use the existing contraction, and the aggregate dense work estimate still governs audit eligibility. [NumPy matrix multiply](https://numpy.org/doc/stable/reference/generated/numpy.matmul.html)

| ChronoRealm isolated audit | Previous median | Tiled median |
| --- | ---: | ---: |
| Telemetry power | 323.0 ms | 220.5 ms |
| Site interlock | 16.04 ms | 11.06 ms |
| Field sensor kit | 61.24 ms | 47.15 ms |

These are six alternating pairs on Python 3.12.15, Qiskit 2.6.2, and NumPy 2.5.4 with inherited BLAS settings. Timing includes gathering, multiplication, and scattering. The largest individual amplitude difference across the 384 fixture columns was `1.11e-16`. The largest column difference in Euclidean norm was `1.25e-16`. The public audit verdicts matched. Tests exercise a partial final tile, a budget too small for one group, and a perturbed gate on both sides of the public phase tolerance. The half-tolerance guard retains whole-audit basis fallback.

#### Direct multiplication for contiguous operands

A dense gate whose operands are consecutive and in increasing physical-wire order can multiply directly into a view of the existing spare buffer. The reshape groups the gate's local basis together and combines lower spectator bits with the input columns. Higher spectator bits are batches. Reordered and nonadjacent gates use the existing packed tiles. The audit allocates packing arrays only when a packed gate needs them. The existing numerical guard still covers either route. [NumPy reshape](https://numpy.org/doc/stable/reference/generated/numpy.reshape.html)

In the archived 256 KiB packed telemetry run, the instrumented median stage times were 1.96 ms for row indices, 5.42 ms for gathering, 41.46 ms for multiplication, and 6.22 ms for scattering. A 64 or 128 KiB packing cap produced mixed timing changes across the fixtures. The production cap remains 256 KiB. The index construction cost gives little room for a cache to improve the whole audit.

| ChronoRealm isolated audit | Previous median | Direct-view median | Packed buffer bytes after |
| --- | ---: | ---: | ---: |
| Telemetry power | 191.7 ms | 184.8 ms | 262,144 |
| Site interlock | 10.71 ms | 10.50 ms | 262,144 |
| Field sensor kit | 42.02 ms | 37.60 ms | 0 |

Six alternating pairs used the same local environment as the tiled benchmark above. Telemetry used four direct applications and still needed one packing pair for its other gates. The field sensor audit used one direct application and avoided the entire 256 KiB packing pair. Site interlock used the packed path throughout. These figures describe explicit packing allocations. NumPy and BLAS can use their own scratch. Raw columns and audit verdicts matched the archived path. Tests cover contiguous spans at the low, middle, and high end of the register and a reversed span that must stay packed.

#### Compile the actual search attempt

For the `constraint` command, compilation and offline IBM snapshot sampling use the measured circuit for the last completed ideal sample. The workflow synthesizes and audits the oracle once, prepares the diffuser once, and builds circuits as needed for sampled round counts. It retains only the latest completed attempt for downstream use. Both optional stages label the circuit with its one-based attempt number and recorded Grover rounds.

A completed zero-round attempt is eligible for export, including a failed sample followed by budget exhaustion. A round that would exceed the query budget creates no attempt. A proven-empty constraint or a budget stop before any sample yields `NOT_RUN_NO_ATTEMPT` for requested downstream stages and creates no OpenQASM file. A zero-round circuit can contain idle work wires, which the portable export removes under its existing verified wire map.

The compilation report records `attempt_build_seconds` spent building that attempt's circuit, with zero on a cache hit. It records `compile_export_verify_seconds` for the downstream target, compilation, and verification. The combined `build_compile_export_verify_seconds` is their sum. The attempt build is already part of the simulator search time. These timings precede any requested file write.

The 2026-11-11 handoff benchmark used eight alternating pairs of the two CLI handlers per fixture before round-count reuse was added. It timed the sum of oracle synthesis, diffuser preparation, and circuit construction. Both versions performed actual compilation and verification with the same outcome.

| Field sensor fixture | Circuit builds before / after | Previous build median | Current build median | Median paired build savings |
| --- | ---: | ---: | ---: | ---: |
| Complement with clean-ancilla diffuser, one sampled attempt | 2 / 1 | 1.72 ms | 0.88 ms | 0.84 ms |
| Query budget exhausted after three zero-round attempts | 4 / 3 | 1.11 ms | 0.66 ms | 0.44 ms |

Both fixtures went from two synthesis and diffuser preparations to one each. These are measured construction costs. The full CLI includes auditing, sampling, compilation, and export verification.

The ChronoRealm and E: CLI replays compared six compile-only or combined requests with archived runs. The tested OpenQASM files matched byte-for-byte. Search attempts, mapped circuits, witness results, and Manila and Jakarta offline shot counts matched. Cases with no sampled attempt produced no files. Separate snapshot-only Jakarta runs reproduced the same shot counts and attempt metadata in both copies.

#### Reuse circuits for repeated round counts

Each ideal search keeps up to four measured circuits, keyed by their Grover round count. It builds a circuit on a miss and evicts the least recently used circuit when full. The cache belongs to that search, where the copied oracle, search width, and diffuser are fixed. The query-budget check comes before cache lookup. Every attempt draws the next sampler seed, samples one shot, and calls the verifier once. The stock sampler prepares a fresh state. The logical query budget counts the rounds used by every attempt, including cached circuits. [Qiskit StatevectorSampler API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.primitives.StatevectorSampler)

The 2026-11-11 benchmark compared the archived scheduler and the production cache in eight alternating pairs per case on Qiskit 2.6.2. It checked the full search report, exact sequence of sampler seeds, verifier calls, and retained last circuit. The two 32-shot cases deliberately rejected every measured candidate to exercise reuse. Timings cover circuit construction only.

| Field sensor search case | Builds before / after | Previous build median | Current build median |
| --- | ---: | ---: | ---: |
| Fixed rounds, 32 rejected shots, clean-ancilla diffuser | 32 / 1 | 11.79 ms | 0.30 ms |
| Randomized rounds, 32 rejected shots | 32 / 10 | 12.70 ms | 5.06 ms |
| Randomized rounds, verified answer on attempt four | 4 / 2 | 0.49 ms | 0.33 ms |
| Query limit after three zero-round attempts | 3 / 1 | 0.23 ms | 0.06 ms |

A verified first attempt built one circuit, and a known-count budget stop before sampling built zero. The focused tests also exercised eviction and verified that repeated submissions leave the measured circuit intact. Both CLI copies reproduced the previous OpenQASM bytes, mapped circuit reports, ideal search results, and Manila and Jakarta offline counts. The last failed zero-round attempt reused its circuit and recorded zero incremental build time. Separate snapshot-only Jakarta replays matched the combined compile and snapshot results.

#### Prepared ideal statevector sampling

The `search` and `constraint` commands accept `--ideal-sampler prepared_statevector`. The corresponding Python APIs accept `ideal_sampler="prepared_statevector"`. The default `statevector_sampler` uses the Qiskit primitive. The prepared mode records its requested and used methods under `ideal_sampler` in the search report. It uses the stock sampler for gates outside the verified deterministic set, including custom gates and altered MCX definitions. A search stopped before sampling records a null used method.

On a cache miss, the prepared path derives a Statevector from the measured circuit with its terminal measurements removed. It retains the measured circuit for compile and offline snapshot handoff. Each attempted shot reseeds its state and calls the public Qiskit sampling API on the search wires in their original order. The result stays MSB-first. A cache eviction releases the circuit and its state together. Four 12-wire complex128 states need at most 256 KiB of retained amplitudes. Circuit storage and simulator temporaries are separate. [Qiskit Statevector sampling API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.quantum_info.Statevector) · [Qiskit StatevectorSampler API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.primitives.StatevectorSampler)

```powershell
python -m austins_algorithm search --problem mod4-range --oracle work-qubit --strategy unknown --seed 7 --ideal-sampler prepared_statevector
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known --ideal-sampler prepared_statevector
```

The 2026-11-11 production benchmark alternated six pairs per case with Qiskit 2.6.2. It measured the whole ideal search, including initial preparation. Before timing, it captured 109 paired attempts with exact matching statevector entries, full measurement probability arrays, sampler seeds, and single-shot draws. The search reports matched apart from the requested and used sampler labels. The repeated-shot cases used a verifier that deliberately rejected every measured candidate. The focused suite also tested Qiskit 2.0.0.

| Local case | Attempts | Stock median | Prepared median |
| --- | ---: | ---: | ---: |
| 7-wire field sensor, fixed rounds | 32 | 304.70 ms | 13.35 ms |
| 7-wire field sensor, randomized rounds | 32 | 331.12 ms | 145.73 ms |
| 12-wire field sensor, clause flags | 24 | 383.53 ms | 18.60 ms |
| 7-wire field sensor, first verified hit | 1 | 10.14 ms | 9.12 ms |
| 7-wire field sensor, randomized verified hit | 4 | 9.57 ms | 6.75 ms |
| 12 measured wires, zero rounds | 16 | 220.12 ms | 195.86 ms |

The wide zero-round case still spends time forming sampled outcome labels in Qiskit's public sampling routine. The benchmark concerns local ideal simulation. Both CLI copies reproduced the previous verified OpenQASM and offline Manila and Jakarta counts from the measured-circuit handoff. Both also verified standalone search and snapshot-only use.

#### Cached ideal measurement probabilities

Use `--ideal-sampler prepared_probabilities` on `search` or `constraint` to retain the measurement probabilities for each eligible round count. The same deterministic gate check applies as for prepared statevectors. On a cache miss, Qiskit evolves the circuit and produces the search-wire probability array. The cache holds that array and the measured circuit for downstream handoff. It releases both together at eviction.

The probability path checks the expected flat shape, floating dtype, finite entries, and nonnegative values. It passes the original weights to NumPy, which checks normalization during its weighted draw. Uniform tables still use weighted sampling. Each attempt passes its existing fresh seed to `default_rng`, draws one integer outcome, and formats it as the same zero-padded MSB-first bitstring. The implementation preserves Qiskit's probability order and dtype. Four 12-search-wire probability arrays retain at most 128 KiB. State preparation and the draw can use additional temporary storage. [Qiskit Statevector API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qiskit.quantum_info.Statevector) · [NumPy weighted choice API](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.choice.html)

```powershell
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known --ideal-sampler prepared_probabilities
```

The 2026-11-11 production benchmark used six permutations of the three methods on Qiskit 2.6.2 and NumPy 2.5.4. It timed the whole ideal search, including state preparation. Before timing, it compared 125 stock and probability-cache attempts and found identical raw probability arrays, dtype, seeds, and draws. The search reports agreed apart from sampler provenance. The focused suite also exercised Qiskit 2.0.0. Seed replay evidence applies to the tested library versions. [NumPy random generator compatibility](https://numpy.org/doc/stable/reference/random/compatibility.html)

| Local case | Attempts | Stock median | Prepared statevector median | Cached probability median |
| --- | ---: | ---: | ---: | ---: |
| 7-wire field sensor, fixed rounds | 32 | 331.16 ms | 13.72 ms | 10.78 ms |
| 7-wire field sensor, randomized rounds | 32 | 323.33 ms | 144.69 ms | 139.22 ms |
| 12-wire field sensor, 6 measured wires | 24 | 382.55 ms | 19.03 ms | 15.93 ms |
| 7-wire field sensor, first verified hit | 1 | 10.25 ms | 9.47 ms | 9.35 ms |
| 7-wire field sensor, randomized verified hit | 4 | 9.22 ms | 6.87 ms | 6.48 ms |
| 8 measured wires, zero rounds | 16 | 21.85 ms | 6.50 ms | 1.14 ms |
| 12 measured wires, zero rounds | 16 | 212.05 ms | 191.17 ms | 1.99 ms |

The zero-round rows isolate repeated draws from a uniform ideal state. They measure removal of repeated outcome labeling and probability calculation. The first-hit row still pays for preparation, and the two prepared methods took similar time there. Both CLI copies verified standalone search, compile handoff, and offline Manila and Jakarta snapshot replay with the new option. The tested exports and offline counts matched their earlier baselines.

## End-to-end first-witness cost

Run the bundled sensor-kit comparison from the package root with the Qiskit and OpenQASM extras installed. Add the IBM extra for the offline Torino experiment.

```powershell
python -m pip install -e ".[qasm,ibm]"
python examples/benchmark_search_cost.py --torino
```

Omit `--torino` to run the classical and ideal arms alone. The JSON report includes raw per-trial timings, fixed seeds, resource counts, export digests and wire mappings, and the scope of each timer. The fixture is checked against its canonical formula digest. Every arm has the same correctness contract. It must find any six-bit assignment accepted by the original CNF. A randomized request can also report budget exhaustion.

The benchmark runs 128 trials per arm after an excluded warmup. Method position and immediate predecessor within a trial are balanced. Each request rereads and validates the fixture. Timings use a warm Windows process through candidate verification, before report serialization. The direct constructor is specific to this problem. Propagation and scan accept generic CNF inputs. The ideal arm includes synthesis, a full 64-input phase and workspace audit, sampling with the prepared-probability cache, and compiling and checking the sampled circuit's portable export. The exact solution set and marked count are available from the exhaustive audit, which is part of the measured validation workflow.

### Local measurements

The ChronoRealm run used Python 3.12.15, Qiskit 2.6.2, NumPy 2.5.4, and SciPy 1.18.1. All three classical methods returned and independently verified `001010` in all 128 trials.

| Method | Verified trials | Median solve or sample time | Median total |
| --- | ---: | ---: | ---: |
| Direct kit constructor | 128 / 128 | 3.1 Âµs | 0.147 ms |
| Unit propagation | 128 / 128 | 23.2 Âµs | 0.166 ms |
| Exact scan | 128 / 128 | 36.2 Âµs | 0.266 ms |
| Prepared ideal Grover search | 128 / 128 | 9.02 ms | 97.39 ms |

Ideal sample time includes circuit construction and per-attempt verification. Median oracle construction took 0.46 ms, exhaustive audit and solution-set comparison took 13.73 ms, and compilation, export, and verification took 73.85 ms. The seven-wire line target produced depth 718 and 504 two-qubit gates. The round-trip measurement distributions agreed within a total variation distance of `6.3e-15`. Phase medians are computed independently.

For the nine marked states, two Grover rounds give an ideal single-shot success probability of 88.1655%. Four shots give a modeled 99.9804% chance of finding a witness. The 128 seeded requests used 143 shots and 286 oracle calls. Their one-sided exact 97.5% lower success bound was 97.1592%, above the 95% target.

### Offline Torino replay

The optional arm uses the bundled Torino snapshot dated 2025-02-26, Qiskit Aer 0.18.2, Runtime 0.51.0, and the documented two-round, seven-wire placement and seed schedule. Each cover supported the 95% target with the one-sided 97.5% exact bound of 95.1257%. The direct cover found a verified witness in 111 of 112 batches with 15 shots per batch and 459 CZ gates. The complement cover found a witness in 111 of 112 batches with 16 shots per batch and 474 CZ gates.

The complete two-cover replay took 1.31 s locally, including compilation, model preparation, calibration, and 224 holdout batches. Prepared sampling and verification had medians of 0.62 ms per direct batch and 0.60 ms per complement batch. Those batch medians start with the noisy states already prepared. Replaying the published seeds checks reproducibility. The dataset is a small synthetic example, and physical-device runtime, cost, and reliability remain unmeasured. See [IBM's local testing documentation](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/local-testing-mode).

### Reuse a verified line compilation

For repeated known-count ideal searches on one unchanged formula, prepare the synthetic-line compilation once and vary the sampler seed or budget on each run.

```python
from austins_algorithm.constraint_search import PreparedConstraintLineSearch
from austins_algorithm.constraints import load_constraint

constraint = load_constraint("examples/field_sensor_kit.json")
plan = PreparedConstraintLineSearch(constraint, synthesis="unit-factorized")

for seed in (3101, 3102):
    result = plan.run(seed=seed, max_shots=4, max_oracle_queries=8)
    print(result["status"], result["candidate"])

print(plan.compilation_report["openqasm"]["sha256"])
```

Preparation audits the oracle and verifies the portable OpenQASM against the ideal measurement distribution. The plan owns a copy of the formula and fixes its synthesis, diffuser, synthetic line, compiler seed, and importer preference. Build a new plan to change any of those inputs. An exhaustively empty formula has no search to compile and raises `ValueError`. The ordinary constraint workflow can return a structured empty proof. Each call to `run` samples the fixed-round logical search and checks sampled bits against the original CNF. The plan exposes the verified export as `openqasm` and returns a detached copy of its compilation report. The report records the export's wire mapping. Each search's `prepared_compilation_sha256` identifies the plan's artifact, including when a budget prevents sampling.

```powershell
python examples/benchmark_search_cost.py --reuse-verified-line
```

This adds a prepared arm after the fresh-request arm in the same warm process. Both arms use the same 128 seeds, four-shot cap, and eight-oracle-call cap. The prepared arm includes one input load, one audit, and one compilation in its total. The benchmark checks the export digest and maps, each sampled bitstring and verification result, candidate, and budget use.

Before probability-table reuse was added, the ChronoRealm run with Python 3.12.15 and Qiskit 2.6.2 spent 11.61 s across 128 fresh ideal requests and 1.07 s across the prepared requests, including the 77.8 ms preparation. The median prepared request took 7.71 ms. The measured reduction includes skipping repeated JSON loads, oracle synthesis and audit, and compilation and export verification. That version prepared its ideal state per request. All 128 seeded attempt sequences and witnesses matched, along with the portable export digest, wire maps, and gate and depth metrics. These are warm local simulation measurements for one repeated workload. IBM snapshot runs use their own target and layout compilation.

### Reuse a plan's ideal probability table

`PreparedConstraintLineSearch.run()` uses `prepared_probabilities` by default. On the first affordable request, the plan evolves its original measured logical circuit and retains one read-only probability table. This is the circuit supplied to the compiler. Compiled and reimported probabilities can differ slightly within verification tolerance. Sampling uses the original table, the existing per-attempt seed and NumPy draw, and the original formula as the verifier. The table holds at most 2 KiB of weights for the supported eight-search-bit limit. Concurrent requests share the table and each request has its own random generators, budgets, and report. [NumPy sampling documentation](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.choice.html)

A budget that permits zero attempts leaves the table unprepared. Zero-round circuits can still take a shot with a zero-query budget. Circuits that fail the existing deterministic-gate or memory checks use the ordinary sampler fallback. Explicit `statevector_sampler` and `prepared_statevector` requests use their existing per-request paths.

```powershell
python examples/benchmark_plan_probabilities.py
```

The benchmark alternates method order across 32 paired seeds per fixture. Both arms use the existing prepared-probability draw and verification loop. The fresh arm rebuilds the logical circuit and table per request, while the shared arm retains both. The site interlock uses the ten-wire clause-flag oracle to exercise work-register evolution with the same six measured bits as the seven-wire sensor kit. All 64 report pairs matched, including every attempt, witness, budget, and sampler decision.

| ChronoRealm run | Later request median, fresh | Later request median, shared | 32 requests, fresh | 32 requests, shared |
| --- | ---: | ---: | ---: | ---: |
| Field sensor kit | 9.33 ms | 0.123 ms | 297.0 ms | 12.3 ms |
| Site interlock | 13.26 ms | 0.134 ms | 459.2 ms | 16.7 ms |

The first shared requests took 8.56 ms for the kit and 12.57 ms for the interlock, including table preparation. Both are included in the request totals. The benchmark reports plan construction, audit, and OpenQASM verification separately. These measurements used Python 3.12.15, Qiskit 2.6.2, and NumPy 2.5.4 and describe local ideal simulation.

## Eight-bit telemetry-power benchmark

The [telemetry-power fixture](examples/telemetry_power.json) models eight control bits. At least one sensor and one uplink must be enabled. The radar needs reserve or boost power, and boost power conflicts with the primary uplink. These four clauses form one connected constraint graph. A direct constructor enables the first sensor and the primary uplink, producing `01000001`. An independent domain predicate and the exhaustive oracle audit agree on all 96 valid assignments among 256 inputs.

```powershell
python examples/benchmark_search_cost.py --workload telemetry-power --reuse-verified-line --torino
```

The benchmark pairs eight fresh requests per method in a warmed interpreter. The prepared arms share one fixed input across 128 requests. Both prepared arms include their setup and verify each witness. The ideal arm checks the eight overlapping seeds against the fresh attempts. Every method seeks any candidate accepted by the same original formula. The fixture-specific direct constructor is the fastest measured classical route. Unit propagation and exhaustive scan also accept general CNF inputs.

| ChronoRealm run | Verified requests | Measured time |
| --- | ---: | ---: |
| Fresh direct constructor | 8 / 8 | 0.169 ms median |
| Fresh unit propagation | 8 / 8 | 0.191 ms median |
| Fresh exhaustive scan | 8 / 8 | 0.399 ms median |
| Fresh ideal Grover search, audit and export | 8 / 8 | 2.34 s median |
| Prepared direct constructor, including setup | 128 / 128 | 0.587 ms total |
| Prepared ideal Grover search, including setup | 128 / 128 | 2.55 s total |

The ideal circuit uses eight measured search bits and four work wires. With 96 marked assignments, one Grover round gives 84.375% ideal single-shot success. The fixed four-shot and four-query cap gives 99.9404% modeled success. The prepared run returned 128 verified witnesses in 157 shots. Its one-sided exact 97.5% lower bound was 97.159%, above the prespecified 95% target. The eight-request fresh arm checks runtime and replay behavior. Its sample is too small to establish the same bound independently. Repeating the same seeds in the other copy checks reproducibility.

The 12-wire synthetic line compiled to depth 563 and 449 CX gates. The portable OpenQASM round trip had total variation distance `1.18e-14`. Before four-block audit support, the fresh oracle audit and solution check took a median 2.23 s, while compilation and export verification took about 98 ms. Reusing one audit and compilation across eight overlapping requests took 2.54 s including setup, compared with 20.08 s across the eight fresh ideal requests. The setup includes the exhaustive audit, which already discovers the full solution set. The measurements used Python 3.12.15, Qiskit 2.6.2, NumPy 2.5.4, and SciPy 1.18.1.

The offline Torino snapshot dated 2025-02-26 compiled the same one-round circuit on 12 active wires at depth 977 and 356 CZ gates. The export preserved the ideal hit probability. The package returned `COMPILE_ONLY_ACTIVE_QUBIT_LIMIT` at its seven-active-wire sampling cap, so this result measures compilation and ideal export verification. No noisy shots or physical-device results were produced for this workload. [IBM's Grover workflow](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm)

## Field sensor kit example

The synthetic [field sensor kit](examples/field_sensor_kit.json) chooses exactly one sensor and one uplink. From `q0` through `q5`, the options are radar, optical sensor, LTE, wired uplink, backup power, and a local data buffer. Radar requires backup power. LTE requires a buffer. The remaining accessories are optional.

From the package directory, with the Qiskit and OpenQASM tools installed, run:

```powershell
python -m pip install ".[qasm]"
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known --compile --qasm-output field-sensor-kit.qasm
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy unknown
python examples/field_sensor_kit_baseline.py
```

The compile option verifies the ideal measurement distribution of the last attempted circuit on a synthetic line target and after OpenQASM reimport. The round count is recorded. It creates the requested file only when there is a circuit to export and the path is new.

With the IBM model dependencies installed, the same audited circuit can be run against a bundled offline noise snapshot. This command also compiles a separate synthetic line circuit for comparison.

```powershell
python -m pip install ".[ibm,qasm]"
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known --compile --ibm-model torino --snapshot-shots 64
```

The snapshot path uses the measured circuit from the last ideal search attempt, including that attempt's round count. It runs locally with no IBM account. It reports compile-only status if routing needs more than seven active wires, and accepts up to 256 snapshot shots. Use `--synthesis clause-flags` to reproduce the reference line circuit.

### Measured local run

On **2026-11-11**, the classical constructor and the exhaustive oracle audit agreed on nine of the 64 possible assignments. The Windows run used Python 3.12.15, Qiskit 2.6.2, compiler seed 7, and optimization level 1.

| Method | Verified kit | Work observed |
|---|---|---|
| Direct classical construction | `001010` | About 3 µs for one kit |
| Classical clause scan | `001010` | 11 assignments checked, about 34 µs |
| Ideal simulator, known count | `111001` | 1 shot, 2 logical oracle calls |
| Ideal simulator, unknown count | `110110` | 3 shots, 1 logical oracle call |

The sensor/power and uplink/buffer choices form two independent groups. Factorization checked eight assignments per group during synthesis. The separate oracle audit still checked all 64 inputs and confirmed the same nine marked assignments with all work wires restored.

| Two-round circuit | Work qubits | Compilation target | Depth | Two-qubit gates |
|---|---:|---|---:|---:|
| Clause flags | 6 | Synthetic 12-wire line | 828 | 826 CX |
| Factorized | 1 | Synthetic 7-wire line | 718 | 504 CX |
| Factorized | 1 | IBM Torino snapshot, 7 active wires | 1473 | 459 CZ |

The line targets have different widths. Their exported ideal measurement distributions agreed to within `4.93e-15` total variation distance. The factorized circuit's own OpenQASM round-trip distance was `6.29e-15`.

The offline Torino run used the snapshot dated 2025-02-26, Qiskit Runtime 0.51.0, Aer 0.18.2, and seed 7. Routing used physical indices `80, 81, 82, 92, 98, 99, 100`. Aer completed 64 shots and the classical verifier accepted **13 of 64** (20.3%, Wilson 95% interval 12.3–31.7%). This interval describes shot variability for the fixed simulation. Physical performance and provider cost remain unmeasured. The direct classical constructor is the first-answer baseline for this structured fixture.

## Compare rounds at two fixed starting placements

The `constraint-sweep` command audits the oracle once and compares zero, one, and two Grover rounds at each of two caller-supplied starting placements. Give each `--placement` in logical order, search wires first and work wires last. The example below uses the prior Torino placement and a second layout that swaps the two independent groups on the same physical wires. Both choices were fixed before sampling.

```powershell
python -m pip install ".[ibm,qasm]"
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --ibm-model torino --shots 128 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --calibration-seed 7 --validation-seed 19 --transpiler-seed 7
```

Each circuit has a verified OpenQASM round trip, logical-to-physical measurement map, active wire list, depth, and two-qubit gate count. The starting map is fixed within each placement. Routing can move qubits, so use the recorded final maps when inspecting measurements. [Qiskit compiler reference](https://quantum.cloud.ibm.com/docs/en/api/qiskit/compiler)

The command reuses each compiled circuit across calibration and validation. It selects the most verified calibration hits, then breaks ties by fewer two-qubit gates, smaller depth, fewer rounds, and earlier placement. Validation uses its own seed on all six cells and reports the selected cell's difference from zero rounds at the same placement. Zero rounds prepares and measures a uniform superposition with no oracle calls. The report includes counts verified against the original constraint and per-cell Wilson intervals for shot variability. The validation differences are descriptive.

Each stage accepts 1 to 256 shots per cell. The simulator runs up to 12 batches on one bundled snapshot. A proven empty constraint stops after the audit. If any circuit exceeds the seven-active-wire limit, the whole grid is compile-only and the per-cell width records identify where routing exceeded the limit. An unconfirmed cancellation stops further sampling and preserves partial results. This workflow uses dated fake-backend properties and runs locally without an IBM account. [IBM local testing documentation](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/local-testing-mode)

### Torino sweep results

The 2026-11-11 local run used the Torino snapshot dated 2025-02-26, Qiskit 2.6.2, Runtime 0.51.0, and Aer 0.18.2. Each entry uses 128 calibration and 128 validation shots. All 12 batches completed, totaling 1,536 shots across the six circuits. All ideal distributions matched their predicted verified rates within the compilation tolerance.

| Placement | Rounds | Active wires | Depth | CZ gates | Ideal hit rate | Calibration hits | Validation hits |
|---|---:|---:|---:|---:|---:|---:|---:|
| Prior layout | 0 | 6 | 4 | 0 | 14.06% | 22 | 23 |
| Prior layout | 1 | 7 | 732 | 222 | 83.55% | 48 | 49 |
| Prior layout | 2 | 7 | 1473 | 459 | 88.17% | 27 | 27 |
| Group swap | 0 | 6 | 4 | 0 | 14.06% | 23 | 24 |
| Group swap | 1 | 7 | 754 | 225 | 83.55% | 39 | 39 |
| Group swap | 2 | 7 | 1501 | 447 | 88.17% | 29 | 29 |

Calibration selected the prior layout at one round. On validation it returned 49 of 128 valid kits (38.3%, Wilson 95% interval 30.3–46.9%). That is 20.3 percentage points above zero rounds and 17.2 points above two rounds at the same placement, with 222 CZ gates compared with 459 at two rounds. Both positive-round circuits used the same seven physical wires. The result describes these layouts, seeds, and the dated simulation. The direct classical constructor remains the first-answer baseline for this fixture. Physical performance, provider cost, and practical advantage remain unmeasured.

## Reuse a clean work qubit for the diffuser

Add `--diffuser clean-ancilla` to `constraint` or `constraint-sweep` to use the first existing work qubit in the reflection. The circuit needs at least four search qubits, a work qubit restored to zero by the oracle, and Qiskit 2.1 or later. The default `standard` diffuser is compatible with the package's Qiskit 2.0 minimum. The clean-ancilla option uses Qiskit's Khattar–Gidney multi-controlled X synthesis and returns the borrowed wire to zero in the ideal circuit. [Qiskit synthesis reference](https://quantum.cloud.ibm.com/docs/en/api/qiskit/2.1/synthesis#synth_mcx_1_clean_kg24) · [Khattar and Gidney](https://arxiv.org/abs/2407.17966)

```powershell
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known --diffuser clean-ancilla --compile
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --ibm-model torino --shots 128 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --calibration-seed 7 --validation-seed 19 --transpiler-seed 7 --diffuser clean-ancilla
```

For callers building their own oracles, the first work wire must be clean between rounds. The generic builders check circuit structure. The constraint commands also audit phase and work-qubit restoration on every input before constructing the search. Simulated errors can disturb that work wire. The clean-ancilla and standard circuits agree on clean inputs, so the noisy comparisons measure what happens when that ideal assumption is imperfect.

### Matched Torino diffuser comparison

The 2026-11-11 comparison reused the two layouts, snapshot, software versions, seeds, and 128-shot stage budgets from the standard sweep. Both versions kept seven active wires for one and two rounds. All six ideal measurement distributions matched within `1e-10` total variation distance, and zero-round seeded counts were identical. The regression tests also checked the complete ideal action on all 64 initialized-workspace inputs after one and two rounds.

| Placement | Rounds | CZ gates, standard → clean | Depth, standard → clean | Calibration hits, standard → clean | Validation hits, standard → clean |
|---|---:|---:|---:|---:|---:|
| Prior layout | 1 | 222 → 177 | 732 → 578 | 48 → 57 | 49 → 59 |
| Prior layout | 2 | 459 → 366 | 1473 → 1160 | 27 → 38 | 27 → 39 |
| Group swap | 1 | 225 → 180 | 754 → 602 | 39 → 46 | 39 → 49 |
| Group swap | 2 | 447 → 354 | 1501 → 1192 | 29 → 22 | 29 → 22 |

Both versions selected one round at the prior layout. The clean-ancilla version returned 59 of 128 valid kits in the validation batch (46.1%, Wilson 95% interval 37.7–54.7%). Its one-round validation hit count increased by 10 at each placement. At two rounds on the group-swap layout, the hit count fell by 7 despite the gate savings. These are descriptive results for the fixed seeds and historical noise model. The standard diffuser remains the default.

## Replicate the diffuser comparison across snapshots

Use `constraint-sweep --compare-diffusers` to compare both diffusers on the same oracle, two starting layouts, and one and two Grover rounds. Each invocation uses one offline IBM snapshot and reports all four comparisons. The four evaluation seeds are in standard, clean, standard, clean order. Each compiled circuit is reused for two equal-budget batches. Supply unused seeds to obtain fresh draws in another run.

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model torino --shots 128 --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --evaluation-seeds 101 103 107 109
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model jakarta --shots 128 --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --evaluation-seeds 113 127 131 137
```

The command verifies the full ideal measurement distribution of each variant and records the OpenQASM digest, active wires, native gate counts, and per-seed counts. It pools the two batches only within the same compiled circuit. Both variants must complete their batches before the cell gets a hit-rate difference. An unconfirmed cancellation stops further sampling and keeps the evidence already obtained.

For each snapshot, the eight arm rates use two-sided Clopper–Pearson intervals at 99.375% confidence each. Subtracting opposite endpoints gives simultaneous clean-minus-standard bounds for the four cells, with at least 95% family coverage under the binomial shot model for that fixed snapshot. Bounds that include zero are unresolved. The bounds describe simulator shot variability and do not measure the uncertainty of the snapshot as a model of hardware. [SciPy proportion intervals](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html) · [NIST simultaneous intervals](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm)

### Fresh-seed results on Torino and Jakarta

The 2026-11-11 runs used the Torino snapshot dated 2025-02-26 and the Jakarta snapshot dated 2024-05-27. Software versions were Qiskit 2.6.2, Aer 0.18.2, Runtime 0.51.0, and SciPy 1.18.1. Each snapshot completed 16 batches of 128 shots. Every compiled variant used seven active wires and passed full ideal distribution comparison. The hit counts below are out of 256 per variant. Rate differences and simultaneous intervals are in percentage points.

| Snapshot | Placement | Rounds | Native two-qubit gates, standard → clean | Hits, standard → clean | Difference and interval |
|---|---:|---:|---:|---:|---:|
| Torino | 1 | 1 | 222 → 177 CZ | 89 → 91 | +0.8 [−15.8, +17.3] |
| Torino | 1 | 2 | 459 → 366 CZ | 56 → 74 | +7.0 [−8.2, +22.0] |
| Torino | 2 | 1 | 225 → 180 CZ | 87 → 89 | +0.8 [−15.7, +17.3] |
| Torino | 2 | 2 | 447 → 354 CZ | 58 → 69 | +4.3 [−10.8, +19.2] |
| Jakarta | 1 | 1 | 195 → 159 CX | 54 → 63 | +3.5 [−11.2, +18.1] |
| Jakarta | 1 | 2 | 396 → 324 CX | 55 → 53 | −0.8 [−15.0, +13.5] |
| Jakarta | 2 | 1 | 201 → 165 CX | 43 → 55 | +4.7 [−9.1, +18.3] |
| Jakarta | 2 | 2 | 402 → 330 CX | 30 → 42 | +4.7 [−7.6, +16.7] |

Gate counts fell in every cell. The hit-rate intervals all include zero, and the earlier Torino one-round increases were smaller with the fresh seeds. These measurements support the gate savings for the tested circuits. The effect on noisy hit rate remains unresolved, and `--diffuser clean-ancilla` is opt-in. The replay in the second package uses the same seeds for reproducibility. It supplies no additional independent draws.




