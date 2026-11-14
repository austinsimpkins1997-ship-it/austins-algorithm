# Worker telemetry and failover

The receiver assesses a bounded batch of up to 256 observations. It preserves worker and boot identities, sequence numbers, event clock and uncertainty, arrival clock and uncertainty, receiver monotonic time, worker monotonic time, and transport. It measures heartbeat age on the receiver's monotonic clock. It measures stalled progress using the worker's own elapsed time within one boot. It calculates a delivery-latency interval only when both wall stamps name the same calibrated frame.

The default heartbeat timeout is 2,500 milliseconds and triggers strictly after that boundary. The work-stall threshold is 4,500 milliseconds with an unchanged progress counter throughout the observed busy interval. A healthy, independent receiver monitor must be no more than 1,000 milliseconds old. Delivery later than 750 milliseconds, or uncertainty spanning that boundary, pauses failover for evidence. A separately declared receiver deadline uses an inclusive equality policy. The current practice action also has its own window, expressed in practice seconds.

The assessment holds on mixed sources, receiver or worker clock discontinuities, delayed transport, reordered or missing sequences, conflicts within the same sequence, practice state divergence, and missed deadlines. Exact retransmissions are counted and do not refresh the original heartbeat. An empty batch cannot establish worker loss. The caller supplies authenticated observations and monitor evidence when operating a real host. The included exercise supplies synthetic records and has no field evidence.

| Observation kind | What the record carries |
| --- | --- |
| `PRACTICE` | Simulated position, history revision, state version, progress and worker state. Eligible for exercise recovery after the other checks pass. |
| `SIMULATED_STATEVECTOR` | A result reference, backend, basis and optional shot count. |
| `REPORTED_HARDWARE_OUTCOME` | A caller-reported result reference, backend, measurement basis and positive shot count. |

Statevector and hardware observations remain distinct evidence types. A shot count alone establishes neither a verified hardware execution nor a full quantum state.

`recover` requires an explicit trusted-host authorization flag, an active controller grant, a matching position and state version, a caller-pinned checkpoint, a current viable practice window, and a new replacement session. The atomic SQLite handoff fences the original writer and stores the assessment digest in the transfer receipt. Preserve the returned assessment separately to recheck that digest. The bounded practice model uses a single local database. Connecting a real worker or cross-machine failover would require authenticated observations, verified clocks and transport, durable external-effect reconciliation, and a shared ownership authority.

Run `python -B -m unittest discover -s tests -p test_telemetry.py` to exercise delayed and contradictory observations, heartbeat and deadline boundaries, stale checkpoints, closed windows, and a verified fenced transfer.
