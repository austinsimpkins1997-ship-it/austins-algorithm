"""Bounded weighted Boolean optimization and a stepwise ideal Grover simulation."""

from __future__ import annotations

import bisect
from copy import deepcopy
import math
import random
from collections.abc import Callable
from typing import Any
from unicodedata import category

from .classical_constraint import _clause_masks
from .constraints import BooleanConstraint
from .core import _bounded_int
from .grover_search import MAX_AUDIT_SEARCH_WIDTH

MAX_VARIABLES = MAX_AUDIT_SEARCH_WIDTH
MAX_WEIGHT = 1_000_000
MAX_SHOTS = 64
MAX_QUERIES = 256
MAX_ROUNDS = 8


def _name(value: object, label: str, max_length: int) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{label} must be text of at most {max_length} characters.")
    name = value.strip()
    if not name or any(category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError(f"{label} must be nonempty text without control characters.")
    return name


def problem_from_document(document: object) -> dict[str, Any]:
    """Validate and copy a weighted CNF problem.

    Variables and their nonnegative weights are in q0-first order. Signed
    one-based literals follow BooleanConstraint, and displayed bits are MSB-first.
    Empty CNF is true. An empty clause makes the problem infeasible.
    """
    if not isinstance(document, dict) or set(document) != {"name", "variables", "clauses"}:
        raise ValueError("A weighted problem requires exactly name, variables and clauses.")
    name = _name(document["name"], "Problem name", 120)
    variables = document["variables"]
    if not isinstance(variables, list) or not 1 <= len(variables) <= MAX_VARIABLES:
        raise ValueError(f"Provide 1 to {MAX_VARIABLES} variables.")
    canonical_variables = []
    used_names = set()
    for index, variable in enumerate(variables):
        if not isinstance(variable, dict) or set(variable) != {"name", "cost"}:
            raise ValueError(f"Variable q{index} requires exactly name and cost.")
        label = _name(variable["name"], f"Variable q{index} name", 60)
        if label.casefold() in used_names:
            raise ValueError(f"Variable names must be unique: {label}.")
        used_names.add(label.casefold())
        cost = _bounded_int(variable["cost"], 0, MAX_WEIGHT, f"Variable q{index} cost")
        canonical_variables.append({"name": label, "cost": cost})
    constraint = BooleanConstraint(len(variables), document["clauses"])
    return {
        "name": name, "variables": canonical_variables,
        "clauses": [list(clause) for clause in constraint.clauses],
    }


def _problem_parts(problem: dict[str, Any]) -> tuple[BooleanConstraint, tuple[int, ...]]:
    return (
        BooleanConstraint(len(problem["variables"]), problem["clauses"]),
        tuple(variable["cost"] for variable in problem["variables"]),
    )


def _candidate_cost(bits: str, weights: tuple[int, ...]) -> int:
    return sum(weight for bit, weight in zip(reversed(bits), weights) if bit == "1")


def exact_minimum(problem: object) -> dict[str, Any]:
    """Find the exact minimum by enumerating the declared bounded search space.

    Return all minimum-cost assignments in ascending unsigned order and use the
    first tied assignment as the canonical reference witness.
    """
    canonical = problem_from_document(problem)
    constraint, weights = _problem_parts(canonical)
    assignments = 1 << constraint.qubits
    minimum = None
    ties: list[str] = []
    feasible_count = 0
    for index in range(assignments):
        bits = format(index, f"0{constraint.qubits}b")
        if not constraint.matches(bits):
            continue
        feasible_count += 1
        cost = _candidate_cost(bits, weights)
        if minimum is None or cost < minimum:
            minimum, ties = cost, [bits]
        elif cost == minimum:
            ties.append(bits)
    return {
        "status": "OPTIMUM" if ties else "INFEASIBLE",
        "bits": ties[0] if ties else None, "cost": minimum, "ties": ties,
        "feasible_count": feasible_count, "assignments_checked": assignments,
    }


def _oracle_basis(
    constraint: BooleanConstraint, weights: tuple[int, ...],
) -> tuple[tuple[str, bool, int], ...]:
    """Build the ideal diagonal oracle lookup independently of string verification."""
    clauses = _clause_masks(constraint)
    all_bits = (1 << constraint.qubits) - 1
    return tuple(
        (
            format(index, f"0{constraint.qubits}b"),
            all(positive & index or negative & (all_bits ^ index) for positive, negative in clauses),
            sum(weight for bit, weight in enumerate(weights) if index & (1 << bit)),
        )
        for index in range(all_bits + 1)
    )


def _simulation(
    basis: tuple[tuple[str, bool, int], ...], incumbent: int | None, rounds: int,
) -> dict[str, Any]:
    states = [
        {"bits": bits, "feasible": feasible, "cost": cost,
         "marked": feasible and (incumbent is None or cost < incumbent)}
        for bits, feasible, cost in basis
    ]
    marked = [index for index, row in enumerate(states) if row["marked"]]
    size = len(basis)
    amplitudes = [1.0 / math.sqrt(size)] * size
    frames = []

    def frame(stage: str, round_index: int) -> None:
        probabilities = [amplitude * amplitude for amplitude in amplitudes]
        total = math.fsum(probabilities)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-10):
            raise RuntimeError("The simulated state is not normalized.")
        frames.append({
            "stage": stage, "round": round_index, "amplitudes": amplitudes.copy(),
            "probabilities": probabilities,
            "marked_probability": min(1.0, math.fsum(probabilities[index] for index in marked)),
        })

    frame("prepare", 0)
    for round_index in range(1, rounds + 1):
        for index in marked:
            amplitudes[index] = -amplitudes[index]
        frame("oracle", round_index)
        twice_mean = 2 * math.fsum(amplitudes) / size
        amplitudes = [twice_mean - amplitude for amplitude in amplitudes]
        frame("diffusion", round_index)
    return {
        "frames": frames, "states": states, "incumbent": incumbent, "rounds": rounds,
        "marked_count": len(marked), "basis_order": "MSB-first display, q0 is rightmost",
    }


