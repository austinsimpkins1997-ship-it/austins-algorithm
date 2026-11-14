"""Local, rule-based explanations of the current Workbench experiment."""
from __future__ import annotations

import re
from typing import Any


STAGES = {
    "prepare": ("Equal starting chances", "The shot starts with every assignment equally likely."),
    "oracle": ("The oracle marks candidates", "Marked states change amplitude sign. Each state's chance of measurement stays the same at this step."),
    "diffusion": ("Diffusion changes the chances", "The simulator reflects each amplitude around the average. The squared amplitudes give the new measurement chances."),
}


def _frame(project: dict[str, Any], frame_index: int | None) -> dict[str, Any] | None:
    trace = project.get("preview")
    if not trace or not trace.get("frames"):
        return None
    frames = trace["frames"]
    if frame_index is None:
        return frames[-1]
    return frames[min(len(frames) - 1, max(0, frame_index))]


def _describe_result(result: dict[str, Any] | None, *, exact: bool = False) -> str:
    if result is None:
        return "The run has yet to measure a feasible assignment."
    if result.get("status") == "INFEASIBLE":
        return "The exact solver checked every assignment and found none that satisfies the rules."
    label = "Exact minimum" if exact else "Best measured assignment"
    return f"{label} is {result['bits']} at cost {result['cost']}."


