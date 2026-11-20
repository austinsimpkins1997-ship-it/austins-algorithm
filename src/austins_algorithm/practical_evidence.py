"""Bounded accounting and repeatable public-toy comparisons; no provider calls."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
from datetime import datetime, timezone
from time import perf_counter


def source_review():
    from importlib.resources import files
    return json.loads(files('austins_algorithm').joinpath('workbench_assets/quantum_review.json').read_text(encoding='utf-8'))


def wilson_interval(successes: int, trials: int) -> dict:
    """Fixed 95% Wilson interval under an IID Bernoulli sampling assumption.

    This is not a probability of correctness, or a guarantee under correlated
    hardware noise. Zero trials is unknown, not a perfect zero-width interval.
    """
    if type(trials) is not int or type(successes) is not int or not 0 <= successes <= trials <= 1_000_000:
        raise ValueError('Require integer 0 <= successes <= trials <= 1,000,000')
    if trials == 0:
        return {'lower': None, 'upper': None, 'confidence': .95, 'method': 'WILSON_IID_BERNOULLI', 'trials': 0}
    z = 1.959963984540054
    p = successes / trials
    denominator = 1 + z*z/trials
    center = (p + z*z/(2*trials))/denominator
    half = z * math.sqrt(p*(1-p)/trials + z*z/(4*trials*trials))/denominator
    return {'lower': max(0.0, center-half), 'upper': min(1.0, center+half),
            'confidence': .95, 'method': 'WILSON_IID_BERNOULLI', 'trials': trials}


def comparison_accounting(comparison: dict) -> dict:
    """Summarize the fixed Austin experiment without approving advantage.

    Deliberately has no branch that declares practical advantage from imported
    report labels. Actual independent review and a useful workload are absent.
    Timers overlap: never add queue/running/usage to an enclosing client wall.
    """
    physical = comparison['hardware']
    hardware_observed = physical.get('quantum_hardware_used') is True and physical.get('measurement', {}).get('source') == 'QUANTUM_DEVICE'
    if hardware_observed:
        evidence_kind = 'REPORTED_HARDWARE_COMPARISON'
    elif physical.get('quantum_hardware_used') is True:
        evidence_kind = 'REPORTED_HARDWARE_RESULT_UNVERIFIED'
    elif physical.get('provider_job_submitted') is not False and physical.get('status') != 'NOT_REQUESTED':
        evidence_kind = 'PROVIDER_EXECUTION_UNRESOLVED'
    else:
        evidence_kind = 'LOCAL_SIMULATION_OR_PREFLIGHT'
    return {
        'schema': 'chronorealm.practical-evidence/1',
        'task': 'Public fixed three-bit teaching predicate; direct Boolean construction is available',
        'evidence_kind': evidence_kind,
        'classical_reference': 'Direct Boolean construction, not exhaustive simulation of a quantum circuit',
        'whole_comparison_wall_seconds': comparison['wall_seconds'],
        'classical_reference_wall_seconds': comparison['classical']['wall_seconds'],
        'circuit_construction_seconds': comparison['build_seconds'],
        'ideal_simulator_wall_seconds': comparison['ideal']['wall_seconds'],
        'noisy_simulator_wall_seconds': comparison['noisy']['wall_seconds'],
        'hardware_client_wall_seconds': physical.get('timing_seconds', {}).get('wall'),
        'provider_usage_seconds': physical.get('provider_reported_qpu_seconds'),
        'provider_monetary_cost': physical.get('provider_reported_monetary_cost'),
        'timing_relation': 'Whole comparison includes its stages; queue, running, usage and client wall overlap and must not be summed.',
        'usage_relation': 'Provider usage is not queue time, wall time, a local circuit duration or a monetary amount. Failed/canceled jobs may consume usage.',
        'practical_quantum_advantage': 'NOT_DEMONSTRATED',
        'production_acceptance': 'NOT_ESTABLISHED_BY_THIS_EXPERIMENT',
        'holds': ['This public toy has a direct classical solution and supplies no new useful-workload advantage.',
                  'A reported device/backend label is not independent authentication of a saved report.',
                  'A useful task, matched strong baselines, all costs, independent evaluation and human/deployment acceptance are required.'],
    }


def sampling_evidence(measurement):
    if 'completed_shots' not in measurement or 'verified_shots' not in measurement:
        return {}
    return {'verified_hit_rate_interval': wilson_interval(measurement['verified_shots'], measurement['completed_shots']),
            'interval_assumption': '95% Wilson interval assuming IID Bernoulli shots; correlated hardware noise may violate this assumption.'}


def _phase_scores(amplitudes):
    # CX(q2,q1), then H(q2), sends the equal-phase pair to |011>.
    # Both probabilities are unconditional, so leakage reduces the score.
    low, high = amplitudes[0b011], amplitudes[0b101]
    answer_probability = abs(low)**2 + abs(high)**2
    return answer_probability, abs((low + high) / math.sqrt(2))**2


def phase_diagnostic():
    """Test phase sensitivity of the Workbench's single-round Austin search on CPU."""
    from .optimization import preview

    problem = {'name': 'Austin phase check', 'clauses': [[1], [2, 3], [-2, -3]],
               'variables': [{'name': f'q{i}', 'cost': 0} for i in range(3)]}
    state = preview(problem, rounds=1)['frames'][-1]['amplitudes']
    cases = []
    for degrees, factor, expected in ((0, 1, 1.0), (90, 1j, 0.5), (180, -1, 0.0)):
        rotated = [amp * factor if index & 4 else amp for index, amp in enumerate(state)]
        basis, interference = _phase_scores(rotated)
        passed = math.isclose(basis, 1.0, abs_tol=1e-10, rel_tol=0) and math.isclose(
            interference, expected, abs_tol=1e-10, rel_tol=0)
        cases.append({'phase_degrees_on_q2': degrees, 'accepted_answer_probability': basis,
                      'interference_probability_011': interference, 'expected_interference': expected,
                      'matches_reference': passed})
    return {'schema': 'chronorealm.austin-phase-check/1', 'mode': 'IDEAL_CPU_STATEVECTOR',
            'basis_order': 'q2 q1 q0', 'valid_answers': ['011', '101'],
            'readout': 'CX(q2,q1), H(q2), then probability of 011', 'absolute_tolerance': 1e-10,
            'cases': cases, 'passed': all(row['matches_reference'] for row in cases)}


