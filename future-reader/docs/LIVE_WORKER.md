# Same-machine worker recovery

Future Reader can execute a verified practice program in a separate local operating-system process. Its effects are simulated board moves in SQLite. Python subprocesses, heartbeats, process exit and replacement are observable host behavior.

From the Future Reader package root, use a fresh SQLite file for the exercise.

```powershell
python -B -m future_reader.live_failover demo .build/local-worker.sqlite
```

The demo starts a worker, observes a committed move to Shelf, terminates the process it launched, and starts a replacement. The replacement opens the same durable checkpoint, runs the remaining move and reaches Goal. Review the original and replacement PIDs, heartbeat count, recovery generation, and the unique effects at each program step. Checkpoint replay independently checks the resulting position, cost and practice time against the program.

For an ordinary persistent job, use a JSON file containing a program that follows the [program schema](../schemas/controller.schema.json).

```powershell
python -B -m future_reader.live_failover create .build/local-worker.sqlite example program.json
python -B -m future_reader.live_failover run .build/local-worker.sqlite example
python -B -m future_reader.live_failover status .build/local-worker.sqlite example
```

Running again resumes from the durable cursor. A completed job returns its existing effects. A worker reports sequence, elapsed monotonic time, process identity and host wall time. The supervisor records when a sequence last changed using a separate monotonic clock. An observed exit of its owned subprocess can trigger immediate recovery. A restarted or second supervisor waits for a stale heartbeat before claiming ownership. This can identify missing progress on the host, while OS policy and system-wide availability remain outside the supervisor's observations.

SQLite serializes the fencing change and each effect commit. Every commit rechecks the current generation, transferred owner token, worker PID, expected cursor, and replayed history. The move, its positive cost and checkpoint digest commit together. Retries return the existing outcome. A stale authority cannot change the job after a newer generation claims it. The private child pipe carries the owner token and lets the worker stop when its supervisor disconnects. Only subprocesses launched by the current supervisor are eligible for termination. The worker and monitor run on the same machine and may share a failure domain.

Each run has a time budget and at most four process starts. The commit path uses the immutable program, practice clock and prerequisite settings recorded at job creation, including the modeled deadline. Real process time is tracked separately. A paused run can resume under the stored practice model. For changing prerequisites or a real deadline, a domain-specific adapter would need current external evidence and authorization.

The operator must be authorized to write the SQLite file. Keep it on a trusted local filesystem. The design uses SQLite transactions for effects inside that file. It has no adapter for external effects, process migration across machines, remote sensors, or physical quantum execution.