def build_overview(
    project: dict[str, Any], *, frame_index: int | None = None,
    historical: bool = False, exact: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Summarize the active project with one decision per section."""
    problem = project["problem"]
    settings = project["settings"]
    run = project.get("run") or {}
    last = (run.get("events") or [None])[-1]
    frame = _frame(project, frame_index)
    shot_count, query_count = run.get("shots_used", 0), run.get("queries_used", 0)
    budget = (
        f"{shot_count} of {settings['max_shots']} shots used. "
        f"{query_count} of {settings['max_queries']} oracle queries used."
    )
    status = run.get("status", "RUNNING")
    goal = "Find a configuration that passes every rule and has the lowest total cost."
    if problem["variables"] and all(row["cost"] == 0 for row in problem["variables"]):
        goal = "Find a configuration that passes every rule. All choices have zero cost."
    if status == "OPTIMUM":
        heading = "The measured result reached the proven minimum"
        explanation = "The exact solver checked the full set of assignments and confirmed the measured cost."
        next_step = "Open Results to inspect the winning measurement, or Export to share the project and replay."
    elif status == "INFEASIBLE":
        heading = "The rules have no feasible assignment"
        explanation = "The exact solver checked every possible assignment."
        next_step = "Open Build and inspect or edit the rules."
    elif status == "BUDGET_EXHAUSTED":
        heading = "The simulation used its shot budget"
        explanation = "The current result summarizes the measurements taken within that budget."
        next_step = "Use Optimize to check the true minimum, or open Results to inspect what was measured."
    elif last:
        if last["accepted"]:
            heading = "The last shot improved the result" if last["incumbent_before"] else "The last shot found a valid assignment"
            explanation = f"Shot {last['shot']} measured {last['bits']} at cost {last['cost']}."
            if last["incumbent_after"] is not None:
                explanation += f" The next search marks feasible assignments costing less than {last['incumbent_after']['cost']}."
        else:
            heading = "The last shot kept the previous result"
            reason = "fails a rule" if not last["feasible"] else "meets the rules but costs at least as much as the current best"
            explanation = f"Shot {last['shot']} measured {last['bits']}, which {reason}."
        next_step = "Open Simulate to inspect the next shot, or use Optimize to compute the exact answer."
    else:
        heading = "Ready to explore"
        explanation = "The problem and budget are loaded. Simulate lets you inspect a shot before measuring it."
        next_step = "Choose Optimize for the exact answer, or Simulate to explore how Grover search changes the chances."
    if frame and (historical or status == "RUNNING"):
        title, caption = STAGES[frame["stage"]]
        if historical:
            explanation += f" The selected frame comes from a recorded shot. {caption}"
        elif last:
            explanation += f" Current view: {title.lower()}."
        elif frame["stage"] != "prepare":
            heading = title
            explanation = caption + " The preview has used no shots."
    outcome = _describe_result(run.get("best"))
    reference = exact or run.get("reference")
    if reference:
        outcome += f" {_describe_result(reference, exact=True)}"
    return {"goal": goal, "heading": heading, "explanation": explanation,
            "next_step": next_step, "outcome": outcome, "budget": budget}


def assist(
    project: dict[str, Any], question: str, *, frame_index: int | None = None,
    historical: bool = False, exact: dict[str, Any] | None = None,
    selected_state: int | None = None,
) -> dict[str, str | None]:
    """Answer supported questions using only local project data."""
    if not isinstance(question, str) or len(question) > 400:
        raise ValueError("Enter a question of up to 400 characters.")
    words = set(re.findall(r"[a-z0-9]+", question.casefold()))
    view = build_overview(project, frame_index=frame_index, historical=historical, exact=exact)
    trace = project.get("preview")
    frame = _frame(project, frame_index)
    run = project.get("run") or {}
    if words & {"state", "tile", "block", "selected"}:
        if trace and frame and selected_state is not None and 0 <= selected_state < len(trace["states"]):
            state = trace["states"][selected_state]
            feasibility = "passes the rules" if state["feasible"] else "fails at least one rule"
            phase = "negative" if frame["amplitudes"][selected_state] < 0 else "zero" if frame["amplitudes"][selected_state] == 0 else "positive"
            marked = "The oracle marks it in this shot." if state["marked"] else "The oracle leaves it unmarked in this shot."
            return {"answer": f"Assignment {state['bits']} {feasibility} and costs {state['cost']}. "
                              f"It has {phase} amplitude and {frame['probabilities'][selected_state]:.1%} probability "
                              f"at the selected frame. {marked}", "action": None}
        return {"answer": "Open Simulate and select a state block to inspect its cost and measurement chance.",
                "action": "simulate"}
    if words & {"phase", "frame", "superposition", "diffusion", "oracle", "amplitude", "round", "rounds"}:
        if frame:
            title, caption = STAGES[frame["stage"]]
            context = "This is a historical frame from a recorded shot." if historical else "This is the current preview."
            return {"answer": f"{title}. {caption} {context} The marked assignments together have "
                              f"{frame['marked_probability']:.1%} probability at this frame.", "action": "simulate"}
        return {"answer": "Open Simulate and preview a shot to see its preparation, oracle and diffusion stages.",
                "action": "simulate"}
    if words & {"optimize", "optimal", "optimum", "cheapest", "minimum", "cost"}:
        reference = exact or run.get("reference")
        if reference:
            return {"answer": _describe_result(reference, exact=True) + " " + _describe_result(run.get("best")),
                    "action": "results"}
        return {"answer": "Optimize checks every possible assignment and returns the cheapest one that passes the rules. "
                          "It uses a classical solver and spends no simulation shots.", "action": "optimize"}
    if words & {"shots", "query", "queries", "budget", "limit"}:
        return {"answer": view["budget"] + " Each measurement uses one shot. Each Grover round uses one oracle query. "
                          "A preview lets you inspect the probabilities before spending either.", "action": "build"}
    if words & {"measure", "measurement", "sample", "sampling", "auto", "simulate"}:
        return {"answer": "Preview a shot, then choose Measure here on preparation or after a full round. "
                          "Auto run chooses affordable rounds and checks each sampled assignment against the rules. "
                          + view["budget"], "action": "simulate"}
    if words & {"export", "zip", "share", "replay", "code", "python"}:
        return {"answer": "Export saves the complete project, simulation data, CSV tables, HTML report and a Python "
                          "program that reproduces the measured choices. Results also lets you copy that program.",
                "action": "results" if words & {"code", "python"} else "export"}
    if words & {"save", "open", "load", "project", "continue"}:
        return {"answer": "Use Save for a project you can reopen. A saved project carries the rules, settings, "
                          "measurements and preview. Open recreates the session and its remaining budget.", "action": None}
    if words & {"result", "results", "best", "last", "happened", "why"}:
        return {"answer": f"{view['heading']}. {view['explanation']} {view['outcome']}", "action": "results"}
    if words & {"happening", "progress", "status", "overview"}:
        return {"answer": f"{view['heading']}. {view['explanation']}\n\n"
                          f"{view['outcome']}\n\n{view['next_step']}", "action": None}
    if words & {"rule", "rules", "clause", "clauses", "variable", "variables", "edit"}:
        return {"answer": "Build defines your choices and costs. Each rule must be true. In a rule like 1 -2, "
                          "at least one condition must hold: q0 is enabled or q1 is disabled.", "action": "build"}
    if words & {"next", "start", "help"} or not words:
        return {"answer": f"{view['goal']} {view['heading']}. {view['next_step']}", "action": "guide"}
    return {"answer": "I can explain the current step, a selected state, the exact optimum, the budget, or saving and export. "
                      + view["next_step"], "action": None}
