# Use-case map: what Grover search can fit

Austin's Algorithm currently means the package's project-defined Grover example. The general Grover circuit can be considered for a task only after it has been reduced to a finite search with a reversible predicate. The rows below are **candidate mappings**, not implemented deployments or demonstrated benefits.

| Candidate problem shape | Mapping that would be required | Current package status |
|---|---|---|
| Small Boolean constraint satisfaction | Encode candidate assignments; build and verify a reversible phase oracle for satisfying assignments. | Three-bit teaching predicate plus explicit marked-state circuit builder; CPU simulation only. |
| Tiny scheduling/configuration search | Define finite candidate encoding and hard constraints; include data-loading and oracle costs. | Not a scheduler; no real constraints or schedules bundled. |
| Small combinatorial toy search | Mark feasible states and compare circuit queries with an exact classical enumerator. | Suitable as a classroom experiment; does not demonstrate useful speedup. |
| Threshold search over a known finite domain | Implement a reversible comparison oracle and specify how the threshold and inputs are loaded. | Not implemented. |
| Search with unknown number of solutions | Use a validated adaptive/unknown-M procedure, then verify measured candidates classically. | Not implemented as a hardware workflow; the CPU reference learns M by exhaustive enumeration. |
| Chemistry or materials | Specify molecule/material, Hamiltonian, basis/active space, observable, decision threshold and baseline; select the appropriate chemistry algorithm. | Not implemented by this Grover package. A domain may or may not admit a useful Grover reduction. |
| QML classification | Define data, encoding, model, training, held-out evaluation and tuned classical baselines. | Not implemented; a search oracle is not a classifier. |
| State preparation, tomography, quantum simulation, QEC or communication | Choose the distinct mathematical task and a validated method designed for it. | Not implemented by Grover search in this repository. |

Grover's query advantage is relative to an oracle model. End-to-end value depends on how the oracle and input data are obtained and compiled. An application claim needs the actual reversible oracle, error target, hardware fit, total-cost accounting and strongest relevant classical baseline. Review the [original Grover paper](https://doi.org/10.1145/237814.237866) and [IBM's tutorial](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm) for the model and circuit steps.

## Case study: ground-penetrating radar (GPR)

**Possible research question:** after a radar survey has been converted into a finite set of candidate locations, could a reversible rule mark candidates that meet a carefully defined condition? That is a possible Grover-shaped search question, not a working GPR feature. The full path would still need to load the measurements, build and verify the reversible rule, account for its gates and extra qubits, and beat a strong classical analysis method on the same data and error target.

The supplied materials describe different tools and tasks:

- [gprMax](https://github.com/gprmax/gprMax) models electromagnetic wave propagation forward from a specified model. Its [current Python API guide](https://docs.gprmax.com/en/latest/input_api.html) builds simulation models; it does not connect to a radar sensor or diagnose a real slab. gprMax is a separate GPL-3.0-or-later project; this Apache-2.0 package includes no gprMax code.
- [FHWA's concrete-pavement GPR overview](https://infotechnology.fhwa.dot.gov/ground-penetrating-radar-defect-detection-concrete/) describes inspection applications and says interpretation requires trained personnel. Results depend on the survey and structure; this is not a general-purpose wall or building diagnostic.
- The [GPRBase ds021 card](https://huggingface.co/datasets/gprbase/ds021-3d-grid-reinforced-concrete) describes raw 3D survey data with a rebar mesh and diagonal cable. The card says the cable can resemble rebar in a single profile, that labels and train/test splits are absent, and that ground truth is partial. Its CC BY-NC-SA 4.0 terms do not become part of this package; no data files are included.
- The MIT [Wi-Vi paper](https://dspace.mit.edu/entities/publication/a67169b8-c328-469d-a67f-b54085b28797) studies motion sensing through walls. Its method suppresses static reflections to focus on moving targets, so it is not evidence for finding static concrete defects.
- Zircon's [StudSensor page](https://www.zircon.com/products/studsensor) describes a consumer tool for locating wood or metal stud edges behind drywall. It is a different sensor and target from concrete GPR.

The screenshot's blinded mock-wall fixture is a proposed test, not a reported result. It could help evaluate a future sensing pipeline: decide the position tolerance and false-alarm limit first, keep fixture truth hidden from the operator, use independent target locations, and report misses, false alarms, and position error separately. It would validate that sensing workflow—not Grover's query advantage.

**Status today:** this package can teach Grover search on small Boolean examples and can build a Qiskit phase oracle from an explicitly supplied short list of bitstrings. It has no GPR file reader, radar model, trained classifier, domain-specific oracle, sensor connection, or validated inspection workflow. Do not use its output to clear a drilling location or make a structural-safety decision. The sources and limits needed to understand this case are listed above; ChronoRealm keeps its longer research record outside this standalone package.
