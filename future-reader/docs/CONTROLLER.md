# Practice controller and recovery

The practice model describes four positions. **S** is Start, **A** is Bench, **B** is Shelf, and **G** is Goal. Positions use exact categorical identity and graph adjacency. Durations are integer seconds in the simulated practice clock frame. Costs are unitless exercise weights. An edge describes an available move with a positive time and cost. A prerequisite describes a dependency on an independent check. Neither adjacency nor timestamp order establishes causality.

The exercise ends at second 17 inclusive. The plan reserves 2 seconds for verification, 1 second for safety, and 1 second for clock uncertainty. The Shelf path costs 3 and takes 8 seconds. Its last safe start is second 5. The Bench path costs 6, takes 7 seconds of movement, and requires the independent check. Its final move opens at second 8. Its last safe start is second 6, when reaching the goal at second 13 leaves all four reserved seconds. At second 7 both paths miss the modeled boundary. A waiting decision keeps the outcomes of the independent check conditional and exposes the last safe fallback start.

`critical_path` separately calculates a duration bound for a declared prerequisite DAG. Parallel branches take the maximum completion bound before they join. Include every stage that must finish before the deadline and add edges for any resource that serializes stages. Missing prerequisites and cycles receive explicit errors. The function assumes justified duration bounds. It reports feasible and safe starts separately.

## Four operations

| Operation | Effect and observable result |
| --- | --- |
| Pinpoint | Reads current position, simulated clock, uncertainty, evidence, revision and writer state. |
| Travel | Previews a route or typed program. Commit checks the same route against current inputs and records the observed local position. |
| Reverse | Previews restoring a chosen earlier position. Commit appends a new event, preserving earlier history and current time. A subsequent Travel gets a current-window check. |
| Certify | Enumerates every simple path under pinned inputs and returns the minimum eligible cost or proof of infeasibility in scope. Independent recomputation checks the certificate. |

The controller treats the SQLite file and its host as trusted. Host-managed grants cover simulation only. The host is responsible for authenticating callers and authorizing all reads, writes and exports. Preview is bound to the actor, session, model digest, current revision, clock and grant epoch. It expires after five minutes on the host wall clock. That clock is separate from the practice clock. A backward host-clock jump before the preview creation time rejects the commit. The host wall clock requires a trusted deployment configuration for stronger expiry guarantees.

SQLite transactions serialize state changes, observed-state verification, audit event insertion and receipt recording. A duplicate commit returns its receipt after rechecking the actor's current grant. A competing preview is stale after the first commit. The controller retains up to 64 pending previews and 1,024 committed events per session. A committed receipt stays available for retry. Read-only archival checks and offline practice are safe within their stated scope. The project has no physical or external effects to reconcile.

## Continuation

Use `Controller.checkpoint()` to export the current state and its chronological event record. `verify_checkpoint` checks the canonical SHA-256 digest and replays all committed events. Supply a trusted prior digest to check the intended original content. A self-declared digest alone can establish consistency, while a semantic replay tests the stated simulation. Current observation and prerequisite fields describe modeled inputs. They do not authenticate a clock or source of evidence.

In the same SQLite file, `Controller.handoff` moves an eligible checkpoint to a new session. It checks an execution grant, the caller-pinned checkpoint and the current modeled opportunity. One transaction fences the original writer, preserves its history and creates a target with the same position, clock and revisions. Pending original previews cannot commit. A closed window keeps the original record inspectable. Transfer between independent databases requires a shared authority and ownership fencing beyond this local model.

Finite search can pause using `search_slice`. The checkpoint carries the expanded cursor, pending branch paths and a fingerprint over the complete model, clock, state, target and prerequisite availability. Resume verifies the prefix against the tiny reference search and rejects changed context. For this graph the full run explores five prefixes. Recomputing at most four prefixes on resume avoids a separate execution log. Larger graphs require a different durability and scheduling design.

## Program and portable conformance

The program schema describes Start, Move, Check, Repeat, If and Goal. Start and Goal declare the expected state. A Move succeeds only for an edge in the model. Repeat and If are bounded and support at most 64 interpreted pieces and three nesting levels. The current domain has no orientation coordinate and gives Turn a typed unsupported result. The browser uses a shared graph and Python-generated fixtures for its supported subset.

Run `python -B -m future_reader.controller` to generate the portable fixture. The checked-in fixture includes nine route scenarios, a real Python program, exact proof output, and projections of an applied Travel followed by Reverse. Run `python -B -m unittest discover -s tests -p test_controller.py` to check the controller, boundary arithmetic, fixture parity, transaction rollback, concurrency, and checkpoint replay.