def benchmark(*, repeats=32, shots=64, seed=701):
    """Compare the same complete answer set; all work is ordinary CPU work."""
    for name, value, lower, upper in (('repeats', repeats, 1, 128), ('shots', shots, 1, 256), ('seed', seed, 0, 2**31-129)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f'{name} must be an integer in {lower}..{upper}')
    from .core import austin_predicate, run_austin_search
    from .device_experiment import austin_classical_baseline
    from . import __version__
    protocol = {'schema': 'chronorealm.practical-protocol/1', 'registered_at': datetime.now(timezone.utc).isoformat(),
                'task': 'Return both valid answers for the fixed public Austin predicate',
                'expected': ['011', '101'], 'repeats': repeats, 'shots': shots, 'seed': seed, 'rounds': 1,
                'baseline': 'Direct Boolean construction followed by verification',
                'candidate': 'Classical ideal statevector simulation with exhaustive preprocessing',
                'order': 'Alternate baseline-first and simulator-first',
                'criterion': 'Exact equality with the complete independently specified answer set',
                'phase_control': 'After timed trials, evaluate 0/90/180 degree q2 rotations. Expect answer probability 1 and interference 1, 0.5, 0 (absolute tolerance 1e-10).',
                'resource_ceiling': {'predicate_preprocessing_evaluations': repeats*8, 'sampled_outcomes': repeats*shots}}
    protocol['source_review_sha256'] = hashlib.sha256(json.dumps(source_review(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    rows = []
    entire = perf_counter()
    for index in range(repeats):
        row = {'seed': seed+index, 'order': 'classical-first' if index%2 == 0 else 'simulator-first'}
        for name in (('classical', 'simulator') if index%2 == 0 else ('simulator', 'classical')):
            started = perf_counter()
            if name == 'classical':
                bits = set(austin_classical_baseline()['solutions'])
            else:
                bits = set(run_austin_search(3, austin_predicate, iterations=1, shots=shots, seed=seed+index)['sample_counts'])
            checked = perf_counter()
            # Independent expression and exact answer-set check; no caller oracle.
            valid = bits == {'011', '101'} and all(b[2] == '1' and b[0] != b[1] for b in bits)
            finished = perf_counter()
            row[name] = {'answers': sorted(bits), 'verified': valid,
                         'solve_and_internal_check_seconds': checked-started,
                         'independent_check_seconds': finished-checked, 'total_seconds': finished-started}
        rows.append(row)
    total_wall = perf_counter()-entire
    sums = {name: sum(row[name]['total_seconds'] for row in rows) for name in ('classical', 'simulator')}
    return {'schema': 'chronorealm.practical-comparison/1', 'protocol': protocol,
            'phase_diagnostic': phase_diagnostic(),
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'evidence_kind': 'LOCAL_CPU_OBSERVATIONS',
            'python': platform.python_version(), 'package_version': __version__,
            'source_sha256': {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                              if (Path(__file__).parent/name).is_file() else None
                              for name in ('__init__.py', 'core.py', 'device_experiment.py', 'practical_evidence.py', 'optimization.py')},
            'trials': rows,
            'all_output_sets_verified': all(row[name]['verified'] for row in rows for name in sums),
            'timing': {'whole_benchmark_seconds': total_wall, 'totals_seconds': sums,
                       'simulator_to_classical_ratio': None if sums['classical'] == 0 else sums['simulator']/sums['classical'],
                       'medians_seconds': {name: statistics.median(row[name]['total_seconds'] for row in rows) for name in sums}},
            'quantum_hardware_used': False, 'provider_job_submitted': False,
            'practical_quantum_advantage': 'NOT_DEMONSTRATED', 'operator_observations': 0,
            'production_acceptance': 'NOT_ESTABLISHED',
            'next_action': 'Use direct construction for this public toy; use simulation for teaching and circuit checks.',
            'limits': ['Repeated seeds are not fresh problems or physical trials.',
                       'Small CPU timings are descriptive, not a general performance claim.',
                       'Shot noise may omit an answer; low-shot failures remain failures rather than disappearing.']}


def export_benchmark(directory, result):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    members = {'protocol.json': result['protocol'], 'comparison.json': result, 'source-review.json': source_review()}
    for name, value in members.items():
        (directory/name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    summary = ('Austin practical evidence — CPU only\n\n' +
               f"Complete answer sets verified: {result['all_output_sets_verified']}\n" +
               f"Trials: {result['protocol']['repeats']}; shots/trial: {result['protocol']['shots']}\n" +
               f"Total seconds: {result['timing']['totals_seconds']}\n\n" +
               result['next_action']+'\nPractical quantum advantage: NOT DEMONSTRATED.\nProduction acceptance: NOT ESTABLISHED.\n')
    if 'phase_diagnostic' in result:
        check = result['phase_diagnostic']
        summary += '\nRelative phase check on the three-qubit search (ideal CPU probabilities)\n'
        for case in check['cases']:
            summary += (f"{case['phase_degrees_on_q2']} degrees: valid answers "
                        f"{case['accepted_answer_probability']:.1%}, interference "
                        f"{case['interference_probability_011']:.1%}\n")
        summary += ('Phase check passed.\n' if check['passed'] else 'Phase check failed.\n')
    (directory/'SUMMARY.txt').write_text(summary, encoding='utf-8')
    manifest = {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in (*members, 'SUMMARY.txt')}
    (directory/'manifest.json').write_text(json.dumps({'algorithm': 'SHA-256', 'files': manifest}, indent=2)+'\n')
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Bounded offline practical-evidence check; never submits provider work')
    parser.add_argument('--output', type=Path, required=True, help='New output directory')
    parser.add_argument('--repeats', type=int, default=32)
    parser.add_argument('--shots', type=int, default=64)
    parser.add_argument('--seed', type=int, default=701)
    args = parser.parse_args(argv)
    try:
        # Validate numerical bounds before creating output or doing the work.
        result = benchmark(repeats=args.repeats, shots=args.shots, seed=args.seed)
        summary = export_benchmark(args.output, result)
        print(summary)
        if not result['all_output_sets_verified'] or not result['phase_diagnostic']['passed']:
            raise SystemExit(1)
    except (ValueError, OSError) as error:
        parser.error(str(error))
