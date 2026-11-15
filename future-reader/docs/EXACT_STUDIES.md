# Exact refinement of the synthetic studies

**Future Reader 0.1.2 · 2026-11-13 · local classical simulation.** The exact
matching targets are the complete ordered winner set and, for adaptive
timing, the earliest integer tick at which that set cannot change. 100% is
the ceiling for agreement. Improvement is measured separately in coverage,
waiting, and work. No decision is graded against a future event that arrives
after its safe cutoff.

| Study | Observed reference agreement | Improvement |
| --- | --- | --- |
| Original one-check grid | 20,000/20,000 at 414 ticks; five mismatches at 413 | Fixed 414 replaces 590 as the smallest exact uniform cap. Using the declared stratum caps, mean wait drops from 179.660 to 179.519 ticks. |
| Multi-check timing | 32,976/32,976 for both final winner set and earliest safe tick | Advances the decision in 15,663 cases. Total wait drops from 154,637 to 80,414 ticks (48.0%) versus waiting for all pending checks or cutoff. |
| Signed binary ranking | 2,304/2,304 scenario variants per policy; no eligible protected routes skipped | Exact evaluations fall from 337,449 to 167,326 for guarded and 138,631 for bound-first (50.4% and 58.9%). |

## Original grid and its exact boundary

[The original study](STUDIES.md) repeats 20,000 input slots (3,476 distinct
event/arrival/cutoff tuples) 125 times for each of four horizons. The ten
million executions are a workload count. The new certificate checks seven
policies per original slot with multiplicities preserved; it uses explicit
reveal-before-commit order and the legacy policy as cross-checks. Every
reachable observation in this grid can change the action. The largest
reachable arrival is 414; the five affected tick-414 cases prove any
smaller cap insufficient. If the stratum is known in advance, clock margins
of 0, 10, 20 and 40 admit tight caps of 414, 404, 394 and 374 ticks,
respectively. The 3.2× baseline still matches 19,790/20,000.

## Earliest safe commitment

Each synthetic check has a declared inclusive window, independent alternative
outcomes, and the option of absence. It can deliver at most once. An outcome
adds integer scores and can permanently veto a route; fixed ineligibility
cannot be removed. An arrival stamped at the cutoff counts if delivered in
the atomic batch before commitment. Closing a window requires the caller to
have delivered all observations through that tick. Untrusted, unannounced,
late, incomplete, or correlated inputs need a reviewed model before this
certificate can be used.

The current winner set is the outcome if every unresolved check is absent.
It is safe to commit exactly when no possible check can remove a current
winner, every current tie stays tied, and every other surviving route stays
strictly below a current winner. For a competitor that survives, the minimum
pairwise gap is the sum of the per-check minima plus the observed gap:
each check may independently contribute its worst feasible delta or zero.
Tests of a current tie also use the maximum gap. Because those extremal
choices can occur jointly in the declared model, the test is necessary as
well as sufficient. It reads no future outcomes. Losing equality matters:
a new tied winner changes the answer.

The separately implemented reference lists admissible completions at each
integer tick and evaluates whole winner sets directly. Verification covered
8,400 exhaustive small traces and 24,576 generated cases across three
fixed seeds, including common shifts, hazards, missing checks, and mixed
outcomes. It examined 434,686 completions before confirming invariance or
finding counterexamples at earlier ticks. None of the runs establishes
real-world timing or the correctness of input assumptions.

## Exact ranking and measured cost

All policies apply deadline, permission and calibration eligibility before
ranking. In a binary model with probability in [0, 1], the exact weighted
score cannot exceed the larger of its two outcome values, including when
the nominal failure outcome is higher. A candidate can be skipped only when
its upper bound is *strictly below* an already verified score and it is not
protected. Ties remain visible. The independent rational reference checks
the score, winners, eligible count, and eligible protected membership. The
model ranks eligible negative scores too; include an explicit no-action
route if it should compete.

The study has 72 cells (8, 32, 128 and 1,024 routes; three bound slack
levels; three generated cohorts; inclusive and exclusive deadline rules).
It rules out a universal gain: for the held-out 1,024-route inclusive cases,
tight bounds cut 16,456 exact evaluations to 1,741, but slack 200 cuts none.
The local call benchmark includes input validation, policy decisions, sorting
and construction of the auditable result. In its 1,024-route tight-bound
cell the p50 was 1.870 ms exhaustive, 1.899 ms guarded and 1.965 ms
bound-first (32 samples per policy). Exhaustive had a lower median than
bound-first in all 12 benchmark cells. Counts alone do not establish a
runtime speedup. Keep exhaustive as the baseline for cheap scoring.

## Reproduce and verify

Run from the Future Reader package. Create its local `.build` directory
first if necessary. These commands write only new report files.

```powershell
python -B tools/run_exact_studies.py --cases-per-seed 8192 --ranking-trials 32 --output .build/exact-refinement-0.1.2-final.json
python -B tools/run_exact_studies.py --verify .build/exact-refinement-0.1.2-final.json
python -B tools/benchmark_exact_ranking.py --output .build/exact-ranking-timing-0.1.2.json
```

The exact receipt stores the source hashes, environment, inputs and result
digests, and deterministic counts. The verifier checks the bytes of the
implementation and recomputes the studies; its reporting metadata is
separate from the deterministic content fingerprint:

```text
f183a05776d24fe28a0fd944b93cbfaaa74de345853c71dcc1dbf5e25764c28a
```

Source references are the [grid certificate](../future_reader/exact_grid.py),
[online timing policy](../future_reader/exact_timing.py),
[enumerating timing reference](../future_reader/exact_timing_study.py), and
[ranker and rational reference](../future_reader/exact_pruning.py). This
is an additive research package release. The existing Workbench and
practice controller keep their prior behavior. The earlier supplied large
workloads remain unreproduced until their original inputs and logs are
available. No physical hardware or live production input was used.
