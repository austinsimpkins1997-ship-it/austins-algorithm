# Integration and requirement map

This map follows the 17 sections of the original brief and carries forward
the timing and runtime supplements. **Verified** applies to the scope and
test named in the row. **Provisional** identifies untested extensions.
**Blocked** names a missing dependency.

| Requirement | Status and evidence |
| --- | --- |
| 1. Isolated project and canonical inventory | **Verified locally.** Austin source 0.1.0, native Workbench and E: mirror inspected. The BIB prototype lives in the independent workspace `projects/beyond-infinities-bounds`. Its 13 tests and 20-file manifest were checked. See Beyond has its own boundary. |
| 2. Five temporal views | **Verified.** `records.py` and `test_records.py` cover timestamp roles, audience, revision, visible dependencies, conflicts, and missing clock anchors. **Provisional:** field-level provenance for arbitrary third-party records and larger stores. |
| 3. Geometry and sequencing | **Verified for the practice graph.** Controller validates adjacency and program types. The record reader distinguishes causal, temporal, adjacent and disputed links, with cycle tests. The CERN equality uses exact unit arithmetic. **Provisional:** cross-frame conversions and lossy translators for additional domains. |
| 4. Oracle contract | **Verified for local simulation.** Python CLI, bearer-scoped loopback API, native Tk, and shared-fixture offline GUI. Controller tests check stale state, revocation, forged proof, expiry, idempotent retry and commit rollback. Evidence cases use the same four operation names with read-only behavior. **Provisional:** role-specific live-effect authorization and external compensation. |
| 5. Opportunities | **Verified for declared deadlines.** Critical-path DAG, parallel work, equality and safety reserves. Practice window and CERN hypothetical detector scenario show when evidence arriving late changes eligibility. **Provisional:** recurring and competing windows with stochastic or uncalibrated clocks. |
| 6. Chance and consequence | **Verified for finite scenario inputs.** `decisions.py` computes dependent joint/conditional outcomes, hard eligibility, expected and worst-case utility, and the 75% break-even illustration. **Provisional:** calibration, drift metrics and held-out field validation. |
| 7. Frontier, telemetry and recovery | **Verified for the finite graph and single SQLite store.** Cursor replay, exhaustion, stale inputs, conflict, retained history, observed commit, timeout, and old-writer fencing have tests. Open-horizon slices resume conditional practice scenarios and account for both check outcomes at each supported tick. **Blocked:** cross-machine failover requires authenticated telemetry, shared ownership and effect reconciliation. **Provisional:** general open-ended scheduling and physical quantum state transfer. |
| 8. Cenomanian | **Verified within source and model scope.** ICS boundary, regional NPS context, local ambiguity and independent archive-loss simulation are included. Erasure tests exercise every one-of-three missing shard. The supplied 62 recoveries lack the original seed. |
| 9. CERN | **Verified selectively.** Published event/plan records, BIPM compact replay, exact modeled timing/path equivalence, and a synthetic detector window. **Blocked:** original trajectory and slope trial inputs, CMS purity source and current independently calibrated path/clock observations. |
| 10. Large-scale studies | **Verified for new synthetic workloads.** 10 million evaluated policy steps and 20,000 pruning cases. **Blocked for original reproduction:** the supplied workload, source model, seeds and decision traces were unavailable. See [Timing and pruning studies](STUDIES.md). |
| 11. Structural integrity | **Verified for listed local faults.** Duplicate JSON, incomparable clock, cyclic order, hidden evidence, stale grants, concurrent commit, interrupted write, delay/conflict, expired window, corrupt archive, rejected update and reduced-motion keyboard workflow are covered. **Provisional:** combined distributed and external-effect failures, migrated third-party schemas, human usability sessions. |
| 12. Best design in scope | **Verified on the four-node workload.** Full enumeration proves the eligible minimum. Direct and Austin encodings match across 16 contexts. `tools/benchmark.py` measures cold, warm, reopened, branched and resumed work. **Provisional:** end-to-end display lag, energy, throughput under contention and larger workloads. |
| 13. Adapters and quantum | **Verified locally.** Versioned capabilities, inspectable program, bounded CPU binding, native BIB ledger conformance, and small Qiskit/OpenQASM simulator verification. **Blocked:** device access, production BIB executor, and credentialed provider integration. Generated adapter activation remains provisional. |
| 14. GUI and HTML | **Verified locally.** Native custom preview, stale rejection, confirm, reverse, certify and synthetic recovery. Offline Chrome checked keyboard, accessible names, checkpoint/revision, fixture parity and three exact prompt downloads. **Provisional:** testing with children and screen-reader users, visual editing of all Python constructs, private browser-to-runtime integration. |
| 15. Revisions and releases | **Verified locally.** Append preview with dependency impact, approval digest, stale rejection, local artifact staging and transactional rollback. Read-only GitHub source and release checks. **Blocked:** publisher identity, private registry permissions, release publication and remote update artifacts. |
| 16. Deep-time handoff | **Verified for bundled artifacts.** Standalone HTML and Python evidence fixtures carry definitions, sources, examples and uncertainty. Random and targeted record loss and shard loss have focused cases. **Provisional:** key rotation, migration across independently implemented readers, and long-term storage assumptions. |
| 17. Delivery | **Verified for the local alpha.** See README, release guide, browser/desktop receipts, manifests, benchmark and schemas. The separate hardware, live operations and publication boundaries remain as described above. |

## Structural checks by layer

| Layer | Injected failure | Acceptance and recovery |
| --- | --- | --- |
| Semantic | Contradictory reports, missing lineage, unsupported version | Preserve alternatives, suppress only dependent views, reject unsupported checkpoint |
| Evidence | Corrupted bytes, re-digested inconsistent trace | Independently replay against pinned model, preserve original evidence |
| Timing | Clock regression, deadline equality, delayed evidence | Keep clock roles distinct, enforce declared bounds, expose fallback |
| Numeric | Equivalent path and flight time, parallel durations | Exact rational equality and exact integer critical-path arithmetic |
| Execution | Concurrent commit, trigger abort, transfer | One receipt, rollback on failure, old writer fenced |
| Authorization | Revocation, stale preview, anonymous or foreign-origin requests | Revalidate grant in transaction, reject unauthorized access |
| Decision | Correlated outcomes, severe downside, missing prerequisite | Joint scenario outcomes and hard constraints preserved |
| Archive | Random 99.9% loss, targeted deletion, one missing shard | Analytical probability, unresolved provenance, byte-exact reconstruction where possible |
| Release | Wrong pin, stale approval, wrong checkpoint schema | No pointer switch until verified, known-good local rollback |
| Interface | Keyboard, narrow viewport, stale choice | Focusable controls, result announcements and fresh preview before confirm |

## 0.1.1 follow-up

The same-host worker check now exits a real child process and verifies that
a replacement finishes from the committed checkpoint. Tests cover heartbeat
expiry, concurrent supervisors, stale generations, and interrupted SQLite
writes. Cross-machine effects still require a shared ownership and effect
store. See [Live local worker recovery](LIVE_WORKER.md).
