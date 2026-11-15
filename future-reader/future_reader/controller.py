"""Local practice controller for a finite, versioned state and timing frame."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import re
import sqlite3
import time
from uuid import uuid4


_DOMAIN = {
    "id": "future-reader.practice/1",
    "nodes": {"S": "Start", "A": "Bench", "B": "Shelf", "G": "Goal"},
    "edges": [
        {"source": "S", "target": "A", "duration": 3, "cost": 3, "opens": 0, "requires_check": False},
        {"source": "A", "target": "G", "duration": 4, "cost": 3, "opens": 8, "requires_check": True},
        {"source": "S", "target": "B", "duration": 2, "cost": 1, "opens": 0, "requires_check": False},
        {"source": "B", "target": "G", "duration": 6, "cost": 2, "opens": 0, "requires_check": False},
    ],
    "deadline": 17,
    "verify": 2,
    "margin": 1,
    "uncertainty": 1,
    "equality": True,
}
_EDGES = {(edge["source"], edge["target"]): edge for edge in _DOMAIN["edges"]}
_FRAME = "future-reader.practice-clock/1"
_PROGRAM = "future-reader.program/1"
_CHECKPOINT = "future-reader.checkpoint/1"
_FRONTIER = "future-reader.frontier/1"
_CONTROLLER = "future-reader.controller/1"
_MAX_EVENTS = 1024
_MAX_TICK = 1_000_000_000
_MAX_OPEN_PREVIEWS = 64


class ControllerError(ValueError):
    """A reason code and bounded diagnostic for a rejected operation."""

    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


def domain():
    """Return the immutable model as caller-owned JSON data."""
    return deepcopy(_DOMAIN)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode("ascii")).hexdigest()


def _require(valid, reason, detail):
    if not valid:
        raise ControllerError(reason, detail)


def _tick(value, label):
    _require(type(value) is int and 0 <= value <= _MAX_TICK, "INVALID_INPUT", f"{label} must be an integer in range.")
    return value


def _identifier(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None,
             "INVALID_INPUT", "Use a name of 1–64 letters, digits, dots, dashes or underscores.")
    return value


def _node(value):
    _require(isinstance(value, str) and value in _DOMAIN["nodes"], "UNKNOWN_STATE", "Choose a state on the practice board.")
    return value


def _boolean(value):
    _require(type(value) is bool, "INVALID_INPUT", "Check availability must be true or false.")
    return value


def _arrival(edges, now):
    for edge in edges:
        now = max(now, edge["opens"]) + edge["duration"]
    return now


def _route(path, now, check_available):
    edges = [_EDGES[pair] for pair in zip(path, path[1:])]
    arrival = _arrival(edges, now)
    end_limit = _DOMAIN["deadline"] - _DOMAIN["verify"] - _DOMAIN["margin"] - _DOMAIN["uncertainty"]
    latest = end_limit - sum(edge["duration"] for edge in edges)
    if latest < 0 or _arrival(edges, latest) > end_limit:
        latest = None
    reason = None
    if not check_available and any(edge["requires_check"] for edge in edges):
        reason = "PREREQUISITE_UNAVAILABLE"
    elif arrival > end_limit:
        reason = "DEADLINE"
    return {
        "path": list(path), "departure": now, "arrival": arrival,
        "cost": sum(edge["cost"] for edge in edges), "latest_safe_start": latest,
        "eligible": reason is None, "reason": reason,
    }


def _frontier_progress(start, goal, now, check_available, progress, budget):
    pending = [(start,)]
    complete = []
    expanded = 0
    stop = progress + budget
    while pending and expanded < stop:
        path = pending.pop()
        expanded += 1
        if path[-1] == goal:
            complete.append(_route(path, now, check_available))
            continue
        for source, target in reversed(tuple(_EDGES)):
            if source == path[-1] and target not in path:
                pending.append((*path, target))
    complete.sort(key=lambda row: (row["cost"], row["arrival"], row["path"]))
    return pending, complete, expanded


def search_slice(start="S", goal="G", now=0, check_available=True, *, budget=1, checkpoint=None):
    """Explore the finite practice graph in bounded slices.

    Checkpoints are reconstructed from the declared input and an expansion
    cursor. Recomputing the tiny prefix verifies the supplied continuation.
    Every edge advances time and has positive cost, so revisits cannot improve
    an eligible path in this waiting-permitted domain.
    """
    _node(start)
    _node(goal)
    _tick(now, "Clock")
    _boolean(check_available)
    _require(type(budget) is int and 1 <= budget <= 16, "INVALID_INPUT", "Slice size must be 1–16 expansions.")
    context = _digest([_DOMAIN, start, goal, now, check_available])
    progress = 0
    if checkpoint is not None:
        _require(isinstance(checkpoint, dict) and set(checkpoint) == {"schema", "context", "expanded", "pending"}
                 and checkpoint["schema"] == _FRONTIER and checkpoint["context"] == context,
                 "STALE_FRONTIER", "The model, state, window or check changed. Recompute the route.")
        progress = checkpoint["expanded"]
        _require(type(progress) is int and 0 <= progress <= 16, "INVALID_CHECKPOINT", "Invalid frontier cursor.")
        pending, _, replayed = _frontier_progress(start, goal, now, check_available, 0, progress)
        _require(replayed == progress and [list(path) for path in pending] == checkpoint["pending"],
                 "INVALID_CHECKPOINT", "The frontier disagrees with the finite reference search.")
    pending, complete, expanded = _frontier_progress(start, goal, now, check_available, progress, budget)
    continuation = None
    if pending:
        continuation = {"schema": _FRONTIER, "context": context, "expanded": expanded,
                        "pending": [list(path) for path in pending]}
    return {
        "status": "PAUSED" if pending else "EXHAUSTED", "expanded": expanded,
        "routes": complete, "checkpoint": continuation, "pruning_basis": "positive cost and time; waiting permitted",
    }


def routes(start="S", goal="G", now=0, check_available=True):
    """List all simple candidate routes, including timing and prerequisite failures."""
    result = search_slice(start, goal, now, check_available, budget=16)
    _require(result["status"] == "EXHAUSTED", "SEARCH_BOUND", "The finite model exceeds this reference search limit.")
    return result["routes"]


def opportunity(now=0, check_available=True, *, start="S", goal="G"):
    """Report the exact timing decision in the practice clock frame."""
    candidates = routes(start, goal, now, check_available)
    eligible = [row for row in candidates if row["eligible"]]
    latest = [row["latest_safe_start"] for row in candidates if row["reason"] != "PREREQUISITE_UNAVAILABLE"
              and row["latest_safe_start"] is not None]
    safe_arrival = _DOMAIN["deadline"] - _DOMAIN["verify"] - _DOMAIN["margin"] - _DOMAIN["uncertainty"]
    status = "UNRESOLVED"
    if eligible:
        status = "NARROWING" if now == max(latest) else "OPEN"
    elif latest:
        status = "MISSED"
    return {
        "schema": "future-reader.opportunity/1", "clock": _FRAME, "now": now, "units": "s",
        "objective": "Reach the goal at minimum transition cost", "scope": _DOMAIN["id"],
        "status": status, "deadline": _DOMAIN["deadline"], "equality": _DOMAIN["equality"],
        "verify_bound": _DOMAIN["verify"], "safety_margin": _DOMAIN["margin"],
        "clock_uncertainty": _DOMAIN["uncertainty"], "last_safe_arrival": safe_arrival,
        "zero_margin_arrival": _DOMAIN["deadline"] - _DOMAIN["verify"],
        "latest_safe_start": max(latest, default=None), "routes": candidates,
        "recommended": eligible[0] if eligible else None,
        "fallback": "Preserve the checkpoint and inspect a new window." if not eligible else "Preserve the current checkpoint.",
        "probability": {"status": "UNKNOWN", "basis": "This finite exercise uses declared deterministic upper bounds."},
        "boundary_basis": "specified deadline, modeled durations, verification bound and safety reserves",
    }


def compare_wait(now=0, check_available=False, *, check_returns, start="S", goal="G"):
    """Compare committing now with waiting for an independent check."""
    current = opportunity(now, check_available, start=start, goal=goal)
    _tick(check_returns, "Check return")
    _require(check_returns >= now, "INVALID_INPUT", "A future check cannot return before the current clock.")
    positive = opportunity(check_returns, True, start=start, goal=goal)
    negative = opportunity(check_returns, False, start=start, goal=goal)
    closing = opportunity(now, True, start=start, goal=goal)["latest_safe_start"]
    return {
        "schema": "future-reader.wait-decision/1", "mode": "SIMULATION", "clock": _FRAME,
        "act_now": current["recommended"], "wait_positive_check": positive["recommended"],
        "wait_negative_check": negative["recommended"], "check_returns": check_returns,
        "latest_decisive_return": closing, "latest_fallback_start": current["latest_safe_start"],
        "can_change_eligibility": positive["recommended"] != negative["recommended"],
        "fallback": "Preserve the latest checkpoint." if negative["recommended"] is None else negative["recommended"],
        "assumptions": ["Check consumed at departure", "Check outcome and success probability unknown",
                        "Interpretation and verification reserves are specified by the practice model"],
    }


def critical_path(stages, *, deadline, margin=0, clock_uncertainty=0):
    """Calculate latest starts for a prerequisite DAG of duration upper bounds.

    The caller must include resource serialization and every stage that must
    finish by the deadline, including any applicable recovery. Different DAG
    branches run in parallel when the declared prerequisites permit.
    """
    _tick(deadline, "Deadline")
    _tick(margin, "Safety margin")
    _tick(clock_uncertainty, "Clock uncertainty")
    _require(isinstance(stages, list) and 1 <= len(stages) <= 64, "INVALID_INPUT", "Supply 1–64 bounded stages.")
    graph = {}
    for stage in stages:
        _require(isinstance(stage, dict) and set(stage) == {"id", "duration", "requires"},
                 "INVALID_INPUT", "Each stage needs an identity, duration bound and prerequisite list.")
        name = _identifier(stage["id"])
        duration = _tick(stage["duration"], "Duration upper bound")
        requires = stage["requires"]
        _require(isinstance(requires, list) and len(requires) <= len(stages),
                 "INVALID_INPUT", "Stage prerequisites must be a bounded list.")
        dependencies = [_identifier(item) for item in requires]
        _require(name not in graph and len(dependencies) == len(set(dependencies)),
                 "INVALID_INPUT", "Stage identities and prerequisite references must be unique.")
        graph[name] = duration, dependencies
    _require(all(dependency in graph for _, dependencies in graph.values() for dependency in dependencies),
             "MISSING_PREREQUISITE", "A stage references a missing prerequisite.")
    paths = {}
    visiting = set()

    def completion(name):
        if name in paths:
            return paths[name]
        _require(name not in visiting, "DEPENDENCY_CYCLE", "A prerequisite cycle leaves the order unresolved.")
        visiting.add(name)
        duration, dependencies = graph[name]
        finish, previous = max((completion(item) for item in dependencies), default=(0, ()))
        paths[name] = finish + duration, (*previous, name)
        visiting.remove(name)
        return paths[name]

    bound, witness = max(completion(name) for name in graph)
    return {
        "schema": "future-reader.critical-path/1", "units": "s", "deadline": deadline,
        "critical_path_bound": bound, "critical_path": list(witness),
        "latest_feasible_start": deadline - bound,
        "latest_safe_start": deadline - bound - margin - clock_uncertainty,
        "safety_margin": margin, "clock_uncertainty": clock_uncertainty, "equality": True,
        "stage_completion_bounds": {name: value[0] for name, value in sorted(paths.items())},
        "assumptions": ["All durations are justified upper bounds", "No extra resource conflict beyond dependencies",
                        "Times use one seconds-based reference frame"],
    }


def _program(path):
    return {"schema": _PROGRAM, "steps": [
        {"op": "Start", "state": path[0]}, *({"op": "Move", "to": node} for node in path[1:]),
        {"op": "Goal", "state": path[-1]},
    ]}


def execute_program(program, *, start="S", now=0, check_available=True):
    """Interpret the bounded typed practice program without committing effects."""
    _node(start)
    _tick(now, "Clock")
    _boolean(check_available)
    _require(isinstance(program, dict) and set(program) == {"schema", "steps"} and program["schema"] == _PROGRAM,
             "INVALID_PROGRAM", "Unsupported program schema.")
    steps = program["steps"]
    _require(isinstance(steps, list) and 2 <= len(steps) <= 32, "INVALID_PROGRAM", "Programs require 2–32 top-level pieces.")
    _require(steps[0] == {"op": "Start", "state": start}, "INVALID_PROGRAM", "Start must match the current state.")
    end = steps[-1]
    _require(isinstance(end, dict) and set(end) == {"op", "state"} and end["op"] == "Goal",
             "INVALID_PROGRAM", "Place a Goal as the final piece.")
    goal = _node(end["state"])
    path = [start]
    visited = 0

    def interpret(items, depth=0, *, active=True):
        nonlocal visited
        _require(isinstance(items, list) and len(items) <= 32 and depth <= 3,
                 "INVALID_PROGRAM", "Program contains too many nested pieces.")
        for step in items:
            visited += 1
            _require(visited <= 64 and isinstance(step, dict), "INVALID_PROGRAM", "The program exceeds 64 interpreted pieces.")
            kind = step.get("op")
            if kind == "Move" and set(step) == {"op", "to"}:
                target = _node(step["to"])
                if active:
                    _require((path[-1], target) in _EDGES, "INVALID_TRANSITION", "That move does not connect the two states.")
                    path.append(target)
            elif kind == "Check" and set(step) == {"op", "condition"} and step["condition"] == "independent_check":
                _require(not active or check_available, "PREREQUISITE_UNAVAILABLE", "The independent check is unavailable.")
            elif kind == "Repeat" and set(step) == {"op", "times", "steps"}:
                times = step["times"]
                _require(type(times) is int and 1 <= times <= 4, "INVALID_PROGRAM", "Repeat count must be 1–4.")
                for _ in range(times if active else 1):
                    interpret(step["steps"], depth + 1, active=active)
            elif kind == "If" and set(step) == {"op", "condition", "then", "else"} and step["condition"] == "independent_check":
                interpret(step["then"], depth + 1, active=active and check_available)
                interpret(step["else"], depth + 1, active=active and not check_available)
            else:
                raise ControllerError("UNSUPPORTED_PIECE", "This model supports Move, Check, Repeat and If within Start and Goal.")

    interpret(steps[1:-1])
    _require(path[-1] == goal, "GOAL_UNREACHED", "The program ends before reaching its stated goal.")
    return _route(path, now, check_available)


class Controller:
    """Transactional practice state. The host application administers grants."""

    def __init__(self, path=":memory:", session="practice"):
        self.session = _identifier(session)
        self.connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS practice_state (
                session TEXT PRIMARY KEY, model TEXT NOT NULL, version INTEGER NOT NULL,
                revision INTEGER NOT NULL, position TEXT NOT NULL, tick INTEGER NOT NULL,
                evidence INTEGER NOT NULL, epoch INTEGER NOT NULL, active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS practice_grants (
                session TEXT NOT NULL, actor TEXT NOT NULL, allowed INTEGER NOT NULL,
                PRIMARY KEY (session, actor)
            );
            CREATE TABLE IF NOT EXISTS practice_previews (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, actor TEXT NOT NULL,
                body TEXT NOT NULL, receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS practice_events (
                session TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY (session, revision)
            );
            CREATE TABLE IF NOT EXISTS practice_handoffs (
                source TEXT PRIMARY KEY, target TEXT UNIQUE NOT NULL, body TEXT NOT NULL
            );
        """)
        try:
            with self._transaction():
                self.connection.execute(
                    "INSERT OR IGNORE INTO practice_state VALUES (?, ?, 0, 0, 'S', 0, 1, 0, 1)",
                    (self.session, _DOMAIN["id"]),
                )
                self._state()
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    def close(self):
        self.connection.close()

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _state(self):
        row = self.connection.execute("SELECT * FROM practice_state WHERE session = ?", (self.session,)).fetchone()
        _require(row is not None and row["model"] == _DOMAIN["id"],
                 "INCOMPATIBLE_MODEL", "The stored session needs its original model or a verified migration.")
        return row

    def _history(self):
        return [json.loads(row["body"]) for row in self.connection.execute(
            "SELECT body FROM practice_events WHERE session = ? ORDER BY revision", (self.session,),
        )]

    def _authorized(self, actor):
        _identifier(actor)
        _require(self._state()["active"] == 1, "WRITER_FENCED", "This session was handed off and is now historical.")
        row = self.connection.execute(
            "SELECT allowed FROM practice_grants WHERE session = ? AND actor = ?", (self.session, actor),
        ).fetchone()
        _require(row is not None and row["allowed"] == 1, "FORBIDDEN", "This actor has no current practice execution grant.")

    def set_grant(self, actor, allowed):
        """Administrative host action. Never route this through an untrusted client."""
        _identifier(actor)
        _boolean(allowed)
        with self._transaction():
            _require(self._state()["active"] == 1, "WRITER_FENCED", "This session was handed off and is now historical.")
            prior = self.connection.execute(
                "SELECT allowed FROM practice_grants WHERE session = ? AND actor = ?", (self.session, actor),
            ).fetchone()
            if bool(prior and prior["allowed"]) != allowed:
                self.connection.execute(
                    "INSERT INTO practice_grants VALUES (?, ?, ?) ON CONFLICT(session, actor) DO UPDATE SET allowed=excluded.allowed",
                    (self.session, actor, int(allowed)),
                )
                self.connection.execute(
                    "UPDATE practice_state SET epoch=epoch+1, version=version+1 WHERE session = ?", (self.session,),
                )

    def set_context(self, *, now=None, check_available=None):
        """Update the synthetic clock or evidence and invalidate dependent previews."""
        with self._transaction():
            state = self._state()
            _require(state["active"] == 1, "WRITER_FENCED", "This session was handed off and is now historical.")
            tick = state["tick"] if now is None else _tick(now, "Clock")
            available = bool(state["evidence"]) if check_available is None else _boolean(check_available)
            _require(tick >= state["tick"], "CLOCK_REGRESSION", "The active practice clock cannot move backward.")
            if (tick, available) != (state["tick"], bool(state["evidence"])):
                self.connection.execute(
                    "UPDATE practice_state SET version=version+1, tick=?, evidence=? WHERE session=?",
                    (tick, int(available), self.session),
                )
        return self.pinpoint()

    def pinpoint(self):
        row = self._state()
        return {
            "schema": _CONTROLLER, "operation": "PINPOINT", "status": "EXACT", "mode": "SIMULATION",
            "state": row["position"], "label": _DOMAIN["nodes"][row["position"]], "clock": _FRAME,
            "now": row["tick"], "units": "s", "uncertainty": _DOMAIN["uncertainty"],
            "state_version": row["version"], "revision": row["revision"],
            "writable": bool(row["active"]),
            "evidence": {"independent_check": bool(row["evidence"]), "basis": "practice model"},
        }

    def _save_preview(self, actor, state, operation, request, plan):
        stamp = time.time_ns() // 1_000_000
        identifier = uuid4().hex
        # Retain committed receipts for idempotent replay. Keep the newest
        # uncommitted previews so a busy session has a bounded pending store.
        self.connection.execute("""
            DELETE FROM practice_previews
            WHERE id IN (
                SELECT id FROM practice_previews WHERE session=? AND receipt IS NULL
                ORDER BY rowid DESC LIMIT -1 OFFSET ?
            )
        """, (self.session, _MAX_OPEN_PREVIEWS - 1))
        preview = {
            "schema": _CONTROLLER, "operation": operation, "status": "PREVIEW", "mode": "SIMULATION",
            "preview_id": identifier, "actor": actor, "session": self.session, "model": _DOMAIN["id"],
            "model_digest": _digest(_DOMAIN), "state_version": state["version"], "revision": state["revision"],
            "authorization_epoch": state["epoch"], "clock": _FRAME, "now": state["tick"],
            "created_at_unix_ms": stamp, "expires_at_unix_ms": stamp + 300_000,
            "request": request, "plan": plan,
            "authorization": "PREVIEW_ONLY",
        }
        preview["digest"] = _digest(preview)
        self.connection.execute("INSERT INTO practice_previews VALUES (?, ?, ?, ?, NULL)",
                                (identifier, self.session, actor, _json(preview)))
        return preview

    def travel(self, actor, *, goal="G", path=None, program=None):
        with self._transaction():
            self._authorized(actor)
            state = self._state()
            start, now, available = state["position"], state["tick"], bool(state["evidence"])
            _node(goal)
            _require(path is None or program is None, "INVALID_INPUT", "Choose either a path or a program.")
            if path is not None:
                _require(isinstance(path, list) and 1 <= len(path) <= 4, "INVALID_INPUT", "Supply a path of one to four states.")
                for node in path:
                    _node(node)
                program = _program(path)
            elif program is None:
                candidates = opportunity(now, available, start=start, goal=goal)
                recommended = candidates["recommended"]
                if recommended is None:
                    return {"schema": _CONTROLLER, "operation": "TRAVEL", "status": "UNAVAILABLE",
                            "mode": "SIMULATION", "opportunity": candidates}
                program = _program(recommended["path"])
            route = execute_program(program, start=start, now=now, check_available=available)
            _require(route["path"][-1] == goal, "GOAL_UNREACHED", "The requested goal and the program goal differ.")
            _require(route["eligible"], route["reason"], "The route is outside the current window or lacks a prerequisite.")
            _require(len(route["path"]) > 1, "ALREADY_AT_GOAL", "The current state already matches the goal.")
            return self._save_preview(actor, state, "TRAVEL", {"goal": goal, "program": program}, route)

    def _prior(self, revision, state):
        _require(type(revision) is int and 0 <= revision < state["revision"], "NO_CHECKPOINT",
                 "Choose an earlier committed revision.")
        if revision == 0:
            return "S"
        row = self.connection.execute(
            "SELECT body FROM practice_events WHERE session=? AND revision=?", (self.session, revision),
        ).fetchone()
        _require(row is not None, "NO_CHECKPOINT", "The requested checkpoint is missing.")
        return json.loads(row["body"])["state"]

    def reverse(self, actor, *, revision=None):
        """Preview a restore of simulated position while retaining current time."""
        with self._transaction():
            self._authorized(actor)
            state = self._state()
            revision = state["revision"] - 1 if revision is None else revision
            position = self._prior(revision, state)
            plan = {"from": state["position"], "to": position, "retains_tick": state["tick"],
                    "restores_revision": revision, "history": "APPEND_ONLY", "external_effects": "NONE"}
            return self._save_preview(actor, state, "REVERSE", {"revision": revision}, plan)

    def commit(self, actor, preview_id):
        """Revalidate and commit one simulated action. Duplicate requests reuse its receipt."""
        _identifier(preview_id)
        with self._transaction():
            self._authorized(actor)
            state = self._state()
            row = self.connection.execute(
                "SELECT body, receipt FROM practice_previews WHERE id=? AND session=? AND actor=?",
                (preview_id, self.session, actor),
            ).fetchone()
            _require(row is not None, "UNKNOWN_PREVIEW", "This preview is unavailable for the current actor and session.")
            if row["receipt"] is not None:
                return json.loads(row["receipt"])
            preview = json.loads(row["body"])
            supplied = preview.pop("digest")
            _require(supplied == _digest(preview) and preview["model_digest"] == _digest(_DOMAIN),
                     "INVALID_PREVIEW", "The preview or model differs from its recorded fingerprint.")
            _require(preview["authorization_epoch"] == state["epoch"], "STALE_PERMISSION",
                     "Permission changed after preview. Create a fresh preview.")
            _require(preview["state_version"] == state["version"] and preview["revision"] == state["revision"],
                     "STALE_PREVIEW", "State, time, or evidence changed. Create a fresh preview.")
            stamp = time.time_ns() // 1_000_000
            _require(preview["created_at_unix_ms"] <= stamp <= preview["expires_at_unix_ms"], "EXPIRED_PREVIEW",
                     "The preview clock changed or expired. Create a fresh preview.")
            _require(state["revision"] < _MAX_EVENTS, "SESSION_LIMIT", "Export this record and begin a new practice session.")
            now, start, available = state["tick"], state["position"], bool(state["evidence"])
            if preview["operation"] == "TRAVEL":
                plan = execute_program(preview["request"]["program"], start=start, now=now, check_available=available)
                _require(plan["eligible"] and plan == preview["plan"], "PLAN_CHANGED", "The route no longer matches the preview.")
                position = plan["path"][-1]
                observed_tick = plan["arrival"]
            else:
                plan = preview["plan"]
                position = self._prior(preview["request"]["revision"], state)
                _require(position == plan["to"] and now == plan["retains_tick"], "PLAN_CHANGED",
                         "The checkpoint no longer matches the preview.")
                observed_tick = now
            revision, version = state["revision"] + 1, state["version"] + 1
            result = {
                "schema": _CONTROLLER, "operation": preview["operation"], "status": "COMMITTED", "mode": "SIMULATION",
                "model": _DOMAIN["id"], "actor": actor, "authorization": "GRANT_REVALIDATED",
                "preview_id": preview_id, "preview_digest": supplied, "request": preview["request"], "plan": plan,
                "parent_revision": state["revision"], "revision": revision, "state_version": version,
                "previous_state": start, "state": position, "clock": _FRAME, "previous_now": now, "now": observed_tick,
                "check_available": available, "observed_at_unix_ms": stamp, "verification": "OBSERVED_MATCH",
            }
            self.connection.execute("UPDATE practice_state SET position=?, tick=?, version=?, revision=? WHERE session=?",
                                    (position, observed_tick, version, revision, self.session))
            observed = self._state()
            _require(
                (observed["position"], observed["tick"], observed["version"], observed["revision"])
                == (position, observed_tick, version, revision),
                "OBSERVATION_DIVERGED", "The simulated write diverged and was rolled back.",
            )
            body = _json(result)
            self.connection.execute("INSERT INTO practice_events VALUES (?, ?, ?)", (self.session, revision, body))
            self.connection.execute("UPDATE practice_previews SET receipt=? WHERE id=?", (body, preview_id))
            return result

    def certify(self, *, start=None, goal="G", now=None, check_available=None):
        """Build a portable exact enumeration claim about the declared graph."""
        state = self._state()
        start = state["position"] if start is None else _node(start)
        now = state["tick"] if now is None else _tick(now, "Clock")
        available = bool(state["evidence"]) if check_available is None else _boolean(check_available)
        return route_certificate(start, goal, now, available)

    def checkpoint(self):
        """Export historical state and a pinned digest without executable previews or grants."""
        with self._transaction():
            return self._checkpoint()

    def _checkpoint(self):
        content = {"schema": _CHECKPOINT, "domain": _DOMAIN["id"], "source": self.session,
                   "state": self.pinpoint(), "history": self._history()}
        return {"content": content, "sha256": _digest(content)}

    def handoff(self, actor, target_session, *, expected_sha256, evidence_sha256=None):
        """Fence the original practice writer and transfer a pinned local checkpoint.

        Both sides use the same trusted database and transaction. Callers can
        still inspect the historical source. Imports from external databases
        remain evidence for inspection, since they cannot fence those writers.
        """
        target = _identifier(target_session)
        _require(isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
                 "INVALID_CHECKPOINT", "A previously recorded SHA-256 checkpoint pin is required.")
        _require(evidence_sha256 is None or
                 (isinstance(evidence_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is not None),
                 "INVALID_EVIDENCE", "Supply a SHA-256 digest for the recovery evidence.")
        with self._transaction():
            self._authorized(actor)
            source = self._state()
            checkpoint = self._checkpoint()
            _require(verify_checkpoint(checkpoint, expected_sha256=expected_sha256), "STALE_CHECKPOINT",
                     "The pinned checkpoint differs from the current practice history.")
            _require(opportunity(source["tick"], bool(source["evidence"]), start=source["position"])["recommended"] is not None,
                     "WINDOW_CLOSED", "Keep the current record and inspect a fresh window before transferring execution.")
            prior = self.connection.execute("SELECT 1 FROM practice_state WHERE session=?", (target,)).fetchone()
            _require(prior is None, "TARGET_EXISTS", "Choose a new target session.")
            self.connection.execute(
                "UPDATE practice_state SET active=0, version=version+1, epoch=epoch+1 WHERE session=?", (self.session,),
            )
            self.connection.execute(
                "INSERT INTO practice_state VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (target, _DOMAIN["id"], source["version"] + 1, source["revision"],
                 source["position"], source["tick"], source["evidence"]),
            )
            self.connection.execute(
                "INSERT INTO practice_events SELECT ?, revision, body FROM practice_events WHERE session=?", (target, self.session),
            )
            self.connection.execute("INSERT INTO practice_grants VALUES (?, ?, 1)", (target, actor))
            receipt = {
                "schema": "future-reader.handoff/1", "mode": "SIMULATION", "status": "TRANSFERRED",
                "source": self.session, "target": target, "actor": actor, "position": source["position"],
                "tick": source["tick"], "revision": source["revision"], "checkpoint_sha256": checkpoint["sha256"],
                "source_fenced": True, "external_effects": "NONE",
            }
            if evidence_sha256 is not None:
                receipt["recovery_evidence_sha256"] = evidence_sha256
            self.connection.execute("INSERT INTO practice_handoffs VALUES (?, ?, ?)", (self.session, target, _json(receipt)))
            return receipt


