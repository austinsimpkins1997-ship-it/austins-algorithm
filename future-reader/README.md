# Future Reader

Open [Future Reader](future-reader.html) directly in a browser. Start with
the practice board. Choose the Shelf, preview the move, then confirm it.
Choose the Goal next. **Reverse** previews a restore that retains the
elapsed practice clock. **Check** explains what the finite model can prove.
Open **Advanced** to compare routes, deadlines, and decisions. **Reader**
opens the historical and reconstruction examples. **Handoff** exports the
record and offers each of the three original briefs for download.

In **Austin’s Algorithm Workbench 0.2.3-alpha**, select **Future Reader** in
the sidebar. The native view uses the Python practice controller and a
separate local SQLite record. Its **Live recovery check** terminates a real
disposable process and verifies that a replacement finishes its practice job.
Its **Checkpoint** control exports or verifies a
portable record. **Offline HTML** saves the browser companion.

## Run and verify locally

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

In the workspace or matching E: sibling layout, add the Austin package to
the Python path for the source integration.

```powershell
$env:PYTHONPATH = (Resolve-Path '../austins-algorithm/src').Path
python -B -m future_reader austin
python -B -m future_reader austin --compile
python -B -m austins_algorithm future-reader capabilities
```

The compile command uses optional Qiskit and the OpenQASM importer. The
bindings are described in [the controller guide](docs/CONTROLLER.md) and
[the BIB guide](docs/BIB_BINDING.md). The latter requires an explicit native
ledger and a reviewed BIB source path. It preserves the original ledger's
vocabulary and evidence status.

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
- [Local release and rollback](docs/RELEASE.md)
- [Changelog](CHANGELOG.md)

The implementation supports a four-node practice graph, deterministic
timing, scenario-based decision analysis, and the bundled public evidence
cases. The exact minimum and timing certificates apply to their stated
models. Cross-machine failover, physical quantum experiments, and
external releases require separately verified integrations. The supplied
large studies remain provisional pending their original inputs and logs.
