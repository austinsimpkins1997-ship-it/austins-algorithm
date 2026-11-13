"""Bounded synthetic checks of timing horizons and route pruning."""

from fractions import Fraction
from itertools import product
from random import Random
from statistics import median, median_low
from time import perf_counter_ns


STUDY_SCHEMA = "future-reader.synthetic-studies/1"
TIMING_MODEL = "future-reader.horizon-grid/1"
PRUNING_MODEL = "future-reader.pruning-holdout/1"
DRIFT = (-20, 0, 20, 40, 60)
CLOCK_UNCERTAINTY = (0, 10, 20, 40)
EVENTS = ("PREREQUISITE", "DEADLINE", "FALLBACK", "HAZARD")
METRICS = ("steps", "matched_full_choice", "missed_feasible_opportunity",
           "consequential_state_not_seen", "excess_loss", "observed_checks", "waiting_ticks")
LATENCIES = tuple(60 + index % 90 if index < 750 else 160 + 2 * (index - 750) for index in range(1_000))
MEDIAN_LATENCY = median_low(LATENCIES)
HORIZON_RATIOS = (Fraction(1), Fraction(2), Fraction(16, 5), Fraction(5))


def _latency_grid():
    """One stratum for each independent variation of environmental drift and clock uncertainty."""
    for drift, clock in product(DRIFT, CLOCK_UNCERTAINTY):
        yield drift, clock, [
            (index % len(EVENTS), max(0, latency + drift), 460 - 45 - clock)
            for index, latency in enumerate(LATENCIES)
        ]


def _full_choice(kind, due, latest):
    # Use this independent table only for the analytical reference.
    if due > latest:
        return (0, 1, 0, 1)[kind]
    return (2, 0, 3, 0)[kind]


def _step(kind, due, latest, cap):
    seen = due <= cap and due <= latest
    full = due <= latest
    if kind in (0, 2):
        # If this prerequisite or fallback opens in time, the complete decision uses it.
        matched = seen or not full
        return matched, not matched, False, (9 if kind == 0 else 5) if not matched else 0, seen, min(due, cap, latest)
    # A deadline change or hazard can force a fallback. Record the missed adverse state.
    matched = seen or not full
    return matched, False, not matched, (11 if kind == 1 else 50) if not matched else 0, seen, min(due, cap, latest)


def _reference(rows, horizons):
    """Enumerate the exact finite support by explicit choices and resulting utility."""
    preferred = (2, 0, 3, 0)
    default = (0, 1, 0, 1)
    losses = (9, 11, 5, 50)
    expected = [[0] * len(METRICS) for _ in horizons]
    for kind, due, latest in rows:
        complete_choice = _full_choice(kind, due, latest)
        for index, cap in enumerate(horizons):
            observation_tick = min(due, cap, latest)
            known = observation_tick == due
            policy_choice = preferred[kind] if known else default[kind]
            changed = complete_choice != policy_choice
            values = (1, int(not changed), int(changed and complete_choice in (2, 3)),
                      int(changed and complete_choice == 0), losses[kind] if changed else 0,
                      int(known), observation_tick)
            for metric, value in enumerate(values):
                expected[index][metric] += value
    return expected


