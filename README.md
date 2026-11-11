# Austin's Algorithm

**Version:** 0.1.0 · **Status:** source repository is public on GitHub; no release tag is present · **License:** [Apache-2.0](LICENSE)

**Project credit:** Austin Gregory Simpkins and Chandler Middlebrook / ChronoRealm.

For a short, ordered, beginner-friendly walkthrough, start with [`GETTING_STARTED.md`](GETTING_STARTED.md). This README remains the package overview and reference.

## What is this?

This is a small program for learning how Grover's quantum search works. In ChronoRealm, **Austin's Algorithm** is the name for our example search problem. The search method comes from Lov K. Grover's published algorithm. This package does not claim to be a brand-new kind of quantum algorithm.

You can try the example on an ordinary computer. The basic program uses regular Python and does not need a quantum computer. It uses math to imitate an ideal quantum circuit, so its results are for learning—not proof of a real-world speed boost.

## The Austin example

A **qubit** is a quantum computer's kind of bit. When measured, it gives `0` or `1`. For this small example, picture three light switches, each either `0` (off) or `1` (on). There are eight possible settings. Our rule accepts a setting when:

- `q0` is on; and
- `q1` and `q2` are different.

The accepted settings are `011` and `101`. The example uses Grover's search steps to make those answers more likely to appear when the circuit is measured. For this tiny, ideal example, one search round gives only accepted answers.

The strings are written from left to right as `q2 q1 q0`. That is why `q0` is the last digit.

## Try it on your computer

You need Python 3.10 or newer. Open a terminal in the folder that contains this README and `pyproject.toml`.

The `.venv` folder is a private place for this project's Python tools. It helps keep them separate from other projects on your computer.

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m austins_algorithm
```

### macOS or Linux

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/python -m austins_algorithm
```

The program prints a JSON report—a text page with labeled results. Look for these fields:

- `solution_examples`: answers that match the rule (`011` and `101` here).
- `recommended_iterations_given_known_solution_count`: the number of search rounds suggested after the simulator counts the matching answers (one).
- `statevector_success_probability`: the ideal math model's chance of finding a matching answer (1.0 here).
- `status`: says that this was a **classical computer simulation**, not a quantum computer run.

The simulator repeats the measurement many times (called `shots`) and shows the results. Because this example is ideal and so small, each sample should be one of the two accepted strings.

### Watch the amplitudes change

To include the starting amplitudes and the amplitudes after each Grover round in the JSON report, run:

```powershell
.\.venv\Scripts\python.exe -m austins_algorithm --trace-amplitudes
```

Or turn the trace on in Python:

```python
from austins_algorithm import austin_predicate, run_austin_search

result = run_austin_search(3, austin_predicate, include_amplitude_trace=True)
for point in result["amplitude_trace"]:
    print(point["iteration"], point["unmarked_plane_coordinate"], point["marked_plane_coordinate"])
```

The trace starts at round `0`, before Grover runs, then adds one point after each full oracle-and-diffuser round. The two plane coordinates show the ideal state's position along the unmarked and marked parts of the search space. For the built-in three-qubit example, the point moves from about `(0.866, 0.5)` to `(0, 1)` after one round. Per-state amplitudes are included too. This is the ordinary computer's ideal math simulation; it is not a measurement from quantum hardware. The trace is off by default so existing reports stay compact.

To see the rotation and sampling in one picture, install the optional plotting extra and run the example:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[visualization]"
.\.venv\Scripts\python.exe .\examples\plot_grover_trace.py
```

The figure from [`examples/plot_grover_trace.py`](examples/plot_grover_trace.py) shows the marked/unmarked rotation, with `θ = arcsin(sqrt(M/N))` and a `2θ` turn for each Grover round, beside two histograms: ideal per-answer probabilities and frequencies from a finite number of simulated shots. It also displays the recommended first-peak round count for the known number of marked answers. The CPU simulator finds that count by checking every candidate, so this is not an unknown-answer-count method. The default uses two rounds to make it easier to see that extra rounds can pass the Austin example's one-round first peak. Change `--iterations`, `--shots`, and `--seed` to explore. The shot histogram samples the ideal classical distribution; it does not add gate or readout noise and does not use quantum hardware. The plot is optional and limited to five qubits so its answer labels remain readable; the simulator's separate limit is 16 qubits.

## Try your own simple rule

You can change the rule and ask the program to search four-bit strings whose first two bits are `11`:

```python
from austins_algorithm import run_austin_search

def starts_with_11(bits):
    return bits.startswith("11")

result = run_austin_search(
    4,
    starts_with_11,
    shots=512,
    seed=7,
)

