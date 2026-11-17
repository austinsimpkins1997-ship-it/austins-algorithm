# Security Lab and bounded sequencing — 0.3.1 alpha

This native Workbench page, Python module and CLI run **synthetic local models**. They accept neither live targets nor passwords, password hashes, keys or credential dumps. No network, provider job, adversary agent, exploit executor, cryptographic cracker or real authentication enforcement is added.

The user requested both ordering search/verification stages and constructing quantum circuit sequences. The implementation supplies finite job-specific round settings, fresh verification and inspectable teaching circuits. “Infinity” and “Absolute” remain aspirations: finite resources, model assumptions and current evidence bound every result. A CPU simulator does not acquire a quantum cracking advantage by sequencing more steps. Actual advantage is **NOT DEMONSTRATED**.

## First session

Open **Security Lab** in Workbench 0.3.1-alpha. The default synthetic policy has a retry ceiling of three failures in a modeled 60-second window, shared modeled login/reset/OTP counters and restart persistence. These are teaching parameters, not recommended production defaults or a NIST conformance claim. Review the named fixtures, select **Run lab**, then inspect **What happened?**. Save/open inputs with the toolbar and select **Export** for a committed job snapshot. Edited inputs require a new pinned run before that view can be exported. Prior jobs remain in the private local store; the existing `plan` status/export/recover commands can inspect their IDs. Security Lab tasks can be explicitly rerun as new jobs; only the existing branch-and-bound planning backend supports checkpoint resume.

```powershell
python -m austins_algorithm security-lab run examples/security_rehearsal.json --store PRIVATE-JOBS --export security-evidence.zip
python -m austins_algorithm plan verify-export security-evidence.zip
```

The example and all its outcomes are synthetic. Change `shared_budget` and `preserve_restart` to `false`, save under a new name and run again. The default fixture suite then exposes failures in shared-path budgeting and restart persistence. Inputs use a closed schema, a 32-KiB reader cap, named fixtures, finite integer settings and a bounded public toy sequence. Unknown fields such as targets and credentials are rejected. Do not put secrets in input labels or reports.

## What is exercised

- Account burst: the ordered model must reject some excess invalid attempts.
- Shared paths: the named login/reset/OTP fixture checks a shared modeled budget.
- Restart: compare a synthetic restart against the same trace without restart. If another condition prevents reaching the tested boundary, record NOT_EXERCISED rather than invent a pass or failure.
- Independent user: a second synthetic account remains usable in the declared trace.
- Expiry: a valid synthetic event is admitted after the modeled window expires.

`valid` is fixture data, not a password verifier. Events at equal times are processed atomically in supplied order. That is not a distributed concurrency or race test. Time is an integer simulated millisecond axis; it does not authenticate wall clocks. The model has two synthetic accounts, three named paths and at most 128 events. Memory exhaustion, real counter persistence, networks, identity, passkeys, password storage, session handling and recovery services require separately owned implementations and evidence.

The existing Planning engine also solves an explicit defense-selection example. Its costs are **illustrative effort points**, not measured effectiveness or monetary costs. Mandatory modeled controls remain mandatory; the result does not deploy controls or authorize action. There is no claim that its selected set is universally strongest or sufficient for a real system.

## Sequencing and actual cost

The stages are preparation → repeated oracle/diffusion → measurement → independent verification → recorded checkpoint. The public three-bit Austin predicate is reused without accepting arbitrary oracles. A job may compare one to nine distinct round counts in 0..8, with 1..256 shots per candidate and a pinned seed. It selects the fewest rounds reaching ideal probability 1, or the highest ideal probability among the declared candidates. The selection criterion is an explicit finite-model rule, not an optimizer over all quantum algorithms. A different seed is reserved for verification. Unverified samples remain visible; cancellation does not claim a completed experiment.

The report charges eight CPU predicate evaluations for every simulator preparation, including held-out verification, and distinguishes unique candidate checks from repeated sampled outcomes. Elapsed measurements are local observations. The simulator's own timings are nested, not additive independent cost stages. The report retains a classical first-match reference and explicitly states that directly constructing the known public answer is stronger than exhaustive search for this structured toy. Query-model counts omit physical oracle construction/error correction and do not establish total-runtime or hardware advantage. No probability, universal accuracy or security guarantee is inferred from a finite sample.

