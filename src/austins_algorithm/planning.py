"""Versioned, bounded classical planning, separate from the teaching simulator."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from time import perf_counter

from .optimization import _name

API_VERSION = "chronorealm.planning/1"
ENGINE_VERSION = "branch-bound/1"
MAX_VARIABLES = 128
MAX_CLAUSES = 4096


def example_problem(size=24):
    if type(size) is not int or not 2 <= size <= MAX_VARIABLES or size % 2:
        raise ValueError('Synthetic coverage example requires an even width within 2..128')
    return {'schema_version':API_VERSION,'name':'Equipment coverage planning (synthetic)',
            'variables':[{'name':f'Equipment {i+1}','cost':(i%7)+1} for i in range(size)],
            'clauses':[[i+1,i+2] for i in range(0,size-1,2)]}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value):
    return sha256(canonical(value)).hexdigest()


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")
    return value


def problem_from_document(document):
    """Migrate legacy weighted JSON explicitly; do not expand simulation limits."""
    if not isinstance(document, dict):
        raise ValueError("Planning problem must be an object")
    if set(document) == {"name", "variables", "clauses"}:
        document = {"schema_version": API_VERSION, **document}
    if set(document) != {"schema_version", "name", "variables", "clauses"} or document["schema_version"] != API_VERSION:
        raise ValueError("Unsupported planning problem version or fields")
    variables, clauses = document["variables"], document["clauses"]
    if not isinstance(variables, list) or not 1 <= len(variables) <= MAX_VARIABLES:
        raise ValueError("Planning supports 1..128 variables; quantum limits are unchanged")
    used, result = set(), []
    for row in variables:
        if not isinstance(row, dict) or set(row) != {"name", "cost"}:
            raise ValueError("Each variable needs name and integer cost")
        name = _name(row["name"], "Variable", 60)
        if name.casefold() in used:
            raise ValueError("Duplicate variable name")
        used.add(name.casefold())
        result.append({"name": name, "cost": integer(row["cost"], 0, 1_000_000, "Cost")})
    if not isinstance(clauses, list) or len(clauses) > MAX_CLAUSES:
        raise ValueError("At most 4096 CNF clauses are allowed")
    normalized = []
    for clause in clauses:
        if not isinstance(clause, list) or len(clause) > 2 * MAX_VARIABLES:
            raise ValueError("Invalid CNF clause")
        for literal in clause:
            integer(literal, -len(result), len(result), "Literal")
            if literal == 0:
                raise ValueError("CNF literals are nonzero signed one-based indices")
        normalized.append(sorted(set(clause), key=lambda n: (abs(n), n)))
    return {"schema_version": API_VERSION, "name": _name(document["name"], "Problem", 120),
            "variables": result, "clauses": normalized}


def budget_from_document(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"nodes", "seconds"}:
        raise ValueError("Budget accepts nodes and seconds only")
    nodes = integer(value.get("nodes", 100_000), 1, 1_000_000, "Node budget")
    seconds = value.get("seconds", 30.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not .01 <= seconds <= 300:
        raise ValueError("Time budget must be finite and within .01..300 seconds")
    return {"nodes": nodes, "seconds": float(seconds)}


def verify_candidate(problem, bits, claimed_cost=None):
    """Independent literal-by-literal checker; does not trust solver status."""
    p = problem_from_document(problem)
    if not isinstance(bits, str) or len(bits) != len(p["variables"]) or set(bits) - {"0", "1"}:
        raise ValueError("Candidate must be an MSB-first binary assignment of the declared width")
    chosen = list(reversed(bits))
    failures = [i for i, clause in enumerate(p["clauses"])
                if not any((chosen[abs(lit)-1] == "1") == (lit > 0) for lit in clause)]
    cost = sum(row["cost"] for row, bit in zip(p["variables"], chosen) if bit == "1")
    if claimed_cost is not None and (type(claimed_cost) is not int or cost != claimed_cost):
        raise ValueError("Candidate cost does not match independently calculated integer cost")
    return {"valid": not failures, "cost": cost, "failed_clauses": failures,
            "selected": [row["name"] for row, bit in zip(p["variables"], chosen) if bit == "1"]}


def _propagate(clauses, ones, zeros):
    while True:
        changed = False
        for pos, neg in clauses:
            if pos & ones or neg & zeros:
                continue
            available = (pos | neg) & ~(ones | zeros)
            if not available:
                return None
            if available & (available - 1) == 0:
                if available & pos:
                    ones |= available
                else:
                    zeros |= available
                changed = True
        if not changed:
            return ones, zeros


def solve(problem, *, budget=None, backend="branch-bound", checkpoint=None, stop=None, progress=None,
          _trusted_checkpoint=False):
    """Solve a finite model. Imported checkpoints cannot establish exact optimality."""
    start = perf_counter()
    p, limits = problem_from_document(problem), budget_from_document(budget)
    if backend == "scipy-milp":
        if checkpoint is not None:
            raise ValueError("SciPy runs can be restarted, but have no portable search checkpoint")
        return _milp(p, limits)
    if backend != "branch-bound":
        raise ValueError("Unknown planning backend")
    n = len(p["variables"])
    weights = [r["cost"] for r in p["variables"]]
    masks = [(sum(1 << (v-1) for v in c if v > 0), sum(1 << (-v-1) for v in c if v < 0))
             for c in p["clauses"] if not any(-v in c for v in c)]
    def cost(mask):
        return sum(w for i, w in enumerate(weights) if mask & (1 << i))
    state = {"engine": ENGINE_VERSION, "problem_sha256": fingerprint(p), "frontier": [[0, 0]],
             "nodes": 0, "best": None, "elapsed_seconds": 0.0}
    trusted = checkpoint is None or _trusted_checkpoint
    if checkpoint is not None:
        state = deepcopy(checkpoint)
        if set(state) != {"engine", "problem_sha256", "frontier", "nodes", "best", "elapsed_seconds"} or state["engine"] != ENGINE_VERSION or state["problem_sha256"] != fingerprint(p):
            raise ValueError("Checkpoint source/model identity differs")
        integer(state["nodes"], 0, 10**12, "Checkpoint nodes")
        if not isinstance(state["frontier"], list) or len(state["frontier"]) > n + 1:
            raise ValueError("Invalid checkpoint frontier")
        for pair in state["frontier"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Invalid checkpoint node")
            for mask in pair:
                integer(mask, 0, (1 << n)-1, "Checkpoint mask")
            if pair[0] & pair[1]:
                raise ValueError("Conflicting checkpoint assignment")
        if not isinstance(state["elapsed_seconds"], (int, float)) or not math.isfinite(state["elapsed_seconds"]) or state["elapsed_seconds"] < 0:
            raise ValueError("Invalid checkpoint elapsed time")
        if state["best"] is not None and not verify_candidate(p, state["best"])["valid"]:
            raise ValueError("Invalid checkpoint incumbent")
    initial_nodes, prior_elapsed = state["nodes"], state["elapsed_seconds"]
    best = state["best"]
    best_cost = None if best is None else verify_candidate(p, best)["cost"]
    reason = "SEARCH_COMPLETE"
    def snapshot():
        state.update(best=best, elapsed_seconds=prior_elapsed + perf_counter()-start)
        return deepcopy(state)
    while state["frontier"]:
        if stop and stop():
            reason = "STOP_REQUESTED"
            break
        if state["nodes"]-initial_nodes >= limits["nodes"] or perf_counter()-start >= limits["seconds"]:
            reason = "RESOURCE_BUDGET"
            break
        ones, zeros = state["frontier"].pop()
        state["nodes"] += 1
        propagated = _propagate(masks, ones, zeros)
        if propagated is None:
            continue
        ones, zeros = propagated
        pending = [(pos, neg) for pos, neg in masks if not (pos & ones or neg & zeros)]
        bound = cost(ones)
        # Disjoint unresolved clauses supply an admissible integer lower bound.
        # A negative literal can be satisfied at zero additional cost. Never
        # double-charge a variable shared by two lower-bound clauses.
        charged = 0
        for pos, neg in pending:
            available = (pos | neg) & ~(ones | zeros)
            if available & charged or neg & available:
                continue
            bound += min(weights[i] for i in range(n) if available & (1 << i))
            charged |= available
        if best_cost is not None and bound >= best_cost:
            continue
        if not pending:
            candidate = format(ones, f"0{n}b")
            checked = verify_candidate(p, candidate, cost(ones))
            if not checked["valid"]:
                raise RuntimeError("Independent candidate verification failed")
            best, best_cost = candidate, checked['cost']
        else:
            remaining = (pending[0][0] | pending[0][1]) & ~(ones | zeros)
            bit = 1 << min((i for i in range(n) if remaining & (1 << i)),key=lambda i:weights[i])
            branches = [[ones | bit, zeros],[ones, zeros | bit]]
            if not pending[0][1] & remaining:
                branches.reverse()  # Try the cheapest positive literal first.
            state["frontier"].extend(branches)
        if progress and state["nodes"] % 128 == 0:
            progress(snapshot())
    checkpoint = snapshot()
    completed = not state["frontier"]
    status = ("OPTIMAL" if best else "INFEASIBLE") if completed and trusted else ("FEASIBLE" if best else "UNDETERMINED")
    return {"schema_version": API_VERSION, "engine": ENGINE_VERSION, "backend": backend,
            "problem_sha256": fingerprint(p), "status": status, "termination": reason,
            "bits": best, "cost": best_cost, "candidate_verification": None if best is None else verify_candidate(p, best, best_cost),
            "optimality": "EXACT_FINITE_MODEL" if completed and trusted else "NOT_PROVED",
            "ties_complete": False, "lower_bound": min([cost(o) for o, _ in state["frontier"]] + ([best_cost] if best_cost is not None else [0])),
            "work": {"nodes_this_attempt": state["nodes"]-initial_nodes, "nodes_total": state["nodes"],
                     "elapsed_seconds": perf_counter()-start, "accumulated_solver_seconds": state["elapsed_seconds"]},
            "budget": limits, "checkpoint": checkpoint, "execution_mode": "CLASSICAL",
            "action_authority": False}


def _milp(p, limits):
    """Optional numerical solver; independent integer validation is mandatory."""
    from scipy.optimize import Bounds, LinearConstraint, milp
    import numpy as np
    from scipy.sparse import coo_array
    start = perf_counter()
    rows, cols, values, lower = [], [], [], []
    for i, clause in enumerate(p["clauses"]):
        lower.append(1-sum(lit < 0 for lit in clause))
        for lit in clause:
            rows.append(i); cols.append(abs(lit)-1); values.append(1 if lit > 0 else -1)
    matrix = coo_array((np.asarray(values,dtype=float), (np.asarray(rows,dtype=np.int32),np.asarray(cols,dtype=np.int32))),
                       shape=(len(lower),len(p["variables"]))).tocsc()
    result = milp([r["cost"] for r in p["variables"]], integrality=np.ones(len(p["variables"])),
                  bounds=Bounds(0, 1), constraints=LinearConstraint(matrix, lower, np.inf),
                  options={"time_limit":limits["seconds"], "node_limit":limits["nodes"], "mip_rel_gap":0.0})
    bits = None
    if result.x is not None:
        if any(not math.isfinite(float(v)) or min(abs(v), abs(v-1)) > 1e-6 for v in result.x):
            raise RuntimeError("MILP returned a non-binary candidate")
        bits = "".join(str(int(round(float(v)))) for v in reversed(result.x))
        if not verify_candidate(p, bits)["valid"]:
            raise RuntimeError("MILP candidate failed independent validation")
    checked = None if bits is None else verify_candidate(p,bits)
    return {"schema_version":API_VERSION,"backend":"scipy-milp","engine":"scipy-milp/1",
            "problem_sha256":fingerprint(p),"status":"FEASIBLE" if bits else "UNDETERMINED",
            "termination":str(result.message),"bits":bits,"cost":None if checked is None else checked["cost"],
            "candidate_verification":checked,"optimality":"SOLVER_REPORTED" if result.status==0 else "NOT_PROVED",
            "solver_status":int(result.status),"budget":limits,"checkpoint":None,"ties_complete":False,
            "work":{"elapsed_seconds":perf_counter()-start},"execution_mode":"CLASSICAL","action_authority":False}


def overview(problem, result=None, *, state="READY", error=None):
    p = problem_from_document(problem)
    result = result or {}
    status = result.get("status", state)
    proved = result.get("optimality") == "EXACT_FINITE_MODEL"
    next_action = ("Review the verified result and export the evidence." if proved else
                   "Resume with a declared budget or revise the model; optimality is not established.")
    if error:
        next_action = "Inspect the recorded failure; correct its cause before starting another attempt."
    return {"schema_version":API_VERSION,"goal":p["name"],"stage":state,"status":status,
            "model":f"{len(p['variables'])} choices, {len(p['clauses'])} mandatory rules",
            "best_cost":result.get("cost"),"selected":(result.get("candidate_verification") or {}).get("selected",[]),
            "verification":"Candidate independently checked" if result.get("candidate_verification",{} ) else "No verified candidate yet",
            "optimality":result.get("optimality","NOT_PROVED"),"costs":result.get("work",{}),
            "uncertainty":[] if proved else ["A feasible answer is distinct from an established optimum."],
            "error":error,"next_action":next_action,"action_authority":False}


def format_overview(view):
    """One readable rendering for desktop, CLI and static exported reports."""
    return '\n'.join([f"Goal: {view['goal']}",f"Stage: {view['stage']}",f"Result: {view['status']}",
        f"Best verified cost: {view.get('best_cost') if view.get('best_cost') is not None else 'No planning answer recorded'}",
        'Selected choices: '+(', '.join(view.get('selected',[])) or 'None'),
        'Verification: '+view.get('verification','See the recorded simulation report'),
        'Optimality: '+view.get('optimality','NOT_APPLICABLE'),
        '', 'Work recorded: '+json.dumps(view.get('costs',view.get('attempts',[]))),
        '', 'Next: '+view['next_action'], '',
        'Uncertainty: '+('; '.join(view['uncertainty']) or 'Exact result within this finite model only.'),
        '', 'A computed result does not authorize a real-world action.'])
