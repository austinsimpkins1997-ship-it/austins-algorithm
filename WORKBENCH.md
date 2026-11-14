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
The desktop version is 0.2.3-alpha. Weighted project and export
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
   session. A preview consumes neither.
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
the exact amplitudes of the bounded ideal CPU simulation. The default bounds
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
release is 0.2.3-alpha. The Python package continues to report 0.1.0 for the
existing checkpoint and receipt compatibility boundary.

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
