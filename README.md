# Austin's Algorithm

**Python package** 0.1.0 (alpha) · **Windows Workbench** 0.2.3-alpha · **License** [Apache-2.0](LICENSE)

**Project credit:** Austin Gregory Simpkins and Chandler Middlebrook / ChronoRealm.

For a short, ordered, beginner-friendly walkthrough, start with [`GETTING_STARTED.md`](GETTING_STARTED.md). This README remains the package overview and reference.

## Current state

Reviewed **2026-11-13** through implementation milestone **#147**. The Python package version matches [`pyproject.toml`](pyproject.toml). The [changelog](CHANGELOG.md) tracks the independent Windows Workbench release.

The **Future Reader** page in Workbench 0.2.3-alpha uses the optional
companion's local practice controller. Inspect the deadline and current
route, preview a standard or custom route, confirm it, then check the
observed result or preview a restore. The standalone build includes the
controller and browser companion. The Python package exposes the same
optional runtime through `python -m austins_algorithm future-reader`.
See the [Workbench guide](WORKBENCH.md) for the source launch and separate
practice checkpoint.

**Live recovery check** stops a disposable local worker and verifies that a
replacement completes the same SQLite-backed job. The [IBM hardware guide](IBM_HARDWARE.md)
covers named-profile preflight and the optional physical device experiment.

The release checkout includes the [Future Reader companion source](future-reader/README.md).
In the ChronoRealm workspace the companion is maintained in the sibling package.

**Austin's Algorithm Workbench** is a desktop application for exploring Grover search and optimizing the cost of a valid assignment. Start with the original Austin predicate, a two-bit cost example, or the weighted field sensor kit. The Overview explains the goal, latest result and next action. Optimize runs the exact solver, and AI Assistant gives local guidance about the active project. Open Simulate for preparation, phase marking and diffusion, choose rounds, and measure a seeded shot or run automatically. Save and reopen a project, copy its generated Python program, or export all evidence in one ZIP. See [the Workbench guide](WORKBENCH.md).

For the command line, run `python -m austins_algorithm optimize examples/weighted_sensor_kit.json --mode exact`. The kit's first feasible answer costs 7 and its exact minimum costs 4. The desktop simulation and optional audited Qiskit threshold circuit use the same Boolean rules and weight order. See [the weighted benchmark](examples/benchmark_weighted_search.py).

The project starts with a three-qubit Grover teaching example and supports bounded Boolean-constraint search with independent classical verification. The base package runs on Python 3.10 and newer. Optional Qiskit and Aer workflows compile for local targets, verify OpenQASM measurement behavior, and compare search budgets on dated IBM models.

