# Practical Planning — 0.3 alpha

Planning is a classical optimizer for 1–128 Boolean choices with nonnegative integer costs and up to 4,096 mandatory CNF clauses. It is separate from the eight-variable weighted quantum teaching workflow and its oracle audits. Existing weighted projects remain supported. Names and costs are in q0-first order; printed bitstrings remain MSB-first. A positive literal selects a choice; a negative literal excludes it. At least one literal in every clause must be true. An empty clause makes the model infeasible; an empty list of clauses imposes no rules.

## First session

Open **Planning** in the Workbench. The synthetic 24-choice equipment example is ready to edit. Review its choices, integer costs and rules, select a node/time budget, then **Start**. The page shows what happened, which choices were selected, the independently calculated cost, verification, optimality and the next step. The local Assistant explains these controls using rules; it does not contact an AI service.

**Stop** requests a checkpoint. **Resume** continues a trusted local branch-and-bound checkpoint with unchanged inputs and a newly declared per-attempt budget. **Recover** releases the recorded active state only when no worker holds the job lock; it does not kill another application's process. A crashed worker becomes interrupted when its owning manager polls it. An interrupted record found after restarting the application requires explicit recovery. **Open saved job** never starts it automatically.

The job store allows at most two active jobs, 1,000 stored jobs and 64 explicitly started attempts per job. Per-attempt limits are 1–1,000,000 search nodes and 0.01–300 seconds. Worker polling enforces a wall-time grace of ten seconds; a polled stop request gets two seconds to finish cooperatively before the manager terminates its own worker tree. Closing the Workbench closes its owned planning workers. A detached CLI start needs a later status/recovery action if the worker crashes. These are local controls, not hard real-time guarantees or an OS memory sandbox. The branch-and-bound frontier is bounded by model width; optional native solvers have their own memory behavior.

**Save project** records versioned inputs, backend and budgets. Open accepts this project format or migrates a legacy weighted problem explicitly. The job list retains checkpoint and attempt history separately. **Export all** writes the project, full committed job/checkpoint, attempt history, worker logs, shared overview, hash manifest and a fresh `replay.py`. It refuses an existing destination or an active job. Its integrity key is never exported. Export verification checks bytes and the candidate; independent optimality requires a fresh replay. Exported source code is not executed automatically. Explicitly delete local jobs with the UI or CLI after exporting anything needed.

## Python and CLI use the same contracts

```python
from austins_algorithm.planning_api import run_request

result = run_request({
    "schema_version": "chronorealm.planning/1",
    "problem": {"name": "Coverage", "variables": [
        {"name": "Existing kit", "cost": 2},
        {"name": "Replacement kit", "cost": 8}], "clauses": [[1, 2]]},
    "budget": {"nodes": 1000, "seconds": 5},
    "backend": "branch-bound",
})
print(result["overview"])
```

```powershell
python -m austins_algorithm plan solve examples/planning_equipment.json --nodes 10000 --seconds 5
python -m austins_algorithm plan --human solve examples/planning_equipment.json --nodes 10000 --seconds 5
python -m austins_algorithm plan create examples/planning_equipment.json --store PATH-TO-PRIVATE-JOBS --nodes 1
python -m austins_algorithm plan start JOB_ID --store PATH-TO-PRIVATE-JOBS --wait
python -m austins_algorithm plan resume JOB_ID --store PATH-TO-PRIVATE-JOBS --wait
python -m austins_algorithm plan export JOB_ID review.zip --store PATH-TO-PRIVATE-JOBS
python -m austins_algorithm plan verify-export review.zip
```

Node/time budgets are per attempt, not free work: attempt wall time and accumulated solver time remain in the record. Resume preserves total node accounting. A resumed CLI job uses its saved budget; use the Python `start(..., resume=True, budget=...)` API or desktop budget controls to change that explicit budget.

