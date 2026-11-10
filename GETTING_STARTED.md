# Getting started

This guide shows how to run the small Austin's Algorithm example and how to try a rule of your own. You can do the first steps on an ordinary computer.

## 1. Install and run the example

You need Python 3.10 or newer. Open PowerShell or a terminal in this package folder—the folder containing `pyproject.toml`.

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m austins_algorithm
```

**macOS or Linux**

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install .
./.venv/bin/python -m austins_algorithm
```

The program prints a report. In the example, the answers are `011` and `101`. The report says **classical ideal simulation** because a regular computer did the work. It is not a quantum-computer result.

## 2. Try a rule of your own

Save this as `my_search.py` in the package folder. This rule accepts four-bit answers that start with `11`:

```python
from austins_algorithm import run_austin_search

def starts_with_11(bits: str) -> bool:
    return bits.startswith("11")

result = run_austin_search(4, starts_with_11, shots=512, seed=7)
print("Answers:", result["solution_examples"])
print("Run type:", result["status"])
```

Run it from PowerShell with:

```powershell
.\.venv\Scripts\python.exe .\my_search.py
```

On macOS or Linux, run `./.venv/bin/python ./my_search.py`.

This teaching program checks every possible string on the regular computer, then models ideal Grover math. It is useful for learning and small checks; it does not show a speed advantage. The built-in simulator stops at 16 qubits to limit ordinary-computer memory and work.

To watch the amplitudes change, add `include_amplitude_trace=True` to the Python call. The report then includes the starting point (round `0`) and one point after each full Grover round. `unmarked_plane_coordinate` and `marked_plane_coordinate` are the two numbers for a simple picture of the Grover rotation. You can also add `--trace-amplitudes` when running the command-line program. This is an optional trace from the ordinary computer's ideal math simulation, not data from quantum hardware.

For a picture, install the optional plotting tool and run [`examples/plot_grover_trace.py`](examples/plot_grover_trace.py):

```powershell
.\.venv\Scripts\python.exe -m pip install ".[visualization]"
.\.venv\Scripts\python.exe .\examples\plot_grover_trace.py
```

The example puts the rotation path next to an ideal probability chart and a finite-shot sample chart. It also shows the recommended first-peak round count, based on `θ = arcsin(sqrt(M/N))` and a `2θ` turn each round. The computer finds `M` by checking all candidates, so that recommendation assumes the solution count is known. The sample chart shows ordinary random sampling from the ideal math, not noise measured from a quantum device. Use `--output .build/my_plot.png` to save the figure instead of opening a window.

## 3. Understand the oracle

An **oracle** is the part that answers, “Does this candidate match the rule?” In a quantum circuit, it must do that reversibly and mark matching answers by changing their phase. Grover's **diffuser** then increases the chance that a measurement returns a marked answer.

This package already includes:

- the exact three-qubit Austin example circuit, with its reversible phase oracle; and
- `build_phase_oracle(n_qubits, marked_states)` in the optional Qiskit module, which builds a circuit for a short, explicit list of bitstrings.

The Python function in step 2 is a **classical teaching rule**, not a quantum oracle. The package cannot automatically turn any Python function into an efficient reversible circuit. Listing all the answers for a large problem can also cost as much as checking the whole search space, so it may erase the reason to use Grover search.

## 4. Optional: see the quantum circuit

These commands install Qiskit only in this project's virtual environment and print/run the local example:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[qiskit]"
.\.venv\Scripts\python.exe .\examples\qiskit_austin_circuit.py
```

On macOS or Linux, replace the Python path with `./.venv/bin/python`. This optional example is still a local simulation. It does not sign in, upload data, or submit a hardware job.

## 5. Before applying it to a real problem

Follow these steps before calling a result useful:

1. **Name the decision.** Say exactly what answer someone needs and what mistakes would cost.
2. **Make the choices finite.** Define every candidate, its bit-string encoding, units, and bit order.
3. **Write the yes/no rule.** Test it on every small example where the right answer is known.
4. **Build a reversible oracle.** Specify its gates, temporary qubits, cleanup, and resource cost. Do not treat a Python callback as this circuit.
5. **Choose search rounds.** Grover's best round count depends on how many answers match. This package's automatic choice assumes that count is known; it does not provide an unknown-answer-count hardware method.
6. **Check the target.** Compile for a named simulator or released device and verify its supported gates, connections, width, errors, and job rules. Compilation alone is not a run.
7. **Compare fairly.** Use the strongest suitable regular-computer method on the same inputs and output. Count data loading, oracle building, compilation, queue time, device time, error handling, and answer checking.
8. **Report what happened.** Name the versions, settings, measurements, failures, costs, and what was not tested. Verify returned candidates with an independent classical check.

## 6. Know what is included

This package teaches a small example of Grover's search. Its name does not mean it is a new family of algorithms or that it solves every quantum problem. Chemistry, machine learning, radar analysis, error correction, and communication each need a method designed for that task. No QPU/provider connection, radar reader, GPR model, or inspection diagnosis is included.

**Small glossary:** a **candidate** is one possible answer; a **qubit** is a quantum information unit; a **reversible circuit** can be run backward without losing information; a **measurement** is the final recorded answer; a **baseline** is the best fair method used for comparison.

For the full project boundaries, see [`README.md`](README.md), [`USE_CASES.md`](USE_CASES.md), [`ADAPTERS.md`](ADAPTERS.md), and [`EXTENDING.md`](EXTENDING.md).
