"""Local practice for interpreting exact decisions and changes in their constraints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import product

from .optimization import exact_minimum

PRACTICE_SCHEMA = "AustinDecisionPractice/1"
REVIEW_DAYS = 7
MAX_EVENTS = 60


def _case(title, kind, labels, costs, rules, hint):
    return {
        "kind": kind, "title": title, "hint": hint,
        "choices": [{"id": f"q{i}", "name": label, "cost": cost}
                    for i, (label, cost) in enumerate(zip(labels, costs))],
        "rules": rules,
    }


CASES = {
    "baseline": _case("Baseline · intake", "baseline", ("Paper", "Local device", "Reconcile"),
                      (4, 6, 3), ((1, 2), (-1, -2), (3,)),
                      "Exactly one intake route is required. Include reconciliation in the total."),
    "practice": _case("Practice · escalation", "practice", ("Phone", "Runner", "Case log"),
                      (3, 7, 2), ((1, 2), (-1, -2), (3,)),
                      "Compare each eligible route with the required log still selected."),
    "independent-a": _case("Independent · overnight intake", "independent",
                           ("Paper", "Local device", "Spare battery", "Reconcile"), (5, 3, 4, 2),
                           ((1, 2), (-1, -2), (-2, 3), (4,)),
                           "The cheaper device requires a second component."),
    "independent-b": _case("Independent retry · dispatch", "independent",
                           ("Phone", "Local terminal", "Battery", "Reconcile"), (7, 4, 1, 1),
                           ((1, 2), (-1, -2), (-2, 3), (4,)),
                           "Compare feasible totals after adding the terminal dependency."),
    "independent-c": _case("Independent retry · shift handoff", "independent",
                           ("Phone", "Paper", "Log"), (4, 4, 1), ((1, 2), (-1, -2), (3,)),
                           "When two eligible routes tie, either minimum is accepted."),
    "transfer-a": _case("Transfer · exhibit during renovation", "transfer",
                        ("North guide", "South guide", "Shared signage", "Printed map"), (3, 1, 1, 3),
                        ((1, 2), (-1, -2), (-2,), (3, 4), (-3, -4)),
                        "The South route remains closed. Count only choices that satisfy every rule."),
    "transfer-b": _case("Transfer retry · temporary workshop", "transfer",
                        ("East door", "West door", "Spotter"), (3, 2, 1),
                        ((1, 2), (-1,), (-2,), (-2, 3)),
                        "Test whether any route can satisfy all the conditions."),
    "retention-a": _case("Delayed check · volunteer intake", "retention",
                         ("Phone", "Local tablet", "Battery", "Ticket ledger"), (4, 2, 4, 2),
                         ((1, 2), (-1, -2), (-2, 3), (4,)),
                         "Verify each route's prerequisites before comparing its total."),
    "retention-b": _case("Delayed recheck · accessible route", "retention",
                         ("Ramp A", "Ramp B", "Spotter"), (6, 4, 3),
                         ((1, 2), (-1, -2), (-2, 3)),
                         "Include the spotter if the selected route requires one."),
}


def _clock(now=None):
    value = datetime.now(timezone.utc) if now is None else now
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Practice time must include a timezone.")
    return value.astimezone(timezone.utc)


def _read_time(value):
    if not isinstance(value, str):
        raise ValueError("Invalid practice timestamp.")
    try:
        return _clock(datetime.fromisoformat(value))
    except (ValueError, OverflowError) as exc:
        raise ValueError("Invalid practice timestamp.") from exc


def new_practice():
    return {"schema": PRACTICE_SCHEMA, "events": []}


def _rule_text(case):
    names = [item["name"] for item in case["choices"]]
    return [" or ".join(("include " if lit > 0 else "exclude ") + names[abs(lit) - 1] for lit in clause)
            for clause in case["rules"]]


def _check_attempt(case, answer, prediction, assisted):
    variables = [{"name": item["name"], "cost": item["cost"]} for item in case["choices"]]
    reference = exact_minimum({"name": case["title"], "variables": variables,
                               "clauses": [list(clause) for clause in case["rules"]]})
    if answer == "infeasible":
        cost = None
        missed = []
        correct = reference["status"] == "INFEASIBLE"
    else:
        keys = {item["id"] for item in case["choices"]}
        if not isinstance(answer, list) or any(not isinstance(item, str) or item not in keys for item in answer):
            raise ValueError("Choose listed options or mark the scenario infeasible.")
        chosen = set(answer)
        if len(chosen) != len(answer):
            raise ValueError("Choose each option at most once.")
        flags = [item["id"] in chosen for item in case["choices"]]
        missed = [text for text, clause in zip(_rule_text(case), case["rules"])
                  if not any(flags[abs(lit) - 1] == (lit > 0) for lit in clause)]
        cost = sum(item["cost"] for item, selected in zip(case["choices"], flags) if selected)
        bits = "".join(str(int(flag)) for flag in reversed(flags))
        correct = not missed and bits in reference["ties"]
    if missed:
        note = "Recheck the first unmet rule: " + missed[0]
    elif correct:
        note = "This is a verified minimum for this exercise." if cost is not None else "The supplied rules leave no feasible assignment."
    elif answer == "infeasible":
        note = "At least one assignment is feasible. Try a fresh case and check the requirements before comparing costs."
    else:
        note = "The plan satisfies every rule. A cheaper feasible choice exists."
    return {"passed": correct, "help": "hinted" if assisted else "independent", "cost": cost,
            "prediction": prediction, "prediction_difference": cost - prediction if cost is not None and prediction is not None else None,
            "minimum_cost": reference["cost"], "unmet_rules": missed, "feedback": note,
            "checked_assignments": reference["assignments_checked"]}


def _case_for(history, now):
    used = {attempt["case"] for attempt in history}

    def first(kind):
        return next((key for key, case in CASES.items() if case["kind"] == kind and key not in used), None)

    independence = next((a for a in history if a["kind"] == "independent" and a["passed"] and a["help"] == "independent"), None)
    retention = next((a for a in history if a["kind"] == "retention" and a["passed"] and a["help"] == "independent"), None)
    transfer = next((a for a in history if a["kind"] == "transfer" and a["passed"] and a["help"] == "independent"), None)
    due = _read_time(independence["recorded_at"]) + timedelta(days=REVIEW_DAYS) if independence else None
    if retention:
        due = None
    evidence = {
        "baseline": next(("passed" if a["passed"] else "needs practice" for a in history if a["kind"] == "baseline"), "unknown"),
        "acquisition": "observed after practice" if independence and history and not history[0]["passed"] else "unknown",
        "independence": "observed" if independence else "unknown",
        "transfer": "observed" if transfer else "unknown",
        "retention": "observed" if retention else "unknown",
    }
    if first("baseline"):
        return first("baseline"), due, evidence
    if first("practice"):
        return first("practice"), due, evidence
    if not independence:
        return first("independent"), due, evidence
    if due and now >= due and first("retention"):
        return first("retention"), due, evidence
    if not transfer and first("transfer"):
        return first("transfer"), due, evidence
    return None, due, evidence


def _replay(state):
    if not isinstance(state, dict) or set(state) != {"schema", "events"} or state["schema"] != PRACTICE_SCHEMA:
        raise ValueError("Expected a version 1 practice record.")
    events = state["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Too many practice events.")
    history = []
    active, prediction, hinted, last = "baseline", None, False, None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Invalid practice event.")
        action = event.get("action")
        extras = {"predict": {"prediction"}, "hint": set(), "submit": {"choices"}, "continue": set()}
        if not isinstance(action, str) or action not in extras or set(event) != {"action", "recorded_at"} | extras[action]:
            raise ValueError("Unknown or malformed practice event.")
        instant = _read_time(event["recorded_at"])
        if last is not None and instant < last:
            raise ValueError("Practice times may not go backward.")
        last = instant
        if action == "continue":
            if active is not None:
                raise ValueError("Finish this practice attempt first.")
            active, _, _ = _case_for(history, instant)
            if active is None:
                raise ValueError("No fresh exercise is available yet.")
            prediction, hinted = None, False
        elif active is None:
            raise ValueError("Continue to another fresh exercise first.")
        elif action == "predict":
            prediction = event["prediction"]
            if type(prediction) is not int or not 0 <= prediction <= 8_000_000:
                raise ValueError("Predict a nonnegative total cost.")
        elif action == "hint":
            if active == "baseline" or hinted:
                raise ValueError("A hint is unavailable for this attempt.")
            hinted = True
        else:
            case = CASES[active]
            result = _check_attempt(case, event["choices"], prediction, hinted)
            history.append({**result, "case": active, "kind": case["kind"], "title": case["title"],
                            "choices": event["choices"], "recorded_at": event["recorded_at"]})
            active = None
    return history, active, prediction, hinted, last


def practice_action(state, action, *, prediction=None, choices=None, now=None):
    _replay(state)
    event = {"action": action, "recorded_at": _clock(now).isoformat()}
    if action == "predict":
        event["prediction"] = prediction
    elif action == "submit":
        event["choices"] = choices.copy() if isinstance(choices, list) else choices
    updated = {"schema": PRACTICE_SCHEMA, "events": [*state["events"], event]}
    _replay(updated)
    return updated


def practice_view(state, *, now=None):
    history, active, prediction, hinted, last = _replay(state)
    instant = _clock(now)
    next_case, due, evidence = _case_for(history, max(instant, last) if last else instant)
    case = CASES.get(active)
    return {
        "kind": case["kind"] if case else None, "title": case["title"] if case else "Next practice",
        "prompt": "Select the lowest-cost combination that satisfies every listed rule, or mark infeasible." if case else
                  "Continue when a fresh check is available.",
        "choices": [item.copy() for item in case["choices"]] if case else [],
        "rules": _rule_text(case) if case else [],
        "prediction": prediction, "hint": case["hint"] if hinted and case else None,
        "hint_available": bool(case and active != "baseline" and not hinted),
        "due_at": due.isoformat() if due else None, "ready": bool(not active and next_case),
        "feedback": history[-1]["feedback"] if history else None, "history": history, "evidence": evidence,
        "evidence_origin": "local practice submissions; clock and help rely on the local record",
    }


def verify_practice_bank():
    """Cross-check declared training cases with plain Boolean enumeration."""
    for case in CASES.values():
        feasible = []
        for bits in product((False, True), repeat=len(case["choices"])):
            if all(any(bits[abs(lit) - 1] == (lit > 0) for lit in clause) for clause in case["rules"]):
                feasible.append(sum(row["cost"] for row, enabled in zip(case["choices"], bits) if enabled))
        reference = _check_attempt(case, "infeasible", None, False)
        expected = min(feasible, default=None)
        if reference["minimum_cost"] != expected:
            raise ValueError("A practice fixture did not match independent enumeration.")