- Use [the constraint guide](CONSTRAINT_WORKFLOW.md) for reversible oracles, classical baselines, verified exports, and bounded known- or unknown-count search.
- Use [the paired calibration study](CONSTRAINT_WORKFLOW.md#run-both-density-policies-in-one-study) to compare default and fused density simulation on the same compiled circuits. The default capture drives budget selection and seeded replay. Live simulator setup is released before planning.
- Use [the offline study verifier](CONSTRAINT_WORKFLOW.md#verify-a-saved-paired-calibration-study-offline) to check saved captures, plans and fusion decisions after the simulator exits.
- Use [`snapshot-calibration-study-checkpoint` and `snapshot-calibration-study-resume`](CONSTRAINT_WORKFLOW.md#restart-a-paired-study-from-completed-reference-captures) to carry completed default captures and verified compiled rounds into a fresh process. Resume verifies the checkpoint, rebuilds the compact simulators, and runs the fusion phase.
- Use [`snapshot-calibration-study-incremental` and `snapshot-calibration-study-incremental-resume`](CONSTRAINT_WORKFLOW.md#resume-between-reference-layouts) to save after each reference layout. Continue from a trusted `generation_sha256` to recover the saved reference prefix, then export the completed study with `--study-output` for offline verification. `--max-new-reference-layouts` provides an explicit pause boundary.
- Use [`snapshot-calibration-study-fusion-continue`](CONSTRAINT_WORKFLOW.md#resume-between-fusion-attempts) to save each fusion attempt against a pinned complete reference chain. Resume from both reference and fusion pins, skip saved attempts, and export the final paired report. `--reference-only` on incremental reference commands stops before fusion; `--max-new-fusion-layouts` pauses between diagnostic attempts.
- Fusion continuation [reuses validated history within one invocation](CONSTRAINT_WORKFLOW.md#reuse-verified-checkpoint-context-within-one-invocation). It rehashes ancestor files before each publication and keeps fresh-process reference and circuit checks. The measured store sequence halves capture/profile validation work while reading additional raw bytes for those rechecks.
- Run [the checkpoint continuation benchmark](CONSTRAINT_WORKFLOW.md#benchmark-checkpoint-continuation-at-eight-layouts) to compare an uninterrupted study with fresh-process reference and fusion chunks. The eight-layout Jakarta check produced identical study bytes in both maintained copies and recorded the additional restart cost.
- Audit [saved benchmark records without the SDK](CONSTRAINT_WORKFLOW.md#verify-saved-checkpoint-benchmark-records). Supply a trusted summary pin to check the available stage sequence, checkpoint handoffs, counters, storage and paired reports. A successful audit can describe a stopped run; inspect `recorded_status` for the benchmark outcome. Worker execution, clocks, memory measurements and source identities remain recorded evidence.
- Restoration [retains programs only for the eligible fusion chunk](CONSTRAINT_WORKFLOW.md#retain-only-the-current-restoration-chunk). Completed and reference-only calls discard their verified programs, while every saved round still receives the full physical and calibration checks. The eight-layout probes preserve exact results and reduce tracked object retention.
- Use [the verified package builder](CONTRIBUTING.md#build-and-check-release-artifacts) to build a wheel from a checked source archive and compare every runtime file with the source. This catches stale unchanged modules that a selected-file artifact check can miss.

Restart continuity preserves completed reference layouts, their compiled circuits, and published fusion attempts. A pause returns `study: null`; finishing or reaching a terminal failure returns the ordinary paired report. Check that report's `fusion.status` separately from reference completion, since a complete reference grid can have partial fusion diagnostics. Work interrupted before its checkpoint publishes may need to run again. A saved terminal error or unconfirmed cancellation prevents later diagnostic jobs on that chain.

The measured results cover local simulations. The optional physical-job path requires a configured IBM account and budget. See the [dated changelog](CHANGELOG.md) for the update history and [GitHub Actions](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions) for hosted verification of committed changes.

The Manila smoke workflow runs from the source distribution. It can compare designated probability fields within an absolute tolerance of `1e-12` while still requiring matching circuit identities, work budgets, seeded counts, and decisions. Ordinary replay and receipt verification use their exact evidence contracts. See the [Manila smoke guide](CONSTRAINT_WORKFLOW.md#run-the-packaged-manila-smoke-baseline).

## What is this?

This is a small program for learning how Grover's quantum search works. In ChronoRealm, **Austin's Algorithm** is the name for our example search problem. The search method comes from Lov K. Grover's published algorithm. This package does not claim to be a brand-new kind of quantum algorithm.

You can try the example on an ordinary computer. The basic program uses regular Python and does not need a quantum computer. It uses math to imitate an ideal quantum circuit, so its results are for learning—not proof of a real-world speed boost.

## The Austin example

A **qubit** is a quantum computer's kind of bit. When measured, it gives `0` or `1`. For this small example, picture three light switches, each either `0` (off) or `1` (on). There are eight possible settings. Our rule accepts a setting when:

- `q0` is on; and
- `q1` and `q2` are different.

The accepted settings are `011` and `101`. The example uses Grover's search steps to make those answers more likely to appear when the circuit is measured. For this tiny, ideal example, one search round gives only accepted answers.

The strings are written from left to right as `q2 q1 q0`. That is why `q0` is the last digit.

## Try it on your computer

You need Python 3.10 or newer. Open a terminal in the folder that contains this README and `pyproject.toml`.

The `.venv` folder is a private place for this project's Python tools. It helps keep them separate from other projects on your computer.

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m austins_algorithm
```

### macOS or Linux

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/python -m austins_algorithm
```

The program prints a JSON report—a text page with labeled results. Look for these fields:

- `solution_examples`: answers that match the rule (`011` and `101` here).
- `recommended_iterations_given_known_solution_count`: the number of search rounds suggested after the simulator counts the matching answers (one).
- `statevector_success_probability`: the ideal math model's chance of finding a matching answer (1.0 here).
- `status`: says that this was a **classical computer simulation**, not a quantum computer run.

The simulator repeats the measurement many times (called `shots`) and shows the results. Because this example is ideal and so small, each sample should be one of the two accepted strings.

### Watch the amplitudes change

To include the starting amplitudes and the amplitudes after each Grover round in the JSON report, run:

```powershell
.\.venv\Scripts\python.exe -m austins_algorithm --trace-amplitudes
```

Or turn the trace on in Python:

```python
from austins_algorithm import austin_predicate, run_austin_search

result = run_austin_search(3, austin_predicate, include_amplitude_trace=True)
for point in result["amplitude_trace"]:
    print(point["iteration"], point["unmarked_plane_coordinate"], point["marked_plane_coordinate"])
```

The trace starts at round `0`, before Grover runs, then adds one point after each full oracle-and-diffuser round. The two plane coordinates show the ideal state's position along the unmarked and marked parts of the search space. For the built-in three-qubit example, the point moves from about `(0.866, 0.5)` to `(0, 1)` after one round. Per-state amplitudes are included too. This is the ordinary computer's ideal math simulation; it is not a measurement from quantum hardware. The trace is off by default so existing reports stay compact.

To see the rotation and sampling in one picture, install the optional plotting extra and run the example:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[visualization]"
.\.venv\Scripts\python.exe .\examples\plot_grover_trace.py
```

The figure from [`examples/plot_grover_trace.py`](examples/plot_grover_trace.py) shows the marked/unmarked rotation, with `θ = arcsin(sqrt(M/N))` and a `2θ` turn for each Grover round, beside two histograms: ideal per-answer probabilities and frequencies from a finite number of simulated shots. It also displays the recommended first-peak round count for the known number of marked answers. The CPU simulator finds that count by checking every candidate, so this is not an unknown-answer-count method. The default uses two rounds to make it easier to see that extra rounds can pass the Austin example's one-round first peak. Change `--iterations`, `--shots`, and `--seed` to explore. The shot histogram samples the ideal classical distribution; it does not add gate or readout noise and does not use quantum hardware. The plot is optional and limited to five qubits so its answer labels remain readable; the simulator's separate limit is 16 qubits.

## Try your own simple rule

You can change the rule and ask the program to search four-bit strings whose first two bits are `11`:

```python
from austins_algorithm import run_austin_search

def starts_with_11(bits):
    return bits.startswith("11")

result = run_austin_search(
    4,
    starts_with_11,
    shots=512,
    seed=7,
)

print("Answers:", result["solution_examples"])
print("Ideal chance:", result["statevector_success_probability"])
print("What ran:", result["status"])
```

The answers are `1100`, `1101`, `1110`, and `1111`. Save the code as `my_search.py` in this folder. Run it with:

```powershell
.\.venv\Scripts\python.exe .\my_search.py
```

On macOS or Linux, run `./.venv/bin/python ./my_search.py`. The `seed` makes the simulated samples repeat the same way when you run the example again.

**What the computer does:** it checks all 16 strings with your rule, then calculates what an ideal Grover circuit would do. That makes it useful for learning and checking small examples. It does not show that a quantum computer searched faster.

## What Grover's search is doing

Grover's method has four simple ideas:

1. Put all possible answers into a mix called a **superposition**.
2. Use an **oracle**: a circuit that recognizes the right answers and gives them a tiny math sign change called a phase flip.
3. Use a **diffuser**, a math step that boosts the chance of finding the marked answers.
4. **Measure** the circuit to get an answer.

The example's oracle is made for its exact three-switch rule. A useful search for another real problem needs an oracle for that problem; changing a label does not create one automatically.

The package includes both a teaching rule that runs on a regular computer and quantum-circuit oracles for the project example and explicitly listed marked answers. These are different things: the regular-computer rule is checked against every candidate, and an arbitrary Python function cannot be converted automatically into an efficient quantum circuit. See [`GETTING_STARTED.md`](GETTING_STARTED.md) for the steps and limits.

The package includes an offline adapter for the classical simulator and an experimental interface for future plug-ins. Each adapter request names its execution mode. The IBM comparison command below has its own explicit physical job option. See [`ADAPTERS.md`](ADAPTERS.md) for the separate general adapter contract.

## Optional circuit examples

The first example above needs no quantum software. If you want to build and run a circuit in a local simulator, install one optional tool from this folder:

The basic teaching program supports Python 3.10 and newer. Use Python 3.11 or newer for the current Cirq releases and the `[all]` extra. These optional examples were run locally with Python 3.12.15, Qiskit 2.6.2, and Cirq 1.7.0.

The commands below are for Windows PowerShell. On macOS or Linux, replace `.\.venv\Scripts\python.exe` with `./.venv/bin/python`.

```powershell
.\.venv\Scripts\python.exe -m pip install ".[qiskit]"
.\.venv\Scripts\python.exe examples\qiskit_austin_circuit.py

.\.venv\Scripts\python.exe -m pip install ".[cirq]"
.\.venv\Scripts\python.exe examples\cirq_austin_circuit.py
```

The Qiskit example draws `build_austin_circuit()` and samples that same circuit through `run_austin_circuit_local_statevector()`. That runner uses the built-in two-CZ reversible oracle and diffuser, then reports measured counts and ideal success probability. `LOCAL_IDEAL_STATEVECTOR_SIMULATION` means Qiskit simulated the circuit on an ordinary computer; the runner submits no provider job and uses no quantum hardware. The generic `run_local_statevector(...)` helper remains a separate route for circuits whose marked bitstrings are supplied explicitly.

After installing the Qiskit extra, run `python -m austins_algorithm --engine qiskit --shots 1024 --seed 7` with your environment's Python. The JSON report includes raw `counts` in `q2 q1 q0` order, `verified_candidates` checked against the Austin predicate, and `verified_shots`. The command uses one Grover round by default. Set `--iterations` to explore how the probability changes. For a custom list of marked bitstrings or an amplitude trace, use the default CPU engine.

These examples run locally.

## Compile, search, and compare

From the package folder, use an isolated Python environment. The commands below use the Python executable for that environment.

For a caller-defined Boolean search problem, see [the JSON constraint workflow](CONSTRAINT_WORKFLOW.md). The six-variable sensor kit uses one work qubit with automatic factorization. The workflow includes an exhaustive reversible oracle audit, OpenQASM export, a direct classical baseline, an optional offline IBM snapshot run, and a [fixed-placement round sweep](CONSTRAINT_WORKFLOW.md#compare-rounds-at-two-fixed-starting-placements) with separate calibration and validation shots. Eligible circuits can also use the [clean-ancilla diffuser](CONSTRAINT_WORKFLOW.md#reuse-a-clean-work-qubit-for-the-diffuser) without increasing circuit width. Use the [matched diffuser comparison](CONSTRAINT_WORKFLOW.md#replicate-the-diffuser-comparison-across-snapshots) to measure gate counts and verified hit rates over distinct seed batches.

Run `python -m austins_algorithm constraint-classical examples/field_sensor_kit.json` for a dependency-free exact first witness. The [classical benchmark](CONSTRAINT_WORKFLOW.md#classical-first-witness-benchmark) compares unit propagation with the unsigned scan on five bounded formulas. The [unit-reduced oracle](CONSTRAINT_WORKFLOW.md#reduce-the-oracle-with-forced-literals) uses forced literals to cut work qubits in the reversible site interlock example. The [fixed-target comparison](CONSTRAINT_WORKFLOW.md#factorization-after-unit-reduction) shows how factorization changes width and compiled cost. The [complement phase cover](CONSTRAINT_WORKFLOW.md#phase-cover-using-failing-assignments) can reduce gates at the same active width. The [`constraint-cover` command](CONSTRAINT_WORKFLOW.md#select-a-cover-by-compiled-resources) compares exact compiled candidates and can export the one that fits a caller-supplied wire cap. It can also [sample those exact circuits](CONSTRAINT_WORKFLOW.md#sample-both-verified-covers-on-a-shared-layout) with a shared starting layout on an offline IBM snapshot. The [witness budget](CONSTRAINT_WORKFLOW.md#plan-and-check-a-verified-witness-budget) estimates shots for a caller-set probability and times fresh batches against the exact classical solver. [Repeated validation](CONSTRAINT_WORKFLOW.md#repeated-validation-at-fixed-budgets) measures batch success at the fixed planned budgets. The [snapshot method option](CONSTRAINT_WORKFLOW.md#choose-the-snapshot-simulation-method) can use Aer density-matrix simulation on verified physical-index circuits.

Run `witness-schedule` to calculate how many validation seeds a target batch success probability and tolerated miss count require. [Pre-run schedule sizing](CONSTRAINT_WORKFLOW.md#size-a-holdout-before-sampling)

```powershell
python -m pip install ".[qasm]"
python -m austins_algorithm compile --problem austin --qasm-output austin-compiled.qasm
python -m austins_algorithm compile --problem mod4-range
python -m austins_algorithm compile --problem mod4-range --oracle work-qubit
python -m austins_algorithm search --problem mod4-range --strategy known --audit-oracle
python -m austins_algorithm search --problem mod4-range --strategy unknown --max-shots 64 --max-oracle-queries 256
python -m austins_algorithm compare --shots 1024 --seed 7
```

- **Compile** checks the ideal distribution in classical bit order after transpilation and reimport, then reports depth and two-qubit gates. It defaults to a reproducible synthetic line. Use `--compile-target manila`, `jakarta`, or `torino` for a [bundled IBM snapshot](CONSTRAINT_WORKFLOW.md#compile-for-an-offline-ibm-snapshot). The portable OpenQASM carries the compiled active wires. Keep the report's wire map if physical qubit identity matters. `--qasm-output` creates a new file.
- **Search** accepts a reversible phase oracle and an independent verifier through [`grover_search.py`](src/austins_algorithm/grover_search.py). The second example searches for a four-bit integer in `[8, 16)` with remainder 1 modulo 4. It defaults to the direct phase oracle on four data wires. Select `--oracle work-qubit` on either the search or compile command to run the five-wire reference. On the synthetic line, both compilation variants use the same five-qubit target. The report names the oracle variant. The optional exhaustive audit verifies phases and clean work qubits separately. An unknown-count run records its attempt and logical oracle budgets. `BUDGET_EXHAUSTED` means the run ended before finding a verified candidate.
- **Compare** reports direct classical solution time, ideal sampling, and a local synthetic per-gate depolarization experiment. The seed controls local sampling and compilation.

Run the [packaged Manila smoke baseline](CONSTRAINT_WORKFLOW.md#run-the-packaged-manila-smoke-baseline) to check compilation, sampling and receipt comparison from a checkout or source distribution.

IBM snapshot comparison uses packaged calibration snapshots and runs offline.

```powershell
python -m pip install ".[ibm]"
python -m austins_algorithm compare --ibm-model all --shots 1024 --seed 7
python -m austins_algorithm compare --problem mod4-range --ibm-model all --shots 1024 --seed 7
python -m austins_algorithm compare --problem mod4-range --ibm-model all --round-sweep --shots 1024 --seed 7 --validation-seed 19 --transpiler-seed 7
python -m austins_algorithm compare --problem mod4-range --ibm-model all --placement-check --minimum-hit-rate 0.40 --shots 512 --evaluation-seeds 29 43
```

Choose `manila`, `jakarta`, or `torino` for a single snapshot. The modulo-4 command runs both oracle variants with two Grover rounds and the same shot budget on each target. It checks ideal distribution equivalence after OpenQASM reimport and reports verified counts, compiled wire maps, and the observed hit-rate difference. A partial comparison leaves the difference unset. See [IBM model research and measured results](IBM_MODEL_RESEARCH.md).

Add `--round-sweep` to compare zero through three rounds for the direct oracle. The shot budget applies to each circuit in each stage, up to 1,024 shots. The sweep fixes the starting placement on each snapshot, uses `--seed` for calibration shots, and tests the selected round count against the ideal baseline using `--validation-seed`. The calibration and validation seeds must differ. The report keeps both stages and records the compiled measurement maps and resources.

Use `--placement-check` with your required `--minimum-hit-rate` to compare one and two rounds across two prespecified placements. It uses two distinct evaluation seeds and pools shots only within the same circuit and placement. A round count qualifies when both placements clear the specified rate using simultaneous confidence bounds. The report selects the qualifying round count with the fewest summed two-qubit gates across the tested placements. The 40% example is a demonstration threshold. For repeated checks in one Python process, use the [reusable placement plan](IBM_MODEL_RESEARCH.md#reuse-verified-circuits-across-placement-checks). Add `--receipt-dir` to [save and verify the placement evidence offline](IBM_MODEL_RESEARCH.md#save-and-verify-a-placement-receipt). Use `placement-receipt-compare` to [compare two verified runs](IBM_MODEL_RESEARCH.md#compare-two-placement-receipts) or `placement-receipt-replay` to [rebuild and rerun a saved receipt](IBM_MODEL_RESEARCH.md#replay-a-placement-receipt) on installed snapshots.

Add `--native-qasm` to an offline `compare --ibm-model` command to try the experimental parser. The `ibm` extra installs the standard parser for the default import and fallback. See [matched importer verification](IBM_MODEL_RESEARCH.md#choose-an-openqasm-importer-for-offline-comparisons).

A physical experiment uses a saved IBM account, an authorized service instance and a named backend. Only invoke this form when the account and usage budget are ready. Replace each placeholder with your local configuration.

```powershell
python -m austins_algorithm compare --shots 256 --backend BACKEND --account SAVED_PROFILE --instance SERVICE_INSTANCE --max-execution-seconds BUDGET_SECONDS
```

The hardware report distinguishes the provider's QPU usage from local elapsed time. A local wait timeout can leave the provider job running. Use the reported job ID to check its status. For paid accounts, use the provider's cost controls as well.

## What this package can and cannot do

It is useful for:

- learning the steps in Grover search;
- trying small search rules and checking the expected answers;
- looking at example Qiskit and Cirq circuits; and
- practicing how to test a quantum algorithm against a regular-computer method.

The built-in regular-computer simulator is limited to 16 qubits. This is a limit of this teaching program, not a limit on quantum computers. Its work grows very quickly as you add more qubits.

This package does not provide a complete tool for every part of quantum computing. Chemistry, machine learning, quantum communication, and other fields have different problems and may need different algorithms. To test a new use, first say what problem it should solve, build the right circuit, compare it fairly with good regular-computer methods, and check the costs and errors. Grover's search is not automatically the best choice.

One possible adaptation use is searching a finite set of fully specified options against explicit hard constraints. That would require an efficient, correct reversible oracle and a fair classical comparison. Search cannot discover missing context, infer a person's motive, or approve an action; evidence review, fairness, permissions, and recovery remain outside Grover's circuit. This package has no adaptation-system bridge, and this example does not claim a practical quantum advantage.

For readers who want the short math version: `N` is the number of choices, `M` is the number of right answers, and `r` is the number of Grover rounds. `θ` (theta) is a number set by how many answers are right. The ideal chance is

\[
P_r=\sin^2((2r+1)\theta),\qquad \sin^2(\theta)=M/N.
\]

The simulator finds `M` by checking every choice first. A real quantum search needs a circuit that checks the rule without doing that full classical list first.

## Tests and project guide

For milestone **#145** on **2026-11-13**, both Windows copies passed
658 SDK-enabled and 412 standard-library checks, with four and 250 skips
respectively. Both executables passed the native Future Reader and weighted
project smoke tests and reproduced the weighted run from exported Python.

For milestone **#144** on **2026-11-13**, the Windows suites passed in both maintained copies. The WSL suite ran against the workspace source.

| Environment | Passed | Skipped | Failures or errors |
| --- | ---: | ---: | ---: |
| Windows Python 3.12.15 with Qiskit, Aer, OpenQASM import and IBM Runtime | 658 | 4 (Cirq and symlink privileges) | 0 |
| Windows Python 3.10.22 with optional SDKs excluded | 412 | 250 (optional dependencies and symlink privileges) | 0 |
| WSL Ubuntu Python 3.12.3 with optional SDKs excluded | 414 | 248 (optional dependencies) | 0 |

Both copies of the standalone executable passed the project save and reopen, exact optimization, manual and automatic simulation, Overview, and assistant checks. Each generated a 23-file export whose standalone Python replay reproduced the saved run. The source distribution and wheel each verified all 103 runtime files in both copies. An isolated wheel installation passed the exact search and desktop smoke check in each location.

The local implementation checks for milestone **#143** on **2026-11-12** passed in both maintained package copies:

| Environment | Passed | Skipped | Failures or errors |
| --- | ---: | ---: | ---: |
| Python 3.12.15 with Qiskit 2.6.2, Aer 0.18.2 and IBM Runtime 0.51.0 | 635 | 4 (2 Cirq; 2 symlink privilege) | 0 |
| Python with optional SDKs excluded | 391 | 248 (optional dependencies and symlink privilege) | 0 |
| Python 3.10.22 with optional SDKs excluded | 391 | 248 (optional dependencies and symlink privilege) | 0 |
| WSL Ubuntu Python 3.12.3 with optional SDKs excluded | 393 | 246 (optional dependencies) | 0 |

These are dated results for the tested source and environments. The eight-layout Jakarta benchmark produced identical uninterrupted and continued study bytes in both copies, with all eight reference and fusion generations independently checked by SDK-free processes. The measured restart costs are recorded in the constraint guide. The new benchmark-record audit also passes both existing eight-layout records and a byte-preserving relocation in each copy, without the SDK or evidence changes. Full runtime archive checks cover all 67 package files, including the previously stale unchanged Qiskit adapter.

[GitHub Actions run #8](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions/runs/43249759269) passed all ten jobs for commit `6c2370b060dbfdcf2ea3feeca72963a282c386ad` on **2026-11-12**, including Python 3.10–3.14, optional frameworks, and the packaged Manila smoke regression. That result applies to that committed revision; later local updates need their own hosted run.

[GitHub Actions run #9](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions/runs/43286454985) exposed the benchmark fixture's shared subprocess mock on Ubuntu. Update #143 scoped the replacement to the benchmark module. All ten hosted jobs passed for repair commit `33eab1190c31188c322951a78cc6b7f4e620c0fa`. [GitHub Actions run #10](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/actions/runs/43300577324)

To run the package checks:

```powershell
python -B -m unittest discover -s tests -v
```

The package has more detail in [`USE_CASES.md`](USE_CASES.md), [`EXTENDING.md`](EXTENDING.md), [`ADAPTERS.md`](ADAPTERS.md), and [`ROADMAP.md`](ROADMAP.md). The test suite checks the example code and math; it does not certify a real device or prove that the method is useful in every field.

For every package update, follow [`CONTRIBUTING.md`](CONTRIBUTING.md). Review this README's commands, limits and dated verification. Check its version against `pyproject.toml`, add a timestamped changelog entry, and reconcile both maintained copies with byte-count and SHA-256 checks.

## Credit and sources

This package credits **Austin Gregory Simpkins and Chandler Middlebrook / ChronoRealm** for the project example. The search method is based on **Lov K. Grover's** 1996 paper, [“A fast quantum mechanical algorithm for database search”](https://doi.org/10.1145/237814.237866). IBM also has a [Grover's algorithm tutorial](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm).
