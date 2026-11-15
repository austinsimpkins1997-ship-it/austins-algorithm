"""Synthetic workloads and an enumerating reference for the exact timing controller."""

from collections import Counter
from hashlib import sha256
from itertools import product
import json
from random import Random

from .codec import digest
from .exact_timing import Check, Controller, Delivery, Effect, Model, Route


DEFAULT_SEEDS = (20261113, 20261114, 4294967311)


def _oracle_choice(model, effects):
    authorized = [i for i, route in enumerate(model.routes)
                  if route.eligible and all(route.id not in effect.veto for effect in effects)]
    utilities = {i: model.routes[i].score + sum(effect.delta[i] for effect in effects) for i in authorized}
    return tuple(model.routes[i].id for i in authorized if utilities[i] == max(utilities.values()))


def reference(model, trace):
    """Return the realized cutoff result and first provably invariant integer tick."""
    if model.cutoff < 0:
        return None, None, 0
    realized = _oracle_choice(model, [model.checks[i].effects[value.effect]
                                     for i, value in enumerate(trace)
                                     if value is not None and value.tick <= model.cutoff])
    enumerated = 0
    for tick in range(model.cutoff + 1):
        choices = []
        for check, delivery in zip(model.checks, trace):
            if delivery is not None and delivery.tick <= tick:
                choices.append((check.effects[delivery.effect],))
            elif max(tick + 1, check.opens) <= min(check.closes, model.cutoff):
                choices.append((None, *check.effects))
        # Build completions without score bounds and compare every winner's identity.
        for world in product(*choices):
            enumerated += 1
            if _oracle_choice(model, [effect for effect in world if effect is not None]) != realized:
                break
        else:
            return realized, tick, enumerated
    raise RuntimeError("The cutoff must agree with its fully observed result.")


def _play(model, trace, *, early):
    controller = Controller(model, early=early)
    if controller.result is not None:
        return controller.result, 0
    events = {0: [], model.cutoff: []}
    for i, (check, delivery) in enumerate(zip(model.checks, trace)):
        if check.opens <= model.cutoff:
            events.setdefault(min(check.closes, model.cutoff), [])
        if delivery is not None and delivery.tick <= model.cutoff:
            events.setdefault(delivery.tick, []).append((i, delivery))
    for evaluations, tick in enumerate(sorted(events), 1):
        result = controller.advance(tick, tuple(events[tick]))
        if result.status != "WAIT":
            return result, evaluations
    raise RuntimeError("The simulation did not visit the cutoff.")


def _exhaustive_cases():
    # Full Cartesian product of declared variants, including cutoff arrivals and lateness.
    a, b = "A", "B"
    for gap, delta, veto, cutoff in product(range(-2, 3), range(-2, 3),
                                            (frozenset(), frozenset({a}), frozenset({b})), range(4)):
        checks = (Check(0, 2, (Effect((delta, 0), veto),)),
                  Check(1, 3, (Effect((0, -1)), Effect((-1, 0), frozenset({b})))))
        model = Model((Route(a, gap), Route(b, 0)), checks, cutoff)
        for trace in product((None, *(Delivery(t, 0) for t in range(3))),
                             (None, *(Delivery(t, i) for t in range(1, 4) for i in range(2)))):
            yield model, trace


def _generated(rng, count):
    groups = ("mixed", "shared_shift", "hazards", "close_only")
    for n in range(count):
        mode = groups[n % len(groups)]
        routes = tuple(Route(str(i), rng.randrange(-4, 5), rng.randrange(8) > 0) for i in range(rng.randrange(2, 6)))
        checks, trace = [], []
        for _ in range(rng.randrange(1, 5)):
            opens = rng.randrange(0, 9)
            closes = opens + rng.randrange(0, 6)
            options = []
            for _ in range(rng.randrange(1, 4)):
                if mode == "shared_shift":
                    delta = (rng.randrange(-8, 9),) * len(routes)
                else:
                    delta = tuple(rng.randrange(-3, 4) for _ in routes)
                veto = frozenset(route.id for route in routes if mode == "hazards" and rng.randrange(4) == 0)
                options.append(Effect(delta, veto))
            checks.append(Check(opens, closes, tuple(options)))
            trace.append(None if mode == "close_only" or rng.randrange(4) == 0 else
                         Delivery(rng.randrange(opens, closes + 1), rng.randrange(len(options))))
        yield mode, Model(routes, tuple(checks), rng.randrange(6, 15), rng.randrange(3), rng.randrange(3)), tuple(trace)


def _measure(rows):
    counts = Counter({key: 0 for key in (
        "cases", "matched_cutoff_winner_sets", "matched_earliest_ticks", "strictly_earlier",
        "fixed_wait_ticks", "certified_wait_ticks", "cutoff_wait_ticks",
        "fixed_decision_checks", "certified_decision_checks", "reference_worlds_examined")})
    trace_hash = sha256()
    for model, trace in rows:
        expected, earliest, worlds = reference(model, trace)
        exact, checks = _play(model, trace, early=True)
        fixed, fixed_checks = _play(model, trace, early=False)
        if not (exact.winners == fixed.winners == expected and exact.tick == earliest
                and (model.cutoff < 0 or earliest <= fixed.tick <= model.cutoff)):
            raise RuntimeError(f"Independent timing mismatch: {model!r}, {trace!r}")
        if model.cutoff < 0:
            raise RuntimeError("Measured cases must have a nonnegative cutoff.")
        trace_hash.update(json.dumps([
            [(route.id, route.score, route.eligible) for route in model.routes],
            [model.deadline, model.reserve, model.clock_margin],
            [(check.opens, check.closes, [(effect.delta, sorted(effect.veto)) for effect in check.effects])
             for check in model.checks],
            [None if value is None else (value.tick, value.effect) for value in trace],
            [expected, earliest, fixed.tick],
        ], separators=(",", ":")).encode("utf-8") + b"\n")
        counts.update({"cases": 1, "matched_cutoff_winner_sets": 1, "matched_earliest_ticks": 1,
                       "strictly_earlier": int(earliest < fixed.tick),
                       "fixed_wait_ticks": fixed.tick, "certified_wait_ticks": earliest,
                       "cutoff_wait_ticks": model.cutoff, "fixed_decision_checks": fixed_checks,
                       "certified_decision_checks": checks, "reference_worlds_examined": worlds})
    return {**counts, "input_and_result_sha256": trace_hash.hexdigest()}


def run_timing(*, cases_per_seed=2048):
    if type(cases_per_seed) is not int or not 4 <= cases_per_seed <= 10_000 or cases_per_seed % 4:
        raise ValueError("Use 4 through 10,000 generated cases per seed, in a multiple of four.")
    exhaustive = _measure(_exhaustive_cases())
    cohorts = []
    for seed in DEFAULT_SEEDS:
        generated = {}
        for mode, model, trace in _generated(Random(seed), cases_per_seed):
            generated.setdefault(mode, []).append((model, trace))
        cohorts.extend({"seed": seed, "cohort": mode, **_measure(rows)} for mode, rows in generated.items())
    report = {"schema": "future-reader.exact-timing/1", "exhaustive": exhaustive, "generated": cohorts,
              "fixed_policy": "Commit when all possible relevant deliveries have closed or by the safe cutoff.",
              "comparison_unit": "Complete ordered winner set and earliest safe integer tick, per trace.",
              "reference": "Tick-by-tick enumeration of independent admissible effects, including absence.",
              "early_policy": "Ignore future deliveries until observed; test unconditional invariance over remaining effects."}
    return {**report, "sha256": digest(report)}
