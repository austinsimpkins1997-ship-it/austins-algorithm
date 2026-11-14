# Timing and pruning studies

The original workload generator, model revision, seeds, decision definitions,
trial-independence record and raw traces were unavailable in the supplied
brief and the named project roots. The 99.4% result, 10 million timing
steps, 10 billion adversarial trials and 100 million modeled years remain
supplied claims. The new tests use explicit synthetic inputs.

## Timing horizon

The finite grid has 1,000 latency slots, five environmental drift values
and four clock uncertainty values. Each slot carries one possible
prerequisite, deadline change, fallback or hazard. The lower median base
latency is 118 ticks. The model reserves 45 ticks for interpretation,
execution and recovery before the commitment deadline of 460 ticks, then
reserves the declared clock uncertainty. Equality at the resulting latest
start qualifies. Each candidate policy only sees checks available by its
horizon and that latest start. The reference uses every check that can
arrive in time.

On November 13, 2026, four horizons each evaluated 20,000 input slots
125 times. That produced **10,000,000 evaluated policy steps**. Repeating
the grid measures workload. The 20,000 input slots are the denominator
for the finite model, with results below expressed on the repeated
2,500,000-step denominator per horizon. Independent finite enumeration
agreed with all counters in all 20 strata.

| Horizon | Matches complete choice | Feasible opportunities missed | Adverse states unseen | Mean wait, ticks |
|---:|---:|---:|---:|---:|
| 118 ticks, 1× | 47.615% | 646,875 | 662,750 | 108.0564 |
| 236 ticks, 2× | 91.975% | 99,375 | 101,250 | 150.4630 |
| 377 ticks, floor of 3.2× | 98.950% | 12,500 | 13,750 | 176.6483 |
| 590 ticks, 5× | 100% | 0 | 0 | 179.6598 |

The last attainable check in this grid arrives at tick 414. That is the
smallest whole-tick horizon giving full agreement across the declared
grid. The 3.2× policy leaves 501,500 synthetic utility points of excess
loss on its repeated denominator. Raising the horizon to 5× removes
that loss for a mean wait increase of 3.0115 ticks. The recorded window,
check mix and stakes determine that tradeoff.

The policy evaluations took 2.843 seconds and the finite reference took
48.935 ms on the measured Windows 11/Python 3.12.15 run. These timings
cover an aggregated Python model on one host. They provide no
device, long-duration, field or statistical calibration evidence.

## Pruning

The generator produces two held-out cohorts of 10,000 scenarios, with
eight candidate routes per scenario and separate, fixed random seeds.
Hard eligibility checks duration, permission and calibration. The
adversarial cohort adds rare favorable outcomes, severe downside,
high-reward infeasible routes and deadline equality cases. The
exhaustive policy evaluates exact weighted utility for all eligible
routes. The probability-only policy discards eligible routes below
10% success. The guarded policy retains protected routes and only skips
an unprotected route if its upper bound is strictly below the current
best exact utility. Strict comparison preserves ties.

| Cohort and policy | Utility evaluations | Decisions changed | Protected paths lost | p50, ms | p95, ms |
|---|---:|---:|---:|---:|---:|
| Ordinary, exhaustive | 43,070 | 0 | 0 | 16.550 | 22.585 |
| Ordinary, probability-only | 38,832 | 90 | 658 | 16.321 | 22.940 |
| Ordinary, guarded | 34,856 | 0 | 0 | 20.995 | 26.137 |
| Adversarial, exhaustive | 45,833 | 0 | 0 | 19.004 | 27.793 |
| Adversarial, probability-only | 38,071 | 2,064 | 4,545 | 20.444 | 27.303 |
| Adversarial, guarded | 38,521 | 0 | 0 | 18.861 | 25.703 |

Timings use seven paired runs with rotated order. The p95 is the largest
of seven measurements. The utility-evaluation count improved under
guarded pruning. Its bookkeeping cost made it slower on the ordinary
cohort, and the adversarial medians were close. Exhaustive ranking is
the simpler choice for cheap eight-route tables. The guarded policy's
benefit depends on the cost of each skipped exact evaluation and the
strength of the available bound. Its proof applies when each certified
upper bound covers the route's attainable utility.

## Reproduce

Run from the Future Reader folder. The full receipt includes the
parameters, source SHA-256, timestamps, per-stratum counters and
environment. Omit `--output` to print the JSON. Use a new output path
for each retained run.

```powershell
python -B tools/run_studies.py --output .build/synthetic-studies-final.json
python -B -m unittest discover -s tests -p test_studies.py -v
```

To reproduce the supplied research, the next useful dependency is the
original workload with pinned code, seeds, modeled time units, held-out
inputs and decision-level outputs. The supplied aggregate percentages
and trial counts alone do not establish that result.