The v1 API lives in `planning_api.py`: `capabilities()`, `problem_schema()`, and `run_request()`. The schema is a portable structural reference; the Python validator additionally checks distinct names and literal indices against the actual variable count. Unknown schema versions, invalid fields, duplicate JSON keys and non-finite budgets are rejected. The general quantum adapter interface remains separately versioned. `IBMSnapshotAdapter` now exposes the built-in Austin circuit on one named offline Manila, Jakarta or Torino model through that existing interface. It remains synchronous simulation, does not accept arbitrary circuits, and never connects an account or submits hardware work.

## What a result establishes

- **OPTIMAL / EXACT_FINITE_MODEL:** the portable integer branch-and-bound search finished its finite model under the recorded rules, with a separately checked witness.
- **INFEASIBLE / EXACT_FINITE_MODEL:** the complete finite search found no feasible assignment. This says nothing about omitted real-world choices or assumptions.
- **FEASIBLE / NOT_PROVED:** the witness satisfies every rule and its integer cost was checked, but the search did not establish optimality.
- **UNDETERMINED:** no independently verified answer is available. A budget stop is not evidence of infeasibility.
- **SOLVER_REPORTED:** optional SciPy numerical MILP reports completion; its rounded binary witness is independently checked. This is kept distinct from the portable solver's exact finite-search result. Numerical infeasibility is not promoted into an exact proof.

Local job records use an HMAC key outside the installed payload. This detects changes relative to that local key; it is not user identity, tenant isolation, permission, consent or Windows publisher trust. Imported standalone checkpoints cannot establish exact optimality. Engine source pins hold resume after a source change; start a new job while preserving the earlier record. Nothing in an optimized plan authorizes a real-world action or clears a Decision Review hold.

The optional backend uses `pip install ".[planning]"` and `--backend scipy-milp`. The portable EXE deliberately keeps the dependency-free backend. Installing an optional package does not expand simulation memory or oracle limits.

## Matched benchmark and operator pilot

Run `python tools/benchmark_planning.py NEW-RESULT-DIRECTORY`. It records input checks, solving, independent validation, existing exhaustive references at small widths, analytical references for structured larger cases, and the managed setup/pause/resume/export path. Report all timings and negative results. Do not use a single synthetic run as a general speedup or operator-benefit claim.

For a real operator comparison, use the same versioned problem and mandatory checks in both arms: the current workflow and Planning. Counterbalance the order, record familiarity, and retain interruptions and failures. Use several matched sessions in the person's available time; no predetermined percentage improvement is assumed. Record active seconds for setup, input checks, evidence refresh, solve, verification, retries and recovery. Keep elapsed machine time separate. Stop on changed authority, missing mandatory evidence, data exposure, incomplete output, or unacceptable burden.

`tools/planning_operator_pilot.py` records caller-reported observations and compares only pairs with the same actor label, pair ID, input digest, required checks and verified outputs. Missing arms and failed checks remain held. Reports are descriptive and do not establish causal benefit. The project contains no fabricated operator observations. The existing Decision Review receipt pilot remains available; this recorder can measure its baseline and upgraded review arms with explicit check names.

An observation JSON requires `operator`, `pair_id`, `arm` (`baseline` or `planning`), `project_sha256`, `seconds` (all seven named stages), `required_checks`, `completed_checks`, and `output_verified`. Run `python tools/planning_operator_pilot.py record pilot.json --observation observation.json`; then `summarize pilot.json`. Keep private labels and records outside the shipped payload. The printable worksheet is below.

## Printable session worksheet

Operator label: __________ Pair ID: __________ Arm/order: __________ Familiarity: __________

Outcome and pinned input/version: __________________________________________

Mandatory checks and evidence freshness: __________________________________

| Active-time stage | Seconds | Evidence / reason |
|---|---:|---|
| Setup | | |
| Input checks | | |
| Evidence refresh | | |
| Solve | | |
| Verification | | |
| Retries | | |
| Recovery | | |

Actual output and independent check: ______________________________________

Interruptions, missing evidence, unresolved assumptions: ___________________

Keep / revise / stop, and next observation: ________________________________
