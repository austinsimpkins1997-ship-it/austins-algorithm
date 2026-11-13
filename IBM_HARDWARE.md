# Austin’s Algorithm on IBM Quantum

The physical comparison uses the fixed three-qubit Austin circuit. Its classical solution is `011` or `101`. The same command reports an ideal statevector sample and a local synthetic-noise sample for comparison. Dated IBM snapshot simulation and weighted Workbench optimization have separate workflows.

## Check the circuit before running it

Install the optional IBM dependencies in an isolated Python environment:

```powershell
python -m pip install ".[ibm]"
python -m austins_algorithm compile --compile-target torino --problem austin
python -m austins_algorithm compare --ibm-model torino --shots 256 --seed 7
```

These commands use a bundled target and calibration snapshot. They work without an IBM account and submit zero jobs.

IBM documents a free Open Plan with up to ten QPU minutes in a rolling 28-day window. It requires a personal or otherwise authorized IBM Quantum account, an API key and access to an eligible instance. Open Plan supports job or batch mode. Select an instance explicitly and check its plan and remaining quota in the provider dashboard. [IBM first hardware run](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/hello-world) · [IBM plan overview](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-platform/plans-overview) · [IBM execution and usage limits](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/max-execution-time) · [IBM execution modes](https://quantum.cloud.ibm.com/docs/en/guides/run-jobs-session)

Save a local profile using IBM’s documented setup. Keep the API key in the local credential configuration. Replace the uppercase placeholders with the operator-approved named backend, local profile and service instance.

```powershell
python -m austins_algorithm compare --qpu-preflight --shots 256 --backend BACKEND --account SAVED_PROFILE --instance SERVICE_INSTANCE --max-execution-seconds 60
```

The preflight authenticates, reads the exact backend and checks its operational status. It verifies target instruction support and compares ideal measurement distributions for the logical circuit, the compiled circuit, and the re-imported OpenQASM. It records depth, two-qubit gate count, export hash and wire mappings. `TARGET_COMPILED_AND_VERIFIED` describes that read and compilation result. The preflight sends no sampling job. A changing target or queue can still prevent a later submission. QPU seconds specify a provider execution limit. Actual usage comes from the completed job and pricing comes from the selected plan. [IBM client-side Sampler](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/executor-sampler-sampler) · [IBM backend information](https://quantum.cloud.ibm.com/docs/en/ibm-quantum-compute/qpu-information)

To submit the one bounded job after selecting and authorizing its budget, use the same command with `--qpu-preflight` removed. The submit path repeats the backend and compiler checks and uses the exact verified target circuit. A local wait deadline can be set separately with `--wait-timeout-seconds`. When a wait expires, inspect the original job ID in IBM Workloads. When the request response is lost, reconcile the original workload before retrying. Use verified hits over completed shots and read the provider-reported queue, running and QPU usage times separately. Monetary cost remains unknown when the result carries no billing amount.

## Verified scope on November 13, 2026

The standard saved-profile path and IBM credential environment variables were checked for presence. No configured account was found. Offline target compilation, local comparison, and test-double runs for the physical submission boundary provide local evidence. Actual hardware counts, live backend availability, pricing for a selected instance, and physical execution times remain unverified.

AWS Braket’s published free tier covers on-demand simulator time. Its pricing examples show separate physical task and shot charges. Azure Quantum publishes provider-specific pricing and directs operators to verify their workspace’s current terms. Neither route has an integration or a configured account in this experiment. [AWS Braket pricing](https://aws.amazon.com/braket/pricing/) · [Azure Quantum provider pricing](https://learn.microsoft.com/en-us/azure/quantum/pricing)
