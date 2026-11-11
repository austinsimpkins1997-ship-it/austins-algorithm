"""Construct solutions to the synthetic field sensor kit constraints."""

from __future__ import annotations

import json
from time import perf_counter_ns


def encode_kit(*, radar: bool, lte: bool, backup_power: bool, local_buffer: bool) -> str:
    """Encode q5 through q0, with one sensor and one uplink selected."""
    choices = (local_buffer, backup_power, not lte, lte, not radar, radar)
    return "".join("1" if enabled else "0" for enabled in choices)


def first_valid_kit() -> str:
    """Choose an optical sensor and wired uplink without either accessory."""
    return encode_kit(radar=False, lte=False, backup_power=False, local_buffer=False)


def all_valid_kits() -> list[str]:
    """Combine valid sensor/power and uplink/buffer configurations directly."""
    sensor_options = ((False, False), (False, True), (True, True))
    uplink_options = ((False, False), (False, True), (True, True))
    return sorted(
        encode_kit(radar=radar, lte=lte, backup_power=backup_power, local_buffer=local_buffer)
        for radar, backup_power in sensor_options
        for lte, local_buffer in uplink_options
    )


def main() -> None:
    start = perf_counter_ns()
    candidate = first_valid_kit()
    first_elapsed_ns = perf_counter_ns() - start

    start = perf_counter_ns()
    solutions = all_valid_kits()
    all_elapsed_ns = perf_counter_ns() - start

    report = {
        "workload": "remote-field-sensor-kit",
        "execution_mode": "CLASSICAL",
        "method": "direct_boolean_construction",
        "basis_order": "q5 q4 q3 q2 q1 q0",
        "first_valid_kit": {
            "candidate": candidate,
            "elapsed_ns": first_elapsed_ns,
        },
        "full_solution_set": {
            "solutions": solutions,
            "marked_count": len(solutions),
            "elapsed_ns": all_elapsed_ns,
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