def preview(problem: object, *, incumbent: int | None = None, rounds: int = 1) -> dict[str, Any]:
    """Inspect uniform preparation, phase marking and diffusion before measurement."""
    canonical = problem_from_document(problem)
    constraint, weights = _problem_parts(canonical)
    rounds = _bounded_int(rounds, 0, MAX_ROUNDS, "Grover rounds")
    if incumbent is not None:
        incumbent = _bounded_int(incumbent, 0, sum(weights) + 1, "Incumbent cost")
    return _simulation(_oracle_basis(constraint, weights), incumbent, rounds)


def _sample(probabilities: list[float], rng: random.Random) -> int:
    cumulative = []
    total = 0.0
    for probability in probabilities:
        total += probability
        cumulative.append(total)
    # Right insertion skips a zero-weight state when the draw lies on its boundary.
    threshold = min(rng.random() * total, math.nextafter(total, 0.0))
    return bisect.bisect_right(cumulative, threshold)


class SearchSession:
    """An ideal CPU simulation with shared budgets across feasibility and improvement."""

    def __init__(
        self, problem: object, *, seed: int = 7, max_shots: int = 16, max_queries: int = 64,
        max_rounds: int = 4, prove: bool = True,
    ) -> None:
        if type(prove) is not bool:
            raise ValueError("Prove optimum must be a boolean.")
        self._settings = {
            "seed": _bounded_int(seed, 0, 2**32 - 1, "Seed"),
            "max_shots": _bounded_int(max_shots, 1, MAX_SHOTS, "Shot budget"),
            "max_queries": _bounded_int(max_queries, 0, MAX_QUERIES, "Oracle query budget"),
            "max_rounds": _bounded_int(max_rounds, 0, MAX_ROUNDS, "Rounds per shot"),
            "prove": prove,
        }
        self._problem = problem_from_document(problem)
        self._constraint, self._weights = _problem_parts(self._problem)
        self._basis = _oracle_basis(self._constraint, self._weights)
        self._reference = exact_minimum(self._problem) if prove else None
        self._events: list[dict[str, Any]] = []
        self._best: dict[str, Any] | None = None
        self._queries = 0
        self._misses = 0

    @property
    def settings(self) -> dict[str, Any]:
        return self._settings.copy()

    @property
    def problem(self) -> dict[str, Any]:
        return deepcopy(self._problem)

    @property
    def best(self) -> dict[str, Any] | None:
        return self._best.copy() if self._best is not None else None

    @property
    def shots_used(self) -> int:
        return len(self._events)

    @property
    def queries_used(self) -> int:
        return self._queries

    @property
    def shots_remaining(self) -> int:
        return self._settings["max_shots"] - self.shots_used

    @property
    def queries_remaining(self) -> int:
        return self._settings["max_queries"] - self.queries_used

    @property
    def termination(self) -> str | None:
        if self._reference is not None:
            if self._reference["status"] == "INFEASIBLE":
                return "PROVEN_INFEASIBLE"
            if self._best is not None and self._best["cost"] == self._reference["cost"]:
                return "PROVEN_OPTIMUM"
        return "SHOT_LIMIT" if self.shots_remaining == 0 else None

    @property
    def status(self) -> str:
        return {
            None: "RUNNING", "PROVEN_INFEASIBLE": "INFEASIBLE", "PROVEN_OPTIMUM": "OPTIMUM",
            "SHOT_LIMIT": "BUDGET_EXHAUSTED",
        }[self.termination]

    def _random(self, purpose: int) -> random.Random:
        # Each shot has separate schedule and measurement draws. Replaying the
        # recorded round choices restores continuation without opaque RNG state.
        shot_seed = (self._settings["seed"] << 8) | (self.shots_used << 1) | purpose
        return random.Random(shot_seed)

    def preview(self, rounds: int = 1) -> dict[str, Any]:
        """Simulate a proposed round count without consuming budget."""
        rounds = _bounded_int(rounds, 0, self._settings["max_rounds"], "Grover rounds")
        incumbent = self._best["cost"] if self._best is not None else None
        return _simulation(self._basis, incumbent, rounds)

    def measure(self, rounds: int) -> dict[str, Any]:
        """Measure one independent shot and verify its assignment and cost."""
        if self.status != "RUNNING":
            raise ValueError("This search has finished. Start a new search to measure again.")
        rounds = _bounded_int(rounds, 0, self._settings["max_rounds"], "Grover rounds")
        if rounds > self.queries_remaining:
            raise ValueError(f"Choose at most {self.queries_remaining} rounds with the remaining query budget.")
        trace = self.preview(rounds)
        index = _sample(trace["frames"][-1]["probabilities"], self._random(1))
        state = trace["states"][index]
        bits = state["bits"]
        before = self.best
        feasible = self._constraint.matches(bits)
        cost = _candidate_cost(bits, self._weights)
        accepted = feasible and (before is None or cost < before["cost"])
        if (feasible, cost, accepted) != (state["feasible"], state["cost"], state["marked"]):
            raise RuntimeError("The simulated oracle and the independent candidate verifier disagree.")
        after = {"bits": bits, "cost": cost} if accepted else before
        event = {
            "shot": self.shots_used + 1, "rounds": rounds, "bits": bits,
            "feasible": feasible, "cost": cost, "accepted": accepted,
            "incumbent_before": before, "incumbent_after": after,
            "shots_used": self.shots_used + 1, "queries_used": self._queries + rounds,
            "trace": trace,
        }
        self._events.append(event)
        self._best = after
        self._queries += rounds
        self._misses = 0 if accepted else self._misses + 1
        return deepcopy(event)

    def auto_step(self) -> dict[str, Any]:
        """Choose one affordable round count without consulting the marked count."""
        choices = math.ceil(min(
            1.2**self._misses, math.sqrt(len(self._basis)),
            self._settings["max_rounds"] + 1, self.queries_remaining + 1,
        ))
        return self.measure(self._random(0).randrange(choices))

    def run(
        self, *, should_stop: Callable[[], bool] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run the automatic schedule and return the report at the next stop boundary."""
        while self.status == "RUNNING" and (should_stop is None or not should_stop()):
            event = self.auto_step()
            if on_event is not None:
                on_event(event)
        return self.report()

    def report(self) -> dict[str, Any]:
        """Return deterministic data for replay and a separate exact reference if requested."""
        return {
            "schema_version": 1, "problem": self.problem, "settings": self.settings,
            "status": self.status, "termination": self.termination, "best": self.best,
            "reference": deepcopy(self._reference), "events": deepcopy(self._events),
            "shots_used": self.shots_used, "queries_used": self.queries_used,
            "shots_remaining": self.shots_remaining, "queries_remaining": self.queries_remaining,
            "execution_mode": "IDEAL_CLASSICAL_STATEVECTOR_SIMULATION",
            "oracle_implementation": "CLASSICALLY_ENUMERATED_DIAGONAL_PHASE_ORACLE",
            "provider_job_submitted": False, "quantum_hardware_used": False,
            "work": {
                "oracle_setup_assignments": len(self._basis),
                "oracle_threshold_checks_for_measured_shots": len(self._basis) * self.shots_used,
                "exact_reference_assignments": self._reference["assignments_checked"] if self._reference else 0,
                "independent_candidate_checks": self.shots_used,
            },
        }
