"""Joint scenario decisions with exact rational arithmetic."""

from fractions import Fraction


def _bounded_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid scenario integer or limit")
    return value


def _ratio(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def evaluate(actions, *, deadline, equality=True):
    """Compare explicitly weighted outcomes and durations in the same scenarios."""
    deadline = _bounded_integer(deadline, 0, 1_000_000)
    if type(equality) is not bool or not isinstance(actions, list) or not 1 <= len(actions) <= 16:
        raise ValueError("Provide bounded actions and an explicit equality policy")
    results, names = [], set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"id", "hard_eligible", "scenarios"}:
            raise ValueError("Invalid decision action")
        name, eligible, scenarios = action["id"], action["hard_eligible"], action["scenarios"]
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or name in names or type(eligible) is not bool:
            raise ValueError("Invalid action identity or hard eligibility")
        names.add(name)
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 128:
            raise ValueError("Each action needs a finite set of weighted outcomes")
        total = timely = success = both = value = 0
        lowest = None
        for row in scenarios:
            if not isinstance(row, dict) or set(row) != {"weight", "duration", "success", "value"} or type(row["success"]) is not bool:
                raise ValueError("Invalid joint scenario")
            weight = _bounded_integer(row["weight"], 1, 1_000_000)
            duration = _bounded_integer(row["duration"], 0, 1_000_000)
            utility = _bounded_integer(row["value"], -1_000_000, 1_000_000)
            arrives = duration < deadline or duration == deadline and equality
            total += weight
            timely += weight * arrives
            success += weight * row["success"]
            both += weight * (arrives and row["success"])
            value += weight * utility
            lowest = utility if lowest is None else min(lowest, utility)
        results.append({"id": name, "eligible": eligible, "weight_sum": total,
                        "p_success": _ratio(Fraction(success, total)), "p_on_time": _ratio(Fraction(timely, total)),
                        "p_success_and_on_time": _ratio(Fraction(both, total)),
                        "p_success_given_on_time": _ratio(Fraction(both, timely)) if timely else None,
                        "expected_value": _ratio(Fraction(value, total)), "worst_value": lowest})
    candidates = [row for row in results if row["eligible"]]
    def expected(row):
        return Fraction(row["expected_value"]["numerator"], row["expected_value"]["denominator"])

    max_value = max(map(expected, candidates), default=None)
    worst = max((row["worst_value"] for row in candidates), default=None)
    pareto = [row["id"] for row in candidates if not any(
        expected(other) >= expected(row) and other["worst_value"] >= row["worst_value"]
        and (expected(other), other["worst_value"]) != (expected(row), row["worst_value"])
        for other in candidates)]
    return {"schema": "future-reader.decision/1", "evidence": "SCENARIO_MODEL",
            "deadline": deadline, "equality": equality, "actions": results, "non_dominated": pareto,
            "expected_value_choice": [row["id"] for row in candidates if expected(row) == max_value],
            "robust_choice": [row["id"] for row in candidates if row["worst_value"] == worst]}


def break_even(*, success, failure, fallback):
    for value in (success, failure, fallback):
        _bounded_integer(value, -1_000_000, 1_000_000)
    if success <= failure:
        raise ValueError("Success must have greater utility than failure")
    threshold = Fraction(fallback - failure, success - failure)
    return {"probability": _ratio(threshold), "attainable": 0 <= threshold <= 1,
            "policy": "Prefer action when its success probability exceeds the threshold. Equality is a tie."}


def demo():
    rows = lambda *items: [{"weight": weight, "duration": duration, "success": outcome, "value": utility}
                          for weight, duration, outcome, utility in items]
    actions = [
        {"id": "act-now", "hard_eligible": True, "scenarios": rows((7, 8, True, 8), (3, 15, False, -12))},
        {"id": "wait-for-check", "hard_eligible": True, "scenarios": rows((8, 11, True, 9), (2, 17, False, -10))},
        {"id": "prepare-reversibly", "hard_eligible": True, "scenarios": rows((6, 11, True, 7), (4, 16, False, -2))},
        {"id": "fallback", "hard_eligible": True, "scenarios": rows((1, 10, True, 3))},
        {"id": "let-close", "hard_eligible": True, "scenarios": rows((1, 0, False, 0))},
    ]
    return {"model": "illustrative joint scenarios/1", "inputs": actions,
            "result": evaluate(actions, deadline=14),
            "act_now_vs_fallback": break_even(success=8, failure=-12, fallback=3),
            "limits": ["Weights and utilities are authored examples.", "No field calibration is claimed.",
                       "Each scenario's utility already accounts for its consequences and timing."]}
