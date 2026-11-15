"""Reproducible sufficiency and minimality certificate for the original timing grid."""

from collections import Counter

from .codec import canonical, digest
from .studies import EVENTS, HORIZON_RATIOS, MEDIAN_LATENCY, METRICS, TIMING_MODEL, _latency_grid, _step


SCHEMA = "future-reader.exact-grid/1"
ORIGINAL_REPETITIONS = 125
_RULES = (
    ("WAIT", "USE_PREREQUISITE", 9),
    ("COMMIT", "WAIT", 11),
    ("WAIT", "USE_FALLBACK", 5),
    ("COMMIT", "WAIT", 50),
)


def _event_choice(kind, due, latest, cap=None):
    """Replay ordered reveal, horizon and deadline events, with reveal first at a tie."""
    if type(kind) is not int or not 0 <= kind < len(_RULES):
        raise ValueError("Unknown check kind.")
    if any(type(tick) is not int or tick < 0 for tick in (due, latest)):
        raise ValueError("Use nonnegative whole ticks for arrival and commitment.")
    if cap is not None and (type(cap) is not int or cap < 0):
        raise ValueError("Use a nonnegative whole-tick horizon.")
    default, revealed, _ = _RULES[kind]
    events = [(due, 0, "REVEAL"), (latest, 2, "COMMIT_DEADLINE")]
    if cap is not None:
        events.append((cap, 1, "HORIZON_CLOSE"))
    tick, _, event = min(events)
    if event == "REVEAL":
        return revealed, True, tick
    return default, False, tick


def _direct_choice(kind, due, latest, cap):
    default, revealed, _ = _RULES[kind]
    cutoff = min(cap, latest)
    if due <= cutoff:
        return revealed, True, due
    return default, False, cutoff


def _row_metrics(row, selected, reference):
    kind = row[0]
    action, seen, waiting = selected
    changed = action != reference[0]
    observed = (not changed, changed and reference[0] in ("USE_PREREQUISITE", "USE_FALLBACK"),
                changed and reference[0] == "WAIT", _RULES[kind][2] if changed else 0, seen, waiting)
    return (1, *observed)


def _latest_witnesses(drift, clock, rows, cap):
    if cap == 0:
        return []
    result = []
    for index, row in enumerate(rows):
        kind, due, latest = row
        if due != cap:
            continue
        full = _event_choice(*row)
        previous = _event_choice(*row, cap - 1)
        if full[0] != previous[0]:
            result.append({"drift_ticks": drift, "clock_uncertainty_ticks": clock, "slot": index,
                           "check": EVENTS[kind], "arrival_tick": due, "latest_commit_tick": latest,
                           "previous_cap_ticks": cap - 1, "previous_choice": previous[0],
                           "reference_choice": full[0]})
    return result


def _counts(values):
    metrics = dict(zip(METRICS, values))
    steps = metrics["steps"]
    return {
        **metrics,
        "exact_choice_fraction": {"numerator": metrics["matched_full_choice"], "denominator": steps},
        "mean_wait_ticks": {"numerator": metrics["waiting_ticks"], "denominator": steps},
    }