def route_certificate(start="S", goal="G", now=0, check_available=True):
    candidates = routes(start, goal, now, check_available)
    eligible = [row for row in candidates if row["eligible"]]
    best = eligible[0] if eligible else None
    cert = {
        "schema": "future-reader.route-certificate/1", "operation": "CERTIFY", "mode": "SIMULATION",
        "property": "MINIMUM_COST_WITHIN_ELIGIBLE_PRACTICE_PATHS", "model": _DOMAIN["id"],
        "model_digest": _digest(_DOMAIN), "scope": {"start": start, "goal": goal, "now": now,
                                                       "check_available": check_available, "clock": _FRAME},
        "method": "complete enumeration of simple paths with exact integer arithmetic",
        "assumptions": ["positive duration and cost", "waiting allowed", "deadline equality eligible"],
        "status": "PROVED" if best else "PROVED_INFEASIBLE_IN_SCOPE", "candidates": candidates,
        "cost": best["cost"] if best else None,
        "co_optimal": [row["path"] for row in eligible if row["cost"] == best["cost"]] if best else [],
        "invalidation": "Model, clock, starting state, goal or prerequisite availability changes",
    }
    return {"certificate": cert, "sha256": _digest(cert)}


def verify_certificate(bundle):
    """Independently recompute the scoped enumeration from a receipt's inputs."""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("certificate"), dict):
        return False
    try:
        scope = bundle["certificate"]["scope"]
        expected = route_certificate(scope["start"], scope["goal"], scope["now"], scope["check_available"])
        return _json(bundle) == _json(expected)
    except (KeyError, TypeError, ValueError):
        return False


