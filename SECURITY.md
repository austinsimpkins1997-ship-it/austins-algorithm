# Security policy

The package core, local adapter, and current examples run locally; they do not open network connections, persist requests, or submit provider jobs. The experimental interface defines how a separate plug-in may do those things only after its caller explicitly invokes it. The local adapter accepts an in-process Python predicate; do not send that object or personal/sensitive inputs to an external plugin without a separate data and permission review. Do not place provider keys, personal data, or sensitive domain inputs in issues, examples, tests, or repository history.

Report a suspected vulnerability privately to the repository maintainers once a public repository and contact channel are established. No public GitHub repository or security contact is claimed yet. For provider integrations, add a separate reviewed contract covering credentials, scope, data handling, billing, cancellation, and incident response before implementation.
