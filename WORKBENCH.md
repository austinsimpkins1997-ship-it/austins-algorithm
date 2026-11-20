# Austin's Algorithm Workbench

Austin's Algorithm Workbench is a local desktop application for small Boolean
search and minimum-cost experiments. The Windows x64 executable bundles the
Python and Tk runtime. From a Python installation with Tk, launch the source
application with `python -m austins_algorithm workbench`.

## Work through a project

**Overview** explains the current goal, latest measurement, best candidate and
what to try next. Start there if the simulation grid feels unfamiliar. Select
**Optimize** to compute the exact minimum using the local classical solver.
Select **AI Assistant** for built-in, rule-based help about the active
simulation, selected state, costs, budgets, or sharing. The assistant runs
inside the app and applies suggested actions only when you select them.
Assistant questions and answers are temporary interface state.

### Decision Review

Open **Decision Review** in the sidebar. The built-in public-office case is a tabletop for five fictional service requests. It compares normal intake, an email and identity outage, and isolated recovery. Costs are illustrative effort points. Start by reading the common plan, then compare it with the exact minimum for each scenario. A cheaper plan can work in one scenario and fail in another. Unavailable options can be ruled out by the plan. Required checks that fail or remain unknown hold the result.

- **Plan** shows named choices, reasons for rejecting alternatives, baseline, dependencies, interfaces and recovery conditions. **Edit case** opens the versioned plan for changes. Use a single additive cost unit. Every named rule must be true in its scenario. Each rule is a list of choice IDs joined by OR, with `-choice` meaning that choice is excluded. An empty clause is contradictory. Import a Dimensional Design journal export to start with its named layers and evidence. Supply reviewed costs and scenario rules before optimizing the imported draft.
- **Evidence** records PASS, FAIL or UNKNOWN with the named checker and a source. A check is pinned to the current project revision and required upstream observations. Changing an upstream observation can hold downstream checks until fresh evidence is recorded. The stored status and the effective status can differ.
- **Handoff** shows affected people, amendment roles, the decision owner, delivery owner, resources and verifier. Authority, consent and funding may each have required evidence. Actor names and sources are supplied by the reviewer. Recording a disposition captures that person's decision on the pinned model and cannot authenticate authority or execute a change.
- **Recovery replay** shows completed, pending, lost, duplicated and other outcomes by lane. Compare staged conditions and reconcile case IDs. **Stasis trace** steps through the reviewed LED guard counterexample and the corrected control state. Both use local data from the preset or a selected replay. Bench timing and real equipment behavior still require measurements.
- **Practice** starts with a prediction and baseline. Use a short correction and a hint, then attempt a new case independently. The app tracks acquisition, independence, transfer and the separate seven-day retention check. It stores the attempt sequence and rule results. Simulated attempts exercise software only.
- **AI Assistant** gives local, rule-based guidance for the active review and points to the next unresolved check or replay.

Use **Save** to choose a Decision Review project file. Later valid edits checkpoint to that file. If it changed outside the workbench, save to a new location after checking which record to keep. Use **Export** for a separate Decision Review ZIP with reviewed inputs, scenario and trace data, practice history, and a readable report. Verify a received file without extracting it. Existing weighted simulation projects use their own format.

```powershell
python -m austins_algorithm decision-review example
python -m austins_algorithm decision-review review
python -m austins_algorithm decision-review review PATH-TO-CASE.json
python -m austins_algorithm decision-review export PATH-TO-NEW-EXPORT.zip
python -m austins_algorithm decision-review verify PATH-TO-EXPORT.zip
python -m austins_algorithm decision-review benchmark --repeats 32
```

An export verifies against the installed implementation using the archived inputs. Give `verify` a `--manifest-sha256` from a separately retained receipt to check that it is the expected bundle. For repeated finite decisions, `prepare` emits a pinned policy that `verify-policy` can check. The `PreparedDecisions` API matches the case and current input digest and holds at the local monotonic deadline. Its output is a proposal. The benchmark compares same-process lookup with fresh exact review and includes preparation and replay costs. It records the high-resolution elapsed clock and the deadline clock separately. Input digest computation remains outside the lookup timing. Compare full workflow cost before choosing prepared lookup.

