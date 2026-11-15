"""Prepare reviewed decisions for bounded offline lookup."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from time import monotonic_ns

from .decision_review import review_decision
from .workbench_project import _json_bytes

POLICY_SCHEMA = "AustinDecisionPolicy/1"
MAX_CASES = 16


def _hash(value):
    return sha256(_json_bytes(value)).hexdigest()


def _check_cases(cases):
    if (not isinstance(cases, dict) or not 1 <= len(cases) <= MAX_CASES
            or any(not isinstance(key, str) or not key or len(key) > 80
                   or any(not char.isprintable() for char in key) for key in cases)):
        raise ValueError("Use one to sixteen explicitly named cases.")


def prepare_policy(cases):
    _check_cases(cases)
    rows = {}
    for case, source in cases.items():
        document = deepcopy(source)
        result = review_decision(document)
        rows[case] = {"document": document, "review": result}
    payload = {"schema": POLICY_SCHEMA, "cases": rows}
    return {**payload, "policy_sha256": _hash(payload)}


def _verify_policy(policy, pinned):
    if (not isinstance(policy, dict) or set(policy) != {"schema", "cases", "policy_sha256"}
            or policy["schema"] != POLICY_SCHEMA or pinned != policy["policy_sha256"]):
        raise ValueError("Choose the expected version 1 prepared-policy digest.")
    if _hash({"schema": POLICY_SCHEMA, "cases": policy["cases"]}) != pinned:
        raise ValueError("The prepared policy does not match the pinned digest.")
    _check_cases(policy["cases"])
    for row in policy["cases"].values():
        if (not isinstance(row, dict) or set(row) != {"document", "review"}
                or _json_bytes(review_decision(row["document"])) != _json_bytes(row["review"])):
            raise ValueError("A prepared policy case disagrees with exact review.")
    return deepcopy(policy["cases"])


class PreparedDecisions:
    """A process-local snapshot. Initialization fully replays every stored case."""

    def __init__(self, policy, expected_sha256):
        self._rows = {key: row["review"] for key, row in _verify_policy(policy, expected_sha256).items()}
        self.policy_sha256 = expected_sha256

    def lookup(self, case, *, input_sha256, policy_sha256, deadline_ns, clock=monotonic_ns):
        """Return a detached proposal if the current version matches and time remains."""
        before = clock()

        def held(reason):
            return {"status": "HELD", "reason": reason, "result": None}

        if type(deadline_ns) is not int or before >= deadline_ns:
            return held("The declared deadline has passed or is invalid.")
        if policy_sha256 != self.policy_sha256:
            return held("The expected prepared policy changed.")
        row = self._rows.get(case) if isinstance(case, str) else None
        if row is None or row["input_sha256"] != input_sha256:
            return held("This case or input revision needs preparation.")
        result = deepcopy(row)
        after = clock()
        if after < before or after >= deadline_ns:
            return held("The monotonic timing check failed or the deadline passed during lookup.")
        return {"status": result["status"], "reason": "Verified prepared case", "result": result}


def benchmark_prepared(cases, *, repeats=32):
    """Report local operation timings against fresh optimization of the same inputs."""
    if type(repeats) is not int or not 1 <= repeats <= 256:
        raise ValueError("Use one to 256 repeats.")
    started = monotonic_ns()
    bundle = prepare_policy(cases)
    digest = bundle["policy_sha256"]
    prepared = PreparedDecisions(bundle, digest)
    preparation_ns = monotonic_ns() - started
    samples = {"prepared_lookup_ns": [], "fresh_review_ns": []}
    matches = 0
    for number in range(repeats):
        for key, document in cases.items():
            input_pin = bundle["cases"][key]["review"]["input_sha256"]

            def lookup():
                return prepared.lookup(key, input_sha256=input_pin, policy_sha256=digest, deadline_ns=monotonic_ns() + 1_000_000_000)["result"]

            methods = (("prepared_lookup_ns", lookup), ("fresh_review_ns", lambda: review_decision(document)))
            values = {}
            for name, operation in methods[::1 if number % 2 == 0 else -1]:
                start = monotonic_ns()
                values[name] = operation()
                samples[name].append(monotonic_ns() - start)
            if values["prepared_lookup_ns"] != values["fresh_review_ns"]:
                raise RuntimeError("Prepared result disagrees with a fresh review.")
            matches += 1

    def summarize(observations):
        ordered = sorted(observations)
        count = len(ordered)
        return {"p50": ordered[(count - 1) // 2], "p95": ordered[(95 * count + 99) // 100 - 1],
                "p99": ordered[(99 * count + 99) // 100 - 1], "max": ordered[-1]}

    return {"origin": "LOCAL_BENCHMARK", "matches": matches, "prepared_solver_calls_per_lookup": 0,
            "preparation_and_replay_ns": preparation_ns, "timing_ns": {name: summarize(rows) for name, rows in samples.items()},
            "scope": "Same process, monotonic clock, no network or device output. The input digest is prepared outside the lookup."}
