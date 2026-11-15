"""Exact early commitment in a bounded synthetic model of unresolved checks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    id: str
    score: int
    eligible: bool = True


@dataclass(frozen=True)
class Effect:
    delta: tuple[int, ...]
    veto: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Check:
    opens: int
    closes: int
    effects: tuple[Effect, ...]


@dataclass(frozen=True)
class Delivery:
    tick: int
    effect: int


@dataclass(frozen=True)
class Decision:
    status: str
    tick: int | None
    winners: tuple[str, ...] | None


@dataclass(frozen=True)
class Model:
    routes: tuple[Route, ...]
    checks: tuple[Check, ...]
    deadline: int
    reserve: int = 0
    clock_margin: int = 0

    @property
    def cutoff(self):
        return self.deadline - self.reserve - self.clock_margin

    def __post_init__(self):
        if not isinstance(self.routes, tuple) or not 1 <= len(self.routes) <= 64:
            raise ValueError("Use a tuple of 1 through 64 routes.")
        ids = set()
        for route in self.routes:
            if (not isinstance(route, Route) or type(route.id) is not str or not route.id.strip()
                    or route.id in ids or type(route.score) is not int or type(route.eligible) is not bool):
                raise ValueError("Each route needs a unique ID, an integer score and boolean eligibility.")
            ids.add(route.id)
        if any(type(tick) is not int or tick < 0 for tick in (self.deadline, self.reserve, self.clock_margin)):
            raise ValueError("Use nonnegative integer timing parameters.")
        if not isinstance(self.checks, tuple) or len(self.checks) > 64:
            raise ValueError("Use a tuple of up to 64 checks.")
        for check in self.checks:
            if (not isinstance(check, Check) or type(check.opens) is not int or type(check.closes) is not int
                    or not 0 <= check.opens <= check.closes or not isinstance(check.effects, tuple)
                    or not 1 <= len(check.effects) <= 16):
                raise ValueError("Each check needs a valid window and 1 through 16 alternative effects.")
            for effect in check.effects:
                if (not isinstance(effect, Effect) or not isinstance(effect.delta, tuple)
                        or len(effect.delta) != len(ids) or any(type(score) is not int for score in effect.delta)
                        or not isinstance(effect.veto, frozenset) or not effect.veto <= ids):
                    raise ValueError("An effect needs one integer delta per route and a set of known veto IDs.")


class Controller:
    """Evaluate batches ordered by tick; deliver all events through each tick before advancing.

    Outcome indices refer to the public check catalog. A check can deliver at most once.
    Absence is always possible, check choices are independent, existing vetoes are permanent,
    and decisions preserve the complete set of highest-scoring eligible routes.
    """

    def __init__(self, model: Model, *, early: bool = True):
        self.model = model
        self.early = early
        self.tick = -1
        self.seen: set[int] = set()
        self.scores = [route.score for route in model.routes]
        self.eligible = {route.id for route in model.routes if route.eligible}
        self.result = Decision("NO_SAFE_WINDOW", None, None) if model.cutoff < 0 else None

    def advance(self, tick: int, arrivals: tuple[tuple[int, Delivery], ...] = ()) -> Decision:
        """Accept an atomic batch, then return WAIT or a final decision."""
        if self.result is not None:
            raise ValueError("The controller already has a final decision.")
        if type(tick) is not int or not self.tick < tick <= self.model.cutoff:
            raise ValueError("Advance to a later tick no later than the safe cutoff.")
        pending_arrivals = {}
        for check_id, delivery in arrivals:
            if (type(check_id) is not int or not 0 <= check_id < len(self.model.checks)
                    or check_id in self.seen or check_id in pending_arrivals
                    or not isinstance(delivery, Delivery)):
                raise ValueError("Each check can deliver at most once.")
            check = self.model.checks[check_id]
            if (type(delivery.tick) is not int or not self.tick < delivery.tick <= tick
                    or not check.opens <= delivery.tick <= check.closes
                    or type(delivery.effect) is not int or not 0 <= delivery.effect < len(check.effects)):
                raise ValueError("Delivery is outside the current batch or its declared check.")
            pending_arrivals[check_id] = check.effects[delivery.effect]

        self.tick = tick
        for check_id, effect in pending_arrivals.items():
            self.seen.add(check_id)
            self.eligible.difference_update(effect.veto)
            for idx, delta in enumerate(effect.delta):
                self.scores[idx] += delta
        model = self.model
        active = [(idx, route.id) for idx, route in enumerate(model.routes) if route.id in self.eligible]
        best = max((self.scores[idx] for idx, _ in active), default=None)
        leaders = [(idx, name) for idx, name in active if self.scores[idx] == best]
        unresolved = [check.effects for idx, check in enumerate(model.checks)
                      if idx not in self.seen and tick < check.closes and check.opens <= model.cutoff]
        if tick == model.cutoff or not unresolved or (self.early and self._invariant(leaders, active, unresolved)):
            status = "AT_CUTOFF" if tick == model.cutoff else "CERTIFIED"
            self.result = Decision(status, tick, tuple(name for _, name in leaders))
            return self.result
        return Decision("WAIT", tick, None)

    def _invariant(self, leaders, active, unresolved):
        # Until a check resolves, its absent option contributes zero and vetoes nothing.
        if not leaders:
            return True
        if any(name in effect.veto for _, name in leaders for options in unresolved for effect in options):
            return False
        leader_indices = {idx for idx, _ in leaders}
        leader = leaders[0][0]
        for idx, name in active:
            if idx == leader:
                continue
            smallest = self.scores[leader] - self.scores[idx]
            largest = smallest
            for options in unresolved:
                # The comparison is needed only in completions where the rival survives.
                gaps = [0] + [effect.delta[leader] - effect.delta[idx]
                              for effect in options if name not in effect.veto]
                smallest += min(gaps)
                largest += max(gaps)
            if idx in leader_indices:
                if smallest != 0 or largest != 0:
                    return False
            elif smallest <= 0:
                # Equality would add a new winner and change the decision.
                return False
        return True
