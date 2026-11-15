"""Exact binary model ranking with strict upper-bound pruning."""

from fractions import Fraction
from random import Random
import re

from .codec import digest


SCORE_SCALE = 10_000
MAX_ROUTES = 4096
POLICIES = ("exhaustive", "guarded", "bound-first")
_REQUIRED = {"id", "duration", "permitted", "calibrated", "protected", "chance_bps", "success", "failure"}
_MAX_MAGNITUDE = 1_000_000_000
_MAX_TICK = 1_000_000_000


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field} must be an integer between {low} and {high}")
    return value


def _validated(routes, deadline, equality):
    _integer(deadline, 0, _MAX_TICK, "deadline")
    if type(equality) is not bool:
        raise ValueError("Deadline equality must be explicit")
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        raise ValueError(f"Provide a list of at most {MAX_ROUTES} routes")
    names = set()
    eligible, rejected = [], []
    for route in routes:
        if not isinstance(route, dict) or set(route) - {"bound_slack"} != _REQUIRED:
            raise ValueError("Invalid route fields")
        name = route["id"]
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) is None or name in names:
            raise ValueError("Route IDs must be unique short ASCII identifiers")
        names.add(name)
        for key in ("permitted", "calibrated", "protected"):
            if type(route[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        duration = _integer(route["duration"], 0, _MAX_TICK, "duration")
        chance = _integer(route["chance_bps"], 0, SCORE_SCALE, "chance_bps")
        success = _integer(route["success"], -_MAX_MAGNITUDE, _MAX_MAGNITUDE, "success")
        failure = _integer(route["failure"], -_MAX_MAGNITUDE, _MAX_MAGNITUDE, "failure")
        slack = _integer(route.get("bound_slack", 0), 0, _MAX_MAGNITUDE, "bound_slack")
        reasons = []
        if duration > deadline or (duration == deadline and not equality):
            reasons.append("DEADLINE")
        if not route["permitted"]:
            reasons.append("PERMISSION")
        if not route["calibrated"]:
            reasons.append("CALIBRATION")
        if reasons:
            rejected.append({"id": name, "reasons": reasons})
        else:
            eligible.append((name, route["protected"], chance, success, failure, max(success, failure) + slack))
    return eligible, rejected


def _rank(eligible, rejected, *, deadline, equality, policy):
    if policy not in POLICIES:
        raise ValueError("Choose exhaustive, guarded, or bound-first")
    ordered = sorted(eligible, key=lambda route: -route[-1]) if policy == "bound-first" else eligible
    best = None
    winners = set()
    evaluated = []
    deferred = []
    for name, protected, chance, success, failure, bound in ordered:
        upper = bound * SCORE_SCALE
        if policy != "exhaustive" and not protected and best is not None and upper < best:
            deferred.append({"id": name, "upper_numerator": upper, "incumbent_numerator": best,
                             "reason": "STRICTLY_BELOW_INCUMBENT"})
            continue
        score = chance * success + (SCORE_SCALE - chance) * failure
        evaluated.append(name)
        if best is None or score > best:
            best, winners = score, {name}
        elif score == best:
            winners.add(name)
    return {
        "schema": "future-reader.exact-pruning/1", "mode": "SYNTHETIC_BINARY_OUTCOMES", "policy": policy,
        "status": "RANKED" if winners else "NO_ELIGIBLE_ROUTE", "deadline": deadline, "equality": equality,
        "score": None if best is None else {"numerator": best, "denominator": SCORE_SCALE},
        "winners": [name for name, *_ in eligible if name in winners],
        "protected_eligible": [name for name, protected, *_ in eligible if protected],
        "evaluated": evaluated, "ineligible": rejected, "deferred": deferred,
        "counts": {"eligible": len(eligible), "exact_evaluations": len(evaluated),
                   "ineligible": len(rejected), "deferred": len(deferred)},
    }


def rank(routes, *, deadline, equality=True, policy="guarded"):
    """Rank all hard-eligible routes and preserve equal-score ties.

    Every supplied probability is an integer in basis points. With p in
    [0, 1], p*success + (1-p)*failure cannot exceed max(success, failure).
    Slack only weakens that ceiling. Eligibility precedes protection and
    ranking. Protection forces evaluation of an eligible alternative even
    when its ceiling falls below the incumbent.
    """
    eligible, rejected = _validated(routes, deadline, equality)
    return _rank(eligible, rejected, deadline=deadline, equality=equality, policy=policy)


def fraction_reference(routes, *, deadline, equality=True):
    """Compare routes by independently evaluating each exact rational utility."""
    _validated(routes, deadline, equality)
    scores, protected = {}, []
    for route in routes:
        arrives = route["duration"] < deadline or route["duration"] == deadline and equality
        if arrives and route["permitted"] and route["calibrated"]:
            p = Fraction(route["chance_bps"], SCORE_SCALE)
            scores[route["id"]] = p * route["success"] + (1 - p) * route["failure"]
            if route["protected"]:
                protected.append(route["id"])
    best = max(scores.values(), default=None)
    return {"score": None if best is None else {"numerator": best.numerator, "denominator": best.denominator},
            "winners": [name for name, value in scores.items() if value == best],
            "protected_eligible": protected, "eligible_count": len(scores)}


def _sample(random, route_count, *, adversarial, trial):
    rows = []
    for index in range(route_count):
        rows.append({
            "id": f"route-{index}", "duration": random.randrange(1, 33), "permitted": random.randrange(10) > 0,
            "calibrated": random.randrange(10) > 0, "protected": random.randrange(16) == 0,
            "chance_bps": random.randrange(SCORE_SCALE + 1), "success": random.randrange(-80, 101),
            "failure": random.randrange(-120, 61),
        })
    if adversarial:
        tie_success = 10_000 if trial % 2 == 0 else 2_000
        for index, chance, success, failure, protected in (
            (0, 10_000, 10, -1, False), (1, 5_000, 1, 500, False),     # Reward-only bound would be unsafe.
            (2, 0, -5, 100, False), (3, 1, 200, -1000, True),          # Rare severe state is preserved.
            (4, tie_success, 500, -1, False), (5, tie_success, 500, -1, False),  # Tie or looser incumbent.
            (6, 10_000, 3000, -1, True),                               # Protection cannot waive timing.
        ):
            rows[index].update(chance_bps=chance, success=success, failure=failure, protected=protected,
                               permitted=True, calibrated=True, duration=21 if index == 6 else 20)
        offset = trial % route_count
        rows = rows[offset:] + rows[:offset]     # Vary when the first strong incumbent is found.
    return rows


def _study_cell(workload, *, slack, equality):
    outcomes = {policy: {"exact_evaluations": 0, "eligible": 0, "ineligible": 0, "deferred": 0,
                         "protected_evaluations": 0, "reference_disagreements": 0} for policy in POLICIES}
    scope = {"deadline": 20, "equality": equality}
    prepared = [[{**row, "bound_slack": slack} for row in case] for case in workload]
    for rows in prepared:
        expected = fraction_reference(rows, **scope)
        expected_score = None if expected["score"] is None else Fraction(
            expected["score"]["numerator"], expected["score"]["denominator"],
        )
        for policy in POLICIES:
            report = rank(rows, policy=policy, **scope)
            score = None if report["score"] is None else Fraction(report["score"]["numerator"], SCORE_SCALE)
            evaluated = set(report["evaluated"])
            protected = set(report["protected_eligible"])
            if (score != expected_score or report["winners"] != expected["winners"]
                    or report["protected_eligible"] != expected["protected_eligible"]
                    or report["counts"]["eligible"] != expected["eligible_count"]
                    or not protected <= evaluated):
                raise RuntimeError("Ranking disagreed with the rational reference or lost a protected route")
            current = outcomes[policy]
            for key, count in report["counts"].items():
                current[key] += count
            current["protected_evaluations"] += len(protected)
    return {"workload_sha256": digest(prepared), "results": outcomes}


def run_scale_study(*, trials=8, seed=20261113):
    """Compare reproducible work counts over bounded generated cohorts.

    Evaluation costs are sensitivity assumptions in abstract work units.
    Cost totals include exact-score work only. Validation, bound checking,
    sorting, memory and machine latency are excluded from those totals.
    """
    _integer(trials, 1, 64, "trials")
    _integer(seed, 0, (1 << 64) - 1, "seed")
    counts = (8, 32, 128, 1024)
    costs = (1, 8, 128, 1024)
    rows = []
    seeds = {"ordinary": seed, "holdout": seed ^ 0xD6E8FEB86659FD93,
             "adversarial": seed ^ 0x9E3779B97F4A7C15}
    for name, cohort_seed in seeds.items():
        for count in counts:
            random = Random(cohort_seed ^ count)
            workload = [_sample(random, count, adversarial=name == "adversarial", trial=index)
                        for index in range(trials)]
            for slack in (0, 30, 200):
                for equality in (False, True):
                    cell = _study_cell(workload, slack=slack, equality=equality)
                    for result in cell["results"].values():
                        result["modeled_exact_work"] = [
                            {"unit_cost": cost, "total_units": cost * result["exact_evaluations"]} for cost in costs
                        ]
                    rows.append({"cohort": name, "stream_seed": str(cohort_seed ^ count), "trials": trials,
                                 "route_count": count, "bound_slack": slack, "equality": equality, **cell})
    return {
        "schema": "future-reader.exact-pruning-scale/1", "mode": "SYNTHETIC_MODEL",
        "generator": "future-reader.exact-pruning-generator/1", "seed": str(seed),
        "results": rows, "score_scale": SCORE_SCALE, "reference": "exhaustive independent Fraction weighted sum",
        "evaluation_cost_basis": "hypothetical work units for each exact-score calculation, excluding all overhead",
        "assumptions": ["fixed generated inputs", "declared integer binary utilities and probability",
                        "no field calibration or measured timing"],
    }
