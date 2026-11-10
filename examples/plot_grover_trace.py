"""Plot Grover's ideal rotation beside a finite-shot sample of its outcomes.

Requires the optional ``visualization`` extra. The plotted shots are sampled
from the ideal classical statevector distribution; they include sampling
variation, not hardware noise or a quantum-device result.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from austins_algorithm import (
    AustinAlgorithmError,
    austin_predicate,
    run_austin_search,
    validate_marked_states,
)


MAX_PLOT_QUBITS = 5


def _make_plot(result: dict, output: str | None) -> None:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.patches import Circle, Patch
    except ImportError as exc:
        raise SystemExit(
            'Matplotlib is optional. Install it with: python -m pip install ".[visualization]"'
        ) from exc

    qubits = result["qubits"]
    total = result["search_space_size"]
    marked_count = result["solution_count_known_by_classical_enumeration"]
    if not 0 < marked_count < total:
        raise SystemExit(
            "The rotation-plane plot needs at least one marked and one unmarked answer."
        )

    theta = math.asin(math.sqrt(marked_count / total))
    first_peak_rounds = result["recommended_iterations_given_known_solution_count"]
    round_label = "round" if first_peak_rounds == 1 else "rounds"
    points = result["amplitude_trace"]
    last_point = points[-1]
    marked_amplitude = last_point["marked_amplitude_per_state"]
    unmarked_amplitude = last_point["unmarked_amplitude_per_state"]
    marked_states = set(result["solution_examples"])
    basis_states = [format(index, f"0{qubits}b") for index in range(total)]

    ideal_probabilities = [
        (marked_amplitude if state in marked_states else unmarked_amplitude) ** 2
        for state in basis_states
    ]
    sampled_counts = result["sample_counts"]
    sampled_frequencies = [sampled_counts.get(state, 0) / result["shots"] for state in basis_states]
    colors = ["#df7a36" if state in marked_states else "#4c83b6" for state in basis_states]

    fig, axes = plt.subplots(1, 3, figsize=(16, 5), layout="constrained")
    fig.suptitle(
        f"Grover learning view  |  θ = arcsin(sqrt(M/N)) = {theta:.3f} rad  |  "
        f"turn per round = 2θ = {2 * theta:.3f} rad\n"
        f"Known-M first-peak estimate: {first_peak_rounds} {round_label}",
        fontsize=12,
    )

    rotation_ax, ideal_ax, sample_ax = axes
    rotation_ax.add_patch(Circle((0, 0), 1, fill=False, color="#9aa7b2", linewidth=1.2))
    xs = [point["unmarked_plane_coordinate"] for point in points]
    ys = [point["marked_plane_coordinate"] for point in points]
    rotation_ax.plot(xs, ys, color="#365f91", linewidth=1.6, marker="o", markersize=6)
    if len(points) <= 12:
        for index in range(len(points) - 1):
            rotation_ax.annotate(
                "",
                xy=(xs[index + 1], ys[index + 1]),
                xytext=(xs[index], ys[index]),
                arrowprops={"arrowstyle": "->", "color": "#365f91", "lw": 1.1},
            )
    label_indices = range(len(points)) if len(points) <= 10 else (0, len(points) - 1)
    for index in label_indices:
        rotation_ax.annotate(
            str(points[index]["iteration"]),
            (xs[index], ys[index]),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=8,
        )
    rotation_ax.axhline(0, color="#c5ccd2", linewidth=0.7)
    rotation_ax.axvline(0, color="#c5ccd2", linewidth=0.7)
    rotation_ax.set(
        title="Ideal amplitude rotation",
        xlabel="Unmarked coordinate",
        ylabel="Marked coordinate",
        xlim=(-1.15, 1.15),
        ylim=(-1.15, 1.15),
        aspect="equal",
    )
    rotation_ax.grid(alpha=0.2)
    rotation_ax.legend(
        handles=[Line2D([], [], color="#365f91", marker="o", label="State after each round")],
        loc="lower left",
        fontsize=8,
    )

    ideal_ax.bar(basis_states, ideal_probabilities, color=colors)
    sample_ax.bar(basis_states, sampled_frequencies, color=colors)
    ideal_ax.set_title(f"Ideal probabilities after round {result['iterations']}")
    sample_ax.set_title(f"Finite-shot sample ({result['shots']} shots)")
    for axis in (ideal_ax, sample_ax):
        axis.set_xlabel("Answer")
        axis.set_ylabel("Probability / observed fraction")
        axis.tick_params(axis="x", labelrotation=45)
        axis.grid(axis="y", alpha=0.2)
        axis.set_ylim(bottom=0)
        axis.legend(
            handles=[
                Patch(color="#df7a36", label="Marked answer"),
                Patch(color="#4c83b6", label="Unmarked answer"),
            ],
            fontsize=8,
        )
    upper = max(max(ideal_probabilities), max(sampled_frequencies), 0.05) * 1.18
    ideal_ax.set_ylim(0, upper)
    sample_ax.set_ylim(0, upper)
    fig.text(
        0.5,
        0.005,
        "The first-peak estimate assumes known M (counted classically here). Finite-shot bars sample the ideal distribution; no gate/readout noise or QPU is modeled.",
        ha="center",
        fontsize=9,
    )

    if output:
        output_path = Path(output)
        if not output_path.is_absolute():
            output_path = Path(__file__).resolve().parents[1] / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=180, bbox_inches="tight")
        print(f"Saved plot: {output_path}")
    else:
        plt.show()
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show ideal Grover rotation and finite-shot sampling from the local simulator."
    )
    parser.add_argument("--qubits", type=int, default=3)
    parser.add_argument(
        "--marked",
        nargs="+",
        help="explicit marked bitstrings (MSB-first); defaults to the Austin three-qubit example",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=2,
        help="rounds to show (default: 2, which demonstrates overshooting the Austin example's first peak)",
    )
    parser.add_argument("--shots", type=int, default=256)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", help="save the figure here instead of opening a plot window")
    args = parser.parse_args()

    if not 1 <= args.qubits <= MAX_PLOT_QUBITS:
        parser.error(f"the plot is limited to 1–{MAX_PLOT_QUBITS} qubits so answer labels stay readable")

    try:
        if args.marked is None:
            if args.qubits != 3:
                parser.error("the built-in Austin rule uses 3 qubits; pass --marked for another search")
            predicate = austin_predicate
            predicate_name = "q0 == 1 and q1 != q2"
        else:
            marked_states = set(validate_marked_states(args.qubits, args.marked))
            predicate = lambda bits: bits in marked_states
            predicate_name = "caller-supplied marked bitstrings"

        result = run_austin_search(
            args.qubits,
            predicate,
            iterations=args.iterations,
            shots=args.shots,
            seed=args.seed,
            predicate_name=predicate_name,
            include_amplitude_trace=True,
        )
    except AustinAlgorithmError as exc:
        parser.error(str(exc))

    _make_plot(result, args.output)


if __name__ == "__main__":
    main()