New Decision Review projects and exports use format version 2 with a revision
record. You can open either version 1 draft project format. Its next save
uses version 2, preserving existing revisions and starting an empty history
when the draft had none. The verifier checks a version 1 archive in its
original format against its archived bytes and optional manifest pin.

### Pilot receipts

From **Decision Review → Pilot receipts**, open a rehearsal receipt and a recovery
receipt. Use the current expected SHA-256 digest recorded when the corresponding
owner command produced each receipt. The owner commands are
`python -B -m decision_containment rehearsal-plan --include-cascade` and
`python -B -m resilience_kernel recovery-plan --fault primary-link`, run from their
respective projects. Keep the two producer digests with the pilot case. A changed
file must match a new trusted producer digest and pass the coverage checks before
it can be displayed.

The tab reports required checks, selected cases, alternative tied minima,
evidence fingerprints, and the recovery gate. A metered route can be proposed
while operator approval is still pending. These are synthetic local receipts.
The view holds verified input for the current session. Refresh evidence through
the owning workflow before loading a revised receipt.

```powershell
python -m austins_algorithm decision-review receipts rehearsal.json recovery.json --rehearsal-sha256 REHEARSAL_PIN --recovery-sha256 RECOVERY_PIN
```

### Future Reader

The **Future Reader** sidebar view runs a separate practice controller.
**Pinpoint** shows the current state and deadline. Pick a valid route or
**Custom**, preview it, and select **Confirm**. **Reverse** restores a
prior position while retaining elapsed time. **Check** certifies the scoped
finite result. **Wait comparison** shows the consequence of a later check.
**Live recovery check** terminates a real disposable local worker process
and verifies that its replacement resumes the practice job. It uses a
separate SQLite database and shows the generation and effects in the receipt.
**Checkpoint** verifies or exports practice history. **Offline HTML** saves
the separate browser companion.

The executable bundles the side project. For source use, set
`PYTHONPATH` to the Future Reader package directory plus Austin's
`src` directory, or install both local packages. Builds accept
`--future-reader PATH` and default to a sibling `future-reader`.
The desktop version is 0.2.6-alpha. Weighted project and export
verification continues to use the 0.2.0-alpha wire version.

1. **Build.** Open a preset or enter a project name, one to eight variables,
   nonnegative integer costs, and hard rules. The costs are additive. Each
   variable contributes its cost when enabled. Rules use signed one-based
   literals. For example, `1 -2` means q0 is enabled or q1 is disabled. Every
   line must be true. An empty set of rules permits all assignments.
2. **Simulate.** Set a seed, shot limit, oracle query limit, and maximum rounds
   per shot. Inspect a preview, move between its stages, and select a state
   block to see its bits, amplitude, probability, feasibility, and cost.
3. **Choose a measurement.** A shot prepares a fresh uniform state and
   applies the selected whole Grover rounds before sampling. Use zero rounds
   to sample directly from the uniform distribution. Automatic search
   chooses affordable rounds and updates its cost threshold when it measures
   a feasible improvement. The shot and query limits cover the entire
   session. A preview consumes neither. The seed makes the local sampled
   sequence repeatable for the same inputs and choices.
4. **Read the results.** Compare the best verified measurement with the
   exact minimum. Inspect historical measurements, then review or copy the
   generated Python program. The report distinguishes an optimum proved by
   exact enumeration, a proved infeasible problem, and the best candidate
   obtained within the simulation budget.
5. **Save or export.** Save a project to continue the same seeded session
   later. Use **Export** to package the project, evidence and replay program
   for handoff.

The **Austin search** preset uses q0 enabled and q1 different from q2. The
**Weighted field sensor kit** preset finds the least-cost valid kit. For the
bundled costs, its first feasible assignment in unsigned order is `001010`
at cost 7, and its unique optimum is `110101` at cost 4. Assignment strings
show the most significant bit on the left, so q0 is rightmost.

## Read a simulation frame

- **Prepare** gives every basis state the same amplitude.
- **Oracle** flips the amplitude sign of the marked states. The probability of
  each state is its squared amplitude, so the sign change alone preserves
  measurement probabilities.
- **Diffusion** reflects amplitudes around their mean. The new probabilities
  can increase or decrease the chance of a marked outcome as rounds advance.