print("Answers:", result["solution_examples"])
print("Ideal chance:", result["statevector_success_probability"])
print("What ran:", result["status"])
```

The answers are `1100`, `1101`, `1110`, and `1111`. Save the code as `my_search.py` in this folder. Run it with:

```powershell
.\.venv\Scripts\python.exe .\my_search.py
```

On macOS or Linux, run `./.venv/bin/python ./my_search.py`. The `seed` makes the simulated samples repeat the same way when you run the example again.

**What the computer does:** it checks all 16 strings with your rule, then calculates what an ideal Grover circuit would do. That makes it useful for learning and checking small examples. It does not show that a quantum computer searched faster.

## What Grover's search is doing

Grover's method has four simple ideas:

1. Put all possible answers into a mix called a **superposition**.
2. Use an **oracle**: a circuit that recognizes the right answers and gives them a tiny math sign change called a phase flip.
3. Use a **diffuser**, a math step that boosts the chance of finding the marked answers.
4. **Measure** the circuit to get an answer.

The example's oracle is made for its exact three-switch rule. A useful search for another real problem needs an oracle for that problem; changing a label does not create one automatically.

The package includes both a teaching rule that runs on a regular computer and quantum-circuit oracles for the project example and explicitly listed marked answers. These are different things: the regular-computer rule is checked against every candidate, and an arbitrary Python function cannot be converted automatically into an efficient quantum circuit. See [`GETTING_STARTED.md`](GETTING_STARTED.md) for the steps and limits.

Want to connect a different simulator or quantum computer? The package includes an offline adapter for the classical simulator and an experimental interface for future plug-ins, but it has no real hardware connection. Each adapter request must name the kind of run it wants, such as a simulator or QPU, so a plug-in cannot quietly switch to a different kind of run. See [`ADAPTERS.md`](ADAPTERS.md) for the input-to-output path and what a new adapter must report. A device that has not been released cannot be connected until its real instructions and tools are known.

## Optional circuit examples

The first example above needs no quantum software. If you want to build and run a circuit in a local simulator, install one optional tool from this folder:

The basic teaching program supports Python 3.10 and newer. Use Python 3.11 or newer for the current Cirq releases and the `[all]` extra. These optional examples were run locally with Python 3.12.15, Qiskit 2.6.2, and Cirq 1.7.0.

The commands below are for Windows PowerShell. On macOS or Linux, replace `.\.venv\Scripts\python.exe` with `./.venv/bin/python`.

```powershell
.\.venv\Scripts\python.exe -m pip install ".[qiskit]"
.\.venv\Scripts\python.exe examples\qiskit_austin_circuit.py

.\.venv\Scripts\python.exe -m pip install ".[cirq]"
.\.venv\Scripts\python.exe examples\cirq_austin_circuit.py
```

The Qiskit example draws `build_austin_circuit()` and samples that same circuit through `run_austin_circuit_local_statevector()`. That runner uses the built-in two-CZ reversible oracle and diffuser, then reports measured counts and ideal success probability. `LOCAL_IDEAL_STATEVECTOR_SIMULATION` means Qiskit simulated the circuit on an ordinary computer; the runner submits no provider job and uses no quantum hardware. The generic `run_local_statevector(...)` helper remains a separate route for circuits whose marked bitstrings are supplied explicitly.

These examples run locally. They do not sign in to a service or send work to a real quantum computer. A real hardware run needs a supported device, an account and permission to use it, a circuit fitted to that device, and checks for errors and cost. Hardware access is not included here.

## What this package can and cannot do

It is useful for:

- learning the steps in Grover search;
- trying small search rules and checking the expected answers;
- looking at example Qiskit and Cirq circuits; and
- practicing how to test a quantum algorithm against a regular-computer method.

The built-in regular-computer simulator is limited to 16 qubits. This is a limit of this teaching program, not a limit on quantum computers. Its work grows very quickly as you add more qubits.

This package does not provide a complete tool for every part of quantum computing. Chemistry, machine learning, quantum communication, and other fields have different problems and may need different algorithms. To test a new use, first say what problem it should solve, build the right circuit, compare it fairly with good regular-computer methods, and check the costs and errors. Grover's search is not automatically the best choice.

One possible adaptation use is searching a finite set of fully specified options against explicit hard constraints. That would require an efficient, correct reversible oracle and a fair classical comparison. Search cannot discover missing context, infer a person's motive, or approve an action; evidence review, fairness, permissions, and recovery remain outside Grover's circuit. This package has no adaptation-system bridge, and this example does not claim a practical quantum advantage.

For readers who want the short math version: `N` is the number of choices, `M` is the number of right answers, and `r` is the number of Grover rounds. `θ` (theta) is a number set by how many answers are right. The ideal chance is

\[
P_r=\sin^2((2r+1)\theta),\qquad \sin^2(\theta)=M/N.
\]

The simulator finds `M` by checking every choice first. A real quantum search needs a circuit that checks the rule without doing that full classical list first.

## Tests and project guide

To run the package checks:

```powershell
python -B -m unittest discover -s tests -v
```

The package has more detail in [`USE_CASES.md`](USE_CASES.md), [`EXTENDING.md`](EXTENDING.md), [`ADAPTERS.md`](ADAPTERS.md), and [`ROADMAP.md`](ROADMAP.md). The test suite checks the example code and math; it does not certify a real device or prove that the method is useful in every field.

## Credit and sources

This package credits **Austin Gregory Simpkins and Chandler Middlebrook / ChronoRealm** for the project example. The search method is based on **Lov K. Grover's** 1996 paper, [“A fast quantum mechanical algorithm for database search”](https://doi.org/10.1145/237814.237866). IBM also has a [Grover's algorithm tutorial](https://quantum.cloud.ibm.com/docs/en/tutorials/grovers-algorithm).
