# Austin's Algorithm Workbench 0.3.0-alpha

**Python package 0.3.0a1 · Windows Workbench 0.3.0-alpha · Apache-2.0**

Build a small plan, run it within a declared budget, understand the result, and export the evidence. This alpha adds a larger classical planning workflow alongside the existing quantum-search experiments and Decision Review tools.

## What's new

- **Practical Planning:** 1–128 Boolean choices, integer costs and up to 4,096 CNF clauses. Budgeted branch-and-bound checks each returned plan independently and distinguishes feasible answers, established finite-model optima and exhausted budgets. Optional SciPy MILP keeps numerical solver claims separate.
- **Managed jobs:** persistent attempts, isolated workers, Start/Stop, visible failures, source-pinned checkpoints and explicit supported recovery/resume. Planning, weighted simulation and named offline IBM snapshot jobs use these controls. Specialized calibration studies retain their own continuation APIs.
- **Readable results:** a shared overview for Python, CLI, desktop and exports; project save/load; a local rule-based assistant; readable HTML reports, recorded inputs, results, checkpoints, worker logs, integrity checks and fresh replay code.
- **Integration and evaluation:** versioned planning contracts, legacy weighted-input compatibility, synthetic matched benchmarks and an operator-pilot recorder/worksheet. No human benefit is claimed from synthetic timings.
- **Release checks:** source-archive-to-wheel verification, hidden executable smoke checks and checksummed downloads. Existing quantum teaching limits, Decision Review holds and provider permissions are preserved.

## Download and start

- **Windows:** download `AustinsAlgorithmWorkbench.exe` and run it. Or extract `AustinsAlgorithmWorkbench-0.3.0-alpha-windows.zip` for the executable, guides, licenses and example. Select **Planning**, load the equipment example and start with its declared budget. The executable is **Windows publisher-unsigned**. Respect your system's security policy; this release does not establish Windows publisher trust.
- **Python:** download the main wheel and optional Future Reader wheel into one directory, then run:

  ```text
  python -m pip install --no-index --find-links . "chronorealm-austins-algorithm[future-reader]==0.3.0a1"
  python -m austins_algorithm workbench
  python -m austins_algorithm plan solve examples/planning_equipment.json --nodes 10000 --seconds 5
  ```

  The CLI example uses `examples/planning_equipment.json` from the source archive or extracted Windows bundle. Python GUI use requires Tk/Tcl. Optional numerical and quantum SDKs require their separately documented extras.

- **Verify downloads:** compare SHA-256 against `SHA256SUMS.txt`; on Windows use `Get-FileHash FILE -Algorithm SHA256`. Checksums establish byte agreement, not publisher identity. `release-evidence.json` records the hosted commit, artifact identities and build checks.

## Verification and limits

Before publication, each maintained local source copy recorded **736 tests: 602 passed, 134 skipped**. Six packaged Manila replay/verification commands passed, including **8/8 placement histograms**. The locally installed ChronoRealm update passed manifest, hidden native workflow and disposable actual-file-delta rollback checks. The local installed release was Ed25519 integrity-signed; that signature does **not** sign or authenticate these separately built hosted downloads.

The GitHub release workflow builds from the identified source commit after the `tests` workflow succeeds, checks its Python distributions, runs available package tests, rebuilds the executable and exercises its hidden project/export/worker workflows. Hosted checks and environment are recorded separately from local evidence. Skipped tests remain skipped evidence.

This is an **alpha prerelease**, not a production certification. A 128-choice input bound does not guarantee arbitrary problems finish within a budget. Physical-QPU performance, practical quantum advantage, real operator time savings, representative usability/accessibility and organizational production acceptance remain unverified. No cloud account or QPU is required for the offline paths. Review replay code before running it; integrity verification does not make supplied code trustworthy.

Project: Austin Gregory Simpkins and Chandler Middlebrooks / ChronoRealm. Quantum search builds on Lov K. Grover's established algorithm.

Feedback is welcome: share what you tried, the expected and actual result, and the smallest non-sensitive example that reproduces a problem in [GitHub Issues](https://github.com/austinsimpkins1997-ship-it/austins-algorithm/issues).

Release preparation: 2026-11-16, America/New_York. Editor and editorial signature: **Codex (AI assistant) / 2026-11-16 / 0.3 alpha release preparation**. Hosted creation time and commit are recorded by GitHub; this editorial signature is not cryptographic identity proof.