Before the first valid measurement the oracle marks feasibility. Afterward it
marks feasible states with costs strictly below the incumbent. Historical
frames retain the threshold that applied to their shot. The display shows
amplitudes and ideal probabilities computed by the bounded CPU simulation.
For the Austin preset before any valid result has been found, one full
Grover round gives each of the two valid bit strings a 50% ideal chance.
A guaranteed valid measurement can still pick either string. Hardware
decoherence and gate or readout errors can change those chances. The default bounds
are 16 shots, 64 oracle queries, and 4 rounds per shot. Supported maxima are
64 shots, 256 queries, and 8 rounds per shot.

## Save and export

The project file is versioned JSON. It carries the problem, simulation
settings, measurements, and saved preview. Loading validates and replays
the saved choices before making them available to the workbench. Saving uses
an atomic replacement, and an existing file requires an explicit overwrite.

The Export ZIP contains the following evidence.

- Project, problem, settings, preview, and full run JSON.
- The exact classical reference and a readable offline HTML report.
- Complete measurements and state amplitudes in CSV.
- A generated Python replay program with its required engine source.
- Version metadata, licenses, and SHA-256 digests for the exported files.

To reproduce a saved run outside the desktop app, extract the entire ZIP and
run `python reproduce.py` from the extracted folder with Python 3.10 or newer.
The program prints the simulation result. A project without measurements
exports its input and preview, and the generated program starts a fresh
seeded run. Treat a replay program received from someone else as executable
code. The workbench loads project JSON as data.

Check a received ZIP without extracting it with
`python -m austins_algorithm workbench-export-verify PATH-TO-EXPORT.zip`.
This checks the manifest and replays the project using the installed engine.

## Command line and benchmark

```powershell
python -m austins_algorithm optimize examples/weighted_sensor_kit.json --mode exact
python -m austins_algorithm optimize examples/weighted_sensor_kit.json --seed 7 --shots 24 --queries 96
python -m austins_algorithm optimize examples/weighted_sensor_kit.json --mode preview --incumbent 7 --rounds 1
python examples/benchmark_weighted_search.py --model jakarta --output .build/weighted-benchmark.json
```

The first three commands use the standard library. The snapshot comparison
uses the optional IBM dependencies and a dated local model. Its reversible
threshold oracle prunes infeasible and over-budget prefixes and synthesizes
disjoint phase terms. Every basis input is audited against a separate
feasibility and cost check. The benchmark reports construction and audit
costs alongside the compiler and sampling costs.

