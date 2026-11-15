# Austin's Algorithm

**A local workbench and Python toolkit for quantum-search experiments, bounded optimization, and evidence-based decision review.**

**Workbench 0.2.6-alpha** · **Python package 0.1.0** · **Python 3.10+** · **[Apache-2.0](LICENSE)**

Created by **Austin Gregory Simpkins and Chandler Middlebrooks / ChronoRealm**. Current source reviewed **November 15, 2026**.

Start with a small search problem, inspect each simulated step, compare a candidate with an exact classical answer, and save the evidence. For a decision involving several operating conditions, use Decision Review to compare plans, inspect missing checks, and rehearse recovery. The Windows application brings these workflows together with instructions, visualization, project files, and a clear Export action.

[Quick start](#quick-start) · [Capabilities](#capabilities) · [Workbench](#use-the-workbench) · [CLI and Python](#use-the-cli-and-python-api) · [Verification](#verification-and-current-status) · [Documentation](#documentation)

## Quick start

### Run from source

Open a terminal in the package folder containing `pyproject.toml`. The base package uses the Python standard library. The graphical Workbench also needs a Python installation with Tk/Tcl.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m austins_algorithm --shots 256 --seed 7
.\.venv\Scripts\python.exe -m austins_algorithm workbench
```

The first run prints a JSON report. The second opens the Workbench. Installation also creates the `austins-algorithm` command and the `austins-algorithm-workbench` desktop launcher.

On macOS or Linux, create the environment with `python3 -m venv .venv` and use `./.venv/bin/python` for the remaining commands. See the beginner walkthrough for an explanation of the oracle, measurement, and a custom search rule. [Getting started](GETTING_STARTED.md)

### Open the existing Windows build

Both maintained local copies have the verified November 15 Workbench build at this package-relative path.

```powershell
& ".\.build\merge-0.2.6-20261115\dist\AustinsAlgorithmWorkbench.exe"
```

This is a local build location. A fresh Git checkout needs the source installation above or its own executable build. The Windows executable bundles its Python runtime, Workbench assets, and Future Reader companion. It remains unsigned. Build instructions describe the required tools, output directory, and verification. [Build and check release artifacts](CONTRIBUTING.md#build-and-check-release-artifacts)

## Capabilities

| Area | What you can do | Guide |
|---|---|---|
| Grover search | Run the original three-qubit example or a small caller-defined predicate. Inspect ideal probabilities, seeded samples, verified answers, and amplitude traces. | [Getting started](GETTING_STARTED.md) |
| Step-by-step visualization | Inspect preparation, oracle phase marking, diffusion, round counts, amplitudes, and measurement. Explore different round choices and compare ideal probabilities with sampled counts. | [Workbench](WORKBENCH.md) |
| Weighted optimization | Define Boolean constraints and nonnegative additive costs. Compute the exact minimum, run a bounded simulated search, or preview a planned attempt. | [Weighted examples](examples/benchmark_weighted_search.py) |
| Circuit construction and search | Use Qiskit or Cirq examples, reversible phase oracles, independent classical verifiers, known-count Grover search, and bounded unknown-count search. Audit small oracles and work-qubit cleanup. | [Constraint workflow](CONSTRAINT_WORKFLOW.md) |
| Compilation and OpenQASM | Compile for a synthetic line or a bundled IBM target. Check measurement behavior after compilation and OpenQASM reimport. Report depth, two-qubit gates, and wire mappings. | [Compilation](CONSTRAINT_WORKFLOW.md#compile-for-an-offline-ibm-snapshot) |
| Offline IBM models | Compare ideal and noisy simulations using dated Manila, Jakarta, and Torino snapshots. Test placements, rounds, oracle covers, and diffuser choices. | [IBM model research](IBM_MODEL_RESEARCH.md) |
| Budget and layout planning | Plan shots, oracle queries, witness schedules, and starting layouts. Compare snapshot probability profiles, sensitivity, and dated calibrations. Validate selected budgets on fresh seeds. | [Planning and studies](CONSTRAINT_WORKFLOW.md) |
| Resumable calibration studies | Compare default and fused density methods, save reference and fusion checkpoints, pause between layouts or attempts, resume pinned generations, and verify completed evidence offline. | [Study continuation](CONSTRAINT_WORKFLOW.md#restart-a-paired-study-from-completed-reference-captures) |
| Receipts and benchmarks | Save, verify, compare, and replay supported placement and diffuser evidence. Audit benchmark records, packaged smoke baselines, and run indexes. | [Receipt workflows](IBM_MODEL_RESEARCH.md#save-and-verify-a-placement-receipt) |
| Decision Review | Compare a shared plan across normal, outage, and recovery scenarios. Inspect dependencies, interfaces, evidence freshness, held checks, tied minima, and recorded human responsibilities. | [Decision Review](WORKBENCH.md#decision-review) |
| Rehearsal and learning | Step through recovery outcomes and Stasis traces. Record predictions, hints, independent attempts, transfer tasks, and delayed practice checks. Review pinned rehearsal and recovery receipts together. | [Pilot receipts](WORKBENCH.md#pilot-receipts) |
| Focus and resumption | Record one goal and next action, park requests, leave priority interruption notes, save a return checkpoint, and review changed inputs before resuming. | [Focus and resume](WORKBENCH.md#focus-and-resume) |
| Future Reader practice | Preview and confirm finite practice routes, inspect deadlines, check observed outcomes, restore a route, save separate checkpoints, and exercise disposable local-worker recovery. | [Future Reader in the Workbench](WORKBENCH.md) |
| Save, load, generate, and export | Reopen weighted-search and Decision Review projects. Generate Python from a weighted simulation. Export supported project inputs, results, focus history, reports, manifests, and replay material together. | [Projects and exports](WORKBENCH.md) |
| Local assistance and adapters | Ask the Workbench's rule-based Assistant about the active project. Use the experimental workload/capability/result contract through `AustinCpuAdapter`. | [Adapter contract](ADAPTERS.md) |
| Optional physical IBM experiment | Check a selected saved account and backend, compile and verify the circuit, then submit a separately authorized bounded comparison job. | [Hardware workflow](IBM_HARDWARE.md) |

The CPU reference enumerates the bounded candidate space to construct its ideal model. Exact optimization and Decision Review use classical methods. Circuit simulation, dated noise models, physical-device measurements, and observed operator outcomes each carry their own evidence and costs.

## Use the Workbench

1. **Choose a problem.** Start on Overview, then use Build to select the Austin search, the two-bit cost example, or the weighted field sensor kit. Review the variables, costs, and rules.
2. **Establish a reference.** Choose **Optimize** to compute the exact classical minimum. The bundled sensor kit's first feasible assignment costs 7 and its exact minimum costs 4.
3. **Inspect a run.** Open Simulate, inspect preparation and phase changes, choose rounds, and measure a seeded shot or use automatic stepping. Results keeps the sampled outcome, verification, and remaining budgets visible.
4. **Get help.** **AI Assistant** explains the current project through local rules. Review a proposed setting before applying it. Its conversation is temporary interface state.
5. **Keep a return point.** **Focus** records the goal, next action, parked requests, and checkpoint history. A priority interruption records a pause. Resume checks the applied problem, settings, or decision evidence against the saved basis.
6. **Save and share.** Choose **Save** for an editable project and **Export** for the complete supported review bundle. Weighted exports include a standalone Python replay. Verify an archive before relying on its contents.

For a multi-scenario plan, open **Decision Review** and start with the synthetic public-office intake continuity case. Its tabs cover the plan, evidence, handoff, recovery replay, Stasis trace, practice, and pilot receipts. Changing a prerequisite can invalidate affected downstream evidence. Held checks remain visible alongside optimization results.

**Future Reader** uses a separate optional practice controller. The frozen Windows build includes it. Source use requires the companion installed or configured on the source path. The merged repository includes its source in `future-reader/`, with the sibling `../future-reader` checkout supported for development. The companion has separate checkpoints and a browser workflow, with a disposable SQLite-backed worker recovery check. [Companion setup and practice workflow](WORKBENCH.md#future-reader)

## Use the CLI and Python API

After installing the package, use your environment's Python for these commands. The first group runs locally with the base package.

```powershell
python -m austins_algorithm --shots 256 --seed 7 --trace-amplitudes
python -m austins_algorithm optimize examples/weighted_sensor_kit.json --mode exact
python -m austins_algorithm constraint-classical examples/field_sensor_kit.json
python -m austins_algorithm decision-review review
python -m austins_algorithm decision-review benchmark --repeats 32
```

For the synthetic public-office preset, exact Decision Review returns an optimum cost of 9 when its required checks pass. Other prepared cases exercise missing authority, stale recovery evidence, and unavailable routes. The `prepare` and `verify-policy` operations support bounded repeated decisions. Reuse checks the current input identity and expiry. Benchmark interpretation must include preparation, input checking, verification, and refresh work.

Use a Python predicate for a small custom teaching example.

```python
from austins_algorithm import run_austin_search

def starts_with_11(bits: str) -> bool:
    return bits.startswith("11")

result = run_austin_search(
    4, starts_with_11, shots=512, seed=7, include_amplitude_trace=True
)
print(result["solution_examples"])
print(result["statevector_success_probability"])
print(result["status"])
```

This accepts `1100`, `1101`, `1110`, and `1111`. A problem-specific reversible circuit is needed for quantum circuit execution. The API's classical predicate supplies the bounded teaching model. [Extension guide](EXTENDING.md)

### Optional dependencies

| Install | Enables |
|---|---|
| `python -m pip install ".[qiskit]"` | Qiskit circuit construction and local ideal sampling |
| `python -m pip install ".[qasm]"` | Qiskit plus OpenQASM import and compilation checks |
| `python -m pip install ".[ibm]"` | Aer, IBM Runtime, bundled snapshot simulation, and the dedicated hardware workflow |
| `python -m pip install ".[cirq]"` | Cirq circuit examples |
| `python -m pip install ".[visualization]"` | Matplotlib amplitude and sampling plots |
| `python -m pip install ".[all]"` | Qiskit, OpenQASM, Cirq, and plots. Add `[ibm]` for Aer and IBM Runtime. |

The base package supports Python 3.10+. Use Python 3.11+ for the documented Cirq and `[all]` environments. Check the declared dependency ranges when preparing an SDK environment. [Package configuration](pyproject.toml)

With the relevant extra installed, explore the circuit and offline-model workflows.

```powershell
python -m austins_algorithm compile --problem austin --qasm-output austin-compiled.qasm
python -m austins_algorithm search --problem mod4-range --strategy known --audit-oracle
python -m austins_algorithm search --problem mod4-range --strategy unknown --max-shots 64 --max-oracle-queries 256
python -m austins_algorithm compare --ibm-model manila --shots 256 --seed 7
python examples/plot_grover_trace.py --output .build/grover-trace.png
```

The CLI also exposes the `constraint-*`, `unknown-count-*`, `snapshot-calibration-*`, `placement-receipt-*`, and `diffuser-*` command families. Their dedicated guides specify input schemas, compatibility checks, pins, methods, and resource limits. Use `python -m austins_algorithm --help` and the selected command's `--help` for its arguments.

Physical-device use requires an explicitly selected saved IBM profile, backend, service instance, and authorized usage budget. Start with the hardware guide's preflight. The inspected verification records cover offline simulation and provider test doubles. Physical-device results remain pending. [IBM hardware guide](IBM_HARDWARE.md)

## Understand the results and bounds

The original Austin example accepts `q0 == 1` with `q1 != q2`. Bit strings use `q2 q1 q0` order. The valid strings are `011` and `101`. One ideal Grover round assigns probability one-half to each, so either can appear in a measurement. A seeded local run reproduces its simulator sampling sequence. A physical device also introduces gate, readout, and decoherence effects.

Read ideal support, sampled counts, and independently verified candidates together. A finite batch records what happened in that batch. `BUDGET_EXHAUSTED` records a search stopping at its declared budget. Exact infeasibility requires the applicable exhaustive check or proof.

| Route | Implemented bound |
|---|---|
| CPU teaching simulator | 16 qubits, 100,000 shots, 256 iterations |
| Generic circuit search | 12 data qubits, 256 shots, 4,096 oracle queries |
| Exhaustive reversible-oracle audit | 8 data qubits |
| Weighted search | 8 variables, 64 shots, 256 queries, 8 rounds |
| Decision Review | 8 choices, 8 scenarios, 32 checks, 96 KiB input |
| Prepared Decision Review policy | 16 cases |
| Focus record | 64 interruptions and 128 history events per project |

Specific commands can impose narrower bounds. Statevector memory grows exponentially with circuit width. Snapshot records have explicit calibration dates. The generic adapter contract and dedicated IBM comparison path are separate interfaces. Check the relevant method's full setup, compilation, execution, verification, and recovery cost before drawing a practical advantage conclusion.

Decision Review records supplied evidence and human responsibilities. Its results remain proposals subject to the receiving operation's current authority and evidence checks. Focus records describe user-entered task choices. Human attention, learning, productivity, and operational reliability require their own observations.

## Verification and current status

The earlier **November 15, 2026** focus release verification recorded the following local results.

| Check | Recorded result |
|---|---|
| Selected Workbench, project, Assistant, Decision Review, and focus regressions | 64 passed in the workspace under Python 3.14.7 and 64 passed in the E-drive copy under Python 3.12.15 |
| Frozen Windows application checks | Both executables passed hidden focus start, interruption, save/open, verified export, and resume checks, alongside the existing desktop journeys |
| Package build | Verified source archive and wheel containing 118 runtime files |
| Runtime and selected-artifact parity | All 118 packaged runtime files matched both source trees. All 18 selected source/build delivery records matched their digests. |
| Executable SHA-256 | `267e3e8b4e2dadb85509bed560ff9b0d36dff1761e7ad9c351012e885914b730` |

These results apply to the recorded release and its selected checks. The existing local build receipt is `.build/focus-0.2.6-20261115-release/verification.json`. This README refresh updates source documentation after that build. Earlier SDK, standard-library, WSL, benchmark, and hosted CI outcomes retain their original dates and revisions in the changelog and workflow guides. [Change history](CHANGELOG.md)

The reconciled November 15 build is available locally at `.build/merge-0.2.6-20261115/dist/AustinsAlgorithmWorkbench.exe`, with SHA-256 `0c8cbefaa3abd59821fdd93737247732d5c9a5ef0282f2126044d8fe933e0e38`. The merged Algorithm suite completed 722 tests in each maintained root under Python 3.12.15, with 588 passing and 134 skipped. The bundled Future Reader suite completed 73 tests, with 72 passing and one skipped. Its four offline browser tests and generated companion check passed. The rebuilt executable passed its hidden smoke check. These local records are in `.build/merge-reconcile-20261115` and the companion's `.build/merge-verification`. The system Python 3.14 full-suite attempt encountered its missing `secrets` standard-library module. The successful source-root rerun used the intact Python 3.12 environment.

The Python library version is **0.1.0**. The desktop release is independently **0.2.6-alpha**. The weighted export's application wire label remains **0.2.0-alpha** for its compatibility contract. Decision Review uses its own versioned project and archive formats. These labels identify different interfaces.

To run package checks in a prepared environment, use the normal unittest entry point. Graphical checks use hidden roots and disposable fixtures.

```powershell
python -B -m unittest discover -s tests -v
```

The current release is alpha. Further evidence includes a real operator comparison, broader compatibility checks after environment changes, and an authorized physical-device study. The roadmap preserves prior measured baselines and open investigations. [Roadmap](ROADMAP.md)

## Documentation

| Guide | Use it for |
|---|---|
| [Getting started](GETTING_STARTED.md) | First CPU run, your own rule, oracle concepts, and circuit examples |
| [Workbench](WORKBENCH.md) | Desktop pages, simulation frames, Optimize, Assistant, Focus, project files, Decision Review, and Future Reader |
| [Constraint workflow](CONSTRAINT_WORKFLOW.md) | Constraint circuits, budget planning, calibration studies, resume, and benchmark verification |
| [IBM model research](IBM_MODEL_RESEARCH.md) | Dated snapshots, placements, model comparisons, receipts, and measured local results |
| [IBM hardware](IBM_HARDWARE.md) | Account/backend preflight, bounded job submission, usage, and pending device evidence |
| [Adapters](ADAPTERS.md) | Generic capability, request, workload, and result contracts |
| [Use cases](USE_CASES.md) | Candidate applications and the evidence each requires |
| [Extending the package](EXTENDING.md) | Building and verifying a supported extension |
| [Roadmap](ROADMAP.md) | Implementation milestones, prior comparisons, and remaining work |
| [Contributing](CONTRIBUTING.md) | Tests, reproducible package builds, documentation, and synchronization |
| [Changelog](CHANGELOG.md) | Timestamped update history |

For every subsequent update, reconcile this README's versions, capabilities, commands, limits, and verification dates with the owning code. Add a dated changelog entry and verify the changed files in both maintained locations by byte count and SHA-256.

## Attribution

Austin's Algorithm is ChronoRealm's project and example name. Its quantum search method builds on Lov K. Grover's published algorithm. Project credit belongs to Austin Gregory Simpkins and Chandler Middlebrooks / ChronoRealm. See the original paper and the circuit tutorial for background. [Grover, 1996](https://doi.org/10.1145/237814.237866) · [IBM Grover tutorial](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm)
