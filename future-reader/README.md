# Future Reader 0.1.2

**Parent Python distribution** 0.2.6a1 · **Workbench** 0.2.6-alpha · **Review date** November 15, 2026.

This side system provides finite route practice, source-aware evidence/time views, synthetic decision studies, local process recovery, checkpoint and release exercises, and explicit bindings to Austin's Algorithm and a caller-selected Beyond Infinity's Bounds ledger. The parent README maps its packaged interfaces and selected ChronoRealm relationships. [Whole-project overview](../README.md#side-systems-and-connection-boundaries)

The parent project's declared credits are Austin Gregory Simpkins and Chandler Middlebrooks / ChronoRealm. This documentation revision was edited by Codex, an AI assistant, on Austin's request. Per-change original authorship, review dates, editorial signatures and integrity hashes are recorded in this side system's changelog. Unknown historical implementers remain explicitly UNKNOWN. [Change records](CHANGELOG.md)

Open [Future Reader](future-reader.html) directly in a browser. Start with
the practice board. Choose the Shelf, preview the move, then confirm it.
Choose the Goal next. **Reverse** previews a restore that retains the
elapsed practice clock. **Check** explains what the finite model can prove.
Open **Advanced** to compare routes, deadlines, and decisions. **Reader**
opens the historical and reconstruction examples. **Handoff** exports the
record and offers each of the three original briefs for download.

In **Austin’s Algorithm Workbench 0.2.6-alpha**, select **Future Reader** in
the sidebar. The native view uses the Python practice controller and a
separate local SQLite record. Its **Live recovery check** terminates a real
disposable process and verifies that a replacement finishes its practice job.
Its **Checkpoint** control exports or verifies a
portable record. **Offline HTML** saves the browser companion.

## Run and verify locally

The optional controller wheel is `chronorealm-future-reader` 0.1.2. With the two delivered wheels in one directory, install the complete parent and controller workflow using `python -m pip install --no-index --find-links PATH-TO-WHEELS "chronorealm-austins-algorithm[future-reader]==0.2.6a1"`. Installed use needs no workspace source path. The parent wheel supplies the generated offline HTML, while the optional wheel supplies the Python controller. SDK compilation remains separately selectable.

From this directory, use Python 3.10 or newer. The core has no third-party
dependencies.

```powershell
python -B -m future_reader demo
python -B -m future_reader compare-wait --now 4 --check-returns 6
python -B -m future_reader telemetry-demo
python -B -m future_reader live-failover-demo
python -B -m future_reader case cern CERTIFY
python -B -m future_reader case cenomanian PINPOINT
python -B -m future_reader case archive CERTIFY
python -B -m future_reader decisions
python -B -m future_reader horizon --budget 4
python -B -m future_reader view as_known_then --as-of 2026-11-13T10:15:00Z
python -B -m future_reader release-demo
python -B -m unittest discover -s tests -v
python -B tools/build_companion.py --check
node --test tests/offline.test.cjs
node tests/browser_smoke.cjs
```

The browser smoke needs local Chrome and Node. Its disposable profile and
report are under `.build`. The companion opens without a server. It uses
local JSON for checkpoints and replay. Its classical implementation is
checked against fixtures produced by the Python controller.

### Austin and Beyond Infinity’s Bounds

From the bundled `future-reader` directory inside Austin's Algorithm, add
the parent's `src` directory for source integration.

```powershell
$env:PYTHONPATH = (Resolve-Path '../src').Path
python -B -m future_reader austin
python -B -m future_reader austin --compile
python -B -m austins_algorithm future-reader capabilities
```

The compile command uses optional Qiskit and the OpenQASM importer. The
bindings are described in [the controller guide](docs/CONTROLLER.md) and
[the BIB guide](docs/BIB_BINDING.md). The latter requires an explicit native
ledger and a reviewed BIB source path. It preserves the original ledger's
vocabulary and evidence status.

A standalone sibling checkout instead uses `../austins-algorithm/src`. Keep the path appropriate to the selected layout. The normal installed wheels use the registered Python packages.

For the local Python API start `python -B -m future_reader serve`. The
process prints a temporary bearer token and loopback URL. POST a JSON object
with `operation` to that URL. Keep the token in the local terminal. The API
exposes bundled practice data through PINPOINT, TRAVEL, REVERSE, CERTIFY,
COMMIT, CONTEXT, CHECKPOINT and WINDOW. TRAVEL and REVERSE return a preview.
Supply that ID to COMMIT to confirm. The same input objects work with
`python -m future_reader operate @request.json`. All API actions are
local simulation.

## Evidence and operating boundaries

- [Requirement map](docs/REQUIREMENTS.md)
- [Evidence and reconstruction](docs/evidence.md)
- [Telemetry and fenced recovery](docs/TELEMETRY.md)
- [Live local workers](docs/LIVE_WORKER.md)
- [Timing and pruning studies](docs/STUDIES.md)
- [Exact refinement: assumptions, measured results, and replay](docs/EXACT_STUDIES.md)
- [Local release and rollback](docs/RELEASE.md)
- [Changelog](CHANGELOG.md)

The exact studies compare whole winner sets and safe decision ticks against
an independent reference. Run them locally with
`python -B tools/run_exact_studies.py --output .build/exact-study.json`
(create `.build` first if it is missing). This research path is separate from
the existing practice board and Workbench view.

The implementation supports a four-node practice graph, deterministic
timing, scenario-based decision analysis, and the bundled public evidence
cases. The exact minimum and timing certificates apply to their stated
models. Cross-machine failover, physical quantum experiments, and
external releases require separately verified integrations. The supplied
large studies remain provisional pending their original inputs and logs.

## Current verification and record signature

The November 15 delivery completed 74 companion tests under Python 3.12.15, with 73 passing and one skipped. The clean parent-plus-controller installation verified 140 runtime files, including all 20 companion runtime files. It exercised native practice and local-worker recovery through the hidden Workbench path. The four offline browser tests and generated companion consistency check passed in the earlier merged-source verification. Their dates and scopes remain separate in the parent records.

The delivered source includes bounded body draining before early loopback denial responses, the actual-parent worker identity, Windows extended-path SQLite reopening and an isolated child-interpreter import root. Access, stale-generation, preview/commit and restore checks retain their owning contracts. Human benefits, physical devices and live external failover need separate evidence.

Documentation record date is `2026-11-15T16:07:39-05:00` in America/New_York. Record editor is **Codex (AI assistant)**. Editorial signature is `Codex / 2026-11-15T16:07:39-05:00 / Future Reader README review`. Document byte integrity is recorded in the parent's local documentation verification receipt. Artifact-embedded documentation follows its recorded build revision.
