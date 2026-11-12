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

## Benchmark unknown-count search across solution densities

Run the bundled benchmark with the Qiskit extra installed.

```powershell
python examples/benchmark_unknown_count.py --trials 64
```

The benchmark uses synthetic six-bit formulas with one alternating-bit witness, 49 witnesses from two independent disjunctions, and an unsatisfiable two-bit core. An independent closed-form reference fixes the complete witness set for each formula. The benchmark checks the audited set, the least witness from both exact classical methods, every sampled candidate, and the reported budget accounting. The default 64 seeds are 4101 through 4164. Both ideal samplers receive each seed and an eight-shot cap, first with five logical oracle calls available and then with zero.

The declarative trials rebuild and audit the oracle and include their embedded classical scan. The report separates synthesis, audit, scan, and controller timings. The standalone scan and propagation timings use the same validated formula object. A separate controller-only empty case reuses one synthesized oracle and passes no solution count or empty proof to the scheduler. Its timing covers the controller. The exact classical work counters and logical oracle calls use different units.

| Formula | Exact witnesses | Found at 8 shots, 5 queries | Found at 8 shots, 0 queries |
| --- | ---: | ---: | ---: |
| Sparse | 1 | 32 / 64 | 3 / 64 |
| Dense | 49 | 62 / 64 | 50 / 64 |
| Empty | 0 | 64 proofs, zero samples | 64 proofs, zero samples |

The default and prepared-probability samplers matched the seeded outcomes and attempts across the entire panel. The controller-only empty case returned `BUDGET_EXHAUSTED` for all 64 seeds under each cap. Under the five-query cap, the sparse search stopped at the query limit on 17 seeds, including 10 with positive query allowance remaining. The dense search stopped at the query limit twice, both with unused queries. The seeded schedule stopped when its next draw exceeded the available allowance.

In the pinned ChronoRealm Windows run with Python 3.12.15, Qiskit 2.6.2 and NumPy 2.5.4, the five-query sparse controller medians were 20.52 ms for the default sampler and 5.53 ms for prepared probabilities. Its corresponding end-to-end medians were 24.93 ms and 10.24 ms. The exact classical sparse witness medians were 0.034 ms for scan and 0.007 ms for propagation. These are warm-process local timings for this panel. They exclude installation and interpreter startup. The report's observation digest covers formulas, budgets, seeds, and deterministic results. Timings are stored separately. The results are for ideal classical simulation.

## Continue with affordable rounds

For unknown-count searches, set `--query-budget-policy affordable` to continue when the scheduled draw exceeds the remaining oracle-query allowance.

```powershell
python -m austins_algorithm search --problem mod4-range --strategy unknown --max-shots 8 --max-oracle-queries 0 --query-budget-policy affordable
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy unknown --max-shots 8 --max-oracle-queries 5 --query-budget-policy affordable
python examples/benchmark_unknown_count.py --compare-budget-policies --trials 64
```