def _timing(repetitions):
    if type(repetitions) is not int or not 1 <= repetitions <= 500:
        raise ValueError("Timing repetitions must be a whole number from 1 through 500.")
    horizons = tuple(int(MEDIAN_LATENCY * ratio) for ratio in HORIZON_RATIOS)
    strata = []
    start = perf_counter_ns()
    for drift, clock, rows in _latency_grid():
        observed = []
        for horizon in horizons:
            matched = missed = lost = regret = checks = waiting = 0
            for _ in range(repetitions):
                for kind, due, latest in rows:
                    same, absent, consequence, loss, seen, elapsed = _step(kind, due, latest, horizon)
                    matched += same
                    missed += absent
                    lost += consequence
                    regret += loss
                    checks += seen
                    waiting += elapsed
            observed.append([len(rows) * repetitions, matched, missed, lost, regret, checks, waiting])
        strata.append((drift, clock, rows, observed))
    elapsed = perf_counter_ns() - start
    analytic_start = perf_counter_ns()
    totals = [[0] * len(METRICS) for _ in horizons]
    grid = []
    smallest_exact_horizon = 0
    for drift, clock, rows, observed in strata:
        smallest_exact_horizon = max(smallest_exact_horizon, max(due for _, due, latest in rows if due <= latest))
        reference = _reference(rows, horizons)
        exact = [[value * repetitions for value in row] for row in reference]
        if exact != observed:
            raise RuntimeError("Timing workload differs from the independent finite reference.")
        for index, values in enumerate(observed):
            for metric, count in enumerate(values):
                totals[index][metric] += count
        grid.append({"drift_ticks": drift, "clock_uncertainty_ticks": clock,
                     "horizons": [dict(zip(METRICS, values)) for values in reference]})
    analytic_elapsed = perf_counter_ns() - analytic_start
    summaries = []
    for cap, ratio, counts in zip(horizons, HORIZON_RATIOS, totals):
        result = dict(zip(METRICS, counts))
        result.update({"horizon_ticks": cap, "median_multiplier": str(ratio),
                       "matched_full_choice_fraction": result["matched_full_choice"] / result["steps"],
                       "mean_waiting_ticks": result["waiting_ticks"] / result["steps"]})
        summaries.append(result)
    return {
        "model": TIMING_MODEL,
        "sample_kind": "DETERMINISTIC_STRATIFIED_GRID",
        "distinct_input_slots": len(DRIFT) * len(CLOCK_UNCERTAINTY) * len(LATENCIES),
        "independent_trial_count": "NOT_APPLICABLE",
        "repeat_count": repetitions,
        "steps_per_horizon": totals[0][0],
        "total_policy_evaluations": totals[0][0] * len(horizons),
        "configured_grid": {"latency_slots": len(LATENCIES), "base_median_lower_ticks": MEDIAN_LATENCY,
                            "drift_ticks": list(DRIFT), "clock_uncertainty_ticks": list(CLOCK_UNCERTAINTY),
                            "check_kinds": list(EVENTS), "commit_deadline_ticks": 460,
                            "reserve_for_interpretation_execution_and_recovery_ticks": 45,
                            "equality_eligible": True, "horizon_rounding": "floor to whole ticks"},
        "smallest_horizon_with_full_grid_agreement_ticks": smallest_exact_horizon,
        "matching_definition": "Decision equals the finite reference that knows each check arriving by the last safe commitment.",
        "late_censoring": "Checks after the last safe commitment are unavailable to the reference and all policies.",
        "horizons": summaries, "grid_one_pass": grid, "analytic_match": True,
        "analytical_evaluation_wall_ms": round(analytic_elapsed / 1e6, 6),
        "policy_evaluation_wall_ms": round(elapsed / 1e6, 6),
        "limits": [
            "The same finite input grid is repeated for workload measurement. Ratios describe this grid.",
            "Tick units and utility points are synthetic and do not represent wall time or calibrated forecasts.",
            "Each case varies drift and clock uncertainty separately and includes one consequential check kind.",
        ],
    }


def _candidate(duration, permission, calibrated, chance, reward, loss, *, protected=False):
    eligible = duration <= 20 and permission and calibrated
    return (eligible, protected or loss <= -30, chance, reward, loss, reward)


def _candidates(random, index, adversarial):
    """Held-out candidate tuples: eligible, protected, success basis points, reward, loss, upper bound."""
    rows = []
    for _ in range(8):
        duration = random.randrange(1, 31)
        permission = random.randrange(10) > 0
        calibrated = random.randrange(10) > 0
        chance = random.randrange(1, 10_001)
        reward = random.randrange(1, 50)
        loss = -random.randrange(1, 35)
        rows.append(_candidate(duration, permission, calibrated, chance, reward, loss))
    if adversarial:
        # Rotate independent eligibility edges and low-frequency/high-consequence traps.
        trap = index % 5
        if trap == 0:
            rows[7] = _candidate(20, True, True, 150, 5_000, -3, protected=True)
        elif trap == 1:
            rows[7] = _candidate(20, True, True, 60, 8, -1_000)
        elif trap == 2:
            rows[7] = _candidate(21, True, True, 10_000, 500, -500)
        elif trap == 3:
            rows[7] = _candidate(20, True, True, 9_980, 22, -1_000)
        else:
            rows[7] = _candidate(20, True, True, 10_000, 25, -1)
    return rows


def _utility(candidate):
    _, _, chance, reward, loss, _ = candidate
    return chance * reward + (10_000 - chance) * loss


