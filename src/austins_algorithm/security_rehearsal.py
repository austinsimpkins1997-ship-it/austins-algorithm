"""Synthetic authentication-policy experiments, never a live authentication boundary."""
from __future__ import annotations

from .planning import API_VERSION, integer, solve

SCHEMA = 'chronorealm.security-rehearsal/1'
POLICY_KEYS = {'max_failures', 'window_ms', 'delay_ms', 'shared_budget', 'preserve_restart'}
FIXTURES = ('account-burst', 'shared-paths', 'restart', 'independent-user', 'expiry')


def policy(value):
    if not isinstance(value, dict) or set(value) != POLICY_KEYS:
        raise ValueError('Policy must contain exactly the documented synthetic controls')
    integer(value['max_failures'], 1, 10, 'Failure ceiling')
    integer(value['window_ms'], 1000, 120000, 'Window milliseconds')
    integer(value['delay_ms'], 0, 10000, 'Progressive delay milliseconds')
    if any(type(value[k]) is not bool for k in ('shared_budget', 'preserve_restart')):
        raise ValueError('Policy switches must be booleans')
    return dict(value)


def example_document():
    return {'schema_version': SCHEMA, 'policy': {'max_failures': 3, 'window_ms': 60000,
            'delay_ms': 0, 'shared_budget': True, 'preserve_restart': True},
            'fixtures': list(FIXTURES), 'sequence': {'rounds': [0, 1, 2, 3], 'shots': 64, 'seed': 17}}


def document(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'policy', 'fixtures', 'sequence'} or value['schema_version'] != SCHEMA:
        raise ValueError('Unsupported security lab input; URLs, targets and credentials are not accepted')
    p = policy(value['policy'])
    names = value['fixtures']
    if not isinstance(names, list) or not names or len(names) > len(FIXTURES) or any(not isinstance(n, str) or n not in FIXTURES for n in names) or len(set(names)) != len(names):
        raise ValueError('Choose distinct built-in synthetic fixtures')
    from .search_sequence import validate
    return {'schema_version': SCHEMA, 'policy': p, 'fixtures': list(names), 'sequence': validate(value['sequence'])}


def simulate(p, events):
    """Atomic ordered-event model; valid is fixture data, never a password verifier."""
    p = policy(p)
    if not isinstance(events, list) or len(events) > 128:
        raise ValueError('At most 128 synthetic events')
    history = {}
    trace = []
    previous = -1
    for event in events:
        if not isinstance(event, dict) or set(event) != {'t_ms', 'account', 'path', 'valid', 'restart'}:
            raise ValueError('Invalid synthetic event shape')
        t = integer(event['t_ms'], 0, 240000, 'Simulated time')
        if t < previous:
            raise ValueError('Event time went backwards')
        previous = t
        if event['account'] not in ('synthetic-a', 'synthetic-b') or event['path'] not in ('login', 'reset', 'otp'):
            raise ValueError('Only synthetic accounts and named paths are accepted')
        if type(event['valid']) is not bool or type(event['restart']) is not bool:
            raise ValueError('Event flags must be booleans')
        if event['restart']:
            if not p['preserve_restart']:
                history.clear()
            trace.append({**event, 'admitted': False, 'reason': 'SIMULATED_RESTART'})
            continue
        key = (event['account'], '*' if p['shared_budget'] else event['path'])
        failures = [stamp for stamp in history.get(key, []) if t - stamp < p['window_ms']]
        history[key] = failures
        ceiling = len(failures) >= p['max_failures']
        delayed = bool(failures and t < failures[-1] + min(p['window_ms'], p['delay_ms'] * len(failures)))
        admitted = not (ceiling or delayed)
        reason = 'FAILURE_CEILING' if ceiling else 'PROGRESSIVE_DELAY' if delayed else 'ADMITTED'
        if admitted:
            if event['valid']:
                history[key] = []
            else:
                history[key].append(t)
        trace.append({**event, 'admitted': admitted, 'reason': reason, 'retained_failures': len(history[key])})
    return trace


