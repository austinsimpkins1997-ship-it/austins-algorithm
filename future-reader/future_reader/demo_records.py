"""Small public record that illustrates revisions without claiming a field event."""

from .records import VIEWS, view

CUTOFF = "2026-11-13T12:00:00Z"
EARLY = "2026-11-13T10:15:00Z"
EVENT = "2026-11-13T10:00:00Z"
FRAME = "fixture-utc"


def _time(start, end=None):
    return {"from": start, "to": end or start, "clock": FRAME}


def _claim(identity, topic, title, kind, value, *, event, available, next_check, dependencies=(), **extra):
    return {
        "id": identity, "topic": topic, "title": title, "kind": kind, "value": value,
        "source": {"title": "Future Reader public training fixture", "actor": "Demo simulator", "version": "1"},
        "times": {"event": event, "recorded": available, "available": available, "valid": None},
        "audience": ["*"], "domain": "Operational simulation", "scope": "Training board",
        "frame": FRAME, "units": "elapsed seconds", "dependencies": list(dependencies), "next_check": next_check,
        **extra,
    }


def timeline():
    return {
        "schema_version": 1, "frame": FRAME,
        "claims": [
            _claim("sensor-v1", "sensor", "Calibration record was unavailable", "REPORTED_OBSERVATION",
                   {"status": "UNKNOWN", "basis": "The operator had only the status report."},
                   event=_time(EVENT), available=_time(EARLY), next_check="Find the calibration receipt."),
            _claim("sensor-v2", "sensor", "Calibration receipt arrived", "OBSERVATION",
                   {"status": "KNOWN", "value": "Demo calibration accepted at 10:24 UTC."},
                   event=_time("2026-11-13T10:24:00Z"), available=_time("2026-11-13T10:25:00Z"),
                   next_check="Check calibration expiry before a new run.", parent="sensor-v1"),
            _claim("conditional-route", "route", "Path B can remain feasible", "FORECAST",
                   {"status": "ESTIMATE", "value": "A bounded path remains if the check arrives by its cutoff.",
                    "basis": "Simulated deadline and deterministic stage bounds."},
                   event=None, available=_time("2026-11-13T10:26:00Z"), dependencies=("sensor-v2",),
                   next_check="Compare check arrival to the latest safe start."),
            _claim("archive-fragment", "fragment", "Recovered route note lacks its clock anchor", "REPORTED_OBSERVATION",
                   {"status": "UNKNOWN", "basis": "The fragment has no reliable acquisition time."},
                   event=None, available=None, next_check="Locate an independently dated companion record."),
        ],
        "order": [{"from": "sensor-v1", "to": "sensor-v2", "relation": "temporal", "evidence": "sensor-v2"}],
    }


def fixture_document():
    document = timeline()
    return {"schema_version": 1, "actor": "reader", "timeline": document, "views": {
        mode: view(document, mode, actor="reader", as_of=EARLY if mode == "as_known_then" else CUTOFF,
                   event_at=EVENT if mode == "at_event" else None)
        for mode in VIEWS
    }}