def run_grid():
    """Certify decision agreement on the original finite grid, without modeled repetition."""
    grid = tuple((drift, clock, tuple(rows)) for drift, clock, rows in _latency_grid())
    if not grid or any(not rows for _, _, rows in grid):
        raise ValueError("The exact-grid certificate requires the original nonempty support.")
    strata = []
    for drift, clock, rows in grid:
        decisive_arrivals = []
        for row in set(rows):
            action, _, tick = _event_choice(*row)
            if action != _RULES[row[0]][0]:
                decisive_arrivals.append(tick)
        cap = max(decisive_arrivals, default=0)
        witnesses = _latest_witnesses(drift, clock, rows, cap)
        if cap and not witnesses:
            raise RuntimeError("A positive stratum cap has no minimality witness.")
        strata.append({"drift_ticks": drift, "clock_uncertainty_ticks": clock,
                        "slots": len(rows), "distinct_cases": len(set(rows)),
                        "minimum_cap_ticks": cap, "minimality_witnesses": witnesses})
    minimum = max(entry["minimum_cap_ticks"] for entry in strata)
    original_policies = [(f"original-{ratio}-median", int(MEDIAN_LATENCY * ratio), None, True)
                         for ratio in HORIZON_RATIOS]
    policies = original_policies + [
        ("one-tick-below-minimum", max(0, minimum - 1), None, False),
        ("minimum-uniform", minimum, None, False),
        ("minimum-known-stratum", None, "Per declared drift and clock stratum", False),
    ]
    totals = {name: [0] * len(METRICS) for name, _, _, _ in policies}
    validation_count = 0
    for (_, _, rows), stratum in zip(grid, strata):
        for row, weight in Counter(rows).items():
            full = _event_choice(*row)
            for name, uniform, _, _ in policies:
                cap = stratum["minimum_cap_ticks"] if uniform is None else uniform
                observed = _direct_choice(*row, cap)
                reference = _event_choice(*row, cap)
                metrics = _row_metrics(row, observed, full)
                if observed != reference or metrics[1:] != _step(*row, cap):
                    raise RuntimeError(f"Exact policy diverged for {row!r} with cap {cap}.")
                if name in ("minimum-uniform", "minimum-known-stratum") and observed[0] != full[0]:
                    raise RuntimeError("A certified cap failed its sufficiency check.")
                for index, value in enumerate(metrics):
                    totals[name][index] += value * weight
                validation_count += 1
    count_slot = sum(len(rows) for _, _, rows in grid)
    count_distinct = len({row for _, _, rows in grid for row in rows})
    global_witnesses = [row for entry in strata if entry["minimum_cap_ticks"] == minimum
                        for row in entry["minimality_witnesses"]]
    if minimum and not global_witnesses:
        raise RuntimeError("The uniform cap has no minimality witness.")
    report = {
        "schema": SCHEMA,
        "status": "PROVED_ON_DECLARED_GRID",
        "scope": {
            "model": TIMING_MODEL,
            "grid_sha256": digest(grid),
            "enumerated_slots": count_slot,
            "distinct_event_arrival_commitment_tuples": count_distinct,
            "strata": len(strata),
            "original_repetitions_per_slot": ORIGINAL_REPETITIONS,
            "original_evaluations_per_horizon": count_slot * ORIGINAL_REPETITIONS,
            "original_total_policy_evaluations": count_slot * ORIGINAL_REPETITIONS * len(original_policies),
            "repeated_slots_are_independent_trials": False,
            "sufficient_support": "All allowed arrivals, check kinds and latest commitments in the original finite grid.",
        },
        "policies": {
            name: {"uniform_cap_ticks": uniform, "cap_selection": selection or "Fixed cap",
                   "original_policy": original, **_counts(totals[name])}
            for name, uniform, selection, original in policies
        },
        "global_minimum": {
            "cap_ticks": minimum,
            "minimality_witnesses": global_witnesses,
            "proof": "The latest reachable choice-changing check supplies a lower bound. A cap at that arrival includes every reachable choice-changing check.",
        },
        "strata": strata,
        "verification": {
            "weighted_case_policy_comparisons": count_slot * len(policies),
            "unique_within_stratum_case_policy_comparisons": validation_count,
            "method": "Compared direct decisions and outcomes with explicit event ordering for each row, and with the original study's per-step metrics. Weighted repeated input slots without replaying identical tuples.",
            "tie_policy": "Reveal is recorded before any commitment at the same tick.",
            "stratum_policy_requires": "The declared drift and clock stratum are independently known before deciding a cap.",
            "stratum_policy_fallback": "Use the uniform minimum when only the full grid is known.",
            "boundary": "This receipt proves the finite grid, whose check kinds each have one fixed choice effect. A changed support, deadline policy, check behavior or stratum requires new verification.",
        },
    }
    return {**report, "sha256": digest(report)}


def verify_grid(receipt):
    """Check both the portable content digest and independent recomputation."""
    if type(receipt) is not dict or receipt.get("schema") != SCHEMA:
        return False
    try:
        return canonical(receipt) == canonical(run_grid())
    except (TypeError, ValueError, RecursionError):
        return False