The seeded ideal benchmark compares the best result reached over the full
budget to the exact minimum. The Jakarta section compares one fixed round
at incumbent cost 7. A verified Jakarta hit is any cheaper feasible
assignment. It is a different endpoint from attaining the exact optimum in
the adaptive simulation. See [the threshold-search paper](https://arxiv.org/abs/quant-ph/9607014).

On November 13, 2026, the exact reference checked 64 assignments and found
nine feasible kits. The ideal adaptive schedule attained the exact cost in
23 of 24 seeded trials with a limit of 24 shots and 96 queries per trial.
The audited fixed-threshold circuit gave 47.27% ideal improvement
probability. On the local Jakarta target, compilation produced depth 746
and 548 two-qubit gates. An OpenQASM export and re-import preserved the
ideal measurement distribution within the benchmark's tolerance of 1e-10.
Jakarta's bundled calibration from May 27, 2024 produced 2 verified
improvements in 64 simulated shots. These observations give separate
benchmarks for search attainment and noise on a fixed circuit.

## Build the Windows executable

Use Windows x64 with CPython 3.12 and Tk 8.6. Run these commands from the package root.

```powershell
uv venv --python 3.12 .build/workbench-build-env
uv pip install --python .build/workbench-build-env/Scripts/python.exe -r tools/workbench-build-requirements.txt
& .build/workbench-build-env/Scripts/python.exe -B tools/build_workbench.py --directory .build/workbench-build
```

The executable and its SHA-256 record are in the selected build directory's
`dist` folder. Use a fresh output directory for each build. The builder
includes the runtime, interface assets, and replay source. The desktop
release is 0.2.6-alpha. The Python distribution reports 0.2.6a1. Existing weighted exports retain
the 0.1.0 replay-engine compatibility label.

## Design sources and operating scope

The layout uses distinct primary and secondary actions and semantic colors
informed by Adabraka UI. The block grid borrows Minecraft's inspectable
building-block approach for basis states. Lucide icons use pinned upstream
artwork. The guide follows the Rust Reference's example-led documentation
practice. The Public APIs catalog was reviewed for integration options.
Local project files and the existing compiler interfaces cover this workflow,
and the desktop application operates offline.
[Adabraka UI](https://github.com/Augani/adabraka-ui) ·
[Minecraft](https://www.minecraft.net/en-us/about-minecraft) ·
[Lucide](https://lucide.dev/icons/) ·
[Rust Reference developer guide](https://rust-lang.github.io/reference/dev-guide/introduction.html) ·
[Public APIs catalog](https://github.com/public-apis/public-apis)

The desktop build targets local Windows use. Organization-wide deployment
still needs installer signing, managed deployment and update procedures, and
accessibility validation with users. The native app and the dated model
benchmark produce local simulation evidence.

## Focus and resume

Open the weighted-search project or Decision Review, then select **Focus** in
the toolbar. Enter a real goal and one concrete next action, then select
**Start**. The footer shows the recorded status and next action while the
dialog is closed.

- **Park request** records another task while keeping the current session active.
- **Pause + checkpoint** saves a short progress note and the next action in the project draft.
- **Priority interrupt** records the interruption and pauses the session with a checkpoint.
- **Mark selected resolved** closes a recorded interruption without changing the task inputs.
- **Resume** compares current applied inputs with the checkpoint. If they changed,
  review the current problem or Decision Review evidence and the next action,
  select the explicit review checkbox, then resume. The history retains the earlier input digest.
- **Finish session** marks the recorded session complete and preserves its history.

Use **Save** to keep the checkpoint across program restarts. **Export** includes
the same focus record in `project.json`, a standalone `focus.json`, its SHA-256
manifest entry, and the readable report. Earlier projects with no focus record
remain supported. The portable API is `start_focus`, `focus_action`, and
`focus_view` in `austins_algorithm.focus_session`.

A session holds up to 64 interruptions and 128 events. Entries are caller
reports and task metadata. The source comparison covers the applied weighted
problem/settings or current Decision Review document. Apply Build edits through
the existing controls before reviewing the changed basis. Future Reader keeps
its separate practice checkpoint. The Focus dialog uses the weighted-search
project when a weighted page or Future Reader is open.

The design draws on the November 15 Bernard Baars excerpt supplied by Austin.
The two practical changes are a clear next action and a place to park competing
requests. Human attention, absorption, productivity and learning effects require
user evaluation. Focus does not change solver outcomes, evidence checks,
approval status or action authority. Normal errors and held checks stay visible
in their existing screens.


## Planning 0.3

The native Planning page adds larger classical plans, isolated jobs, shared summaries, project files and complete evidence export. Use [Planning](PLANNING.md) for the first session, exact limits, CLI/API, recovery semantics and operator worksheet. Weighted quantum simulation and Decision Review keep their existing formats and boundaries.

## Security Lab — local 0.3.1 alpha

The native Security Lab page runs named synthetic policy checks and bounded public teaching-oracle sequences through isolated jobs. Save/open inputs, inspect failure traces, stop work, and export recorded evidence. Circuit QASM needs the optional Python Qiskit extra. No live targets, passwords/hashes, adversary executors or practical quantum cracking advantage are implemented. See [Security Lab](SECURITY_LAB.md).

## Practical evidence (0.3.2)

Overview → **Check answers and phase & export** creates a new evidence folder for the fixed public teaching task. It preserves the active project. The bounded CPU comparison exports protocol, raw trials, a readable summary, all 23 source decisions and a checksum manifest. It cannot declare quantum advantage or production acceptance. See [the guide](PRACTICAL_EVIDENCE.md).

## Relative phase check

The CPU comparison also checks the relative phase between the two valid answers, `011` and `101`. A modeled half-turn keeps both answers valid while changing the interference readout from 100% to 0%. Each export includes all three phase settings. The offline Workbench preview produces the state. After a rotation of 0, 90 or 180 degrees on q2, CX(q2,q1) and H(q2) convert relative phase into an observable probability for 011. The expected values are 1, 1/2 and 0. Probabilities include all outcomes. The phase study runs outside the timed answer comparison. A failed check remains in the export and causes a nonzero CLI exit. This circuit diagnostic measures an ideal model. Hardware noise and application speed require separate evidence.
