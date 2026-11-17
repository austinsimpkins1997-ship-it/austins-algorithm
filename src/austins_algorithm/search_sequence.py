"""Bounded sequencing of a public three-bit teaching oracle, not cryptanalysis."""
from __future__ import annotations

from time import perf_counter
from .planning import integer
from .core import austin_predicate, run_austin_search


def validate(value):
    if not isinstance(value, dict) or set(value) != {'rounds', 'shots', 'seed'}:
        raise ValueError('Sequence requires rounds, shots and seed; arbitrary oracles and targets are unsupported')
    rounds = value['rounds']
    if not isinstance(rounds, list) or not 1 <= len(rounds) <= 9 or any(type(n) is not int for n in rounds) or len(set(rounds)) != len(rounds):
        raise ValueError('Choose 1–9 distinct round counts')
    for count in rounds:
        integer(count, 0, 8, 'Rounds')
    integer(value['shots'], 1, 256, 'Shots per sequence')
    integer(value['seed'], 0, 2**31 - 2, 'Seed')
    return {'rounds': sorted(rounds), 'shots': value['shots'], 'seed': value['seed']}


def stages(rounds):
    integer(rounds, 0, 8, 'Rounds')
    return ['prepare'] + [stage for _ in range(rounds) for stage in ('oracle', 'diffuse')] + ['measure', 'verify', 'checkpoint']


def experiment(value, *, stop=lambda: False):
    value = validate(value)
    started = perf_counter()
    evaluated = []
    for rounds in value['rounds']:
        if stop():
            return {'status': 'CANCELLED', 'candidates': evaluated, 'quantum_advantage': 'NOT_DEMONSTRATED'}
        report = run_austin_search(3, austin_predicate, iterations=rounds, shots=value['shots'], seed=value['seed'], include_amplitude_trace=True)
        evaluated.append({'rounds': rounds, 'stages': stages(rounds), 'report': report,
                          'cpu_predicate_preprocessing_evaluations': 8,
                          'query_model_oracle_calls_for_all_shots': rounds * value['shots']})
    # Selection uses declared ideal-model quality, never the held-out samples.
    eligible = [c for c in evaluated if c['report']['statevector_success_probability'] >= 1 - 1e-9]
    selected = min(eligible, key=lambda c: c['rounds']) if eligible else max(evaluated, key=lambda c: (c['report']['statevector_success_probability'], -c['rounds']))
    independent = run_austin_search(3, austin_predicate, iterations=selected['rounds'], shots=value['shots'], seed=value['seed'] + 1)
    checked = {bits: ((bits[0] != bits[1]) and bits[2] == '1') for bits in independent['sample_counts']}
    # Independent expression above is the original public Austin predicate.
    verification = all(checked[bits] for bits in independent['sample_counts'])
    baseline_start = perf_counter()
    baseline = next(bits for bits in ('000', '001', '010', '011', '100', '101', '110', '111') if (bits[0] != bits[1]) and bits[2] == '1')
    return {'status': 'VERIFIED_SAMPLES' if verification else 'UNVERIFIED_SAMPLES_PRESENT',
            'oracle': 'Public Austin three-bit teaching predicate', 'selected_rounds': selected['rounds'],
            'selection_basis': 'Lowest modeled oracle count reaching ideal probability 1, otherwise best modeled probability; no hardware optimization claim',
            'candidates': evaluated, 'held_out_seed': value['seed'] + 1, 'held_out_result': independent,
            'independent_sample_verification': checked, 'classical_baseline': {'first_match': baseline, 'predicate_evaluations': 4, 'seconds': perf_counter() - baseline_start,
            'stronger_structured_baseline': 'The public known expression directly constructs 011; exhaustive search is not the strongest baseline.'},
            'work': {'cpu_preprocessing_evaluations': 8 * (len(evaluated) + 1),
                     'distinct_candidate_verifications': len(checked), 'sampled_outcomes_validated': value['shots'],
                     'elapsed_seconds': perf_counter() - started},
            'quantum_advantage': 'NOT_DEMONSTRATED', 'quantum_hardware_used': False,
            'limits': ['Query-model reductions exclude oracle construction, CPU simulation and physical error correction.',
                       'This public toy predicate is not a password, hash, encryption key or hidden target.',
                       'Finite samples and ideal-model selection do not establish general reliability or speedup.']}


def circuit_qasm(rounds):
    integer(rounds, 0, 8, 'Rounds')
    from qiskit import qasm3
    from .grover_search import build_austin_phase_oracle, build_grover_search_circuit
    return qasm3.dumps(build_grover_search_circuit(build_austin_phase_oracle(), 3, iterations=rounds))