def _ranking(rows, policy):
    """Resolve hard constraints before inspecting optional weighted routes."""
    best = None
    winners = []
    lost = protected_lost = evaluations = 0
    for index, row in enumerate(rows):
        eligible, protected, chance, _, _, upper_bound = row
        if not eligible:
            continue
        if policy == "probability_only" and chance < 1_000:
            lost += 1
            protected_lost += protected
            continue
        if policy == "guarded" and not protected and best is not None and upper_bound * 10_000 < best:
            lost += 1
            continue
        score = _utility(row)
        evaluations += 1
        if best is None or score > best:
            best, winners = score, [index]
        elif score == best:
            winners.append(index)
    return best, winners, lost, protected_lost, evaluations


def _pruning(seed, trials):
    if type(seed) is not int or not 0 <= seed < 2**64 or type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("Use an unsigned 64-bit seed and 1–100,000 pruning trials.")
    policies = ("full", "probability_only", "guarded")
    result = []
    for cohort, stream_seed in (("ordinary", seed), ("adversarial", seed ^ 0xD6E8FEB86659FD93)):
        random = Random(stream_seed)
        workload = [_candidates(random, index, cohort == "adversarial") for index in range(trials)]
        times = {policy: [] for policy in policies}
        results_by_policy = {}
        for repeat in range(7):
            offset = repeat % len(policies)
            for policy in policies[offset:] + policies[:offset]:
                start = perf_counter_ns()
                evaluated = [_ranking(rows, policy) for rows in workload]
                times[policy].append(perf_counter_ns() - start)
                if repeat == 0:
                    results_by_policy[policy] = evaluated
                elif evaluated != results_by_policy[policy]:
                    raise RuntimeError("Repeated pruning benchmark changed the decision records.")
        exhaustive = results_by_policy["full"]
        for policy in policies:
            evaluated = results_by_policy[policy]
            decision_errors = sum(result[:2] != baseline[:2] for result, baseline in zip(evaluated, exhaustive))
            protected_losses = sum(row[3] for row in evaluated)
            if policy == "guarded" and (decision_errors or protected_losses):
                raise RuntimeError("Guarded pruning changed a baseline decision or lost a protected path.")
            result.append({
                "cohort": cohort, "seed": stream_seed, "policy": policy, "trials": trials,
                "route_utility_evaluations": sum(row[4] for row in evaluated),
                "feasible_paths_pruned": sum(row[2] for row in evaluated),
                "protected_paths_lost": protected_losses, "altered_decisions": decision_errors,
                "timing_repeats": len(times[policy]),
                "p50_wall_ms": round(median(times[policy]) / 1e6, 6),
                "p95_wall_ms": round(max(times[policy]) / 1e6, 6),
            })
    return {"model": PRUNING_MODEL, "sample_kind": "HELD_OUT_SYNTHETIC_SCENARIOS",
            "trials_per_cohort": trials, "candidates_per_trial": 8,
            "rule": "An unprotected feasible path can be deferred only if its upper reward bound is strictly below the best exact utility already evaluated. Equality stays eligible for ties.",
            "adversarial_cases": ["Rare large reward", "Low success with severe downside",
                                  "Ineligible high reward", "Low-frequency severe downside",
                                  "Exact score and deadline boundaries"],
            "input_limits": "Small synthetic utility tables and hard feasibility flags. Guarded ranking proof assumes its declared utility upper bounds are valid.",
            "measurement": "Repeated paired timings rotate policy order. Workload generation is outside timing. Nearest-rank p95 is the maximum of seven observations.",
            "rng": "Python random.Random MT19937 integer draws. Reproduce with the recorded interpreter and seeds.",
            "results": result}


def run_study(*, repetitions=125, seed=20261113, pruning_trials=10_000):
    """Evaluate the named synthetic workloads and retain the supplied runs separately."""
    return {
        "schema": STUDY_SCHEMA,
        "status": "VERIFIED_NEW_SYNTHETIC_WORKLOAD",
        "timing": _timing(repetitions),
        "pruning": _pruning(seed, pruning_trials),
        "supplied": {
            "status": "UNRESOLVED",
            "timing_steps": 10_000_000,
            "claimed_match_percent": "99.4",
            "claimed_multiplier": "3.2",
            "modeled_years": 100_000_000,
            "claimed_adversarial_trials": 10_000_000_000,
            "missing": ["Original workload and generator", "Model and source revision", "Seed and random stream",
                        "Decision and convergence definitions", "Trial denominator, dependence and stopping rules",
                        "Raw result logs and calibration inputs"],
        },
    }
