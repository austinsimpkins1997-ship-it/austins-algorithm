"""An open-ended, fair stream of fresh practice scenarios at distinct times."""

from .codec import digest
from .controller import domain, opportunity

SCHEMA = "future-reader.open-horizon/1"
MAX_TICK = 1_000_000_000


def slice_horizon(*, opening=0, budget=4, checkpoint=None):
    """Inspect both prerequisite outcomes per tick without collapsing them."""
    if type(opening) is not int or not 0 <= opening <= MAX_TICK or type(budget) is not int or not 1 <= budget <= 32:
        raise ValueError("Use a supported opening tick and a budget of 1–32 evaluations")
    context = digest({"schema": SCHEMA, "opening": opening, "model": domain()})
    cursor, parent = 0, None
    if checkpoint is not None:
        if not isinstance(checkpoint, dict) or set(checkpoint) != {"schema", "context", "cursor", "parent", "sha256"}:
            raise ValueError("Invalid open-horizon checkpoint")
        cursor, parent = checkpoint["cursor"], checkpoint["sha256"]
        fields = {key: value for key, value in checkpoint.items() if key != "sha256"}
        if (type(cursor) is not int or cursor < 0 or opening + cursor // 2 > MAX_TICK
                or checkpoint["schema"] != SCHEMA or checkpoint["context"] != context
                or parent != digest(fields)):
            raise ValueError("Stale, incompatible, or damaged open-horizon checkpoint")
    rows = []
    for index in range(cursor, cursor + budget):
        tick, check = opening + index // 2, index % 2 == 1
        if tick > MAX_TICK:
            break
        result = opportunity(tick, check)
        rows.append({"index": index, "time": tick, "check_available": check,
                     "possibility": "EVALUATED", "feasibility": result["status"],
                     "recommended": result["recommended"], "fulfillment": "NOT_EXECUTED",
                     "model": result["scope"], "frame": result["clock"]})
    next_index = cursor + len(rows)
    supported = opening + next_index // 2 <= MAX_TICK
    body = {"schema": SCHEMA, "context": context, "cursor": next_index, "parent": parent}
    return {"schema": SCHEMA, "status": "PAUSED" if supported else "REPRESENTATION_LIMIT",
            "observed": rows, "checkpoint": {**body, "sha256": digest(body)} if supported else None,
            "scope": "Independent conditional practice contexts. No new live windows are inferred.",
            "scheduling": "Two prerequisite outcomes in sequence per tick. Each supported candidate gets a finite turn.",
            "pruning": "None. Retain infeasible and feasible alternatives with their distinct inputs."}
