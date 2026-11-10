# Contributing

Contributions should improve reproducibility, clarity or a named use case. Keep the package's honest boundary: the current named algorithm is a project-specific Grover example, while the base CPU program is a classical simulator.

Before adding a feature, state the use case, why the existing smallest implementation does not meet it, expected cost/complexity, input/output contract, tests, and rollback. Prefer a small optional adapter over a new core dependency. Preserve Qiskit/Cirq optionality and keep network, account, provider, hardware submission, data persistence and model invocation out of default code paths.

Required checks for code changes:

```powershell
python -B -m unittest discover -s tests -v
python -m austins_algorithm --shots 64 --seed 7
```

For circuit changes, add a basis-order/oracle test and compare measured ideal probabilities with the analytic Grover formula. Hardware claims require a separately recorded device, target, date, job identifier, shots, noise/mitigation settings, verified output, comparison baseline and limits. A passing simulator test is not hardware evidence.

For adapter API changes, version the input/output schema when meanings change. Test capability mismatches and unknown limits without network access. Keep credentials out of request/result records and label each timing by source.

Please include a concise changelog entry for user-visible behavior. Keep generated files, credentials, local simulator caches and datasets out of commits. Cite sources and preserve licenses when adding external code/data.