The default `stop` policy ends the search on an unaffordable draw. Under `affordable`, the scheduler keeps each affordable draw and replaces each unaffordable draw with a uniform draw from zero through the remaining allowance. That range fits inside the current scheduling window. A separate seeded stream supplies these replacements, so the nominal draw stream and per-attempt sampling seeds retain their order. At zero remaining queries, each replacement uses zero Grover rounds. The shot cap bounds these additional samples. The policy applies to unknown-count searches. Known-count schedules use `stop`. [Boyer, Brassard, Hoyer, and Tapp's unknown-count search](https://arxiv.org/abs/quant-ph/9605034)

The Python APIs `run_grover_search` and `run_constraint_search` accept `query_budget_policy="affordable"`. An opted-in controller report includes `round_adjustments` with the attempt number, nominal draw, executed rounds, and remaining allowance before the attempt. The declarative workflow records the request on empty proofs and still proves emptiness before sampling. When a search requests compilation or a local IBM snapshot, that downstream step receives the actual last completed attempt.

The paired benchmark uses the same 64 seeds, formulas, prepared-probability sampler, and budgets for both policies. It reconstructs the seeded draws and checks that the attempts executed before the default stop match exactly. All default witnesses and empty proofs were retained in the local panel.

| Formula | Query cap | Witnesses with stop | Witnesses with affordable | Added shots | Added logical queries |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sparse | 5 | 32 | 34 | 26 | 7 |
| Sparse | 0 | 3 | 7 | 365 | 0 |
| Dense | 5 | 62 | 64 | 4 | 2 |
| Dense | 0 | 50 | 64 | 17 | 0 |

Each row covers 64 paired trials with up to eight shots per trial. Added work is summed across the panel. Zero-round attempts still consume shots and verifier calls. The controller-only empty case exhausted the shot cap under the affordable policy on both budgets. These results describe the tested ideal simulations. The separate Manila handoff check used four locally simulated snapshot shots on an adjusted zero-round circuit.

## Model the unknown-count budgets before sampling

The SDK-free planner estimates first-witness probability and expected logical work under both policies. Supply a hypothetical solution count and the caps you want to assess.

```powershell
python -m austins_algorithm unknown-count-plan --qubits 6 --solutions 1 --max-shots 8 --max-oracle-queries 5
```

Python callers can use `austins_algorithm.unknown_count_planning.plan_unknown_count_search`. The planner propagates surviving probability mass by attempt and spent queries. It shares the runtime's round-choice window and uses the ideal Grover hit probability for each executed round count. Each branch assumes a fresh uniform starting state, independent draws and measurements, and a verifier that agrees with the oracle. The solution count is a planning input. The search controller receives no count in unknown-count mode. [Boyer et al. on Grover probabilities and unknown-count search](https://arxiv.org/abs/quant-ph/9605034)

The model accounts separately for a witness, shot-cap exhaustion, and an unaffordable draw under `stop`. It includes the successful shot in expected work and charges queries only for executed rounds. The report also estimates expected verifier calls and redraws. An assumed count of zero gives zero ideal success probability. The declarative workflow establishes emptiness separately through its exhaustive audit.

The existing paired policy benchmark now includes the `ideal_budget_model` comparison. It compares the ideal expectations with the fixed seed observations, using the controller-only arm for the empty case and retaining the separate declarative proof result.

| Formula and query cap | Modeled hit probability, stop to affordable | Observed witnesses, stop to affordable |
| --- | ---: | ---: |
| Sparse, 5 queries | 54.33% to 56.29% | 32 to 34 of 64 |
| Sparse, 0 queries | 2.99% to 11.84% | 3 to 7 of 64 |
| Dense, 5 queries | 98.61% to 99.80% | 62 to 64 of 64 |
| Dense, 0 queries | 86.72% to 99.999% | 50 to 64 of 64 |

For the sparse five-query case, the model expects 5.78 to 6.21 shots and 3.31 to 3.42 logical queries per trial. The modeled expectations and observed seed panel have different evidence bases. The model integrates ideal outcome probabilities and the panel records finite seeded simulations. The comparison is descriptive and covers neither device noise nor physical cost.

## Select budgets for a modeled witness target

Supply candidate caps, a witness probability target, and the solution counts the plan should cover. The SDK-free selector checks every combination under both policies.

```powershell
python -m austins_algorithm unknown-count-select --qubits 6 --solutions 1 49 --target-probability 0.55 --shot-caps 1 2 3 4 5 6 7 8 --query-caps 0 1 2 3 4 5
```

Python callers can use `austins_algorithm.unknown_count_planning.select_unknown_count_budgets`. A candidate meets the target when it does so under every supplied count. No likelihood weights are assigned to the counts. Each row gives the probability and logical work under each count. The worst expected shots and worst expected queries are computed separately and can come from different counts.

The feasible work frontier shows choices for which neither expected-work cost can improve without increasing the other. If several choices have the same modeled costs, the frontier shows one representative and the full list retains the others. The recommendations give the lowest-cost frontier choice for each objective. Use the selected policy and caps when running the search. Caps are hard runtime limits and the reported work figures are ideal expectations.

| Search bits and solution counts | Target | Selection |
| --- | ---: | --- |
| 6 bits, 1 or 49 solutions | 55% | Affordable, 8 shots and 5 queries. Only one of the 96 candidates qualifies. Worst-case modeled success is 56.2942%. |
| 6 bits, 1 or 49 solutions, same caps | 60% | No candidate qualifies. The same affordable plan gives the best attainable probability within the grid. |
| 2 bits, 1 solution, 1–10 shots and 0–3 queries | 90% | Stop with caps of 4 shots and 1 query minimizes expected shots. Affordable with caps of 9 shots and 0 queries minimizes expected queries. |

The `UNKNOWN_COUNT_TARGET_INFEASIBLE` report gives the best modeled candidate inside the supplied grid and leaves both recommendations empty. Increasing the query cap can lower the modeled witness probability when it changes which rounds the scheduler can execute. For 2 bits and 3 solutions with 2 shots, affordable success is 93.75% at zero queries and 84.375% at one. A 100% target requires every search state to be marked under these bounded policies. The report uses the modeled failure tail for high targets because a displayed hit probability can round to 100%.

The selector reuses each policy recurrence across the requested shot caps. It accepts up to 8,192 candidate and count combinations, including both policies, and rejects grids above its conservative bound of 250 million recurrence updates. Its result applies to the supplied caps and counts under ideal independent sampling. Device noise, compiler cost, and physical timings require separate evidence.

## Validate selected budgets on fresh seeds and counts

Run the local holdout benchmark with the optional Qiskit stack installed.

```powershell
python examples/benchmark_unknown_count.py --validate-budget-selection --trials 128
```

The benchmark freezes two grids and selects the plans before sampling. It uses disjoint seed blocks across cases, outside the earlier benchmark's range. Two plans for the same case share a seed block for comparison. A short run uses the first seeds of each block, so rerunning at a larger trial count extends the same panel. Each trial is a full invocation of the unknown-count controller. Each witness is checked against an exhaustive unsigned-integer threshold reference. The oracle is audited once per case, and the actual count is never passed to the controller.

| Selection grid | Declared counts | Held-out counts | Target |
| --- | --- | --- | ---: |
| Six bits, 1–8 shots and 0–5 queries | 1, 49 | 0, 2, 16, 32, 50, 64 | 55% |
| Two bits, 1–10 shots and 0–3 queries | 1 | 2, 3 | 90% |

The report records each selected cap and policy, raw search outcomes, modeled probabilities, observed witness rates, and mean logical work. The two-bit comparison changes both policy and caps. Its outcome cannot isolate the effect of policy alone. One row covers the empty controller control, which exhausts its search budget. The declarative route can prove an empty constraint before sampling.

Each interval is a Clopper–Pearson binomial interval with Bonferroni adjustment across the 14 case-and-plan arms at 95% family confidence. This interpretation assumes independently distributed full-search outcomes within an arm. The run uses fixed pseudorandom seeds. Intervals entirely at or above the target, entirely below the target, and overlapping the target are reported separately. A near-target modeled result may remain unresolved in a finite panel. The observation digest includes the seed assignments and trial outcomes. Local synthesis, audit, and sampling times are reported separately.

In a local run with 128 trials per arm, all 14 intervals contained their respective ideal predictions. Nine intervals were entirely at or above the target. The empty controller's interval was below target, and four intervals overlapped their targets.

| Case and selected plan | Ideal witness probability | Witnesses in 128 trials | Interval compared with target |
| --- | ---: | ---: | --- |
| Six bits, 1 solution, affordable 8 shots / 5 queries | 56.29% | 66 | Overlaps 55% |
| Six bits, 2 held-out solutions, same plan | 81.80% | 106 | At or above 55% |
| Two bits, 2 held-out solutions, stop 4 shots / 1 query | 87.50% | 115 | Overlaps 90% |
| Two bits, 2 held-out solutions, affordable 9 shots / 0 queries | 99.80% | 128 | At or above 90% |

The six-bit sparse result leaves its 55% target unresolved. These results cover the fixed threshold formulas and the ideal sampler used here.

## Compare complete searches under IBM snapshot noise

Install the optional `ibm` extra, then run the matched local panel.

```powershell
python examples/benchmark_unknown_count.py --compare-budget-snapshot --snapshot-model jakarta --trials 128
```

Jakarta's seven-wire model can run the two- and six-bit threshold cases. Use `--snapshot-model manila` to repeat the two-bit cases. Its report marks the six-bit cases as outside the five-wire target width. Each case uses an ancilla-free threshold oracle checked against the same integer truth table used in the ideal holdout. The round budgets and selected policies come from the same two selection grids.

The panel compiles each needed Grover round count once per case, using a shared initial layout for that case. Each compiled circuit has an independently verified OpenQASM round trip. The ideal lane uses its compiled measurement probabilities. The noisy lane uses Qiskit Aer's compact density-matrix evolution and calibrated local readout probabilities. Both lanes draw one bitstring per executed attempt, using matching schedule and measurement seeds. Each lane verifies its own measured bits and stops on its own first witness or budget boundary. The model assumes fresh preparation on every attempt and static snapshot channels. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider) · [Qiskit Aer simulator documentation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

The report separates predicted full-search probabilities from seeded outcomes. It records each circuit's QASM digest, mapped wires, depth, and compiled operations. The nominal two-qubit workload multiplies each circuit's gate count by the number of completed attempts at that round count. Compilation, density preparation, and adaptive sampling times are separate local CPU timings. Density preparation is shared across trials, so the sampling timings reflect draws from prepared distributions. The snapshot was recorded in the past. These results describe the tested noise model and carry no provider usage or physical runtime measurement.

Python callers can use `austins_algorithm.unknown_count_snapshot.compare_unknown_count_snapshot` with a small reversible phase oracle, deterministic verifier, named budgets, and distinct trial seeds. The runner audits the oracle before executing the controller, caps each request at four plans and 128 trials per plan, and reports width limits or a density-preparation timeout before sampling. A completed comparison contains the raw trial traces and paired witness outcomes. If a density preparation times out, the example preserves the reported failure and marks later cases as unattempted.

## Reuse verified snapshot round profiles

Add `--reuse-round-profiles` to share eligible profiles across cases within one benchmark process.

```powershell
python examples/benchmark_unknown_count.py --compare-budget-snapshot --snapshot-model jakarta --trials 128 --reuse-round-profiles
```

Python callers can pass the same `austins_algorithm.unknown_count_snapshot_cache.SnapshotRoundProfileCache` instance to repeated calls to `compare_unknown_count_snapshot` or `run_selection_snapshot_benchmark`. Each comparison still audits its own oracle and verifier, determines its starting layout, and runs the requested seeds and budgets. The comparison also accepts `initial_layout` to pin an ordered list of target wires.

A profile matches the serialized logical measurement circuit, ordered layout, compiler settings and versions, snapshot identity, and effective target calibration. The cache accepts a bounded stock-gate vocabulary and runs other circuits through the regular preparation path. A matching zero-round circuit can share its outcome distribution across different predicates. The current predicate's witness probability is recalculated from that distribution. [Qiskit QPY documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qpy)

The default cache retains up to 64 completed profiles and 8 MiB of serialized evidence. It evicts the least recently used entries, keeps preparation serialized per cache, and stores detached compile evidence and probability tables. Each report records cache keys, hits, misses, bypasses, and local preparation times in its timing section. A failed preparation publishes no new profiles. An unconfirmed simulator cancellation disables that cache instance. Cache contents live for the life of the instance within the process.

In the local 128-seed Jakarta panel, five round lookups reused profiles across cases during the first pass. The full warm pass reused all 38, reproducing the archived observation digest. The ChronoRealm copy took 60.94 seconds for the first pass and a median of 0.55 seconds across three warm passes. These timings measure the local benchmark with the cache held in memory.

## Export and verify a portable snapshot profile

The compact profile bundle keeps the compiled round fingerprints, layout and measurement maps, gate counts, complete noisy outcome weights, source budgets, audited case roles, and source seeds. It omits trial traces, timing data, ideal distributions, and the original candidate grid. Both the exporter and verifier run with standard-library Python.

```powershell
python -m austins_algorithm unknown-count-snapshot-profile-export jakarta-panel.json |
  Set-Content -Encoding utf8 jakarta-profile.json
python -m austins_algorithm unknown-count-snapshot-profile-verify jakarta-profile.json --source-panel jakarta-panel.json
```

The exporter checks the full panel's observation digest before projection. Standalone verification checks the bundle's own digest, retained profile structure, and coverage. Use `--expected-bundle-sha256` to pin a separately recorded bundle digest. Providing `--source-panel` also rebuilds the projection from that panel and checks for an exact match. That receipt records the supplied source file hash separately. The bundle records the upstream observation digest, which is independent of local timing values. The hashes establish agreement with the supplied artifacts.

The `unknown-count-snapshot-select` command accepts a full panel or a bundle. It uses the declared cases for selection and preserves source plan order and source seeds for fresh validation. For Manila, the bundle records the six-bit cases blocked by target width. A selection request for those cases still fails with the same missing-profile diagnostic.

In the saved 128-seed local panels, Jakarta exported from 5,682,416 bytes to 109,908 bytes and Manila from 1,964,883 bytes to 29,620 bytes. Five selection grids reproduced their candidate forecasts, recommendations, and held-out trace digests from the bundles.

Python callers can use `export_snapshot_profile_bundle` and `verify_snapshot_profile_bundle` from `austins_algorithm.unknown_count_snapshot_profile`.

## Select budgets from saved snapshot distributions

Save the output of the snapshot comparison as UTF-8 JSON. The selection command reads that file or a verified profile bundle with standard-library Python. It can reuse the same compiled circuits and snapshot noise distributions for a different budget grid.

```powershell
python -m austins_algorithm unknown-count-snapshot-select jakarta-panel.json --suite sparse_and_dense --target-probability 0.12 --shot-caps 1 2 3 4 5 6 7 8 --query-caps 0 1 2 3 4 5 --validate-trials 128
```

The selector verifies the panel's semantic digest, threshold witness audits, calibration identity, layout and measurement maps, and probability mass. The result records the file digest, compiler versions, and declared cases' circuit fingerprints. An independently recorded digest can be supplied with `--expected-observations-sha256`. The recorded hashes establish consistency with the saved panel. The command relies on the panel's reported compile and noise-model evidence.

A plan must meet the target under every declared solution count. The recommendations minimize three separate worst-case expectations: shots, logical oracle queries, and nominal compiled two-qubit gates. A two-qubit operation is charged for every reached attempt at that round count, including the successful attempt. The available round profiles bound the candidate grid. A missing required round returns an error.

With `--validate-trials`, the selector freezes its recommendations, then replays them and the source ideal budgets on new seeds at declared and held-out counts. The replay uses the saved bitstring distributions and the audited threshold predicate. It preserves the original compilation and calibration, and reports seed lists, observed work, trace digests, and simultaneous binomial intervals under the independent-trial model. It exercises the controller on the saved distributions. Further calibration evidence would require a separate experiment.

| Saved Jakarta case | Requested target | Modeled result |
| --- | ---: | --- |
| Six bits, 1 or 49 solutions, 1–8 shots and 0–5 queries | 55% | Infeasible. Best attainable worst-case success is 12.4955%. |
| Same six-bit grid | 12% | Affordable with 8 shots and 1 query clears the target at 12.0752%. It uses about 320 expected nominal two-qubit gates in the worst case, compared with 1,397 for the original 8-shot, 5-query plan. |
| Two bits, 1 solution, 1–9 shots and 0–1 queries | 92% | Stop at 4 shots and 1 query minimizes expected shots. Affordable at 9 shots and 0 queries minimizes expected logical queries and compiled gates. |

In the six-bit sparse replay, the 8-shot, 1-query plan found 16 of 128 witnesses. The source 8-shot, 5-query plan found 17. Both intervals overlapped the 12% target. The one-query plan used 40,172 nominal two-qubit gates in the replay, compared with 172,648 for the source plan. At the held-out count of two solutions, it found 33 of 128 witnesses.

The report also models a uniformly random order of distinct classical verifier calls up to the same shot cap. For one solution among 64 states, eight distinct checks give exactly 12.5% success with no phase-oracle queries. This is a bounded reference for the synthetic threshold case.

## Compare snapshot profiles on a common grid

Compare two verified bundles with one grid and a common replay schedule. This command runs with standard-library Python.

```powershell
python -m austins_algorithm unknown-count-snapshot-profile-compare jakarta-profile.json manila-profile.json --suite work_tradeoff --target-probability 0.93 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1 --validate-trials 128
```

The report gives the candidates feasible on each side and on both. Shared recommendations minimize the worst expected work across both snapshots and every declared count. The report also records calibration digests, compiler seeds and versions, anchor layouts, compiled round fingerprints and wire maps, and changes in the outcome distributions.

The same search width, declared counts, case roles, and constraint fingerprints are required. Missing or blocked declared profiles produce an explicit unavailable report. The coverage section identifies missing, blocked, or too-short held-out profiles. Validation freezes the union of recommended budgets and any best-attainable fallback, then replays each budget on both sides using the same seeds for each matched case. Those seed blocks are disjoint from both source suites. The paired counts describe the chosen seed panel. The simultaneous intervals cover the individual hit rates under independent trials within each arm.

On the saved two-bit grid at a 93% target, 11 of 36 candidates qualify on both snapshots. Jakarta selects stop at 4 shots and 1 query. Manila and the shared ranking select affordable at the same caps, with 93.36% worst-case modeled success. The compared Jakarta and Manila profiles retain matching circuit fingerprints and wire maps and different calibration fingerprints. In a separate Jakarta comparison with a fixed starting layout of `[0, 2]`, the calibration fingerprint stayed the same. The one-solution, one-round compiled circuit rose from two to five two-qubit gates. These are comparisons of the retained local models and compiler evidence.

Python callers can use `compare_snapshot_profile_bundles` from `austins_algorithm.unknown_count_snapshot_profile_comparison`.

## Plan a starting layout and search budget together

Capture a separate panel for each ordered layout on one named snapshot. Use the same suite and compiler seed throughout. The following example captures one Jakarta layout, then exports its portable profile.

```powershell
python examples/benchmark_unknown_count.py --compare-budget-snapshot --snapshot-model jakarta --snapshot-suite work_tradeoff --snapshot-layout 0 1 --trials 32 |
  Set-Content -Encoding utf8 jakarta-0-1-panel.json
python -m austins_algorithm unknown-count-snapshot-profile-export jakarta-0-1-panel.json |
  Set-Content -Encoding utf8 jakarta-0-1-profile.json
```

Repeat the capture for `0 2` and `1 2`, then run the SDK-free planner.

```powershell
python -m austins_algorithm unknown-count-snapshot-layout-plan jakarta-0-1-profile.json jakarta-0-2-profile.json jakarta-1-2-profile.json --suite work_tradeoff --target-probability 0.9325 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1 --validate-trials 128
```

The planner requires distinct fixed ordered layouts, one snapshot calibration, one software stack and compiler seed, and matching workload identities. Each feasible candidate fixes a layout and budget that meets the target for every declared count. The report ranks candidates separately by worst-case expected shots, queries, and nominal compiled two-qubit gates. It identifies layouts missing declared profiles and uses ordered layout as the final tie-breaker.

Validation freezes the selected budgets, or the best attainable budget when the target is infeasible, and replays them across eligible layouts with matching case seeds disjoint from the source suites. It includes declared and held-out cases with common coverage and lists any omitted holdouts. Forecasts and replay estimates are tied to these saved distributions. The two-qubit gate counts are nominal compiled work.

On the tested Jakarta grid, 34 of 108 layout-budget pairs qualified at a 93.25% target. The fewest-expected-shots choice was `[1, 2]` with `stop` at 4 shots and 1 query. The fewest-expected-queries and gates choice was `[0, 1]` with `affordable` at the same caps. The held-out two-solution case returned 106 of 128 witnesses for the former plan and 117 for the latter on their selected layouts. The former interval was below the target and the latter overlapped it.

Python callers can use `plan_snapshot_layout_budgets` from `austins_algorithm.unknown_count_snapshot_layout`.

## Check layout and budget sensitivity to round probabilities

Add `--probability-radius` to a layout plan to evaluate an absolute change in each compiled round's verified-hit probability. A radius of `0.0005` means 0.05 percentage points. The bounds are clipped to the probability range. Empty and all-marked predicates retain exact zero and one probabilities.

```powershell
python -m austins_algorithm unknown-count-snapshot-layout-plan jakarta-0-1-profile.json jakarta-0-2-profile.json jakarta-1-2-profile.json --suite work_tradeoff --target-probability 0.9325 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1 --probability-radius 0.0005 --validate-trials 128
```

The controller chooses its round sequence from the seed and budget state. For any fixed sequence, raising hit probabilities can only make the first witness arrive earlier. The simultaneous lower endpoints therefore give the least success and greatest expected shots, queries, and nominal gate work. The upper endpoints give the opposite bounds. The report preserves the nominal results and ranks plans that meet the target at the lower endpoints. Candidates can qualify throughout the bound, qualify for only some probabilities, or fall short throughout it.

At the 93.25% target, the nominal fewest-shots choice on layout `[1, 2]` with `stop` spans **93.2497% to 93.3265%** at radius 0.0005. It leaves the robust set. The planner selects `affordable` at the same caps and layout for fewest shots. At the 99.4% target, the sole nominal candidate also leaves the robust set at this radius, so the report shows the best attainable lower endpoint and the remaining possible candidates.

Seeded validation replays the frozen selected budgets on the nominal saved outcome distributions. The supplied radius defines a hypothetical bound. Statistical intervals apply to the nominal replay. Calibration drift remains unmeasured.

### Compare several radii and find each fixed plan's tolerance

Use `--probability-radii` with up to 16 values. The planner sorts the values and merges duplicates. It loads the same profiles and computes the nominal grid once. Each radius has its own classification counts, recommendations, and best attainable plan. The main candidate report and optional seeded replay use the largest requested radius for selection. Replay still samples the nominal saved distributions.

```powershell
python -m austins_algorithm unknown-count-snapshot-layout-plan jakarta-0-1-profile.json jakarta-0-2-profile.json jakarta-1-2-profile.json --suite work_tradeoff --target-probability 0.9325 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1 --probability-radii 0 0.0001 0.0005 0.001 0.01 --validate-trials 128
```

Under `sensitivity_grid.fixed_plan_tolerances`, each layout, policy, and budget has one record. `BRACKETED` gives a passing lower radius and a failing upper radius within `2^-30` of each other using the same model and target. `NOMINAL_TARGET_MISSED` gives a failing zero-radius endpoint. `FULL_RADIUS_DOMAIN` records that radius one still passes. Tolerance uses the entire radius domain and may extend beyond the sampled grid. The aggregate modeled transition limit applies to the sweep and all tolerance probes together.

The saved Jakarta case puts the tolerance of the `[1, 2]` four-shot, one-query `stop` plan near `0.000495645` for a 93.25% target. For a 99.4% target, the only nominally feasible plan uses `[1, 2]`, nine shots, one query, and `affordable`. Its tolerance is near `0.000333387`. These are floating-point model results for the saved profiles. The thresholds are conditional on the supplied independent probability bounds.

## Compare two dated IBM snapshots on the same circuits

Use `snapshot-calibration-compare` with local IBM configuration and properties JSON files and their SHA-256 digests. Each side must record the same device and width with an explicit calibration time. Supply the earlier calibration first. Use one physical wire per oracle wire, including any clause flags. Install the optional SDK stack with `python -m pip install ".[ibm]"`.

For a two-bit test that marks `00`, save `{"schema_version":1,"qubits":2,"clauses":[[-1],[-2]]}` as `threshold-zero.json`. Place the [2021 Jakarta configuration and properties](https://github.com/Qiskit/qiskit-ibm-runtime/tree/2a396172c25aa2971e99b434b430177745878a53/qiskit_ibm_runtime/fake_provider/backends/jakarta) in `earlier/` and the [2024 Jakarta configuration and properties](https://github.com/Qiskit/qiskit-ibm-runtime/tree/7b9ecb722f55ecb3aea01d83e6d406a161efc770/qiskit_ibm_runtime/fake_provider/backends/jakarta) in `later/`. Use the original raw file bytes.

```powershell
python -m austins_algorithm snapshot-calibration-compare threshold-zero.json `
  --earlier earlier/conf_jakarta.json earlier/props_jakarta.json 23c9edfb7f675241a1604bac3cee34ff9139acae016d061f0adee1ea4743e455 c984a51ad8968c24abe62c5762b28ca8e674e1474c76236e855714ef324a69b1 `
  --later later/conf_jakarta.json later/props_jakarta.json 15b25bc0c7fdf72a4db606cfd20dfc4363a20c5233c6325cfd3d8115ea859d0b 29a90998e66c95580125b41456202ba0893a2ec37f467d15caa6aa8be3b34b43 `
  --initial-layout 0 1 --max-rounds 1
```

The command audits the constraint oracle and compiles rounds zero through the requested cap once on the later target. It verifies the OpenQASM round trip and checks every compiled instruction against both targets on the exact same wires. Each row records the common circuit and measurement map, the used calibration parameters, Aer readout assignment matrices, the two outcome distributions, their signed differences, and total variation distance. The matrix rows are prepared bits and the columns are reported bits. The maximum absolute hit difference is a descriptive value for the tested circuit and snapshot pair.

For the example on layout `[0, 1]`, one-round modeled witness probability rose from **91.8468%** in July 2021 to **94.2226%** in May 2024. The archived configuration and backend version both changed. The simulations use static gate noise and the readout channels derived from each SDK target. In the tested SDK, those channels use symmetric assignment matrices derived from aggregate error. The outputs describe the two archived snapshot models. Live-device behavior and future calibration remain unmeasured.

### Plan fixed search budgets across the two dates

Pass one completed comparison report per layout and the same constraint to `snapshot-calibration-budget-plan`. The planner runs with standard-library Python. Each report must use the same dated pair, SDK versions, compiler seed, and optimization level. Use `--expected-report-sha256` with a known file digest for each report in input order when you want to pin the exact saved bytes.

```powershell
python -m austins_algorithm snapshot-calibration-budget-plan threshold-zero.json jakarta-0-1.json jakarta-0-2.json jakarta-1-2.json --target-probability 0.9325 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1
```

The planner exhaustively checks the supplied constraint against the saved witness list, checks the reported circuit and probability fields for consistency, and derives hit and miss probabilities from the complete outcome distributions. Each candidate uses one layout, policy, and set of caps on both dates. Its joint success is the lower forecast, and each expected work figure is the larger forecast for that resource. The joint recommendations require both dates to pass. The report also shows each date's selection, signed changes, and a classical reference that tests distinct candidates uniformly in a random order. Layouts with too few saved rounds for the requested grid are marked unavailable.

For the example with target **93.25%**, the shared recommendation for shots, queries, and nominal two-qubit gate work is layout `[1,2]` with `affordable`, four shots, and one query. It forecasts **93.7413%** success in 2021 and **93.9471%** in 2024. At **99.4%**, the nine-shot, one-query plan on that layout qualifies only on the later date. The forecast assumes independent draws from the saved models. A report pin verifies the saved bytes. Hardware performance and future calibration remain unmeasured.

### Replay selected budgets with matched seeds

Add `--validate-trials 128` to the dated budget planner for an SDK-free replay. It freezes the unique shared and date-specific recommendations and keeps their objective labels. A selection with no qualifying plan contributes its best attainable choice. Each frozen plan runs on both dates with the same seed block. Both arms verify sampled bitstrings with the supplied constraint. The report records hits, work, paired outcomes, and a trace digest for each arm.

The classical reference tests distinct candidates in a seeded random order, stops at the first witness, and uses one result per selected shot cap. Smaller caps use prefixes of the same candidate order within the report. The attempt cap lets you compare witness outcomes at a common bound. Shots, logical queries, compiled gates, and classical verifier calls retain their own units.

At target **93.25%**, the shared four-shot plan on `[1,2]` found **117/128** witnesses in the 2021 replay and **120/128** in the 2024 replay. The classical reference found **128/128** using **302** verifier calls. The dated replay intervals overlapped the target. The intervals are exact binomial marginals with a Bonferroni correction across the reported arms. They describe draw variability in the saved models. The paired outcomes describe the chosen shared seed coupling. Hardware behavior and calibration uncertainty remain unmeasured.

### Capture and plan a multi-layout study in one command

Use `snapshot-calibration-study` with the archived inputs above and repeat `--initial-layout` for each ordered layout. The study computes the round range reachable under the largest shot and query caps, captures that range on each layout, and passes completed captures to the planner. The output includes the capture reports, their canonical JSON SHA-256 digests, layout coverage, and the nested planning and replay results.

The study verifies and decodes the archive pair once, then builds and audits the constraint oracle once. It checks every requested layout against the archived device width and the oracle's full wire count before compiling or starting density simulations. Each layout still gets its own compiled and simulated circuits. Shared preparation lasts for the current invocation. The capture reports preserve their existing format and digests.

Layouts in the same study can share static gate and readout channels on the same dated target. The cache uses an exact active-wire union first. A smaller union can also use channels from the smallest cached union containing all its wires. The projection keeps channels whose physical wires are all in the smaller union and preserves directed gate order while remapping the indices. Full-device channels can supply a subset when the target has no calibrated global operations. Gate channels are read-only during the study. Each layout uses fresh noise models, simulators, and independent readout buffers. Physical gate direction and the verified measurement map still determine how each circuit is simulated.

The existing [diffuser smoke constraint](examples/diffuser-smoke-regression/constraint.json) has four search bits and one clause flag. Its witnesses are `0010`, `0110`, `1010`, and `1110`. Run it on the same Jakarta archives using two different orderings of the same five physical wires.

```powershell
python -m austins_algorithm snapshot-calibration-study examples/diffuser-smoke-regression/constraint.json `
  --earlier earlier/conf_jakarta.json earlier/props_jakarta.json 23c9edfb7f675241a1604bac3cee34ff9139acae016d061f0adee1ea4743e455 c984a51ad8968c24abe62c5762b28ca8e674e1474c76236e855714ef324a69b1 `
  --later later/conf_jakarta.json later/props_jakarta.json 15b25bc0c7fdf72a4db606cfd20dfc4363a20c5233c6325cfd3d8115ea859d0b 29a90998e66c95580125b41456202ba0893a2ec37f467d15caa6aa8be3b34b43 `
  --initial-layout 0 1 2 3 5 --initial-layout 1 2 3 5 0 `
  --target-probability 0.9325 --shot-caps 1 2 3 4 5 6 7 8 9 --query-caps 0 1 2 3 --validate-trials 128
```

At the **93.25%** target, 18 of 144 plans met the target on both dates. The fewest-expected-shots choice used six shots and three queries on `[0,1,2,3,5]`, with a worst-date forecast of **93.6285%**. The lowest expected gate work used eight shots and one query on that layout. On the common zero-or-one-query grid, the second constraint's fewest-expected-shots choice used eight shots. The earlier two-bit example selected four shots on its tested layouts. These are workload-specific results under the archived simulation models and tested layouts.

### Reuse compiled rounds across dated studies

Pass the same `SnapshotCalibrationCompilationCache` to multiple Python calls in one process. This works with `run_snapshot_calibration_study` and `compare_snapshot_calibrations`. For the four-bit example, start with the smaller query range and then extend it. Here `earlier` and `later` are the `SnapshotCalibrationFiles` objects for the pinned archives above.

```python
from austins_algorithm.constraints import load_constraint
from austins_algorithm.snapshot_calibration_compilation_cache import SnapshotCalibrationCompilationCache
from austins_algorithm.snapshot_calibration_study import run_snapshot_calibration_study

constraint = load_constraint("examples/diffuser-smoke-regression/constraint.json")
cache = SnapshotCalibrationCompilationCache()
options = dict(
    initial_layouts=[(0, 1, 2, 3, 5), (1, 2, 3, 5, 0)],
    target_probability=0.9325,
    shot_caps=range(1, 10),
    compilation_cache=cache,
)
short = run_snapshot_calibration_study(constraint, earlier, later, query_caps=(0, 1), **options)
extended = run_snapshot_calibration_study(constraint, earlier, later, query_caps=(0, 1, 2, 3), **options)
print(cache.stats())
```

The key binds the logical circuit's QPY fingerprint, ordered initial layout, later archive hashes, effective compiler target, seed, compiler settings, and SDK and importer provenance. Unrecognized or altered operations bypass the cache. Both archive pins, the oracle, the compiled instruction support and wire maps, and both dates' density simulations are checked on every call. Changing only the earlier archive can reuse the later target's compilation while recalculating that date's probabilities. Budgets, replay, and report digests are produced for each request.

The cache retains detached copies of verified physical circuits, OpenQASM, and compiler reports from completed layouts. Its least-recently-used limits default to 64 entries and 8 MiB of retained OpenQASM and report bytes. Circuit objects add memory beyond the reported byte count. Concurrent callers can compile the same miss before either publishes. The cache retains no jobs or noise models, and incomplete layouts add no entries. The default Python and CLI paths compile fresh.

#### Carry compiled rounds into a separate process

Save a bundle after a study to reuse those compiled rounds in later CLI invocations. In this PowerShell example, `$studyArgs` contains the constraint path, the pinned archive arguments, layouts, and budget options from the study above. Choose a new file in an existing local directory.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs --save-compilation-bundle .build/jakarta-rounds.json
$bundlePin = (Get-FileHash -Algorithm SHA256 .build/jakarta-rounds.json).Hash.ToLowerInvariant()
python -m austins_algorithm snapshot-calibration-study @studyArgs --load-compilation-bundle .build/jakarta-rounds.json $bundlePin
```

Use a bundle you generated or received from a trusted producer, with an expected SHA-256 recorded separately. The pin checks the whole file before decoding. The bundle records QPY physical circuits, their global phases, compiler reports, and software versions. Loading requires the recorded software versions and current QPY format. For each matching round, the study checks the current logical circuit, ordered physical layout, target instructions, original and compiled ideal probabilities, measurement map, and regenerated OpenQASM bytes. The pinned compiler report supplies the original OpenQASM import evidence. Both dated calibrations are checked and simulated fresh. [IBM QPY format and compatibility](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qpy)

An unmatched round compiles normally. To extend a shorter study, use a wider query range and supply both flags with a new save destination. The new bundle contains the completed rounds from the current request that fit the cache limits. Existing files are protected from replacement. Saving requires a filesystem that supports hard links. The CLI uses a 256-entry cache and a 32 MiB file limit. Study JSON keeps the same format.

In Python, call `cache.save_bundle(path)` for the file and its SHA-256. In another process use `SnapshotCalibrationCompilationCache.load_bundle(path, expected_sha256=pin)` and pass the returned cache to the study or direct comparison. Only completed rounds resident in the cache are saved.

#### Check bundle coverage before a study

Inspect a pinned bundle with standard-library Python to see its recorded software versions, saved keys, and each key's recorded layout, compiler seed, and OpenQASM digest.

```powershell
python -m austins_algorithm snapshot-calibration-bundle-inspect .build/jakarta-rounds.json --expected-sha256 $bundlePin
```

With the recorded SDK versions installed, use the actual study arguments to check which requested layouts and rounds match the bundle.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --load-compilation-bundle .build/jakarta-rounds.json $bundlePin --check-compilation-bundle
```

The check verifies both archive pins, audits the logical oracle, and uses the same round range, ordered layouts, and compiler keys as the study. Each cell reports `PRESENT`, `ABSENT`, or `UNKEYABLE`. The report includes the compiler scope digest and unused bundle keys. A present key establishes coverage. Running the study validates the stored physical circuit and simulates both dates. The preflight performs no stored QPY decode, transpilation, snapshot simulation, or bundle writes.

In Python, use `inspect_snapshot_calibration_compilation_bundle` from the `snapshot_calibration_compilation_bundle` module for the inventory. Use `check_snapshot_calibration_compilation_coverage` from `snapshot_calibration_study` with the study arguments and a cache loaded from the pinned bundle for request coverage.

#### Compare bundle coverage for one study

Repeat `--compare-compilation-bundle` with two to eight trusted files and their distinct SHA-256 pins. Supply the same study arguments you intend to run.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --compare-compilation-bundle .build/jakarta-prefix.json $prefixPin `
  --compare-compilation-bundle .build/jakarta-rounds.json $bundlePin
```

The comparison checks each pinned file, then builds the requested compiler key grid once for all candidates with matching software. Each candidate reports covered and missing cells and unused keys. Ranking uses the number of requested cells present in the bundle. Ties retain all matching candidates in input order. Compatible bundles with zero matches and software-incompatible bundles have separate outcomes.

Use the ranked bundle's file and pin with `--load-compilation-bundle` to run the study. The comparison measures key coverage. The study verifies restored physical circuits, compiles missing rounds, and simulates both dates. Candidates are evaluated individually.

The Python entry point is `compare_snapshot_calibration_compilation_coverage` in `snapshot_calibration_study`. Pass the study arguments and a sequence of `(path, sha256)` pairs as `bundles`.

#### Compose complementary compilation bundles

Use standard-library Python to combine two to eight independently pinned bundles with the same recorded software versions. Choose a new output file and use the SHA-256 pins recorded for each source.

```powershell
python -m austins_algorithm snapshot-calibration-bundle-compose .build/jakarta-combined.json `
  --bundle .build/jakarta-layout-1.json $firstPin `
  --bundle .build/jakarta-layout-2.json $secondPin
```

Composition copies the saved QPY payloads, global phases, and compiler reports. Identical entries sharing a key are kept once. When entries with the same key differ, the command returns a conflict report with the source pins and differing fields, exits with status 1, and leaves the destination path untouched. Successful writes are atomic on filesystems that support hard links. The result includes the composed file's SHA-256 for later inspection, preflight, or study.

Composition verifies the source pins and envelope contents using the standard library. The study checks the selected physical circuits and runs both dated simulations. The Python entry point is `compose_snapshot_calibration_compilation_bundles` in `snapshot_calibration_compilation_bundle`.

#### Verify differing QPY rows before composition

Use the recorded SDK versions and the intended study arguments to resolve overlapping rows whose saved phase and compiler report agree. Supply two to eight trusted inputs with their independently recorded pins and choose a new destination.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --resolve-compilation-overlap .build/jakarta-wheel.json $wheelPin `
  --resolve-compilation-overlap .build/jakarta-sdist.json $sdistPin `
  --save-compilation-bundle .build/jakarta-resolved.json
```

Each eligible conflict must match a unique layout and round in the request. The command restores each candidate against the current logical circuit and the pinned targets, then compares the ordered physical operations, gate matrices, labels, measurement and routing mappings, and phase. When they agree, the original row from the first source in SHA-256 order is retained. The report records the source pins and physical verification evidence. Conflicting report or phase evidence, an unmatched key, or a failed physical check leaves the destination untouched.

The command prepares the study request only when an overlap needs QPY verification. It performs no transpilation or snapshot simulation. Run the study with `--load-compilation-bundle` and the new pin to verify the compiled rounds again and simulate both calibrations. The Python entry point is `compose_snapshot_calibration_compilation_for_study` in `snapshot_calibration_study`.

#### Verify or replay an overlap receipt

Save the successful overlap resolution command's JSON output as a UTF-8 receipt. Keep the receipt's SHA-256 alongside the independently recorded source and output bundle pins.

```powershell
python -m austins_algorithm snapshot-calibration-overlap-receipt-verify .build/resolution-report.json `
  --expected-receipt-sha256 $receiptPin --output-bundle .build/jakarta-resolved.json $resolvedPin `
  --bundle .build/jakarta-wheel.json $wheelPin --bundle .build/jakarta-sdist.json $sdistPin
```

The SDK-free check reconstructs the exact output from the pinned source rows, verifies each selected source and QPY payload hash, and checks consistency of the recorded study fields. The physical-signature digests in that result describe the producer's recorded checks.

With the recorded SDK versions installed, supply the same study arguments to replay the physical checks.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --replay-overlap-receipt .build/resolution-report.json $receiptPin `
  --load-compilation-bundle .build/jakarta-resolved.json $resolvedPin `
  --overlap-source-bundle .build/jakarta-wheel.json $wheelPin `
  --overlap-source-bundle .build/jakarta-sdist.json $sdistPin
```

Replay rebuilds the audited request scope, checks each recorded layout and round, and restores each pinned candidate to compare its physical signature. Older receipts bind the layouts associated with recorded overlaps. New receipts also bind the complete requested layout grid. Both verification routes are read-only. Replay performs no transpilation or snapshot simulation. A successful composition with zero conflicts can be checked as an exact union and has no physical overlaps to replay.

The Python entry points are `verify_snapshot_calibration_overlap_receipt` in `snapshot_calibration_overlap_receipt` and `replay_snapshot_calibration_overlap_receipt` in `snapshot_calibration_study`.

#### Replay a receipt and run the study together

Add `--run-after-overlap-replay` to the replay command to run the dated study after successful verification. The command shares the verified bundle, pinned archive preparation, oracle audit, and compiler key grid. It also reuses the winning physical programs already verified during replay. Both dates receive fresh target checks and simulations.

The output has separate `replay` and `study` reports. A replay mismatch stops the study before simulation. A timeout in the study retains the completed replay report and the usual study coverage and cancellation evidence.

In Python, pass the replay arguments to `run_snapshot_calibration_study_with_overlap_receipt` in `snapshot_calibration_study`.

#### Profile a receipt-backed study on archived Manila

Run the benchmark with a scenario that names the constraint, both archive pairs and their SHA-256 pins, the pinned receipt and resolved bundle, the original source bundle pins, and the study arguments. The scenario can also pin a saved study report for exact canonical JSON comparison. Paths are relative to the scenario file unless absolute.

```powershell
python examples/benchmark_receipt_calibration.py .build/manila-calibration-receipt-study/benchmark-scenario.json --fresh-baseline
```

The example warms both routes and alternates their execution order over matched pairs. It checks both reports on every pair. Timing separates receipt replay and verification, compilation, density preparation and result retrieval, and the remaining work. The optional fresh baseline also reports the compiler's transpilation and OpenQASM timings. These are local warm-process timings with the recorded SDK and scenario.

The Manila verification used the five-wire forward and reverse layouts, rounds zero through three, and the four-bit diffuser constraint. Both dates supported all eight matched programs. A separate full-wire Aer calculation checked all 256 outcome probabilities, with a largest absolute difference of about `5.55e-17`. Thirteen of 144 modeled plans met the 93.25% target on both dates. The receipt exercise used one metadata-only QPY reserialization of a saved Manila circuit to make the overlap repeatable. The combined workflow reproduced the fresh study report byte for byte in both local copies. [2021 Manila properties](https://raw.githubusercontent.com/Qiskit/qiskit-ibm-runtime/2a396172c25aa2971e99b434b430177745878a53/qiskit_ibm_runtime/fake_provider/backends/manila/props_manila.json) · [2024 Manila properties](https://raw.githubusercontent.com/Qiskit/qiskit-ibm-runtime/7b9ecb722f55ecb3aea01d83e6d406a161efc770/qiskit_ibm_runtime/fake_provider/backends/manila/props_manila.json)

The pinned archive manifest, scenario, receipt, reports, and measurements are in `.build/manila-calibration-receipt-study/` in each copy.

#### Prefer native OpenQASM for a dated calibration study

Add `--native-qasm` to `snapshot-calibration-compare` or `snapshot-calibration-study` to try Qiskit's experimental Rust importer for each fresh round. The command uses the same physical target, export, measurement mapping, and exact probability checks. When the native parser is unavailable or rejects the export and the standard parser is installed, the compiler can use the standard parser. A failed verification stops the run.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs --native-qasm `
  --save-compilation-bundle .build/jakarta-native-rounds.json
```

Reuse `--native-qasm` when checking or comparing bundle coverage, resolving a physical overlap, or replaying a receipt created with that preference. The flag changes the compiler keys. A key from another importer policy counts as missing. For Python entry points, pass `prefer_native_qasm=True`. If a benchmark scenario describes a native-preferred receipt, set `"prefer_native_qasm": true` in its `study` object.

The request records `prefer_native_qasm: true`, and each verified export records both `preferred_importer` and the actual `importer`. Saved-round restore checks recorded provenance against the request and regenerates the export and physical measurement evidence using the pinned QPY. The original parser evidence remains bound to the trusted bundle. A resolved overlap receipt checks the requested policy before physical replay. Budget planning requires one preference across the supplied captures and retains each actual importer, including fallback.

Both archived models were evaluated with the four-bit diffuser constraint, two ordered layouts, rounds zero through three, and Qiskit 2.6.2. The native parser handled all 16 exports. The OpenQASM bytes, physical operations and mappings, verified distributions, 512 modeled probability bins, and all budget results matched the standard-import studies. Importer provenance and its dependent report digests record the new choice. This is a local benchmark on these archived inputs. Qiskit documents the native parser as experimental with a reduced supported feature set. [Qiskit OpenQASM 3 documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3#experimental-import-interface)

#### Batch density preparation for dated calibration

Add `--batch-density` to `snapshot-calibration-compare` or `snapshot-calibration-study`. The study can also batch density preparation after overlap receipt replay with `--run-after-overlap-replay`. In Python, pass `batch_density=True` to the corresponding comparison, study, or combined workflow function.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs --native-qasm --batch-density
```

The option groups up to three consecutive rounds that share the same compact simulator within one layout and calibration date. Each job shares readout setup and checks every result's circuit identity, active wires, density method, and replay state. A singleton still uses its own job. Successful reports record `batch_density: true`. The same pinned compiled bundles and overlap receipts can be used in either mode. [AerSimulator options](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

A batch has one 30-second result wait. A timeout identifies all of its rounds in `blocked_rounds`. A singleton timeout identifies one `blocked_round`. An incomplete layout contributes no probability results and publishes no compiled rounds. A confirmed cancellation lets the study try the next layout. Unconfirmed cancellation stops later layouts. [AerJob result and cancellation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.jobs.AerJob.html)

Use the same pinned receipt scenario to time serial and batched studies.

```powershell
python examples/benchmark_receipt_calibration.py .build/calibration-native-qasm-verified/manila/native-benchmark-scenario.json --compare-density-batching
```

The benchmark alternates warm matched runs. It checks every replay and scientific report, tracks circuit counts separately from Aer job counts, and reports full density preparation, Aer submission and wait, and noise serialization time. The inclusive timers can overlap. On the pinned Manila and Jakarta workloads, each run evolved 16 circuits using eight Aer jobs with batching, and the raw density matrices, readout channels, and all reported probabilities matched. These results apply to the tested local archives and software stack.

#### Profile density workers on wider Jakarta circuits

Dated calibration studies omit unused quantum noise channels for eligible stock gates. The rounds sharing a compact simulator contribute their used gate names and ordered physical wires. The simulator retains other channel types, its full basis, and the readout channels for every active wire. The complete noise model is used for groups with labeled, open-control, or unsupported gates, or when the retained gates have no nonideal channel. The archive pins and per-operation calibration checks still cover the dated targets. [Aer NoiseModel](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.NoiseModel.html)

Use the Jakarta archive paths and SHA-256 pins from the comparison above. The profiler uses seed 7 and two fixed constraints that mark bit strings ending in `10`. Rounds one through three use six or seven active wires, respectively. The profiler compiles once before timing and alternates complete and pruned noise models. It checks exact density bytes, measurement maps, readout matrices, and every modeled outcome probability.

```powershell
python examples/profile_calibration_density_worker.py @archiveArgs --pairs 5
```

Set `$archiveArgs` to the two `--earlier` and `--later` argument groups from the dated comparison. Worker measurements use the pinned Aer 0.18.2 implementation. Circuit assembly, noise serialization, controller execution, and result formatting have separate timers. The caller's result wait overlaps those worker stages. The wall measurement includes compact noise setup and batched evolution, and excludes compilation and startup. The recorded matrix shapes and dtypes describe the retained states. Aer retains its configured per-state memory bound. [AerSimulator options](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

The matched runs cover eight circuit/date combinations and four local jobs per arm at each width. The six- and seven-wire cases account for 768 modeled probability values. The calibrated comparisons, seeded budget replays, and earlier pinned Manila and Jakarta reports also matched the complete-noise references. Lowering the fusion threshold was tested separately. It produced rounding-scale changes in the density and probability data and is outside the exact-report optimization.

#### Capture density fusion and check it against a pinned reference

Add `--density-fusion` to `snapshot-calibration-compare` to request Aer density fusion with threshold 5 and a maximum fused width of 2. The capture records the requested options and Aer metadata for each date and round. A null observation means Aer did not report that metadata. This is a local simulation policy. The existing compiled circuits and compilation keys can be reused. [AerSimulator fusion options](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

For a seven-active-wire case, use a constraint with six search bits and clauses `[[1,2],[-1]]`. Set `$compareArgs` to its path, the earlier and later Jakarta archive arguments, and `--initial-layout 0 1 2 3 5 4 6 --max-rounds 3 --native-qasm --batch-density`. Use the same arguments for both captures.

```powershell
python -m austins_algorithm snapshot-calibration-compare @compareArgs |
  Set-Content -Encoding utf8 reference.json
python -m austins_algorithm snapshot-calibration-compare @compareArgs --density-fusion |
  Set-Content -Encoding utf8 fused.json
$referencePin = (Get-FileHash -LiteralPath reference.json -Algorithm SHA256).Hash.ToLowerInvariant()
$fusedPin = (Get-FileHash -LiteralPath fused.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

Supply the fused capture as the positional report and the default capture as a pinned reference. Repeat `--fusion-reference PATH SHA256` for additional matched layouts. The comparison runs with the Python standard library.

```powershell
python -m austins_algorithm snapshot-calibration-budget-plan constraint.json fused.json `
  --expected-report-sha256 $fusedPin --fusion-reference reference.json $referencePin `
  --fusion-probability-tolerance 1e-12 --target-probability 0.7397673062029266 `
  --shot-caps 4 --query-caps 1 --max-rounds-per-shot 3 --validate-trials 128
```

The audit checks the dated archive identities, ordered layouts, batch setting, circuit evidence, and readout maps. It reports observed total variation and maximum outcome, hit, and miss probability differences. The tolerance applies to each matched round and date in the supplied pair. It is an acceptance threshold for this comparison. A separate check compares each candidate's exact target status and the recommended and best-attainable budget identities across both dates and the shared plan.

The output's `reference_plan` uses the default capture for feasibility, recommendations, and seeded replay. Ordinary budget planning requires a default capture. A fused capture can be used in the paired audit with its matching reference. At the original Jakarta cutoff of 0.6373106031787343, all feasibility classifications match. Two later-date recommendations change under fusion. At 0.7397673062029266, the four-shot, one-query `affordable` candidate changes from `MEETS_NEITHER` to `LATER_ONLY` under fusion, even though the observed distribution changes are within 1e-12 total variation.

#### Run both density policies in one study

Add a finite total-variation tolerance from 0 through 1 to run a paired study. The same option works after a compilation-overlap receipt is replayed with `--run-after-overlap-replay`. A zero tolerance checks exact probability agreement.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --native-qasm --batch-density --fusion-probability-tolerance 1e-12
```

The study completes the default captures before starting any fusion jobs. Both policies execute their own density simulations using the same verified circuits and compact simulator setup for each layout and date. The grid work limit accounts for both policies. The study's `captures`, `planning`, and seeded replay use the default policy, and the fusion diagnostic carries its own capture digests and coverage. The study returns `fusion.check` after every requested pair completes. Its decision and recommendation checks can flag changes even when the probability differences are within tolerance.

If a candidate fails, the study keeps the completed default captures and plan. `fusion.check` is null for incomplete pairing. Candidate timeouts record the affected layout, date, and round or batch, and whether Aer confirmed cancellation. An unconfirmed cancellation stops further density submissions. Captured execution errors identify the affected layout and halt the candidate phase. See [AerJob cancellation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.jobs.AerJob.html).

In the six- and seven-active-wire Jakarta checks, each paired study read four archive files, built two targets, audited the oracle once, and assembled two sets of compact simulators. Running the two captures separately doubled those setup counts. Both paths restored the same four verified rounds and submitted eight local Aer jobs over sixteen circuits. The two-layout Manila receipt check reproduced the saved default report and matched fusion decisions at its configured target. These checks use dated local IBM models.

#### Release simulator setup and carry evidence across runs

The reference phase keeps the compact circuits and dated simulators needed by the fusion phase. Each full compiled program can leave the reference scope after compaction, with any caller-owned cache retaining its own entries. The fusion phase releases each layout's compact setup as it completes and releases unused setup when it stops. The report keeps the compiled wire maps, outcome distributions, and capture digests needed for the audit.

The three-layout Jakarta check retained 12 full compiled programs at the start of planning in the baseline and zero with early release. Compact simulators went from 12 to zero at that boundary. In the receipt-backed Manila check, compact simulators went from eight to zero and one verified overlap program remained in scope. These counts measure tracked Python objects. Process peak also includes simulator and allocator memory across the full run, including the point at which all compact layouts are still needed.

The three-layout Jakarta and receipt-backed two-layout Manila checks produced byte-identical reports before and after the change. Fresh SDK-free processes independently verified both reports after the simulator processes exited. Keep the constraint document, study JSON and known SHA-256 together for that handoff.

#### Restart a paired study from completed reference captures

Use `snapshot-calibration-study-checkpoint` to run the default captures and stop before fusion. Choose a new directory. The command saves the completed captures, settings and archive scope in `checkpoint.json`, and saves the verified compiled rounds in `compilation-bundle.json`. Retain both files and the reported `checkpoint_sha256`.

In this PowerShell example, `$archiveArgs` supplies the `--earlier` and `--later` archive arguments with their SHA-256 pins. Choose the layout and budget grid for the constraint.

```powershell
New-Item -ItemType Directory -Path .build -Force | Out-Null
$saved = python -m austins_algorithm snapshot-calibration-study-checkpoint constraint.json .build/paired-checkpoint @archiveArgs `
  --initial-layout 0 1 2 3 5 4 6 --target-probability 0.9 --shot-caps 1 4 8 9 --query-caps 0 1 2 3 `
  --max-rounds-per-shot 3 --validate-trials 128 --native-qasm --batch-density --fusion-probability-tolerance 1e-12
if ($LASTEXITCODE -ne 0) { throw 'Checkpoint creation did not complete' }
$checkpointPin = (($saved -join [Environment]::NewLine) | ConvertFrom-Json).checkpoint_sha256

python -m austins_algorithm snapshot-calibration-study-resume constraint.json .build/paired-checkpoint @archiveArgs `
  --expected-checkpoint-sha256 $checkpointPin
```

Resume checks the checkpoint pin, reference capture digests, constraint, dated archives, compiler scope, software versions, restored circuits, target calibrations and readout matrices before submitting fusion jobs. It rebuilds each layout's compact simulator setup from the verified bundle and returns the ordinary paired study report with its authoritative default plan and seeded replay. The checkpoint stays reusable for an explicit later run.

A ready checkpoint represents a complete reference phase. Reference failures leave coverage evidence and prevent publication of the ready manifest. Resume uses the settings saved in the checkpoint and runs only the fusion phase. The workflow uses local archived models and supports the ordinary paired study. Receipt-backed studies have a separate compiler lineage workflow.

#### Resume between reference layouts

Use `snapshot-calibration-study-incremental` to publish one checkpoint per completed default layout. Each generation links to its predecessor by SHA-256 and stores one sealed reference capture and its compiled bundle. The generation file is published after its bundle and restored circuits verify. Keep the checkpoint directory and the returned `generation_sha256` together.

This example pauses after the first layout. Supply the pinned archive arguments in `$archiveArgs` and use the same constraint and archives for both commands.

```powershell
New-Item -ItemType Directory -Path .build -Force | Out-Null
$saved = python -m austins_algorithm snapshot-calibration-study-incremental constraint.json .build/layout-checkpoints @archiveArgs `
  --initial-layout 0 1 2 3 5 4 6 --initial-layout 6 4 5 3 2 1 0 `
  --target-probability 0.9 --shot-caps 1 4 8 9 --query-caps 0 1 2 3 --max-rounds-per-shot 3 `
  --validate-trials 128 --native-qasm --batch-density --fusion-probability-tolerance 1e-12 --max-new-reference-layouts 1
if ($LASTEXITCODE -ne 0) { throw 'Incremental capture stopped with a failure' }
$pin = (($saved -join [Environment]::NewLine) | ConvertFrom-Json).generation_sha256

python -m austins_algorithm snapshot-calibration-study-incremental-resume constraint.json .build/layout-checkpoints @archiveArgs `
  --expected-generation-sha256 $pin --study-output .build/completed-paired-study.json
```

Resume verifies the chain, archives, compiler scope, captures, compiled circuits and readout evidence before submitting density jobs. It runs only the missing reference layouts. Fusion starts after the full reference grid completes. The completed JSON contains the paired result under `study`. Read `study.fusion.status` for the coverage of the fusion diagnostics. With `--study-output`, the command also writes the paired result to a new file and reports its SHA-256 for the offline verifier. If that file cannot be published, the command returns the result and checkpoint pin with the write error.

After an interrupted process exits, choose a trusted generation file from `generations/` and use the SHA-256 in its filename. Resume verifies that filename against the bytes and follows the predecessor chain. A failed reference or publication leaves the earlier tip available. Keep that tip when comparing branches or investigating a stop. Add `--reference-only` to either incremental command to return `CALIBRATION_STUDY_INCREMENTAL_REFERENCES_COMPLETED` after the full reference grid, before any fusion. This mode returns `study: null`. Use the separate fusion continuation command below to save diagnostic progress.

#### Resume between fusion attempts

`snapshot-calibration-study-fusion-continue` requires a pinned complete incremental reference chain. It saves each completed diagnostic attempt in `fusion-generations/<SHA-256>.json`, including recognized failures and timed-out captures. Each record binds the reference tip, canonical layout, predecessor and outcome. It reuses the reference circuits without creating another compiled bundle.

This example creates the complete reference chain, pauses after one fusion attempt, and finishes in another invocation. Supply the pinned archive arguments in `$archiveArgs` and the same seven-wire constraint as in the reference example.

```powershell
New-Item -ItemType Directory -Path .build -Force | Out-Null
$reference = python -m austins_algorithm snapshot-calibration-study-incremental constraint.json .build/fusion-checkpoints @archiveArgs `
  --initial-layout 0 1 2 3 5 4 6 --initial-layout 6 4 5 3 2 1 0 `
  --target-probability 0.9 --shot-caps 1 4 8 9 --query-caps 0 1 2 3 --max-rounds-per-shot 3 `
  --validate-trials 128 --native-qasm --batch-density --fusion-probability-tolerance 1e-12 --reference-only
if ($LASTEXITCODE -ne 0) { throw 'Reference checkpoint did not complete' }
$referencePin = (($reference -join [Environment]::NewLine) | ConvertFrom-Json).generation_sha256

$first = python -m austins_algorithm snapshot-calibration-study-fusion-continue constraint.json .build/fusion-checkpoints @archiveArgs `
  --expected-reference-generation-sha256 $referencePin --max-new-fusion-layouts 1
if ($LASTEXITCODE -ne 0) { throw 'Fusion attempt stopped; inspect its returned status and pins' }
$fusionPin = (($first -join [Environment]::NewLine) | ConvertFrom-Json).generation_sha256

python -m austins_algorithm snapshot-calibration-study-fusion-continue constraint.json .build/fusion-checkpoints @archiveArgs `
  --expected-reference-generation-sha256 $referencePin --expected-fusion-generation-sha256 $fusionPin `
  --study-output .build/fusion-completed-study.json
```

Both pins are explicit evidence inputs. Omitting the fusion pin starts from the first attempt; the command does not discover a latest tip. Resume verifies the complete reference chain, software versions, archives, compiler scope, physical circuits, calibrations and readout evidence. It checks every saved fusion capture against its same-layout reference before launching any new density job. A recognized saved error or unconfirmed cancellation is terminal: reopening that tip returns the same partial report and starts no new diagnostic jobs. A timeout with confirmed cancellation can be saved and followed by later layouts.

`--max-new-fusion-layouts` accepts 1–8 and limits new attempts. `CALIBRATION_STUDY_FUSION_CHECKPOINT_PAUSED` has `study: null`; `COMPLETED` and `TERMINAL` suffixes return the ordinary paired report under `study`. `COMPLETED` means all attempts are recorded; inspect `study.fusion.status` for diagnostic coverage. A terminal result exits with status 1. A full or terminal report exported with `--study-output` uses the existing SDK-free study verifier. An output-write failure retains the report and checkpoint pins in the command result.

Publication validates the serialized outcome before the immutable marker is installed. Failure leaves the previous pin usable and reports the attempted outcome. If that unpublished attempt reports unconfirmed cancellation, establish that the local job has stopped before another attempt; a missing marker does not confirm cancellation. Keep the returned reference and fusion pins with the store. If a process exits after publication but before printing its result, recover the known marker's pin and verify it on resume. A job interrupted before publication is outside the saved prefix and may run again. These checkpoints preserve software evidence across runs; they do not keep a process alive or provide quantum-device evidence.

#### Reuse verified checkpoint context within one invocation

Fusion continuation retains its validated reference and history for the current invocation. Each new attempt validates its own outcome and runs the existing runtime callback. Before installing its marker, the store checks cached-state integrity and streams the raw SHA-256 of every ancestor manifest again. A failed callback or publication leaves the context at its previous tip. Reentrant appends to the same context are rejected.

The public command, manifest schema and generation digests are unchanged. Each new invocation creates a fresh context; standalone readers and publishers still fully validate their explicit inputs. Archive, compiler, physical-circuit, calibration and readout checks remain in the runtime restoration path. This reuse does not skip those checks or select a different tip.

The local three-append Jakarta store sequence used the same complete reference tip and exact saved candidate captures before and after the change:

| Store operation | Previous implementation | Invocation context |
| --- | ---: | ---: |
| Reference manifests parsed | 9 | 3 |
| Prior fusion manifests parsed | 3 | 0 |
| Semantic capture validations | 6 | 3 |
| Calibration profile calculations | 12 | 6 |
| Publication callbacks | 3 | 3 |
| Raw manifest bytes read | 1,070,154 | 1,338,000 |

The extra 267,846 bytes preserve fresh raw-pin checks after callbacks while prior JSON/profile results are reused. These are instrumented operation counts for three store appends. The profiling callback checked staged ordinals; runtime callback validation, density execution and independent circuit restoration are outside these counts. They establish reduced repeated validation, not a general wall-time speedup. The resulting marker files retain the original generation pins and report evidence.

#### Retain only the current restoration chunk

Fusion continuation restores and validates all saved reference rounds, then keeps compiled-program objects only for the next canonical chunk allowed by `--max-new-fusion-layouts`. Saved attempts, layouts beyond that chunk, and completed or terminal histories require no pending fusion setup. Reference-only and predicted reference-pause calls also discard their restored programs. A reference call that completes the grid and runs fusion in the same invocation retains the programs needed by that full fusion phase.

This changes object ownership after verification. It preserves the public commands, checkpoint schemas, generation pins, complete capture maps and report bytes. Every requested round still passes QPY restoration, exact compilation-report comparison, both dated target-calibration comparisons and readout checks before new density work. Late corruption in a skipped layout still rejects the call. Partially built callback maps are cleared on errors, and each temporary restored-program reference is released after its checks.

The 2026-11-12 weak-reference probes used the same pinned eight-layout Jakarta store before and after the change, with four rounds per layout and two new fusion layouts allowed per call:

| Call | Prior peak tracked programs | Current peak tracked programs | Prior programs retained after restoration | Current programs retained after restoration |
| --- | ---: | ---: | ---: | ---: |
| Two saved fusion attempts, next two allowed | 32 | 9 | 32 | 8 |
| Six saved fusion attempts, final two allowed | 32 | 8 | 32 | 8 |
| All eight fusion attempts saved | 32 | 1 | 32 | 0 |
| Reference-only call with all eight references saved | 32 | 1 | 32 | 0 |

Both maintained copies reproduced these counts. Each call still performed 32 round restorations, 64 dated calibration comparisons and eight readout-layout checks. Calls with pending fusion ran eight density preparations; complete and reference-only calls ran none. Each before/after result envelope matched byte-for-byte. The complete study remained identical to the eight-layout benchmark report below.

The probes track returned `CompiledProgram` instances through weak references. The single transient object is observed while its own round is checked; other Python/native objects and memory retained by SDKs are outside this count. These object-lifetime measurements establish reduced retention under the tested conditions. Process-wide memory or latency savings require separate representative measurement.

#### Benchmark checkpoint continuation at eight layouts

[`examples/benchmark_checkpoint_continuation.py`](examples/benchmark_checkpoint_continuation.py) runs one uninterrupted paired study and a sequence of fresh reference/fusion workers. It checks exact final study bytes and runs the SDK-free verifier. A scenario supplies the constraint file and its raw SHA-256, both pinned archive records, study options, chunk sizes and a worker deadline. The example accepts one to eight layouts. Install the package with its IBM extra in the interpreter running the example.

Use [`examples/checkpoint_constraint.json`](examples/checkpoint_constraint.json) for the measured six-variable case. It accepts `q0 = 0` and `q1 = 1`, with four free bits. Its reversible circuit uses seven active wires. Prepare the four local Jakarta archive files as in the dated calibration workflow, then construct the scenario with their actual paths and pins:

```powershell
function Get-ArchiveRecord([string]$configuration, [string]$properties) {
  $config = (Resolve-Path -LiteralPath $configuration).Path
  $props = (Resolve-Path -LiteralPath $properties).Path
  return @{
    configuration = $config; properties = $props
    configuration_sha256 = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash.ToLowerInvariant()
    properties_sha256 = (Get-FileHash -LiteralPath $props -Algorithm SHA256).Hash.ToLowerInvariant()
  }
}
$constraint = (Resolve-Path -LiteralPath examples/checkpoint_constraint.json).Path
$scenario = @{
  schema_version = 1
  constraint = @($constraint, (Get-FileHash -LiteralPath $constraint -Algorithm SHA256).Hash.ToLowerInvariant())
  earlier = Get-ArchiveRecord $earlierConfiguration $earlierProperties
  later = Get-ArchiveRecord $laterConfiguration $laterProperties
  study = @{
    initial_layouts = @(
      @(0,1,2,3,4,5,6), @(0,1,2,3,5,4,6), @(0,1,2,4,3,5,6), @(0,1,3,2,4,5,6),
      @(0,2,1,3,4,5,6), @(1,0,2,3,4,5,6), @(1,2,3,5,4,6,0), @(6,4,5,3,2,1,0)
    )
    target_probability = 0.9; shot_caps = @(1,4,8,9); query_caps = @(0,1,2,3)
    max_rounds_per_shot = 3; seed_transpiler = 7; validation_trials = 128
    prefer_native_qasm = $true; batch_density = $true; fusion_probability_tolerance = 1e-12
  }
  limits = @{ reference_layouts_per_process = 2; fusion_layouts_per_process = 2; worker_timeout_seconds = 180 }
}
New-Item -ItemType Directory -Path .build -Force | Out-Null
$scenario | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath .build/checkpoint-scenario.json -Encoding utf8
python -B examples/benchmark_checkpoint_continuation.py --scenario .build/checkpoint-scenario.json --directory .build/checkpoint-benchmark
```

Set `$earlierConfiguration`, `$earlierProperties`, `$laterConfiguration` and `$laterProperties` to the four archive paths before running the block. The output directory must be new. Each stage retains its stdout/stderr, result, metrics and any study export. `summary.json` records the last known reference/fusion pins, report hashes, preparation counts, per-phase wall time and storage. Failure or incomplete diagnostics stops later workers. A timeout records cancellation as `UNKNOWN`; confirm any local worker has stopped before resuming from trusted pins. The example does not automatically retry or overwrite evidence.

On 2026-11-12, both maintained copies completed all nine workers: one baseline, four reference chunks and four fusion chunks. Each arm compiled 32 rounds and ran 64 density preparations, for 128 preparations across both arms under the general 256-preparation limit. The restart arm restored 208 compiled-round instances across its fresh processes. Both final reports contained 3,640,898 bytes and had SHA-256 `bf5333a5663c97b7960001a6a1336622e80df78e143f8b25eb4be39576f141f7`. The checkpoint store contained eight reference markers, eight fusion markers and eight compiled bundles: 24 files totaling 2,548,178 bytes. Separate SDK-free processes verified both reports and both eight-generation chains.

| Local copy | Baseline worker wall seconds | Sum of continuation worker wall seconds | Baseline peak working set MiB | Highest continuation-worker peak MiB |
| --- | ---: | ---: | ---: | ---: |
| ChronoRealm workspace | 14.31 | 44.49 | 206.11 | 220.20 |
| E: standalone | 11.22 | 40.06 | 206.54 | 221.19 |

These are single-run observations on Windows with Python 3.12.15, Qiskit 2.6.2, Aer 0.18.2 and IBM Runtime 0.51.0. Thread settings were `QISKIT_PARALLEL=FALSE`, `OMP_NUM_THREADS=1`, and `RAYON_NUM_THREADS=2`. Parent wall time includes startup, input checks and output. Windows peak working set covers each worker's lifetime, including imports; endpoint private usage is recorded separately. The continuation columns aggregate different fresh processes and show added restart cost in these runs. They do not establish a speedup, reduced memory use, practical quantum advantage or a system capacity limit. Storage for all evidence explicitly excludes `summary.json` from its own count.

#### Verify saved checkpoint benchmark records

Keep a trusted SHA-256 of the benchmark's `summary.json`, then use the base package to audit its saved evidence. This command needs no Qiskit, Aer, IBM credentials or simulator rerun. Supply the original constraint document explicitly; its raw bytes must match the normalized scenario's recorded constraint pin.

```powershell
$summaryPin = (Get-FileHash -LiteralPath .build/checkpoint-benchmark/summary.json -Algorithm SHA256).Hash.ToLowerInvariant()
python -m austins_algorithm checkpoint-benchmark-verify examples/checkpoint_constraint.json .build/checkpoint-benchmark `
  --expected-summary-sha256 $summaryPin
```

Computing a digest from a newly received file records the bytes you have; it does not authenticate their origin. For transfer or later review, compare against the digest retained from the trusted run. Save any audit output outside the selected evidence directory: its storage totals cover the saved tree and exclude only `summary.json`.

The verifier checks the supplied summary pin before parsing, strict finite and duplicate-free JSON, canonical local stage directories, available result/metrics/log files, scenario bindings, reference/fusion chain prefixes and checkpoint handoffs. It checks logical operation counts and caps, finite nonnegative durations, memory-field availability, recorded phase sums and actual storage totals. Available paired reports must match their stamps and pass the independent study verifier. Complete records must substantiate their final report comparison. Legitimately stopped records retain their last known reachable checkpoint pins and available evidence; no later stage may follow a failed or timed-out worker.

An audit that passes returns `CHECKPOINT_BENCHMARK_RECORD_VERIFIED` and exit code zero. Inspect `recorded_status` and coverage before interpreting the original run: a valid worker-failure or timeout record can pass the audit. Corrupt or inconsistent available evidence returns a CLI error, including an invalid scientific report inside a summary marked `BENCHMARK_VERIFICATION_FAILED`. This command never resumes work or changes cancellation status.

Schema 1 summaries embed parsed stage JSON rather than historical raw-stage digests. The audit requires those parsed values to match the local files and reports the current raw file hashes with `PARSED_CONTENT_MATCHES_SUMMARY`. Equal-length formatting changes can preserve that binding; changes in file sizes also have to satisfy the recorded storage totals. This is not a historical raw-byte signature or proof of execution. Original scenario, calibration-archive, report and module path labels are never followed; fixed local files and explicitly pinned local chains are used, allowing byte-preserving relocation of an evidence directory.

The read-only evidence inventory excludes `summary.json` and is bounded to 8,192 files and 1 GiB. Each inspected file, including the separately pinned summary, is limited to 64 MiB. Unsafe path traversal, symlink/reparse entries, malformed records and inconsistent totals reject the audit. Worker execution, clocks, timing/memory accuracy, archive contents and recorded software/source identity remain `RECORDED_ONLY`; QPY circuits are not decoded. No hardware result, practical quantum advantage or memory-saving claim follows from this check.

On 2026-11-12, SDK-free source and wheel processes in both maintained copies audited the existing #140 and #141 nine-worker eight-layout records, plus a byte-preserving relocated copy. Each saved directory contained 64 files including the summary. Before/after fingerprints confirmed the audits were read-only. The wheel CLI matched the API result, and an incorrect trusted summary pin returned exit code one. Regression fixtures additionally cover stopped/failing/timed-out workers, publication errors, corrupted embedded captures, chain corruption, counter and handoff changes, and unsafe paths. These are local consistency checks of the available records.

The #143 CI repair changes only the controlled test fixture: it substitutes the benchmark module's subprocess binding instead of changing the shared standard-library module. Linux `platform.processor()` can launch a read-only host query; that query must stay separate from simulated benchmark workers. The regression forces this interaction on Windows too. Production benchmark execution, evidence schemas, cancellation behavior and verifier rules are unchanged. GitHub run #9 failed before this repair; the corrected hosted result remains pending.

#### Verify a saved paired calibration study offline

Save the paired study JSON and retain its file SHA-256. Supply the same constraint document and the known digest when reviewing or transferring the result. The verifier supports files up to 64 MiB, including the output of a receipt-backed study.

```powershell
python -m austins_algorithm snapshot-calibration-study @studyArgs `
  --native-qasm --batch-density --fusion-probability-tolerance 1e-12 |
  Set-Content -Encoding utf8 -LiteralPath paired-study.json
$studyPin = (Get-FileHash -LiteralPath paired-study.json -Algorithm SHA256).Hash.ToLowerInvariant()
python -m austins_algorithm snapshot-calibration-study-verify constraint.json paired-study.json `
  --expected-study-sha256 $studyPin
```

The verifier checks the file bytes, the constraint and round request, both sets of capture digests, and the recorded order of execution, failures, and cancellation. It recomputes the budget plan and any seeded replay from the saved default outcomes. With complete pairing it also recomputes the fusion comparison. A partial study can verify with a null `fusion_check_status`. A complete diagnostic retains its recorded result, including `DECISIONS_CHANGED` or `TOLERANCE_EXCEEDED`.

This check uses standard-library Python. The output describes consistency with the supplied file pin and saved outcomes. The calibration archives, circuit execution, and any receipt replay stay outside that verification scope. For receipt-backed input, `receipt_replay_evidence` is `RECORDED_ONLY`, and the verifier checks that the saved replay request and study agree. Use `snapshot-calibration-overlap-receipt-verify` with the original pinned bundles to verify their recorded lineage separately.

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

#### Size a holdout before sampling

Use `witness-schedule` to find the minimum number of batches per cover for a target and a tolerated number of misses. The calculation uses SciPy's one-sided 97.5% Clopper–Pearson lower bound, the same calculation used for completed holdouts. It can run before formula loading, compilation, calibration, or seed selection. The Python entry point is `plan_witness_schedule` in `austins_algorithm.witness_budget`. [SciPy exact interval method](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html)

```powershell
python -m austins_algorithm witness-schedule --target-probability 0.95
python -m austins_algorithm witness-schedule --target-probability 0.95 --tolerated-misses 1
python -m austins_algorithm witness-schedule --target-probability 0.95 --tolerated-misses 2 --max-batches-per-cover 128
```

`minimum_batches_per_cover` is the smallest qualifying schedule within the supplied cap. `minimum_validation_seeds` is twice that count for the two interleaved covers. Supply that many distinct seeds to `--witness-seed-schedule`, with no overlap with the four calibration seeds. The result describes what a completed schedule would support if the observed misses stay within the allowance. The lower bound at the cap is always available. `BATCH_CAP_EXCEEDED` leaves the minimum fields null.

| Misses allowed per cover at a 95% target | Minimum batches per cover | Interleaved seeds | Lower bound |
| --- | ---: | ---: | ---: |
| 0 | 72 | 144 | 95.0056% at minimum |
| 1 | 110 | 220 | 95.0389% at minimum |
| 2 | Cap exceeded | — | 94.4697% at cap 128 |

The ChronoRealm CLI used the computed counts to replay archived offline IBM schedules. The Jakarta direct cover returned 71 of 71 and then 72 of 72 successes. Its lower bound crossed from 94.9371% to 95.0056%. Each Torino cover returned 108 of 109 and then 109 of 110 successes, crossing from 94.9943% to 95.0389%. The retained count maps, calibration, and OpenQASM matched the archive. An independent exact-integer binomial check confirmed the two in-cap minima and the cap failure at two misses. These replays exercise the calculation and command path using known seeds.

#### Check a proposed seed list before sampling

Add `--witness-seed-schedule` to `witness-schedule` to check the exact list you plan to run. Pass the same `--evaluation-seeds` you will use for calibration. With no explicit evaluation seeds, the check uses the phase-cover defaults `167 173 179 181`. The Python entry point is `check_witness_schedule`.

```powershell
$validationSeeds = 1001..1144
python -m austins_algorithm witness-schedule --target-probability 0.95 --evaluation-seeds 211 223 227 229 --witness-seed-schedule @validationSeeds
```

The planner and runtime share the seed rules. A proposal must contain an even number of distinct unsigned 32-bit seeds, from 2 to 256. Its four evaluation seeds must be distinct and disjoint from the validation list. Malformed input fails before the bound calculation. The report preserves the submitted order within each cover and exposes the assessment under `proposed_schedule`.

- `SCHEDULE_FEASIBLE` means the list meets the sizing requirement within the chosen cap.
- `SCHEDULE_TOO_SHORT` includes the number of additional interleaved validation seeds needed.
- `SCHEDULE_EXCEEDS_CAP` means the proposed list is longer than the caller's cap.
- `BATCH_CAP_EXCEEDED` means the target and tolerated misses require more batches than the cap permits.

The bound at the proposed size assumes the selected miss allowance. The later sampling results determine whether each cover supports the target. Use the same seed lists when running `constraint-cover`.

At the 95% target, the ChronoRealm CLI reported a shortfall of two seeds for the 142-seed Jakarta list with zero tolerated misses and the 218-seed Torino list with one. It accepted the 144- and 220-seed lists. Feeding those exact lists into the offline runtime reproduced the archived counts, witness bounds, and OpenQASM. The Jakarta direct cover supported the target, and its complement observed one miss and remained unresolved. Both Torino covers supported the target with one miss each.

#### Save and run a checked offline replay

Add `--save-replay-request` to a `constraint-cover` request with a named offline snapshot, shared layout, calibration shots, and witness target. Saving checks the seed list and compiles and verifies the covers without starting an Aer job. The output includes the schedule assessment. A short but structurally valid list can still be saved and run for diagnostics. Use `--tolerated-misses` to record the miss allowance used for that assessment.

```powershell
$validationSeeds = 1001..1144
python -m austins_algorithm constraint-cover examples/site_interlock.json --ibm-model jakarta --snapshot-method prepared_density_matrix --density-fusion --max-active-qubits 6 --initial-layout 1 2 0 6 3 5 --snapshot-shots 128 --evaluation-seeds 211 223 227 229 --witness-probability 0.95 --witness-seed-schedule @validationSeeds --native-qasm --save-replay-request site-interlock-replay.json
python -m austins_algorithm constraint-replay site-interlock-replay.json --qasm-output site-interlock-replay.qasm
```

The version-1 JSON embeds the Boolean formula and canonical SHA-256, the ordered layout and seed lists, and the resolved compiler, simulator, and witness options. The snapshot fingerprint hashes the local configuration and calibration properties. Each saved export records the verified OpenQASM digest, initial layout, and physical measurement and export wire maps. The request contains the formula itself, so it can be moved to another folder or drive. Supply any export destination separately when running it. [IBM local fake backend API](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider-fake-manhattan-v2)

Replay checks the embedded formula, seed inputs, and local snapshot. It recompiles and compares the OpenQASM and wire maps before sampling with the same backend instance. The CLI refuses to overwrite an existing request or replay export. The file is an editable experiment input. The fingerprints verify the recorded content, and the replay report records the actual installed simulator versions. Seeded counts were verified on the local Qiskit 2.6.2, Aer 0.18.2, Runtime 0.51.0, and SciPy 1.18.1 stack.

The ChronoRealm comparison used the Jakarta 144-seed and Torino 220-seed schedules. Saved replay and explicit CLI runs matched each other and the archived counts, witness bounds, wire maps, and OpenQASM. Tests also exercised rejection of changed formula, snapshot fingerprint, OpenQASM digest, wire maps, and overlapping seed lists.

#### Inspect a saved replay without sampling

```powershell
python -m austins_algorithm constraint-replay site-interlock-replay.json --check-only
```

The check reads the saved file and verifies it through the same path as a full replay. It validates the formula digest and seed schedule, fingerprints the installed snapshot, recompiles the covers, and checks their OpenQASM digests and wire maps. It also checks the classical witness against the exhaustive oracle audit. The check writes its report to stdout and launches no Aer job.

`REPLAY_VERIFIED` records agreement with the saved request. `compiled_sampling_status` reports whether the compiled covers meet the runtime's alternate-cover and active-wire rules. `schedule_check` gives the conditional confidence assessment for the saved seed list and miss allowance. A valid short schedule can pass verification and report `SCHEDULE_TOO_SHORT`. A full replay accepts a new export destination. The CLI treats `--check-only` and `--qasm-output` as mutually exclusive.

The Jakarta and Torino checks matched their saved formula digests, snapshot fingerprints, wire maps, exports, and schedule assessments. Full CLI replays reproduced the archived counts, witness bounds, and OpenQASM. A short-list check reported its sizing shortfall, and the command rejected edited formula, snapshot, and export evidence before sampling.

#### Save and verify a replay receipt

```powershell
python -m austins_algorithm constraint-replay site-interlock-replay.json --receipt-dir site-interlock-run
python -m austins_algorithm constraint-receipt-verify site-interlock-run
```

Choose a new directory under an existing parent. A full replay saves the exact input bytes as `request.json`, the observed report as `report.json`, and the selected cover's OpenQASM as `selected.qasm` when one is available. It writes `receipt.json` last with each file's byte count and SHA-256, the Python version, and package versions. An absent selected export is recorded as null. The normal JSON report still goes to stdout. Choose only one of `--receipt-dir`, `--qasm-output`, and `--check-only`.

The verification command accepts the bundle directory after it has been moved or copied. It checks the receipt schema and exact file bytes with the standard library and returns `REPLAY_RECEIPT_VERIFIED` on success. The input is capped at 64 KiB, the report and export at 16 MiB each, and the receipt at 16 KiB. Verification uses the receipt as its reference. It authenticates neither the author nor the recorded environment and performs no compilation or sampling. A failed write can leave partial artifacts. A valid receipt requires every recorded artifact to pass verification.

The 144-seed Jakarta and 220-seed Torino replays matched their archived reports after excluding timings and the export destination. The OpenQASM matched byte for byte. Both bundles verified with Python site packages disabled.

#### Compare two verified replay receipts

```powershell
python -m austins_algorithm constraint-receipt-compare site-interlock-run site-interlock-rerun
```

The command verifies both bundles and compares their recorded request, report, selected OpenQASM, and software versions. Request and report comparisons use JSON content and separately report whether the original bytes match. Object key order and whitespace may differ. Array order, value types, and missing fields remain meaningful. For each changed JSON section, the command shows the first differing field as a JSON Pointer. OpenQASM comparison uses the verified byte count and SHA-256, with null recording an absent export.

Report comparison skips recorded duration fields in the snapshot preparation, sampling batches, classical reference, and witness validation. Configured wait limits, timeouts, seeds, measured counts, and witness bounds remain in the comparison. The command uses the verified bytes and runs with the Python standard library. `REPLAY_RECEIPTS_MATCH` and `REPLAY_RECEIPTS_DIFFER` are both successful comparisons. A corrupt, incomplete, or invalid bundle produces an error.

Independent Jakarta and Torino receipts from the two local copies matched by model with different raw report timings. Comparing Jakarta against Torino found differences in the request, report, and selected OpenQASM. Changing Jakarta's first holdout seed from 1001 to 999 located `/witness/validation_seeds/0` in the request and `/witness_budget/candidates/direct/validation/seed` in the report. The selected OpenQASM matched. These checks used local IBM snapshot simulations and receipt comparisons with Python site packages disabled.

#### Rerun a named set of offline baselines

Save a version-1 manifest such as `regression.json`. Each baseline directory contains the verified receipt from an earlier replay. Relative baseline paths are resolved from the manifest's directory.

```json
{
  "schema_version": 1,
  "max_total_sampling_shots": 94208,
  "cases": [
    {"id": "jakarta", "baseline": "baselines/jakarta", "max_sampling_shots": 37376, "timeout_seconds": 60},
    {"id": "torino", "baseline": "baselines/torino", "max_sampling_shots": 56832, "timeout_seconds": 60}
  ]
}
```

```powershell
python -m austins_algorithm constraint-regression regression.json --output-dir regression-results
```

The command verifies all baseline receipts and checks the sampling budget before launching a worker. The sampling ceiling is the number of calibration seeds times the calibration shots per seed, plus the number of validation seeds times the holdout shot cap per seed. It covers measurement sampling across both covers. Compilation and state preparation are covered by the worker timeout. The suite accepts one to eight cases, each with a unique name, an explicit shot cap, and a worker timeout of 1 to 300 seconds. A case or suite cap below the planned ceiling prevents all launches.

Use a new output directory. It receives the exact manifest, a numbered directory per attempted case with the staged request, worker stderr and any replay receipt, and a `summary.json`. The command also prints the summary. Workers run sequentially in separate local Python processes and verify the staged request digest before compiling. The runner verifies the baseline again and requires its receipt to agree with the preflight record before comparing results. A worker timeout terminates that process. The next case keeps its own limits. Preflight and final verification run outside the worker timeouts.

`REGRESSION_MATCHED` exits with status 0. Preflight failure, a changed result, or a worker or comparison failure exits with status 1 and identifies the affected cases. The summary records the comparison and the underlying compilation, snapshot, and witness statuses separately. A matched baseline that recorded partial validation still reports partial validation.

The archived Jakarta and Torino baselines matched at planned ceilings of 37,376 and 56,832 shots. A 94,207-shot suite cap launched no workers. With a one-second timeout, the Torino worker timed out and the following Jakarta case matched. All baseline artifacts retained their original hashes.

#### Run the packaged Manila smoke baseline

The source checkout and source distribution include [a 25 KiB smoke fixture](examples/smoke-regression/manifest.json). It uses a two-bit XOR formula and the public FakeManila snapshot with four 32-shot calibration seeds and two holdout seeds capped at 16 shots each. The manifest's conservative ceiling is 160 sampling shots.

For an exact comparison, use 64-bit Windows CPython 3.12.15 with the versions pinned in [the smoke requirements](examples/smoke-regression/requirements.txt). Run these commands from the checkout or unpacked source distribution:

```powershell
python -m venv .build/smoke-venv
.\.build\smoke-venv\Scripts\python.exe -m pip install ".[ibm]" -r examples/smoke-regression/requirements.txt
.\.build\smoke-venv\Scripts\python.exe -m austins_algorithm constraint-regression examples/smoke-regression/manifest.json --output-dir .build/manila-smoke-results
```

Choose a fresh output directory for each run. Expect `REGRESSION_MATCHED` for the recorded environment and seed results. The fixture records completed calibration and validation and two independently verified witness shots in each four-shot holdout batch. The short holdout leaves the 80% reliability target unresolved. Receipt comparison includes recorded software versions, and the replay checks the saved compiler output. Results on other platforms or software stacks require separate verification. A wheel installation can run the command with the fixture directory from the source distribution.

Windows verification used the short `.build/smoke-venv` path after Aer reported a DLL path-length error in a deeper environment. A checkout with `core.autocrlf=true` preserved all four baseline files. An isolated installation from the source distribution matched the fixture from outside the source tree.

The [Manila smoke regression job](.github/workflows/tests.yml) runs on Windows alongside the test matrices. It provisions the recorded interpreter with pinned uv, builds and extracts the source distribution, installs the pinned dependencies, and replays the packaged fixture from outside the checkout. The regression exit status controls the job result. Look for `manila-smoke-evidence` for the summary, worker log, and any receipts after the replay step. [Astral's GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/)

Local execution of the workflow's PowerShell steps reproduced the baseline. A copy of the source archive with a changed report byte failed receipt preflight and saved the reason before any sampling worker launched. Hosted execution remains unverified.

To generate a candidate baseline for review, save the request and replay it into a new directory:

```powershell
python -m austins_algorithm constraint-cover examples/smoke-regression/constraint.json --ibm-model manila --snapshot-method prepared_density_matrix --initial-layout 0 1 --max-active-qubits 2 --rounds 2 --seed 7 --snapshot-shots 32 --evaluation-seeds 11 17 23 31 --witness-probability 0.8 --witness-max-shots 16 --witness-seeds 41 43 --save-replay-request .build/manila-candidate.json
python -m austins_algorithm constraint-replay .build/manila-candidate.json --receipt-dir .build/manila-candidate
```

#### Stop a holdout whose target is unreachable

Opt in with `--witness-stop-on-futility` or `WitnessBudgetRequest(..., stop_on_futility=True)`. The policy starts checking after the first completed batch for each eligible cover, giving that batch a chance to produce a witness. While seeds remain, the rule gives every remaining batch a success in the calculation. For `N` prespecified batches and `f` observed completed misses, the best possible result has `N - f` successes. The cover stops only when the one-sided 97.5% Clopperâ€“Pearson lower bound for that best case is strictly below the target. The other cover follows its own schedule and decision. [SciPy exact interval method](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html)

A stopped cover reports `STOPPED_FOR_FUTILITY` and retains the observed counts, any verified witness, and the original seed schedule. Its `futility_stop` records the completed misses, best-case success count and lower bound, and exact skipped seed suffix. The observed `lower_success_probability` is null because the full schedule is incomplete. The conclusion applies to that fixed schedule's ability to support the target. An unsubmitted seed could still have yielded a witness. A confirmed timeout disables futility for the affected cover, which continues its schedule and reports incomplete validation. An unconfirmed cancellation halts validation immediately.

```powershell
$jakartaSeeds = 1001..1144
python -m austins_algorithm constraint-cover examples/site_interlock.json --ibm-model jakarta --snapshot-method prepared_density_matrix --density-fusion --max-active-qubits 6 --initial-layout 1 2 0 6 3 5 --snapshot-shots 128 --evaluation-seeds 211 223 227 229 --witness-probability 0.95 --witness-seed-schedule @jakartaSeeds --witness-stop-on-futility --native-qasm
```

Four alternating pairs compared the full schedule and the stop policy on warm prepared plans with the dated Jakarta and Torino models. Both arms used the same compilation, density cache, calibration seeds, and fixed 95% target. The three holdout schedules used the archived seeds with offsets of 0, 10,000, and 20,000. Every executed count map, unchanged complete-cover result, and OpenQASM export matched the archive. The table gives the three stops and ChronoRealm's median holdout times.

| Offline protocol and cover | Seed offset | Last submitted seed | Best-case lower bound | Jobs skipped | Shots skipped | Holdout, full to stop |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Jakarta interlock, complement | 0 | 1066 | 92.503% | 39 | 1,716 | 158.75 to 130.39 ms |
| Jakarta interlock, direct | 20,000 | 21095 | 92.503% | 24 | 1,224 | 163.60 to 134.41 ms |
| Torino sensor kit, complement | 20,000 | 22160 | 93.698% | 32 | 512 | 242.69 to 236.49 ms |

The other three schedules completed in full. Timing varied with simulator wait and the cost of the additional bound checks. The avoided jobs and shots above are exact for these seeded offline schedules.

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

#### Opt-in Aer fusion for prepared density

Use `--density-fusion` with `--snapshot-method prepared_density_matrix`. The Python snapshot API accepts `density_fusion=True`. The option sets Aer fusion on the gate-only preparation job, with threshold 5 and maximum fused width 2. It uses the compiled circuit and the calibrated gate channels. The separate readout replay still uses the prepared matrix, local readout channels, original measurement destinations, and each batch's prescribed seed. [Aer simulator options](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.AerSimulator.html)

```powershell
$torinoFullSeeds = 2001..2224
python -m austins_algorithm constraint-cover examples/field_sensor_kit.json --ibm-model torino --snapshot-method prepared_density_matrix --density-fusion --max-active-qubits 7 --initial-layout 92 98 81 80 100 82 99 --snapshot-shots 128 --evaluation-seeds 1301 1303 1307 1319 --witness-probability 0.95 --witness-seed-schedule @torinoFullSeeds --native-qasm
```

`snapshot.density_fusion_requested` records the job options. Each completed cover's preparation records Aer fusion observations when supplied, including whether fusion applied and its method. A circuit may complete with no fusion applied. A timed-out preparation records the timeout and cancellation outcome and ends the workflow before sampling.

Four alternating warm pairs on the dated Torino snapshot used Aer 0.18.2, Qiskit 2.6.2, and Runtime 0.51.0. Both covers reported applied superoperator fusion. ChronoRealm median density preparation fell from 0.983 to 0.530 s, and median inner workflow time fell from 2.524 to 2.069 s. The largest elementwise difference between the saved default and fused matrices was `1.67e-16`, below the prespecified absolute tolerance of `1e-12`. The maximum trace difference was `3.89e-15`. These are comparisons of the raw matrices Aer supplied for replay.

All 228 seeded calibration and holdout count maps matched the archived protocol, along with the compiled hashes, measurement maps, and witness bounds. Both covers again produced 111 verified batches out of 112 and a one-sided 97.5% lower success bound of 95.1257%. Exact count agreement describes this fixed snapshot, circuits, SDK, and seed schedule.


The same comparison is available as a packaged example. Install the IBM extra and run either preset from the package root. The example writes JSON to stdout by default. Use `--output` to write to a new UTF-8 file in an existing directory.

```powershell
python examples/benchmark_density_fusion.py --model jakarta
python examples/benchmark_density_fusion.py --model torino
```

Each preset verifies its formula digest and runs one warmup pair, then four timed pairs in alternating order. It checks the completed seed schedules and compares the full reports after excluding timings and the fusion and trace fields. It then prepares the raw pre-readout matrices again, outside the timed trials, and compares each cover at absolute tolerance `1e-12` for both the largest elementwise difference and the trace difference. The report includes per-stage timings, observed Aer fusion metadata, and the last full report for each arm.

On the Jakarta snapshot dated 2024-05-27, the site interlock used six active wires. Both covers reported superoperator fusion. On the same local stack as Torino, the ChronoRealm median preparation time moved from 0.248 to 0.227 s and median workflow time from 0.979 to 0.930 s. The maximum elementwise difference was `1.18e-16` and the maximum trace difference was `5.33e-15`. The Torino preset repeated its larger preparation improvement, from 0.993 to 0.540 s in this run.

Jakarta matched all 148 archived count maps and preserved the verified OpenQASM digests, wire maps, and witness results. The direct cover supported the 95% target with 72 of 72 verified batches and a 95.0056% lower success bound. The complement returned 71 of 72 with a 92.5029% lower bound and remained unresolved. The compiler still selected the complement on resource cost. Torino matched all 228 archived count maps and both witness decisions. These are repeated offline simulations using the same seeded schedules on dated snapshots.

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

Add `--native-qasm` to `compile`, `constraint`, or `constraint-cover` to prefer Qiskit's experimental native importer for the generated OpenQASM. For `constraint`, it applies to the selected synthetic-line or offline IBM target. Use `--compile`, `--ibm-model`, or both. The Python compiler, constraint snapshot, and cover APIs accept `prefer_native_qasm=True`. Reports record the preference in `openqasm.preferred_importer` and the successful importer in `openqasm.importer`.

The existing default prefers the standard importer when installed. A native parse rejection uses the installed standard importer. A successful reimport proceeds through the same verification, and a verification failure ends the workflow. Both paths check the reimported circuit type, declared quantum and classical widths, and instruction and statevector work limits before allocating the statevector. Verification checks unique terminal measurements, their classical mapping, finite normalized probabilities, and all three full-distribution comparisons. The scope is the measured distribution from the initialized input. [Qiskit OpenQASM importer documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3)

```powershell
python -m austins_algorithm compile --problem austin --native-qasm
python -m austins_algorithm constraint examples/telemetry_power.json --strategy known --synthesis clause-flags --max-shots 4 --max-oracle-queries 4 --ibm-model torino --snapshot-shots 4 --native-qasm
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

## Workflow profile after the audit changes

The ChronoRealm warm run repeated the existing sensor-kit and telemetry-power protocols. The table uses medians for the fresh requests and the actual setup and first request times for each prepared plan. The prepared plan holds the input, audit, and verified compilation and initializes its probability table on the first affordable request.

| Ideal workflow | Sensor kit | Telemetry power |
| --- | ---: | ---: |
| Fresh request, median | 197.8 ms | 532.2 ms |
| Oracle audit and solution check, median | 25.4 ms | 308.2 ms |
| Sampling and per-attempt verification, median | 19.7 ms | 35.5 ms |
| Compilation, export and verification, median | 150.9 ms | 192.5 ms |
| Prepared setup | 181.3 ms | 573.3 ms |
| First prepared request | 19.3 ms | 26.9 ms |
| Later prepared request, median | 0.091 ms | 0.157 ms |

The sensor kit spent the largest share of fresh ideal time on compilation and verification. The telemetry audit was still the largest fresh ideal stage. Each prepared ideal arm reproduced all 128 verified witnesses. The timings come from this local run, and independently computed phase medians need not add to the total median.

The offline Torino sensor-kit run spent 935 ms preparing gate-only density states, 787 ms compiling and auditing the two covers, 275 ms constructing the noise model, and 281 ms on holdout validation. It reproduced all 228 calibration and holdout count maps and the prior witness bounds. These are offline snapshot timings.

### Native import on the constraint route

For the telemetry-power compile-only Torino circuit, six warm alternating compiler pairs kept the same circuit, target, and seed. The standard import stage took a median 351.5 ms and the native stage took 9.6 ms. Median full compile and verification time fell from 594.2 to 238.1 ms. Both reports had identical OpenQASM bytes, wire maps, and original, compiled, and round-trip probability distributions. The phase profile used Qiskit 2.6.2, Runtime 0.51.0, NumPy 2.5.4, and standard importer 0.6.0.

The new `constraint --native-qasm` option sends the preference to either selected compiler. An actual CLI replay verified the telemetry export on Torino and a 16-shot field sensor simulation on Jakarta. With standard and native importers, it reproduced the same exports, verification distributions, and Jakarta counts. The experimental importer and standard parse fallback follow the same verification rules described above.

### Compile for an offline IBM snapshot

Install `.[ibm]` to compile against the bundled Manila, Jakarta, and Torino targets. `compile` exports a built-in search circuit with zero sampling shots. `constraint --compile` exports the last attempted circuit from a local search of a JSON constraint. Either command can choose an IBM snapshot with `--compile-target`. The synthetic line is the default target. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
python -m austins_algorithm compile --problem austin --compile-target manila `
  --native-qasm --qasm-output .build/austin-manila.qasm
python -m austins_algorithm constraint examples/field_sensor_kit.json --strategy known `
  --max-shots 4 --max-oracle-queries 8 --compile --compile-target jakarta `
  --native-qasm --qasm-output .build/field-kit-jakarta.qasm
```

Each report names the actual importer, the snapshot's backend name and version, the snapshot calibration date, and a SHA-256 of the bundled configuration and properties. Keep the report with the portable export so the compact wire indices can be traced to target qubits. Use a fresh file path for each export. The Python entry point is `austins_algorithm.ibm_models.compile_ibm_snapshot`, which returns the compiled circuit, verified OpenQASM text, and report.

Use `--line-width` with a synthetic line. An undersized snapshot fails with the available qubit count and required circuit width before writing the export. The existing `constraint --ibm-model` option requests a separate local noisy simulation. `--native-qasm` tries the experimental parser and reports the actual parser used. The [custom-gate regression](tests/test_compilation.py) exercises standard fallback and verifies the resulting probability when the native parser rejects the `twist` export. [Qiskit OpenQASM importer documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3#experimental-import-interface)

On 2026-11-11, both copies checked the following four fixtures with CPython 3.12.15, Qiskit 2.6.2, IBM Runtime 0.51.0, and the standard importer 0.6.0.

| Circuit | Snapshot | Export bytes | Active wires | Measured bits |
| --- | --- | ---: | ---: | ---: |
| Austin | Manila | 998 | 3 | 3 |
| Modulo-4 direct oracle | Jakarta | 3,199 | 4 | 4 |
| Modulo-4 work-qubit oracle | Torino | 16,372 | 5 | 4 |
| Field sensor kit | Jakarta | 16,410 | 7 | 6 |

Every standard/native pair produced byte-identical exports and identical measurement distributions. The largest total variation distance from the analytic ideal distribution was `1.51e-14`. The seven-wire field kit was rejected on the five-qubit Manila target. The compiled distributions are ideal statevector evidence from these local snapshots. Physical execution remains unverified.

### Faster ideal verification for stock gates

Compilation still evolves the original, compiled, and reimported circuits separately from the all-zero state. For a measured body with up to eight active wires and at least 64 gates, verification can apply the actual matrices of stock RZ, SX, X, CX, and CZ gates directly. It reuses each ordered operand tuple's tensor axes within that evolution. The Qiskit circuit evaluator handles other operations, custom matrix or operand behavior, and circuits outside those bounds. Both paths use the same terminal-measurement mapping and probability checks. [Qiskit Statevector API](https://quantum.cloud.ibm.com/docs/en/api/qiskit/2.0/qiskit.quantum_info.Statevector)

Four warm alternating pairs ran the complete prepared-density workflow with fusion enabled. Each request used the same fixed layout and archived calibration and validation schedules, with Qiskit 2.6.2, Aer 0.18.2, and Runtime 0.51.0. These ChronoRealm medians cover both phase covers.

| Offline protocol | Six ideal distributions | Compile and audit | Whole workflow |
| --- | ---: | ---: | ---: |
| Jakarta site interlock | 234.0 to 74.5 ms | 356.1 to 205.2 ms | 922.2 to 799.5 ms |
| Torino field sensor kit | 456.6 to 141.5 ms | 914.7 to 653.4 ms | 2,200.0 to 2,005.8 ms |

The ideal probability dictionaries, oracle audits, measurement maps, and OpenQASM bytes matched the reference. Both OpenQASM importers reproduced all 148 Jakarta and 228 Torino archived calibration and validation count maps and both protocols' witness decisions. These timings are local measurements for the named circuits, dated snapshots, and software stack.

### Reuse a verified snapshot compilation

`PreparedConstraintCoverSnapshot` owns the normalized Boolean formula, dated local backend, fixed layout, and verified phase-cover programs. Use one plan for repeated requests with the same compiler inputs. Each run validates its shots and seeds. When sampling can proceed, it constructs its own simulator and noise state and verifies the sampled candidates against the formula. Calls on a single plan are serialized. The plan exposes detached reports and portable OpenQASM strings by cover name.

```python
from austins_algorithm.constraint_cover_comparison import PreparedConstraintCoverSnapshot
from austins_algorithm.constraints import load_constraint

plan = PreparedConstraintCoverSnapshot(
    load_constraint("examples/site_interlock.json"),
    model="jakarta", initial_layout=(1, 2, 0, 6, 3, 5),
    max_active_qubits=6, prefer_native_qasm=True,
)
first = plan.run(shots=128, evaluation_seeds=(211, 223, 227, 229))
second = plan.run(shots=128, evaluation_seeds=(307, 311, 313, 317))
selected = second["selected_cover"]
openqasm = None if selected is None else plan.openqasm[selected]
```

For holdout validation, pass a `WitnessBudgetRequest` to each run. Reuse a seed schedule to replay a result or supply a different schedule for fresh draws. `snapshot.compilation_source` records `PREPARED_PLAN`. Per-run witness timings cover the new request. Measure one-time compilation at construction. To change the model, formula, layout, or compiler options, construct a new plan. [IBM offline snapshot model](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/dev/fake-provider-fake-jakarta-v2)

The example below times complete requests on the same Jakarta and Torino layouts and archived formulas. It warms both arms, alternates order across four measured pairs, and charges construction to every prepared series. Each series has three requests, with fixed calibration seeds and distinct holdout schedules. Both arms run prepared-density sampling with fusion. The normalized full reports and both exports agree within every pair. The first request also reproduces the archived 148 Jakarta and 228 Torino count maps.

```powershell
python examples/benchmark_prepared_snapshot.py --model jakarta
python examples/benchmark_prepared_snapshot.py --model torino
```

These ChronoRealm medians are cumulative wall times. The observed setup medians were 0.235 s for Jakarta and 0.554 s for Torino.

| Offline protocol | Requests | Fresh | Prepared, including setup | Pairs faster with reuse |
| --- | ---: | ---: | ---: | ---: |
| Jakarta site interlock | 1 | 0.759 s | 0.821 s | 0 of 4 |
| Jakarta site interlock | 2 | 1.557 s | 1.319 s | 4 of 4 |
| Jakarta site interlock | 3 | 2.332 s | 1.855 s | 4 of 4 |
| Torino field sensor kit | 1 | 1.737 s | 1.649 s | 3 of 4 |
| Torino field sensor kit | 2 | 3.393 s | 2.612 s | 4 of 4 |
| Torino field sensor kit | 3 | 5.115 s | 3.679 s | 4 of 4 |

The second request was the first point where reuse led in every observed pair on both snapshots. The apparent first-request lead on Torino varied by pair. The measurements apply to the named dated public models, layouts, schedules, and local software stack.

#### Reuse pre-readout density across requests

Set `cache_density=True` when constructing a `PreparedConstraintCoverSnapshot`. Use `simulation_method="prepared_density_matrix"` on the requests that should use it. The first successful preparation retains the exact raw density bytes, ordered measurements, and calibrated readout inputs for both covers. Each cache hit builds new replay circuits and Aer simulators. Readout shots, seeds, witness budgets, count maps, and verification are per request. [Aer state instructions](https://qiskit.github.io/qiskit-aer/tutorials/1_aersimulator.html)

Each plan keeps a separate entry for each fusion setting it uses. The two entries together retain at most 1 MiB of raw matrix payload under the seven-wire cap, plus readout and measurement data. Entry lifetime follows the plan. The first request reports `snapshot.density_cache.status` as `FILLED` after both covers succeed, or `MISS` if preparation stops before publication. Subsequent requests report `HIT`. Its `matrix_bytes` refers to the selected entry. Hit reports retain the original trace and fusion evidence and record new replay setup time as `density_replay_setup`.

```powershell
python examples/benchmark_prepared_snapshot.py --density-cache --model jakarta
python examples/benchmark_prepared_snapshot.py --density-cache --model torino
```

Each arm already uses a prepared compilation. The benchmark warms one pair and times four alternating pairs, using a new plan per series and three requests with distinct holdout schedules. Timed wall includes plan construction, cache fill, replay, and verification. These ChronoRealm medians used Qiskit 2.6.2, Aer 0.18.2, and Runtime 0.51.0 with fusion enabled.

| Offline protocol | Second request, uncached to cached | Three requests including setup | Matrix payload per entry |
| --- | ---: | ---: | ---: |
| Jakarta site interlock | 0.685 to 0.206 s | 2.336 to 1.178 s | 128 KiB |
| Torino field sensor kit | 1.020 to 0.340 s | 3.666 to 2.483 s | 512 KiB |

By the third request, cache reuse led in all four pairs on each snapshot. First-request timing varied. Every paired request matched the archived OpenQASM, trace and fusion evidence, seeded count maps, and witness results. Each request had 148 calibration and holdout batches on Jakarta or 228 on Torino.

#### Profile and reduce holdout verification work

Each completed snapshot batch exposes `submit`, `result_wait`, `count_format`, and `count_verify` in its `timing_seconds`. The result-wait timer covers the call to the local Aer job, which can include scheduling, execution, and result construction. The aggregate `sample` and `verify` timings cover the broader stages. Timeout reports include submission and result-wait timing along with the existing `sample_wait` through the cancellation attempt. [Aer job API](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.jobs.AerJob.html)

Count verification validates and snapshots the input mapping before invoking the verifier once per observed bitstring in sorted order. The snapshot gives consistent counts and verified-shot totals even if a callback changes the caller's mapping. A prepared plan checks each observed key against a private immutable set of the keys accepted by its original Boolean formula during the exhaustive audit. The audit evaluates that formula for every basis key and separately checks oracle phase and workspace. Each holdout seed still has its own Aer submission, result deadline, and verification.

The initial ChronoRealm profile measured about 7 ms of submission, 136 ms of result wait, 14 ms of count formatting, and 21 ms of count validation and predicate checks on the 144-batch Jakarta holdout. On Torino's 224-batch holdout, the corresponding measurements were about 11, 222, 13, and 21 ms. These are medians of four warm requests with the first archived seed schedule and an instrumented local Aer runner.

| Offline protocol | Isolated archived count maps, reference to prepared | Holdout count verification, reference to prepared | Warm request, reference to prepared |
| --- | ---: | ---: | ---: |
| Jakarta site interlock | 14.41 to 2.48 ms | 17.55 to 4.51 ms | 175.39 to 154.43 ms |
| Torino field sensor kit | 12.86 to 2.30 ms | 16.50 to 4.83 ms | 254.29 to 240.32 ms |

The ChronoRealm comparison used four alternating pairs of three warm requests on the dated public snapshots, with the same timing instrumentation in both arms. The reference used the previous count routine and evaluated the formula per observed key. The prepared arm used the validated count snapshot and lookup. The isolated comparison also measured the count cleanup alone at 12.06 ms for Jakarta and 11.49 ms for Torino. All archived seeded count maps, compilation evidence, and witness reports matched across the three schedules. Aer wait varied by request and still took the largest share of the wall time.

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

Use `constraint-sweep --compare-diffusers` to compare both diffusers on the same oracle, two starting layouts, and one and two Grover rounds. Each invocation uses one offline IBM snapshot and reports all four comparisons. Use `--evaluation-seeds` for four seeds, which remains the default schedule size. For a longer run, use `--evaluation-seed-schedule` with 4 to 16 distinct seeds, in alternating standard and clean-ancilla order. Every compiled variant reuses its verified circuit for 2 to 8 equal-shot batches. The report records the full schedule and its batch ceiling. At the maximum of 8 batches per variant and 256 shots per batch, one snapshot comparison schedules up to 64 sampling batches and 16,384 shots.

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model torino --shots 128 --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --evaluation-seeds 101 103 107 109
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model jakarta --shots 128 --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --evaluation-seeds 113 127 131 137
```

The command verifies the full ideal measurement distribution of each variant and records the OpenQASM digest, active wires, native gate counts, and per-seed counts. It pools completed batches within each fixed compiled circuit. Both variants must complete their entire schedules before the cell gets a hit-rate difference. A confirmed cancellation leaves that cell incomplete and allows the remaining batches to run. An unconfirmed cancellation stops further sampling and keeps the evidence already obtained.

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

### Longer fixed seed schedules

The follow-up kept the compiled circuits, placements, simulator method, 128 shots per batch, and earlier seed prefix fixed for each snapshot. Two additional seeds per variant were chosen before the run. Both copies completed all 32 batches per snapshot and reproduced the archived prefix count maps, circuit exports, and gate counts. Each variant now has 512 shots. The earlier and longer intervals share the prefix, so the width comparison is descriptive.

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model jakarta --shots 128 --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --evaluation-seed-schedule 113 127 131 137 2003 2011 2017 2027
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --ibm-model torino --shots 128 --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --evaluation-seed-schedule 101 103 107 109 1009 1013 1019 1021
```

Differences and interval widths are in percentage points. The simultaneous confidence level applies within each snapshot.

| Snapshot | Placement | Rounds | Verified hits, standard → clean | Difference and interval | Width, earlier → longer |
|---|---:|---:|---:|---:|---:|
| Jakarta | 1 | 1 | 113 → 120 | +1.4 [−8.9, +11.6] | 29.2 → 20.6 |
| Jakarta | 1 | 2 | 102 → 95 | −1.4 [−11.0, +8.3] | 28.5 → 19.4 |
| Jakarta | 2 | 1 | 99 → 119 | +3.9 [−6.2, +13.9] | 27.4 → 20.1 |
| Jakarta | 2 | 2 | 59 → 93 | +6.6 [−2.1, +15.3] | 24.3 → 17.4 |
| Torino | 1 | 1 | 191 → 187 | −0.8 [−12.6, +11.0] | 33.1 → 23.6 |
| Torino | 1 | 2 | 114 → 157 | +8.4 [−2.4, +19.1] | 30.2 → 21.5 |
| Torino | 2 | 1 | 163 → 181 | +3.5 [−8.1, +15.0] | 33.0 → 23.1 |
| Torino | 2 | 2 | 107 → 148 | +8.0 [−2.6, +18.5] | 30.0 → 21.1 |

The widths fell by 26.8–32.0%. All eight intervals included zero. The clean-ancilla gate savings held across the tested cells, and the hit-rate effect on these snapshots remains unresolved. In the concurrent verification runs, each Torino comparison took about 420 seconds on the matrix-product-state path. This elapsed time motivates testing state preparation reuse for fixed diffuser circuits.

### Reuse prepared diffuser states

Use `constraint-sweep --compare-diffusers --snapshot-method prepared_density_matrix` to evolve each verified compact circuit once and reload its density matrix for every batch. The original routed circuit supplies the OpenQASM, physical wire mapping, and nominal gate counts. The reload applies calibrated readout channels in the original measurement order. The comparison records one preparation per variant and a separate eight-job preparation limit alongside the existing sampling ceiling. A preparation timeout ends the run with `STATE_PREPARATION_INCOMPLETE` and retains earlier evidence. [Aer density save](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.SaveDensityMatrix.html) · [Aer density load](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.SetDensityMatrix.html)

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method prepared_density_matrix --ibm-model torino --shots 128 --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --evaluation-seed-schedule 101 103 107 109 1009 1013 1019 1021
```

For the physical-index reference, use `--snapshot-method density_matrix` with the same formula, layout, shots, and seeds. Both explicit methods report noise-model setup, state preparation, and repeated-batch sampling time, including count verification. Use the original command without the option to select the snapshot's established method.

#### Physical-index verification and timing

The local comparison used the longer Jakarta and Torino schedules above, with 32 batches of 128 shots per method and snapshot. Each copy ran one pair per snapshot, with physical-index density first in ChronoRealm and prepared density first on E:. All compiled reports and physical measurement maps matched. Times are seconds for these paired runs on the same machine and SDK stack.

| Snapshot | Copy | Noise setup, physical → prepared | State preparation | Batch time, physical → prepared | Total, physical → prepared |
|---|---|---:|---:|---:|---:|
| Jakarta | ChronoRealm | 0.11 → 0.20 | 1.95 | 4.89 → 0.08 | 9.38 → 6.74 |
| Jakarta | E: | 0.13 → 0.20 | 1.38 | 5.78 → 0.06 | 9.72 → 5.14 |
| Torino | ChronoRealm | 4.58 → 0.42 | 2.32 | 53.25 → 0.07 | 65.21 → 8.15 |
| Torino | E: | 3.98 → 0.34 | 2.19 | 52.45 → 0.04 | 61.68 → 8.17 |

A separate numerical audit rebuilt all eight variants per snapshot and matched their compiled records against the physical reference. For each variant it saved the 128 × 128 gate-evolved density matrix on the ordered active wires, compared it with the loaded matrix, checked the physical readout matrices and classical destinations, and compared all 64 output probabilities. The largest observed difference was zero for each comparison at the declared absolute tolerance of `1e-10` in both copies.

Within each method, the two copies reproduced all seeded counts and hit-rate summaries. Between the two methods, same-seed count maps matched in 16 of 32 Jakarta batches and 24 of 32 Torino batches. Record the method with the seeds for exact replay. The probability check applies to the tested static circuits and packaged simulator noise. Physical hardware performance remains unverified.

### Calculate modeled diffuser hit probabilities

Use `--snapshot-method modeled_probabilities` with `constraint-sweep --compare-diffusers`. The mode prepares each verified static circuit once, normalizes its density diagonal to remove floating-point trace drift, marginalizes to the verified classical destinations, and applies the snapshot's single-wire readout channels. Each variant reports all classical outcome probabilities and their sum over the assignments accepted by the Boolean verifier. The cell reports the modeled clean-minus-standard hit-rate difference. [Qiskit bit order](https://quantum.cloud.ibm.com/docs/en/qiskit/bit-ordering) · [Aer readout transition probabilities](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.ReadoutError.html)

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model torino --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99
```

This mode uses up to eight local gate-only density saves and zero measurement batches. The report retains the raw preparation traces and records compilation, probability calculation, and preparation status. Explicit shot and sampling-seed flags are rejected. A timed-out preparation keeps completed evidence and leaves affected modeled results unset. The modeled probabilities refer to the packaged noise model. Physical-device uncertainty and performance require separate evidence.

The local verification compared every reported output with an independent physical-index density calculation and its mapped readout channels. The same fixed sensor-kit circuits and placements produced 1,024 checked outcome probabilities across Jakarta and Torino, with a maximum absolute difference of `2.8e-17` in each copy. The existing sampled density modes also reproduced their archived Jakarta reports after excluding measured durations.

| Snapshot | Placement | Rounds | Standard hit probability | Clean-ancilla hit probability | Modeled gain, percentage points |
|---|---:|---:|---:|---:|---:|
| Jakarta | 1 | 1 | 19.68% | 21.69% | +2.01 |
| Jakarta | 1 | 2 | 16.08% | 17.05% | +0.97 |
| Jakarta | 2 | 1 | 19.19% | 21.06% | +1.86 |
| Jakarta | 2 | 2 | 15.81% | 16.71% | +0.90 |
| Torino | 1 | 1 | 35.40% | 40.12% | +4.72 |
| Torino | 1 | 2 | 24.63% | 28.61% | +3.97 |
| Torino | 2 | 1 | 35.35% | 39.73% | +4.37 |
| Torino | 2 | 2 | 25.12% | 28.91% | +3.79 |

### Plan witness shots from a modeled diffuser rate

Add `--witness-probability` to the modeled diffuser command to size a batch for at least one verified witness. For per-shot hit probability (p), the modeled chance of at least one hit in (n) independent shots is `1 - (1 - p)^n`. The planner compares the reported probabilities and target at integer shot counts and returns the smallest feasible budget. Supply a target strictly between zero and one and an optional `--witness-max-shots` from 1 to 256. The default cap is 256. [NIST binomial probability model](https://www.itl.nist.gov/div898/handbook/eda/section3/eda366i.htm)

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model torino --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --witness-probability 0.95
```

Each variant reports its required shots, modeled success at the cap and at the planned budget, and the compiled two-qubit gate count multiplied by all planned shots. A zero modeled hit rate or an insufficient cap leaves the budget and nominal cost unset. The per-cell report lists feasible diffusers and gives nominal gate savings when both budgets fit. Planning uses the modeled distribution and executes zero measurement batches. The totals describe complete fixed-size batches. Physical-device success rates and execution costs require separate evidence.

Both copies reproduced the following plans for a 95% modeled witness target. A separate integer scan checked every minimum. Clean-ancilla reduced nominal two-qubit gate executions by 22.5–35.2% across these paired plans.

| Snapshot | Placement | Rounds | Shots, standard → clean | Nominal two-qubit gates, standard → clean | Gates saved |
|---|---:|---:|---:|---:|---:|
| Jakarta | 1 | 1 | 14 → 13 | 2,730 → 2,067 | 663 |
| Jakarta | 1 | 2 | 18 → 17 | 7,128 → 5,508 | 1,620 |
| Jakarta | 2 | 1 | 15 → 13 | 3,015 → 2,145 | 870 |
| Jakarta | 2 | 2 | 18 → 17 | 7,236 → 5,610 | 1,626 |
| Torino | 1 | 1 | 7 → 6 | 1,554 → 1,062 | 492 |
| Torino | 1 | 2 | 11 → 9 | 5,049 → 3,294 | 1,755 |
| Torino | 2 | 1 | 7 → 6 | 1,575 → 1,080 | 495 |
| Torino | 2 | 2 | 11 → 9 | 4,917 → 3,186 | 1,731 |

With a nine-shot cap, all Jakarta variants exceeded the cap. Both Torino one-round pairs stayed feasible. The two-round Torino cases admitted only clean-ancilla, with paired savings left unset.

### Validate modeled witness budgets on fresh seeds

Add `--witness-seed-schedule` to a modeled diffuser comparison with `--witness-probability`. Supply 2 to 256 distinct seeds in alternating standard and clean-ancilla order, with an even total length. Choose seeds unused in earlier measurements for the same experiment. The command fixes every budget before sampling and reuses each verified prepared circuit at its own planned shot count. The same schedule is used at each placement and round count. Variants whose budget exceeds the cap are reported and skipped.

```powershell
$witnessSeeds = 20001..20200
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model torino --transpiler-seed 7 --placement 92 98 81 80 100 82 99 --placement 81 80 92 98 82 100 99 --witness-probability 0.95 --witness-seed-schedule @witnessSeeds
```

A batch succeeds when the verifier accepts at least one measured outcome. Each complete schedule gets a one-sided exact Clopper–Pearson lower bound at 99.375% confidence. The Bonferroni adjustment gives at least 95% simultaneous coverage across the eight prespecified variants within each snapshot under the fixed batch model. For a 95% witness target, 99 successes in 99 batches is the first all-success schedule that clears the bound. [SciPy exact proportion interval](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html) · [NIST Bonferroni method](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm)

The validation report records the seed lists, planned and completed shots, observed success fractions, and completed nominal two-qubit gate executions. It can schedule up to 1,024 batches. An incomplete variant retains its observed counts and leaves its bound unset. A confirmed cancellation lets later batches proceed. An unconfirmed cancellation stops new launches. Validation begins after preparation and planning complete.

The local run used 100 batches per variant and seed ranges `10001..10200` for Jakarta and `20001..20200` for Torino. Both copies reproduced the 1,600 batches and 19,100 shots. Reusing the same seeds on E: checked reproducibility. The table lists the observed number of witness-bearing batches and the simultaneous lower bounds.

| Snapshot | Placement | Rounds | Successful batches of 100, standard → clean | Lower bound, standard → clean |
|---|---:|---:|---:|---:|
| Jakarta | 1 | 1 | 97 → 100 | 89.72% → 95.05% |
| Jakarta | 1 | 2 | 97 → 99 | 89.72% → 93.04% |
| Jakarta | 2 | 1 | 96 → 99 | 88.22% → 93.04% |
| Jakarta | 2 | 2 | 93 → 95 | 84.03% → 86.78% |
| Torino | 1 | 1 | 95 → 96 | 86.78% → 88.22% |
| Torino | 1 | 2 | 94 → 100 | 85.39% → 95.05% |
| Torino | 2 | 1 | 96 → 98 | 88.22% → 91.31% |
| Torino | 2 | 2 | 97 → 93 | 89.72% → 84.03% |

The clean-ancilla variant at Jakarta placement 1 and one round, and the clean-ancilla variant at Torino placement 1 and two rounds, each reached a 95.05% lower bound. The other 14 remained unresolved at the 95% target. A separate nine-shot-cap check skipped all Jakarta variants and completed 12 batches across the six feasible Torino variants. These observations apply to the tested offline snapshot simulations.

### Check a diffuser holdout before sampling

Use `witness-schedule --comparison diffuser` to size a holdout for the same eight-variant confidence family used by diffuser validation. Set the target, the tolerated misses per variant, and an optional `--max-batches-per-variant` from 1 to 128. The default miss allowance is zero and the default cap is 128. The planner uses the one-sided 99.375% Clopper–Pearson bound for each variant. This command needs the statistical calculation only and runs before formula loading, compilation, or measurement. The Python entry point is `plan_diffuser_witness_schedule`. [SciPy exact proportion interval](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html)

```powershell
python -m austins_algorithm witness-schedule --comparison diffuser --target-probability 0.95
$witnessSeeds = 40001..40198
python -m austins_algorithm witness-schedule --comparison diffuser --target-probability 0.95 --witness-seed-schedule @witnessSeeds
python -m austins_algorithm witness-schedule --comparison diffuser --target-probability 0.95 --tolerated-misses 1
```

The proposed list follows the runtime's alternating standard and clean-ancilla order. Its report gives the hypothetical lower bound at the stated miss allowance and the maximum jobs if all eight variants qualify. Use `proposed_schedule.status` to assess the list. The report gives the extra seeds for `SCHEDULE_TOO_SHORT`. `SCHEDULE_EXCEEDS_CAP` identifies a list above the chosen cap. `BATCH_CAP_EXCEEDED` means the target and miss allowance require more batches than the cap permits. These are sizing results. Observed witness counts come from a completed holdout.

At a 95% target, 98 all-success batches give a 94.9531% bound. The first feasible all-success schedule has 99 batches per variant, 198 seeds, and a ceiling of 792 jobs. Its bound is 95.0027%. With one tolerated miss, the 128-batch cap gives 94.5268%. An independent exact binomial tail calculation found the uncapped minimum of 141 batches. Both copies verified the boundaries and matched all 16 archived Jakarta and Torino holdout bounds at their observed miss counts. The phase-cover defaults continued to size their zero-miss and one-miss schedules at 72 and 110 batches.

### Stop a diffuser holdout whose target is unreachable

Add `--witness-stop-on-futility` to a modeled diffuser comparison with an explicit witness seed schedule and target. The Python equivalent is `witness_stop_on_futility=True` on `run_constraint_diffuser_comparison`. Each feasible variant tries at least one batch. As long as seeds remain, the rule checks the one-sided 99.375% lower bound the variant would have if all remaining batches succeeded. It skips that variant's remaining seeds when the best-case bound is strictly below the target. Other variants follow their own schedules. The flag is off by default.

```powershell
$witnessSeeds = 10001..10200
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model jakarta --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --witness-probability 0.95 --witness-seed-schedule @witnessSeeds --witness-stop-on-futility
```

A stopped variant reports `STOPPED_FOR_FUTILITY`, the original plan, its completed batch outcomes, and the `futility_stop` record with the exact skipped seed suffix and hypothetical best-case lower bound. Its observed `lower_success_probability` is null because the prespecified schedule is incomplete. Skipped seeds could still have produced witnesses. The aggregate counts intentional skips in `skipped_batches_for_futility` and `skipped_shots_for_futility`. A confirmed timeout disables futility for the affected variant and lets later batches proceed. An unconfirmed cancellation stops new jobs. An incomplete batch keeps the overall report partial, even if another variant stopped intentionally.

Both copies replayed the fixed Jakarta and Torino schedules above at the 95% target. Every executed outcome matched the original run at the same seed, and the two variants that supported the target retained their complete results. The default replay also matched the archived results excluding timing fields.

| Offline snapshot | Completed batches, full → stop | Shots skipped | Nominal two-qubit gate executions skipped |
|---|---:|---:|---:|
| Jakarta | 800 → 283 | 8,294 | 2,494,857 |
| Torino | 800 → 323 | 3,929 | 1,279,425 |

The replay compares deterministic work counts on the two local IBM snapshots with the same seeds. It provides no independent holdout sample or physical QPU measurement.

### Estimate diffuser validation power before sampling

Add `--assumed-batch-success-probability` to `witness-schedule --comparison diffuser` to estimate how often one variant's planned holdout would support the target. For a modeled witness budget, use the variant's `success_probability_at_budget`. It describes a complete batch at the planned shot count. The single-shot rate `modeled_hit_probability` represents a different event. The calculation assumes independent batches with the supplied success probability fixed within the variant.

For each schedule size, the planner finds the fewest successful batches whose one-sided 99.375% Clopper–Pearson lower bound reaches the target. It then sums the binomial probabilities from that success count through the full schedule. When even an all-success schedule falls short, the required success count is null and the chance of target support is zero. [SciPy exact proportion interval](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html) · [SciPy binomial distribution](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binom.html)

```powershell
python -m austins_algorithm witness-schedule --comparison diffuser --target-probability 0.95 --assumed-batch-success-probability 0.99 --minimum-validation-power 0.35
```

The Python entry point is `plan_diffuser_witness_schedule` with `assumed_batch_success_probability` and optional `minimum_validation_power`. The `validation_power` report describes the cap, the minimum size allowed by the requested miss allowance, and any proposed seed schedule. `best_within_cap` reports the highest probability among schedules that meet the miss allowance and cap. `smallest_meeting_minimum_power` reports the first eligible size that reaches the requested power, when one exists. Each result gives the number of batches and alternating seeds. When displayed probabilities round to the same value, selection still uses the exact binomial sums for the supplied numeric inputs.

At a 95% target and assumed 99% batch success, 99 batches give a 36.97% chance of passing and 128 give 27.63%. Both sizes require all batches to succeed, so 99 is the smallest eligible schedule to meet a 35% power requirement. Increasing the size can lower the chance of passing while the allowed number of misses stays fixed. `NO_ELIGIBLE_SCHEDULE` indicates that the target and requested miss allowance cannot both fit under the cap. `MINIMUM_POWER_UNREACHABLE` means eligible sizes exist and their modeled chances fall below the requested power.

Both local copies checked the calculations against independent beta quantiles and binomial tail probabilities. In the 16 archived Jakarta and Torino variants, the modeled chance of a witness per batch ranged from 95.18% to 95.91%. With 100 batches per variant, the adjusted rule required 100 successes and the corresponding chances of target support were 0.72%–1.54%. These are per-variant forecasts under the supplied model. The archived runs shared seed schedules across variants, and the planner does not estimate a probability for the entire family. This calculation prepares no circuits and runs no shots.

### Preview modeled diffuser holdout costs and power

Add `--witness-plan-only` to a modeled diffuser sweep with a witness target and an explicit seed schedule. The sweep compiles the same variants and uses the same snapshot density preparations, readout model, and per-batch budgets as validation. It reports the planned workload and the chance of reaching the confidence threshold for each feasible circuit. The Python option is `witness_plan_only=True` on `run_constraint_diffuser_comparison`. Futility stopping applies to a running validation and is unavailable with plan-only.

```powershell
$witnessSeeds = 10001..10200
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model jakarta --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --witness-probability 0.95 --witness-seed-schedule @witnessSeeds --witness-plan-only
```

The `modeled_witness_plan` records the shared success threshold and totals for batches, shots, and nominal two-qubit gate executions. Each variant's row uses that variant's modeled batch success probability. A circuit with `NO_FEASIBLE_BUDGET` contributes zero jobs and has a null forecast. A feasible circuit whose schedule cannot clear the threshold has a zero forecast. A preparation failure leaves the aggregate plan at `NOT_STARTED` with totals unset. These forecasts assume independent batches within each variant. The seed lists are shared across variants, so the report gives per-variant probabilities.

Both copies previewed the archived Jakarta and Torino runs with the same formulas, placements, and seeds. The forecasts matched the standalone planner for all 16 default variants. Every prospective workload matched the corresponding completed validation counts.

| Offline snapshot | Shot cap per batch | Planned batches | Planned shots | Planned nominal two-qubit gate executions |
|---|---:|---:|---:|---:|
| Jakarta | 256 | 800 | 12,500 | 3,543,900 |
| Torino | 256 | 800 | 6,600 | 2,171,700 |
| Jakarta | 9 | 0 | 0 | 0 |
| Torino | 9 | 12 | 88 | 23,502 |

The instrumented preview observed eight local density preparations per run and zero measurement jobs. The workload figures describe the prospective holdout. The default validation replay reproduced its archived results excluding timing fields. [Aer density save instruction](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.library.save_density_matrix.html)

### Size witness shots for a validation power target

Add `--witness-validation-power` to a modeled diffuser sweep with a witness target and a fixed seed schedule. The Python option is `witness_validation_power` on `run_constraint_diffuser_comparison`. For each variant, the sweep chooses the fewest shots per batch that meet the original witness probability and the requested chance of supporting that target after the full holdout. The option accepts a probability greater than zero and at most one. It works in both preview and validation.

```powershell
$witnessSeeds = 10001..10200
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model jakarta --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --witness-probability 0.95 --witness-seed-schedule @witnessSeeds --witness-validation-power 0.8 --witness-plan-only
```

The confidence rule supplies the required successful batch count. For modeled shot hit rate `p` and `s` shots, the chance of at least one hit is `1 - (1 - p)^s`. The binomial upper tail gives the probability of reaching the required count. The search tests the shot cap and then finds the first sufficient shot count using exact comparisons on the supplied numeric probabilities. Reported probabilities are rounded for display. In particular, `meets_minimum_validation_power_at_cap` can be false when the displayed cap power rounds to one. [Binomial distribution](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binom.html)

The per-variant `modeled_witness_budget` records `validation_power_at_budget` and `validation_power_at_cap`. `VALIDATION_SCHEDULE_TOO_SHORT` means even an all-success holdout cannot support the target. `VALIDATION_POWER_CAP_EXCEEDED` means the original per-batch target fits under the shot cap and the desired holdout power falls short. The existing per-batch budget failures keep their status. Any variant without a feasible budget contributes zero validation jobs.

Both copies checked the selected minima against an independent high-precision scan. On the archived 100-batch schedules, a 95% witness target and 80% modeled validation power gave the following prospective and completed local workloads.

| Offline snapshot | Shot cap per batch | Feasible variants | Shots | Nominal two-qubit gate executions |
|---|---:|---:|---:|---:|
| Jakarta | 256 | 8 | 24,600 | 6,979,800 |
| Torino | 256 | 8 | 13,400 | 4,398,000 |
| Jakarta | 20 | 0 | 0 | 0 |
| Torino | 20 | 6 | 9,000 | 2,404,800 |

These forecasts assume a fixed snapshot hit probability and independent shots and batches within a variant. All feasible variants reached the confidence threshold in these local runs. The two copies used the same seed schedules for reproducibility.

### Compare diffuser holdout schedules before assigning seeds

Use `--witness-search-max-batches` with the witness target, minimum validation power, and optional shot cap. The modeled diffuser sweep searches from one batch to the supplied maximum, up to 128 batches per variant. It compiles and prepares each circuit once and runs zero measurement batches. The Python option is `witness_search_max_batches` on `run_constraint_diffuser_comparison`. Supply the explicit seed schedule separately when running a chosen holdout.

```powershell
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model jakarta --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --witness-probability 0.95 --witness-validation-power 0.8 --witness-search-max-batches 128
```

The `modeled_witness_schedule_search` lists every batch count whose all-success confidence bound can support the target. Each row reports the required successful batches, the number of alternating seeds, per-variant shot minima and power, and total planned work. At each size, the planner uses the same exact budget calculation as fixed-schedule validation. Larger sizes can allow more misses and change the shot budget. [Binomial distribution](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binom.html)

The lowest-shot and lowest-nominal-gate selections consider only schedules feasible for all eight variants. Each uses the other cost to break ties, then the smaller batch count. Partial rows show the work for their feasible variants and are excluded from both rankings. `NO_FULLY_FEASIBLE_SCHEDULE` reports this condition. `NO_ELIGIBLE_SCHEDULE` means the all-success confidence bound falls short at every size through the batch cap.

At a 95% witness target and 80% modeled validation power, both copies found 30 eligible sizes from 99 to 128. The 99-batch schedule minimized both costs on each tested snapshot under the default shot cap.

| Offline snapshot | Shots at 100 → 99 batches | Nominal two-qubit gate executions at 100 → 99 batches |
|---|---:|---:|
| Jakarta | 24,600 → 24,354 | 6,979,800 → 6,910,002 |
| Torino | 13,400 → 13,266 | 4,398,000 → 4,354,020 |

An independent high-precision scan checked every candidate in both copies. At a 20-shot cap, Jakarta had zero feasible variants and Torino had at most six, so neither search selected a full sweep. The selected 99-batch runs reproduced the corresponding prefix of the archived 100-batch results. All 16 variants supported the target in that same-seed replay.

### Save and verify a diffuser holdout request

Use `constraint-sweep --save-replay-request` with the modeled diffuser options, chosen witness target and validation power, and explicit alternating seeds for the selected batch count. The command prepares all eight variants and writes a new JSON request when all eight budgets are feasible. The request embeds the Boolean formula, so it travels independently of the source formula file. It records the original seed order, snapshot fingerprint, simulator and compiler library versions, circuit and modeled plan digests, and planned workload.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
$witnessSeeds = 10001..10198
python -m austins_algorithm constraint-sweep examples/field_sensor_kit.json --compare-diffusers --snapshot-method modeled_probabilities --ibm-model jakarta --transpiler-seed 7 --placement 3 2 5 4 0 6 1 --placement 5 4 3 2 6 0 1 --witness-probability 0.95 --witness-validation-power 0.8 --witness-seed-schedule @witnessSeeds --save-replay-request .build/jakarta-diffuser.json
python -m austins_algorithm diffuser-replay .build/jakarta-diffuser.json --check-only
python -m austins_algorithm diffuser-replay .build/jakarta-diffuser.json
```

Save and check-only run zero measurement batches. A validation replay checks the same prepared run before launching its first measurement. It compares the snapshot and library versions, the eight compiled OpenQASM fingerprints and wire mappings, and the exact seed bindings, modeled outcome distributions, shot budgets, and totals. Any mismatch fails before measurement. A request saved with `--witness-stop-on-futility` applies that option during replay. The Python entry points are `save_diffuser_replay` and `run_diffuser_replay` in `austins_algorithm.diffuser_replay`.

The digests and live checks establish local content and execution agreement under the recorded software and snapshot. IBM's public fake backends provide dated device models for offline simulation. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

Both copies saved byte-identical requests for the 99-batch Jakarta and Torino selections, verified them without sampling, and reproduced all 1,584 archived seeded outcomes. The completed workloads were 24,354 Jakarta shots and 13,266 Torino shots. Tests rejected altered content, snapshot, circuit identity, budgets, and seed bindings before the first measurement, including changes with a recomputed content digest.

### Prefer native OpenQASM import for a diffuser holdout

Add `--native-qasm` to `constraint-sweep --compare-diffusers` to try Qiskit's experimental native importer. The compiler still checks the exported wires, terminal measurement mapping, and ideal probabilities. If the native importer is unavailable or rejects the export, the compiler tries the standard importer when installed. Each variant reports both the preferred importer and the one that actually ran. [Qiskit OpenQASM import documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3#experimental-import-interface)

With `--save-replay-request`, this option writes schema 2 and records `sampling.prefer_native_qasm`. The saved request selects the importer preference during replay. Its circuit digest binds the actual importer, preference, and export evidence. The default save path still writes schema 1, and the existing Manila fixture replays with its original importer. A comparison across importer choices records different circuit conditions, even when an independent check finds identical counts.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
python -m austins_algorithm constraint-sweep examples/diffuser-smoke-regression/constraint.json `
  --compare-diffusers --native-qasm --snapshot-method modeled_probabilities --ibm-model manila `
  --synthesis clause-flags --placement 0 1 2 3 4 --placement 4 3 2 1 0 `
  --witness-probability 0.05 --witness-max-shots 16 --witness-validation-power 0.9 `
  --witness-seed-schedule 41 43 47 53 --save-replay-request .build/manila-native.json
python -m austins_algorithm diffuser-replay .build/manila-native.json --check-only
python -m austins_algorithm diffuser-replay .build/manila-native.json --receipt-dir .build/manila-native-run
python -m austins_algorithm diffuser-receipt-verify .build/manila-native-run
```

On 2026-11-11, an in-process profile on Windows with CPython 3.12.15 and Qiskit 2.6.2 attributed 11.56 s of a 12.85 s profiled Manila plan check to eight standard importer calls. Each export was distinct. The native probe matched the compiled OpenQASM digests, wire maps, modeled plans, and ideal probabilities for all 24 variants across the tested Manila, Jakarta, and Torino placements.

One paired CLI run per copy included fresh Python startup. Check-only wall time went from 10.49 s to 5.09 s in ChronoRealm and from 10.98 s to 6.15 s on E:. The bounded regression went from 11.30 s to 5.82 s in ChronoRealm and from 11.98 s to 5.87 s on E:. These timings describe the pinned local Windows runs.

Both copies reproduced the original Manila request bytes and matched the original regression. Each also verified the new native receipt and matched its own bounded replay. The tested importer modes produced the same 16 seeded histograms, 112 shots, and 11,088 nominal two-qubit gate executions. The replay and offline receipt checks rejected altered importer evidence.

### Inspect diffuser compilation timings

Diffuser comparisons and replay reports show elapsed seconds in each variant's `compilation_timing_seconds`. The report's `timing_seconds` sums each phase across the completed compilations. Phase totals appear once a variant compiles. A request save returns those totals alongside the request digest. The elapsed times come from Python's performance counter. [Python clock reference](https://docs.python.org/3.12/library/time.html#time.perf_counter)

| Field | Measured interval |
| --- | --- |
| `compile_export_verify` | The full `compile_and_verify` call, including input checks, reference distributions, and report construction |
| `transpile` | Qiskit's target transpilation call |
| `openqasm_export` | OpenQASM serialization |
| `openqasm_import` | Importer selection and parsing, including an attempted native parse and standard fallback when both run |
| `openqasm_verify` | Validation of the imported wires, work limits, measurement mapping, and ideal probability distances |

The four phase timings are disjoint parts of `compile_export_verify`. The report's `wall_seconds` covers the broader diffuser workflow. A check-only replay exposes the compilation timings with zero measurement batches.

```powershell
$checked = python -m austins_algorithm diffuser-replay examples/diffuser-smoke-regression/baseline-native/request.json --check-only | ConvertFrom-Json
$checked.timing_seconds
$checked.cells[0].variants.standard.compilation_timing_seconds
```

Elapsed diagnostics sit outside the saved request and deterministic circuit and plan fingerprints. The receipt still hashes the exact report bytes, so separate runs can have different report hashes and match in the receipt comparison.

On 2026-11-11, both copies verified the original and native Manila requests from fresh installed source distributions. Each mode reproduced its saved request byte for byte and matched all 16 seeded batches at 112 shots. In one isolated check per mode and copy with CPython 3.12.15 and Qiskit 2.6.2, the eight standard imports took 3.75–3.96 s and the eight native imports took 0.011–0.013 s. These are local import-stage observations.

### Record and verify a diffuser execution receipt

Use `--receipt-dir` with `diffuser-replay` to record a validation run in a new directory. The bundle contains the exact request bytes, the execution report, and `receipt.json` with the artifact sizes, SHA-256 digests, and library versions. The replay reserves the directory before sampling and writes the receipt after the request and report are present.

```powershell
python -m austins_algorithm diffuser-replay .build/jakarta-diffuser.json --receipt-dir .build/jakarta-diffuser-run
python -m austins_algorithm diffuser-receipt-verify .build/jakarta-diffuser-run
```

The verifier reads the bundle without simulator dependencies. It checks the artifact bytes, request bindings, circuit and plan digests, and the ordered seed prefixes. It applies the embedded Boolean formula to the recorded measurement counts and reconstructs the completed batches, shots, and nominal two-qubit gate executions. It reports attempted batches, timed-out batches, intentional futility skips, and other unattempted batches separately. A valid receipt can describe a completed, partial, or intentionally stopped execution. The checks establish consistency among the recorded artifacts.

Both copies verified the Jakarta and Torino receipt bundles without importing the quantum SDK or SciPy. Each recorded 792 completed batches, with 24,354 shots on Jakarta and 13,266 on Torino. The verifier rejected a changed report total even after the receipt's report digest was recomputed. Injected-outcome tests also verified a futility stop and an interrupted run with an unconfirmed cancellation.

### Compare diffuser receipts by circuit and seed

Use `diffuser-receipt-compare LEFT RIGHT` to verify both bundles and compare their recorded work without simulator dependencies. The command aligns variants by ordered physical layout, round count, and diffuser. It pairs seeds within each aligned variant, including when the two requests list the placements in different orders.

```powershell
python -m austins_algorithm diffuser-replay .build/jakarta-diffuser.json --receipt-dir .build/jakarta-repeat-run
python -m austins_algorithm diffuser-receipt-compare .build/jakarta-diffuser-run .build/jakarta-repeat-run
```

The report distinguishes completed batches, timeouts and their cancellation state, futility skips, other unattempted batches, and seeds outside one side's plan. Exact histogram comparison requires matching formula, snapshot, recorded software, compiled circuit identity, modeled distribution, and shots per batch. A completed pair under different conditions shows both recorded shot and hit totals. For a comparable pair with changed counts, `count_deltas` shows the right count minus the left count for each changed bitstring. The workload deltas also use right minus left. Artifact byte equality is reported separately from agreement on the compared evidence.

Both local copies reproduced all 792 comparable completed batches for each snapshot. Comparing Jakarta's 99-batch and 100-batch receipts found the same 792 shared outcomes and eight additional completed batches using 246 shots. The Jakarta and Torino receipts used disjoint layouts, so their aggregate workload difference had zero paired outcomes. The checks also covered different shot budgets and a reordered placement request with an injected futility stop and timeout.

### Compare recorded diffuser compilation timings

Add `--timings` to `diffuser-receipt-compare` to inspect the recorded compiler diagnostics for each aligned layout, round count, and diffuser. The Python equivalent is `compare_diffuser_receipts(left, right, include_timings=True)`.

```powershell
python -m austins_algorithm diffuser-receipt-compare .build/manila-stable-run .build/manila-native-run --timings
```

Each variant reports the actual and preferred importer for each side and the availability of the five compilation times. `RECORDED` includes numeric zero. `NOT_RECORDED` identifies a receipt with no timing field. `INVALID` identifies a present field that lacks a complete, finite, consistent phase breakdown. `VARIANT_NOT_PRESENT` identifies a missing circuit on that side. Unavailable seconds and deltas appear as JSON `null`.

An aligned pair with two valid records reports each phase's right-minus-left delta in seconds. The top-level `compilation_timing` sums exactly those paired variants on each side, reports how many pairs contributed, and counts the pairs with matching or different recorded conditions. `NO_PAIRED_TIMINGS` leaves the aggregate values unavailable. `OUT_OF_RANGE` preserves valid per-variant observations when the paired aggregate exceeds the numeric range.

The timing comparison checks recorded formula, snapshot, software, transpiler settings, and compiled circuit details. It lists changes in actual importer and importer preference separately from other circuit changes, so an importer change can be seen even when both OpenQASM exports have the same digest. Matching recorded conditions describe the receipt fields. Host load and machine differences can still affect elapsed time. Timing observations are independent of the semantic receipt match and work offline without Qiskit.

Both copies checked the archived and timed Manila receipts with site packages disabled. The timed stable and native receipts aligned all eight measured variants and identified the actual importer and preference as the only recorded timing-context changes. The archived-versus-timed stable pair matched semantically and reported timing on only one side. Each default comparison matched the output of the previous comparator.

### Benchmark matched diffuser requests

`diffuser-benchmark` compares two checked requests in fresh, isolated Python processes. It preflights the formula, placements, synthesis, transpiler seed, witness settings, snapshot, software versions, and modeled plan before launching a worker. The requests can differ in importer preference. Each worker recompiles in check-only mode and checks the saved circuit and plan fingerprints. The benchmark also verifies that the compiled outputs agree across trials once importer metadata is set aside.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
python -m austins_algorithm diffuser-benchmark `
  examples/diffuser-smoke-regression/baseline/request.json `
  examples/diffuser-smoke-regression/baseline-native/request.json `
  --pairs 4 --timeout-seconds 60 --output-dir .build/manila-compiler-benchmark
```

The command accepts one to eight pairs and a per-worker timeout of one to 300 seconds. Odd pairs run left then right. Even pairs run right then left. The command runs one worker at a time and records the actual importer used for each compiled variant, including any fallback. The staged request files and each trial's report and standard error are retained in the new output directory.

The summary records minimum, median, and maximum times for each side and for the within-pair right-minus-left differences. `process_wall` covers worker launch through exit, including Python and SDK startup. `compile_export_verify` and the four compiler phases come from the worker's verified timing report. Process isolation leaves operating-system caches in their normal local state. A failed or timed-out trial stops the series. Its artifacts and prior verified trials remain available, and the summary leaves the comparative statistics unavailable.

On 2026-11-11, each copy ran four alternating pairs from its newly built wheel with CPython 3.12.15 and Qiskit 2.6.2. All 16 workers verified their saved request, and every native trial used the native importer for all eight circuits. The plan and compiled outputs matched across the two modes. A separate one-second timeout check stopped after the first worker in each copy, returned exit code one, and preserved its evidence.

| Copy | Standard median process time | Native median process time | Standard median import time | Native median import time |
| --- | ---: | ---: | ---: | ---: |
| ChronoRealm | 8.88 s | 5.10 s | 3.596 s | 0.012 s |
| E: drive | 7.76 s | 4.43 s | 3.259 s | 0.010 s |

### Benchmark diffuser importers across IBM snapshots

`examples/benchmark_diffuser_snapshots.py` runs matched check-only requests for Manila, Jakarta, and Torino. It uses the packaged Manila requests and regenerates the Jakarta and Torino requests from `field_sensor_kit.json`, fixed placements, and their saved witness settings. Each model pairs the standard and native-preferred importers on the same compiled workload. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

Use the pinned Windows environment from the smoke example.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
& $python -I -B examples/benchmark_diffuser_snapshots.py --output-dir .build/diffuser-snapshot-study --pairs 2
```

The runner accepts an ordered `--snapshots` list and saves one verified benchmark bundle per snapshot. The top-level `snapshot-summary.json` retains the recorded importer counts, per-variant OpenQASM byte size, active wires, compiled depth, two-qubit gates, and timing statistics. Incomplete runs retain their per-snapshot evidence.

Both copies ran two alternating pairs per snapshot on the same Windows host with CPython 3.12.15, Qiskit 2.6.2, and the pinned snapshot stack. ChronoRealm ran Manila, Jakarta, then Torino. E: ran the reverse order. The regenerated standard Jakarta and Torino requests reproduced their archived bytes in both copies. All 24 trials were verified with zero measurement batches. Every native-preferred trial used the native importer for all eight variants. The cross-copy comparison matched the request bytes, compiled sizes, and recorded conditions for each model.

| Snapshot | OpenQASM bytes across 8 exports | Active wires | Compiled depth range | ChronoRealm import median, standard / native | E: import median, standard / native |
| --- | ---: | ---: | ---: | ---: | ---: |
| Manila | 29,754 | 5 | 93–181 | 3.604 / 0.0140 s | 4.276 / 0.0148 s |
| Jakarta | 91,744 | 7 | 234–585 | 10.969 / 0.0268 s | 11.152 / 0.0264 s |
| Torino | 204,387 | 7 | 578–1501 | 25.954 / 0.0506 s | 25.238 / 0.0522 s |

Each median uses the sum of the eight import durations in each trial. Target basis, topology, and placement vary across snapshots. Manila also uses a different formula. The table describes the measured workloads, and the retained reports include full process and compilation timings.

### Benchmark OpenQASM importers on fixed exports

`examples/benchmark_openqasm_importers.py` isolates the standard `qiskit.qasm3.loads` and experimental `qiskit.qasm3.loads_experimental` calls. Install the current package in the pinned Windows environment. Use the standard requests staged by the three-snapshot benchmark above. [Qiskit OpenQASM importer documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit/qasm3#experimental-import-interface)

```powershell
$study = '.build/diffuser-snapshot-study'
& $python -I -B examples/benchmark_openqasm_importers.py prepare `
  "$study/manila/benchmark/left-request.json" `
  "$study/jakarta/benchmark/left-request.json" `
  "$study/torino/benchmark/left-request.json" `
  --output-dir .build/qasm-corpus
& $python -I -B examples/benchmark_openqasm_importers.py run .build/qasm-corpus `
  --output-dir .build/qasm-import-study --pairs 2
```

Choose fresh output directories. `prepare` performs each check-only replay and saves its eight exact UTF-8 exports with the request and compiler report. The corpus manifest binds the local requests and reports by SHA-256. `run` verifies the retained replay evidence, declared export sizes, and export hashes before starting the parsers. Use a corpus prepared in a trusted local environment. The measurement reference comes from that preparation.

Each parser gets one untimed one-qubit warm-up. The clock covers each direct loader call on an in-memory string. File reads, UTF-8 decoding, compilation, and measurement checks happen outside the timer. Parser order alternates between pairs. Each parsed circuit must match the saved wire counts and terminal measurement mapping, then agree with the compiled measurement distribution within the compiler's total variation tolerance. Either parser's failure stops the run. A completed `summary.json` records per-variant samples, per-parser totals and statistics, the environment, and the verified export identities.

On the same Windows host, both copies ran two pairs per snapshot with CPython 3.12.15, Qiskit 2.6.2, and `qiskit-qasm3-import` 0.6.0. E: reversed the snapshot order. The request bytes and all 24 export sizes and hashes matched across copies. All 192 timed imports passed the measurement check, with maximum total variation distance of `2.33e-15`. Both copies rejected a same-length edit to one export before loading a parser.

| Snapshot | OpenQASM bytes across 8 exports | ChronoRealm import median, standard / native | E: import median, standard / native |
| --- | ---: | ---: | ---: |
| Manila | 29,754 | 2.835 / 0.0079 s | 2.839 / 0.0062 s |
| Jakarta | 91,744 | 9.003 / 0.0224 s | 8.864 / 0.0199 s |
| Torino | 204,387 | 20.673 / 0.0642 s | 21.934 / 0.0472 s |

The medians summarize two totals per parser, with each total summing eight direct calls. These are local observations on these workloads and SDK versions. The replays used zero sampling shots.

### Verify saved diffuser benchmark evidence

`diffuser-benchmark-verify` reads a saved benchmark directory with the standard library. It checks the staged request hashes, alternating trial order, circuit and plan evidence, and compiler timing totals. For a completed benchmark it recomputes the paired minimum, median, and maximum statistics. The directory can be relocated as a unit because artifact paths are derived from its layout.

```powershell
python -m austins_algorithm diffuser-benchmark-verify .build/manila-compiler-benchmark
```

New benchmarks write summary schema 2 with a byte count and SHA-256 for each trial report and worker log that was created. Schema 1 bundles still verify their request and report evidence and show `TRIAL_CHECKSUMS_UNRECORDED`. The command returns the exact summary SHA-256. These checks establish consistency with the supplied bundle. Elapsed process time and worker failure status remain recorded observations.

`BENCHMARK_EVIDENCE_VERIFIED` describes the retained evidence. The result also reports the original benchmark status. A saved failure or timeout retains its terminal status and any verified trials that preceded it. Comparative timing statistics are available for completed runs.

Both local copies verified their original eight-trial run and timeout, a new four-trial run and timeout, and a relocated copy with site packages disabled. Both rejected changes to staged request bytes, trial order, the paired median, a report's bytes with its JSON values intact, and a worker log.

### Compare saved diffuser benchmarks

`diffuser-benchmark-compare` independently verifies two retained benchmark directories. It compares their declared left arms and their declared right arms and shows each run's verified sample counts, minimum, median, and maximum times. It also shows each run's within-pair right-minus-left effect. The between-run `median_delta_seconds` subtracts the baseline median from the candidate median. It uses the two verified summaries, so the runs can contain different numbers of pairs.

```powershell
python -m austins_algorithm diffuser-benchmark-compare .build/manila-compiler-benchmark .build/manila-compiler-repeat
```

The report names changes in the workload, importer preference and observed importer counts, saved circuit digests, recorded environment, and worker timeout. A repeated input with identical summary bytes is marked by `summary_bytes_match`. `MATCHING_RECORDED_CONDITIONS` describes agreement on the compared bundle fields. With `RECORDED_CONDITIONS_DIFFER`, the reported median differences describe runs with the listed changes. When a bundle is incomplete, the report preserves the verified prefix and any completed run's statistics and leaves between-run timing deltas unavailable.

Both local copies compared their saved four-pair and two-pair Manila runs without Qiskit and recomputed the differences from the verified medians. They also recognized a real reversed-importer run, flagged a synthetic change to the recorded CPU count, preserved a timeout, and rejected a candidate with altered report bytes.

| Copy | Four-pair median process effect | Two-pair median process effect | Candidate minus baseline |
| --- | ---: | ---: | ---: |
| ChronoRealm | −3.774 s | −3.177 s | +0.597 s |
| E: drive | −3.319 s | −3.667 s | −0.348 s |

The process effect in these runs is the native arm's process time minus the standard arm's within each pair.

### Run a diffuser regression within a declared limit

Use `diffuser-regression` with one verified baseline receipt, a new output directory, a maximum measurement-shot count, and a worker timeout. The command verifies and retains the baseline record and its exact request bytes before launching one local replay. The shot limit applies to the full checked plan, including when the baseline stopped early. The replay verifies the staged request hash and recomputes the plan before measurement. The limit counts measurement shots, and the worker timeout covers preparation and execution.

```powershell
python -m austins_algorithm diffuser-regression .build/jakarta-diffuser-run --output-dir .build/jakarta-regression --max-sampling-shots 24354 --timeout-seconds 60
```

The output directory holds `summary.json` and, when the worker launches, the staged request, worker error log, and any completed execution receipt. The worker timeout applies to the child process after process creation. The comparison uses the baseline verified at preflight, with its artifact fingerprints in the summary. `REGRESSION_MATCHED` returns exit code zero. Preflight rejection, worker timeout or failure, receipt verification failure, and differing results have separate statuses and return exit code one. [Python subprocess timeout behavior](https://docs.python.org/3/library/subprocess.html#subprocess.run)

Both copies reproduced all 792 batches per snapshot with exact limits of 24,354 Jakarta shots and 13,266 Torino shots. A limit one shot below each plan stopped at preflight. Local fixtures also exercised a timed-out worker, a successful worker exit with a missing receipt, an altered staged request, an invalid baseline, and a changed histogram in a baseline that was replaced after preflight. The last case showed one recorded seed difference against the pinned baseline.

### Run named diffuser regressions from a manifest

Use `diffuser-regression --manifest` to run up to eight named baseline receipts. Each case declares its shot limit and worker timeout, and the manifest declares the combined measurement-shot limit. Relative baseline paths resolve from the original manifest's directory. For example, save the following as `.build/diffuser-suite.json` when the two named receipt directories are alongside it.

```json
{
  "schema_version": 1,
  "max_total_sampling_shots": 37620,
  "cases": [
    {
      "id": "jakarta",
      "baseline": "jakarta-diffuser-run",
      "max_sampling_shots": 24354,
      "timeout_seconds": 60
    },
    {
      "id": "torino",
      "baseline": "torino-diffuser-run",
      "max_sampling_shots": 13266,
      "timeout_seconds": 60
    }
  ]
}
```

```powershell
python -m austins_algorithm diffuser-regression --manifest .build/diffuser-suite.json --output-dir .build/diffuser-suite-run
```

The suite verifies and retains every baseline before the first worker starts. The sum uses each full planned workload. An invalid baseline, over-limit case, or over-limit total leaves every worker unstarted. Once preflight passes, workers run once in manifest order under their declared timeouts. The output includes the exact manifest, a suite summary, and numbered case directories with each staged request, log, and available replay receipt. The summary's verified shot count includes the completed measurements from receipts that passed verification. A suite returns exit code zero when every case matches.

Both copies matched the Jakarta and Torino baselines at their exact per-case limits and exact combined limit of 37,620 shots. Each replay matched 792 batches. Lowering the suite cap or the second case's cap by one shot prevented both workers from starting. An injected first-worker timeout left the second verified case eligible to run, and it used the request bytes retained at preflight.

### Run the packaged Manila diffuser smoke baseline

The `examples/diffuser-smoke-regression` fixture packages a four-bit CNF, its checked request and receipt, and a one-case manifest. The formula fixes `q0` to zero and `q1` to one. Clause-flag synthesis uses one work qubit, so both diffusers fit on the five-wire Manila snapshot. The manifest covers two placements, two round counts, both diffusers, and two batches per variant. [IBM fake backend documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/fake-provider)

From the package root on Windows, use the shared smoke requirements on CPython 3.12.15.

```powershell
New-Item -ItemType Directory -Force .build | Out-Null
uv venv --python cpython-3.12.15-windows-x86_64-none .build/manila-diffuser-venv
$python = (Resolve-Path -LiteralPath .build/manila-diffuser-venv/Scripts/python.exe).Path
uv pip install --python $python ".[ibm]" -r examples/smoke-regression/requirements.txt
& $python -I -B -m austins_algorithm diffuser-regression --manifest examples/diffuser-smoke-regression/manifest.json --output-dir .build/manila-diffuser-smoke-run
```

The saved request uses a 5% witness target, 90% modeled validation power, a 16-shot per-batch cap, and seeds `41 43 47 53` in alternating diffuser order. The selected budgets are four shots per one-round batch and ten per two-round batch. The smoke regression matches exact seeded outcomes and workload at a 112-shot case and suite cap.

Both copies verified all 16 batches, 112 shots, and 11,088 nominal two-qubit gate executions. All eight variants reached the fixture's confidence threshold on the recorded seeds. Both source distributions preserved all five fixture files byte-for-byte, and each built wheel reproduced the baseline using the packaged fixture and pinned simulator stack. [uv distribution build documentation](https://docs.astral.sh/uv/concepts/projects/build/)

### Run the packaged Manila native diffuser baseline

The native manifest shares the formula and pinned simulator environment with the original diffuser fixture. Its schema 2 request records the native importer preference. Its receipt records that Qiskit's native importer handled all eight exports. Both baselines record the same 16 seeded histograms, 112 shots, and 11,088 nominal two-qubit gate executions. Each manifest checks its own importer evidence.

Use the pinned Windows environment from the previous example.

```powershell
& $python -I -B -m austins_algorithm diffuser-regression --manifest examples/diffuser-smoke-regression/manifest-native.json --output-dir .build/manila-native-diffuser-smoke-run
```

### Run the Manila smoke regressions in the Windows workflow

The `manila-smoke-regression` job builds one source distribution and installs it into the recorded Windows Python environment. Each push and pull request runs the phase-cover replay with a 160-shot ceiling and two diffuser replays with 112-shot ceilings each. Their shared step records all three exit codes. A separate placement step verifies the packaged receipt, checks circuit identities, and replays eight four-shot trials. Its 32 shots bring the combined ceiling to 416 local simulated shots. Once installation succeeds, the placement step can run after a failure in the preceding replay step. The evidence bundles use `manila-smoke-evidence`, `manila-diffuser-smoke-evidence`, `manila-diffuser-native-smoke-evidence`, and `manila-placement-smoke-evidence`. The upload steps are eligible after a replay failure and stop on cancellation. [PowerShell parallel execution](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/foreach-object?view=powershell-7.6#parallel-parameter-set) · [GitHub workflow shell behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#exit-codes-and-error-action-preference) · [GitHub status checks](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions)

The `manila-smoke-environment` artifact records the installed dependency versions, expected pins, exact source archive and requirements fingerprints, and available Python and runner details. Capture follows installation and writes its evidence before replay starts. A capture failure marks that step failed. The replay steps can still run after a capture failure if installation succeeded. [GitHub runner variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables)

Before the placement lane was added, local verification ran the workflow command blocks from fresh source distributions in both copies. All three baselines matched their installed-package replays. For each manifest in turn, lowering its total limit by one shot rejected its replay before its worker started. The other two replays matched and the combined step failed. A scratch Git checkout with `core.autocrlf=true` reproduced the original bytes of both diffuser receipts and verified them offline. Subsequently, [GitHub Actions run #8](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions/runs/43249759269) passed the hosted Manila smoke job for commit `6c2370b060dbfdcf2ea3feeca72963a282c386ad` on 2026-11-12. Later local changes require their own hosted verification.

#### Local replay timings

A fresh local run of the original workflow blocks on Windows with CPython 3.12.15 spent 4.46 s installing the package and dependencies and 17.28 s running the two replays. Each copy then ran three paired trials from an installed environment with the pinned smoke requirements. Each trial ran the same two CLI commands and verified both saved receipts. The timed interval covers both commands.

| Package copy | Median serial time | Median concurrent time | Reduction |
| --- | ---: | ---: | ---: |
| ChronoRealm | 15.89 s | 10.06 s | 36.7% |
| E: drive | 13.69 s | 9.36 s | 31.6% |

A separate timing pair per copy executed the original and updated PowerShell replay blocks with the same installed dependencies. ChronoRealm took 14.56 s serially and 9.62 s concurrently. E: took 16.83 s serially and 8.99 s concurrently. Both receipts matched in each run. The timings cover local execution. Hosted runner time and evidence upload time remain unmeasured.

After adding the native replay, two paired local trials per copy ran the exact two-replay and three-replay PowerShell blocks against the same installed source distribution. The timed interval includes PowerShell launch and the replays. Both configurations matched their respective saved receipts.

| Package copy | Median for two replays | Median for three replays |
| --- | ---: | ---: |
| ChronoRealm | 11.33 s | 12.06 s |
| E: drive | 8.70 s | 9.00 s |




