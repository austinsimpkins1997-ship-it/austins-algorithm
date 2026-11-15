"""Bounded worker telemetry and checkpoint recovery in the practice model."""

from copy import deepcopy

from .codec import digest
from .controller import Controller, ControllerError


SCHEMA = "future-reader.telemetry/1"
_MAX_MS = (1 << 53) - 1
_LIMITS = {
    "heartbeat_timeout_ms": 2500, "stall_timeout_ms": 4500,
    "delivery_limit_ms": 750, "monitor_freshness_ms": 1000, "deadline_mono_ms": None,
}
_KINDS = {"PRACTICE", "SIMULATED_STATEVECTOR", "REPORTED_HARDWARE_OUTCOME"}
_WORKER_STATES = {"IDLE", "BUSY", "FAILED"}


def _bounded_int(value, name):
    if type(value) is not int or not 0 <= value <= _MAX_MS:
        raise ValueError(f"{name} must be a nonnegative, exactly representable integer")
    return value


def _name(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isascii() or not value.isprintable():
        raise ValueError(f"{name} must be a short ASCII identifier")
    return value


def _shape(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError(f"Invalid {name} fields")


def _sample(value, now):
    _shape(value, "schema kind session worker boot sequence event arrival worker_mono_ms transport payload", "telemetry sample")
    if value["schema"] != SCHEMA or not isinstance(value["kind"], str) or value["kind"] not in _KINDS:
        raise ValueError("Unsupported telemetry schema or observation kind")
    for key in ("session", "worker", "boot", "transport"):
        _name(value[key], key)
    _bounded_int(value["sequence"], "Sequence")
    _bounded_int(value["worker_mono_ms"], "Worker monotonic timestamp")
    _shape(value["event"], "clock time_ms uncertainty_ms", "event clock")
    _shape(value["arrival"], "clock time_ms uncertainty_ms receiver_clock mono_ms", "arrival clock")
    for key in ("event", "arrival"):
        stamp = value[key]
        _name(stamp["clock"], "Wall clock")
        _bounded_int(stamp["time_ms"], "Wall timestamp")
        _bounded_int(stamp["uncertainty_ms"], "Synchronization uncertainty")
    _name(value["arrival"]["receiver_clock"], "Receiver monotonic clock")
    if _bounded_int(value["arrival"]["mono_ms"], "Arrival timestamp") > now:
        raise ValueError("Arrival cannot be later than the receiver evaluation")
    payload = value["payload"]
    if value["kind"] == "PRACTICE":
        _shape(payload, "state revision state_version progress worker_state", "practice payload")
        if payload["state"] not in ("S", "A", "B", "G") or payload["worker_state"] not in tuple(_WORKER_STATES):
            raise ValueError("Unknown practice state")
        _bounded_int(payload["revision"], "State revision")
        _bounded_int(payload["state_version"], "State version")
        _bounded_int(payload["progress"], "Work progress")
    else:
        _shape(payload, "basis backend shots result_ref", "model or hardware payload")
        _name(payload["basis"], "Measurement basis")
        _name(payload["backend"], "Backend")
        _name(payload["result_ref"], "Result reference")
        shots = payload["shots"]
        if value["kind"] == "REPORTED_HARDWARE_OUTCOME":
            if _bounded_int(shots, "Physical shot count") == 0:
                raise ValueError("A reported hardware outcome requires at least one shot")
        elif shots is not None:
            _bounded_int(shots, "Simulated shot count")
    return value


def assess(samples, *, now_mono_ms, monitor=None, limits=None):
    """Assess one bounded stream using its receiver's monotonic clock."""
    now = _bounded_int(now_mono_ms, "Receiver evaluation time")
    policy = dict(_LIMITS)
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) - set(policy):
            raise ValueError("Unknown timing limit")
        policy.update(limits)
    for key, threshold in policy.items():
        if threshold is None and key == "deadline_mono_ms":
            continue
        if _bounded_int(threshold, key) == 0 and key != "deadline_mono_ms":
            raise ValueError("Timing limits must be positive")
    if not isinstance(samples, list) or len(samples) > 256:
        raise ValueError("Supply at most 256 telemetry observations")
    if monitor is not None:
        _shape(monitor, "id receiver_clock at_mono_ms healthy", "receiver monitor")
        _name(monitor["id"], "Monitor")
        _name(monitor["receiver_clock"], "Monitor clock")
        if _bounded_int(monitor["at_mono_ms"], "Monitor observation") > now or type(monitor["healthy"]) is not bool:
            raise ValueError("Invalid receiver monitor reading")
    observations = sorted((_sample(sample, now) for sample in samples), key=lambda sample: sample["arrival"]["mono_ms"])
    flags = set()
    for key, label in (("kind", "MIXED_OBSERVATION_KIND"), ("session", "MIXED_SESSION"),
                       ("worker", "MIXED_WORKER"), ("boot", "WORKER_RESTART")):
        if len({value[key] for value in observations}) > 1:
            flags.add(label)
    clocks = {value["arrival"]["receiver_clock"] for value in observations}
    if monitor is not None:
        clocks.add(monitor["receiver_clock"])
    if len(clocks) > 1:
        flags.add("RECEIVER_CLOCK_DISCONTINUITY")
    monitor_current = bool(monitor and monitor["healthy"] and now - monitor["at_mono_ms"] <= policy["monitor_freshness_ms"]
                           and (not observations or monitor["id"] not in {value["worker"] for value in observations}))
    if not monitor_current:
        flags.add("RECEIVER_UNVERIFIED")
    if not observations:
        flags.add("NO_BASELINE")
    seen = {}
    highest = -1
    duplicate_count = 0
    for value in observations:
        key = (value["worker"], value["boot"], value["sequence"])
        identity = digest({name: value[name] for name in value if name not in {"arrival", "transport"}})
        if key in seen:
            duplicate_count += 1
            if seen[key][0] != identity:
                flags.add("CONFLICTING_SEQUENCE")
        else:
            seen[key] = identity, value
            if value["sequence"] < highest:
                flags.add("REORDERED_DELIVERY")
            highest = max(highest, value["sequence"])
    ordered = sorted((value for _, value in seen.values()), key=lambda value: value["sequence"])
    for first, second in zip(ordered, ordered[1:]):
        if first["worker"] != second["worker"] or first["boot"] != second["boot"]:
            continue
        if second["sequence"] - first["sequence"] > 1:
            flags.add("SEQUENCE_GAP")
        if second["worker_mono_ms"] < first["worker_mono_ms"]:
            flags.add("WORKER_MONOTONIC_DISCONTINUITY")
        prior, later = first["event"], second["event"]
        if (prior["clock"] != later["clock"]
                or later["time_ms"] + later["uncertainty_ms"] < prior["time_ms"] - prior["uncertainty_ms"]):
            flags.add("EVENT_CLOCK_DISCONTINUITY")
        if first["kind"] == second["kind"] == "PRACTICE":
            before, after = first["payload"], second["payload"]
            if (after["revision"] < before["revision"] or after["state_version"] < before["state_version"]
                    or after["progress"] < before["progress"]
                    or (after["state_version"] == before["state_version"] and after["revision"] != before["revision"])
                    or (after["revision"] == before["revision"] and after["state"] != before["state"])):
                flags.add("STATE_DISCONTINUITY")
    latest = ordered[-1] if ordered else None
    freshness = None
    delay = None
    if latest:
        freshest_arrival = max(value["arrival"]["mono_ms"] for value in ordered)
        freshness = now - freshest_arrival if len(clocks) == 1 else None
        event, arrival = latest["event"], latest["arrival"]
        if event["clock"] == arrival["clock"]:
            center = arrival["time_ms"] - event["time_ms"]
            uncertainty = event["uncertainty_ms"] + arrival["uncertainty_ms"]
            delay = [center - uncertainty, center + uncertainty]
            if delay[1] < 0:
                flags.add("CLOCK_CALIBRATION_INVALID")
            elif delay[0] > policy["delivery_limit_ms"]:
                flags.add("DELAYED_DELIVERY")
            elif delay[1] > policy["delivery_limit_ms"]:
                flags.add("DELIVERY_LATENCY_UNCERTAIN")
        else:
            flags.add("CLOCK_CALIBRATION_UNAVAILABLE")
    if policy["deadline_mono_ms"] is not None and now > policy["deadline_mono_ms"]:
        flags.add("RECEIVER_DEADLINE_MISSED")
    stalled_duration = None
    if latest and latest["kind"] == "PRACTICE":
        if monitor_current and freshness is not None and freshness > policy["heartbeat_timeout_ms"]:
            flags.add("WORKER_HEARTBEAT_LOST")
        progress = latest["payload"]["progress"]
        busy_since = latest["worker_mono_ms"]
        for item in reversed(ordered):
            if item["kind"] != "PRACTICE" or item["payload"]["progress"] != progress or item["payload"]["worker_state"] != "BUSY":
                break
            busy_since = item["worker_mono_ms"]
        stalled_duration = latest["worker_mono_ms"] - busy_since
        if stalled_duration >= policy["stall_timeout_ms"]:
            flags.add("WORKER_STALLED")
        if latest["payload"]["worker_state"] == "FAILED":
            flags.add("REPORTED_PRACTICE_FAILURE")
    else:
        flags.add("NO_PRACTICE_STATE")
    triggers = {"WORKER_HEARTBEAT_LOST", "WORKER_STALLED", "REPORTED_PRACTICE_FAILURE"}
    if flags - triggers:
        status = "HOLD_FOR_EVIDENCE"
    elif flags & triggers:
        status = "RECOVERY_CANDIDATE"
    else:
        status = "HEALTHY_PRACTICE_STREAM"
    return {
        "schema": "future-reader.telemetry-assessment/1", "status": status, "flags": sorted(flags),
        "observations": len(observations), "distinct_sequences": len(ordered), "duplicates": duplicate_count,
        "now_mono_ms": now, "policy": policy, "monitor": deepcopy(monitor), "heartbeat_age_ms": freshness,
        "delivery_latency_interval_ms": delay, "busy_without_progress_ms": stalled_duration,
        "latest": deepcopy(latest), "samples_sha256": digest(observations),
        "evidence_class": latest["kind"] if latest else "UNKNOWN",
        "support": "Caller-supplied stream and monitor. Eligibility tests compare declared calibration and timing.",
    }


def recover(controller, actor, target_session, *, samples, now_mono_ms, expected_sha256,
            authorized=False, monitor=None, limits=None):
    """Attempt the separately authorized, same-store practice handoff."""
    assessment = assess(samples, now_mono_ms=now_mono_ms, monitor=monitor, limits=limits)
    response = {"schema": "future-reader.telemetry-recovery/1", "mode": "SIMULATION",
                "assessment": assessment, "evidence_sha256": digest(assessment)}
    if authorized is not True:
        return {**response, "status": "HOLD", "reason": "RECOVERY_NOT_AUTHORIZED"}
    if assessment["status"] != "RECOVERY_CANDIDATE":
        return {**response, "status": "HOLD", "reason": "NO_UNAMBIGUOUS_FAILURE_TRIGGER"}
    latest = assessment["latest"]
    current = controller.pinpoint()
    fields = ("state", "revision", "state_version")
    if latest["session"] != controller.session or any(latest["payload"][field] != current[field] for field in fields):
        return {**response, "status": "HOLD", "reason": "TELEMETRY_CHECKPOINT_MISMATCH"}
    try:
        receipt = controller.handoff(actor, target_session, expected_sha256=expected_sha256,
                                     evidence_sha256=response["evidence_sha256"])
    except ControllerError as error:
        return {**response, "status": "HOLD", "reason": error.reason}
    return {**response, "status": "TRANSFERRED", "receipt": receipt}


def demo():
    """Exercise a delayed sample and a distinct, corroborated practice failure."""
    sample = {
        "schema": SCHEMA, "kind": "PRACTICE", "session": "practice", "worker": "worker-one", "boot": "boot-one",
        "sequence": 1, "event": {"clock": "fixture-wall", "time_ms": 1000, "uncertainty_ms": 10},
        "arrival": {"clock": "fixture-wall", "time_ms": 1100, "uncertainty_ms": 10,
                    "receiver_clock": "fixture-monotonic", "mono_ms": 1100},
        "worker_mono_ms": 1000, "transport": "synthetic-local-queue",
        "payload": {"state": "S", "revision": 0, "state_version": 1, "progress": 0, "worker_state": "BUSY"},
    }
    monitor = {"id": "receiver-monitor", "receiver_clock": "fixture-monotonic", "at_mono_ms": 3700, "healthy": True}
    delayed = deepcopy(sample)
    delayed["arrival"]["time_ms"] = 2150
    with Controller() as source:
        source.set_grant("practice", True)
        pin = source.checkpoint()["sha256"]
        waiting = assess([delayed], now_mono_ms=3700, monitor=monitor)
        result = recover(source, "practice", "replacement", samples=[sample], now_mono_ms=3700,
                         expected_sha256=pin, authorized=True, monitor=monitor)
        return {"late_transport": waiting, "recovery": result, "original": source.pinpoint()}