With the optional Qiskit extra, export the existing public teaching circuit:

```powershell
python -m austins_algorithm security-lab circuit --rounds 1 --output austin-teaching-sequence.qasm
```

The **Circuit QASM** button uses the selected round count when the displayed inputs still match the job. The frozen standard-library executable excludes optional Qiskit; use the SDK-enabled Python installation for circuit export. This builds a circuit; it does not submit a job or prove advantage. No password/hash or encryption oracle is constructed.

## Representative research decisions

Reviewed on **2026-11-17**, editor **Codex (AI assistant)**. These are primary-source roles and implementation decisions, not a benchmark of ALL offensive/defensive models, proprietary platforms or AI agents. “Strongest” depends on threat, deployment, operator and cost; the reviewed sources are not directly interchangeable. No external offensive commands, payloads or source code are vendored. See the packaged `workbench_assets/security_sources.json` for source identity, URL, decision and limits.

| Source | Role and decision | Evidence boundary / verification |
|---|---|---|
| [MITRE ATT&CK T1110](https://attack.mitre.org/techniques/T1110/) | Use adversary behavior categories to name relevant scenarios. | Taxonomy, not attack execution or a detection benchmark. Test only synthetic admission traces. |
| [MITRE D3FEND](https://d3fend.mitre.org/) | Use explicit countermeasure mappings and vocabulary. | A mapping does not prove deployment or effectiveness. |
| [OWASP ASVS 5.0](https://asvs.owasp.org/) | Adopt reviewable requirements and negative tests. | Partial source influence; no ASVS certification or complete coverage. |
| [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html) | Model retry and recovery boundaries; separate authentication from proposals. | Real authenticator, assurance-level and identity integration remain unverified. |
| [NIST CSF](https://www.nist.gov/cyberframework) | Retain governance, response and recovery as well as protection. | No risk/compliance score is inferred. |
| [NIST zero trust](https://csrc.nist.gov/pubs/sp/800/207/final) | Keep authority at the actual enforcing boundary. | No network-wide policy deployment. |
| [Apache Caldera transition](https://caldera.mitre.org/) | Adopt repeatable exercise and evidence patterns; research only. | No agent, executor or live emulation installed. |
| [Atomic Red Team](https://github.com/redcanaryco/atomic-red-team) | Adopt small named tests and repair/retest discipline; research only. | No upstream commands imported or run. |
| [NIST PQC](https://csrc.nist.gov/projects/post-quantum-cryptography) | Keep cryptographic migration distinct from toy search. | No PQC integration or custom encryption added. |
| [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) | Reference established storage guidance for a future owning service. | No password store, password input or offline cracker. |
| [IBM Grover tutorial](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm) | Reuse existing oracle/circuit stages and inspect QASM. | Local simulation and circuit construction are not physical-device evidence. |
| [Zalka lower bound](https://arxiv.org/abs/quant-ph/9711070) | Respect black-box oracle assumptions and complexity bounds. | More sequencing does not bypass the bound or imply unbounded capability. |

Research has documentation/setup and review cost; those costs were not benchmarked against commercial alternatives. Caldera/Atomic installation and operating burden are not incurred because they were not adopted as dependencies. The lab remains standard-library Python, with optional Qiskit for circuit export. Keyboard buttons, readable summaries and ordinary Tk controls are implemented; representative human accessibility, security effectiveness and operational benefit remain unverified.

## Evidence and failure handling

Managed jobs retain inputs, source pins, attempts, worker state and result. Exports include the complete job, readable report, security/sequence results, source table, requirements, logs and replay code. The private local HMAC key is excluded. Export verification checks integrity and input structure without running supplied code; it does not authenticate source authorship or reproduce result claims. Review code before executing replay. A source revision holds old jobs rather than silently running changed code. Use explicit recovery for a failed worker and start a new job where no resumable checkpoint exists.

Unit tests cover default success, counterexamples, exact window expiry, synthetic equal-time ordering, delays, account isolation, malformed/unsupported inputs, cancellation, export/fresh replay and optional circuit probabilities. A hidden Workbench/native smoke exercises managed run/save/open/export. These establish only named local software behavior. They do not establish brute-force resistance of any deployed application, physical-QPU behavior, absolute accuracy, organizational production acceptance or practical quantum advantage.