def verify_checkpoint(bundle, *, expected_sha256=None):
    """Verify byte-equivalent content and independently replay its semantic history."""
    if not isinstance(bundle, dict) or set(bundle) != {"content", "sha256"}:
        return False
    try:
        content = bundle["content"]
        digest = bundle["sha256"]
        _require(_digest(content) == digest and (expected_sha256 is None or expected_sha256 == digest),
                 "INVALID_CHECKPOINT", "Checkpoint digest differs from the expected value.")
        _require(isinstance(content, dict) and set(content) == {"schema", "domain", "source", "state", "history"}
                 and content["schema"] == _CHECKPOINT and content["domain"] == _DOMAIN["id"],
                 "INVALID_CHECKPOINT", "Checkpoint uses an unsupported model.")
        _identifier(content["source"])
        history = content["history"]
        state = content["state"]
        _require(isinstance(history, list) and len(history) <= _MAX_EVENTS, "INVALID_CHECKPOINT", "Invalid history.")
        positions = ["S"]
        tick = 0
        version = 0
        previews = set()
        for revision, event in enumerate(history, 1):
            _require(event["model"] == _DOMAIN["id"] and event["mode"] == "SIMULATION"
                     and event["schema"] == _CONTROLLER and event["clock"] == _FRAME
                     and event["status"] == "COMMITTED" and _tick(event["revision"], "Event revision") == revision
                     and _tick(event["parent_revision"], "Parent revision") == revision - 1
                     and event["previous_state"] == positions[-1]
                     and _tick(event["previous_now"], "Event clock") >= tick
                     and _tick(event["state_version"], "State version") > version
                     and event["preview_id"] not in previews and type(event["check_available"]) is bool
                     and event["authorization"] == "GRANT_REVALIDATED" and event["verification"] == "OBSERVED_MATCH",
                     "INVALID_CHECKPOINT", "A history event violates its lineage.")
            previews.add(_identifier(event["preview_id"]))
            version = event["state_version"]
            tick = event["previous_now"]
            if event["operation"] == "TRAVEL":
                plan = execute_program(event["request"]["program"], start=positions[-1], now=tick,
                                       check_available=event["check_available"])
                _require(_json(plan) == _json(event["plan"]) and plan["eligible"] and len(plan["path"]) > 1
                         and event["request"]["goal"] == plan["path"][-1],
                         "INVALID_CHECKPOINT", "Travel diverges from the reference program.")
                tick, position = plan["arrival"], plan["path"][-1]
            elif event["operation"] == "REVERSE":
                prior = event["request"]["revision"]
                _require(type(prior) is int and 0 <= prior < revision - 1, "INVALID_CHECKPOINT", "Invalid restoration.")
                position = positions[prior]
                expected_plan = {"from": positions[-1], "to": position, "retains_tick": tick,
                                 "restores_revision": prior, "history": "APPEND_ONLY", "external_effects": "NONE"}
                _require(_json(event["plan"]) == _json(expected_plan),
                         "INVALID_CHECKPOINT", "Restore diverges from its referenced state.")
            else:
                return False
            _require(_tick(event["now"], "Observed clock") == tick and event["state"] == position,
                     "INVALID_CHECKPOINT", "Event state diverged.")
            positions.append(position)
        return (
            state["schema"] == _CONTROLLER and state["mode"] == "SIMULATION" and state["clock"] == _FRAME
            and state["operation"] == "PINPOINT" and state["status"] == "EXACT"
            and state["units"] == "s" and state["label"] == _DOMAIN["nodes"][positions[-1]]
            and _tick(state["revision"], "Checkpoint revision") == len(history) and state["state"] == positions[-1]
            and _tick(state["now"], "Checkpoint clock") >= tick
            and _tick(state["state_version"], "State version") >= version
            and type(state["writable"]) is bool
            and type(state["evidence"]["independent_check"]) is bool
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def conformance_fixture():
    """Generate browser practice fixtures from the real Python runtime."""
    cases = [("S", "G", 0, True), ("S", "G", 5, True), ("S", "G", 6, True),
             ("S", "G", 6, False), ("S", "G", 7, True), ("S", "G", 17, True),
             ("B", "G", 2, True), ("A", "G", 3, True), ("G", "G", 13, True)]
    trials = [{"start": start, "goal": goal, "now": now, "check_available": available,
               "result": routes(start, goal, now, available)} for start, goal, now, available in cases]
    with Controller() as controller:
        controller.set_grant("practice", True)
        initial = controller.pinpoint()
        travel = controller.travel("practice")
        moved = controller.commit("practice", travel["preview_id"])
        restored = controller.commit("practice", controller.reverse("practice")["preview_id"])
    return {
        "schema": "future-reader.conformance/1", "domain": domain(), "routes": trials,
        "session": {
            "initial": {key: initial[key] for key in ("state", "now", "revision")},
            "travel": {key: moved[key] for key in ("state", "now", "revision")},
            "reverse": {key: restored[key] for key in ("state", "now", "revision")},
            "certificate": route_certificate()["certificate"],
            "program": travel["request"]["program"], "program_route": travel["plan"],
        },
    }


if __name__ == "__main__":
    print(json.dumps(conformance_fixture(), indent=2))