def fixtures(p):
    def event(t, *, account='synthetic-a', path='login', valid=False, restart=False):
        return {'t_ms': t, 'account': account, 'path': path, 'valid': valid, 'restart': restart}
    n = p['max_failures']
    # Space admitted failures far enough apart to isolate the ceiling, not the delay.
    gap = max(1, p['delay_ms'] * n)
    if gap * (n + 2) >= p['window_ms']:
        gap = 1  # The fixture then measures the combined delay/ceiling behavior explicitly.
    burst = [event(i * gap) for i in range(n + 1)]
    return {
        'account-burst': (burst, 'Unbounded rapid invalid attempts must not all be admitted'),
        'shared-paths': ([event(i, path=('login', 'reset', 'otp')[i % 3]) for i in range(n + 1)], 'A shared retry budget must span the three modeled paths'),
        'restart': ([event(i) for i in range(n)] + [event(n, restart=True), event(n + 1)], 'A modeled restart must not erase the failure ceiling'),
        'independent-user': (burst + [event((n + 1) * gap, account='synthetic-b', valid=True)], 'Another synthetic account remains usable'),
        'expiry': ([event(i) for i in range(n)] + [event(p['window_ms'] + n, valid=True)], 'An expired window permits a valid synthetic attempt'),
    }


def defense_problem():
    # Illustrative effort points, not measured effectiveness or financial cost.
    names = [('Phishing-resistant authentication', 5), ('Password plus OTP', 4), ('Account retry budget', 2),
             ('Recovery-path budget', 2), ('Memory-hard password storage', 3), ('Security event audit', 1), ('Persistent retry state', 2)]
    return {'schema_version': API_VERSION, 'name': 'Illustrative defense selection; no deployment authority',
            'variables': [{'name': name, 'cost': cost} for name, cost in names],
            'clauses': [[1], [3], [4], [6], [7], [-2, 5]]}


def run(value, *, stop=lambda: False):
    value = document(value)
    p = value['policy']
    results = []
    for name in value['fixtures']:
        if stop():
            return {'schema_version': SCHEMA, 'status': 'CANCELLED', 'fixtures': results, 'action_authority': False}
        events, requirement = fixtures(p)[name]
        trace = simulate(p, events)
        exercised=True
        if name in ('account-burst', 'shared-paths'):
            passed = any(not row['admitted'] for row in trace)
        elif name == 'restart':
            without_restart=simulate(p,[e for e in events if not e['restart']])
            exercised=not without_restart[-1]['admitted']
            passed=trace[-1]['admitted']==without_restart[-1]['admitted']
        else:
            passed = trace[-1]['admitted']
        results.append({'fixture': name, 'requirement': requirement, 'status': ('PASS' if passed else 'FAIL') if exercised else 'NOT_EXERCISED',
                        'admitted_invalid': sum(r['admitted'] and not r['valid'] for r in trace),
                        'blocked_valid': sum(not r['admitted'] and r['valid'] for r in trace), 'trace': trace})
    from .search_sequence import experiment
    sequence = experiment(value['sequence'], stop=stop)
    if sequence['status']=='CANCELLED' or stop():
        return {'schema_version':SCHEMA,'status':'CANCELLED','fixtures':results,'sequence':sequence,'action_authority':False}
    selected = solve(defense_problem(), budget={'nodes': 10000, 'seconds': 2}, stop=stop)
    status='MODEL_FAILURES_FOUND' if any(r['status']=='FAIL' for r in results) else 'MODEL_CHECKS_INCOMPLETE' if any(r['status']=='NOT_EXERCISED' for r in results) else 'MODEL_CHECKS_PASSED'
    return {'schema_version': SCHEMA, 'status': status,
            'execution_mode': 'SYNTHETIC_LOCAL_MODEL', 'fixtures': results, 'sequence': sequence,
            'defense_plan': selected, 'defense_cost_basis': 'ILLUSTRATIVE_EFFORT_POINTS_NOT_MEASURED_EFFECTIVENESS',
            'action_authority': False, 'external_network_used': False, 'credential_inputs_supported': False,
            'limits': ['This model is not a deployed authentication control or identity verifier.',
                       'No real distributed race, service, human operator, MFA, password hash or recovery integration was exercised.',
                       'Policy tradeoffs and legitimate-user lockout require separate deployment-specific testing.',
                       'No quantum cracking or practical quantum advantage is established.']}


def summary(report):
    rows = [f"Security Lab: {report['status']}", 'Synthetic local evidence; no live targets, passwords or network calls.', '']
    for item in report['fixtures']:
        rows.append(f"{item['fixture']}: {item['status']} — {item['requirement']}")
    if 'sequence' in report:
        sequence = report['sequence']
        rows += ['', f"Sequencing: {sequence['status']}; selected rounds: {sequence.get('selected_rounds')}",
                 'Quantum cracking advantage: NOT DEMONSTRATED. Classical preprocessing and verification are charged.',
                 'Next: inspect failure traces, change one policy condition, then run a new pinned job.']
    return '\n'.join(rows)
