import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from austins_algorithm.practical_evidence import benchmark, comparison_accounting, wilson_interval, sampling_evidence
from austins_algorithm.device_experiment import _provider_metrics, summarize_austin_counts


class PracticalEvidenceTests(unittest.TestCase):
    def test_all_success_shots_do_not_prove_probability_one(self):
        interval = wilson_interval(64, 64)
        self.assertLess(interval['lower'], 1)
        self.assertAlmostEqual(interval['upper'], 1)
        self.assertGreater(wilson_interval(1024, 1024)['lower'], interval['lower'])

    def test_zero_missing_and_invalid_counts(self):
        self.assertIsNone(wilson_interval(0, 0)['lower'])
        self.assertGreater(wilson_interval(0, 64)['upper'], 0)
        for successes, trials in ((True, 1), (1, False), (-1, 1), (2, 1), (0, 1_000_001)):
            with self.assertRaises(ValueError):
                wilson_interval(successes, trials)

    def test_interval_uses_completed_not_requested_shots(self):
        report = summarize_austin_counts({'011': 2, '000': 1}, requested_shots=8)
        self.assertNotIn('verified_hit_rate_interval', report)
        annotation = sampling_evidence(report)
        self.assertEqual(annotation['verified_hit_rate_interval']['trials'], 3)
        self.assertIn('correlated', annotation['interval_assumption'])

    def test_cpu_benchmark_retains_trials_and_no_advantage_claim(self):
        report = benchmark(repeats=4, shots=64, seed=7)
        self.assertTrue(report['all_output_sets_verified'])
        self.assertEqual(len(report['trials']), 4)
        self.assertFalse(report['quantum_hardware_used'])
        self.assertEqual(report['practical_quantum_advantage'], 'NOT_DEMONSTRATED')
        self.assertNotEqual(report['trials'][0]['order'], report['trials'][1]['order'])

    def test_small_shot_failure_is_retained(self):
        report = benchmark(repeats=2, shots=1)
        self.assertFalse(report['all_output_sets_verified'])
        self.assertEqual(len(report['trials']), 2)

    def test_bad_budget_rejected(self):
        for kwargs in ({'repeats': False}, {'repeats': 129}, {'shots': 0}, {'seed': 2**31}):
            with self.assertRaises(ValueError):
                benchmark(**kwargs)

    def test_report_labels_do_not_establish_advantage(self):
        data = {'hardware': {'quantum_hardware_used': True, 'measurement': {'source': 'QUANTUM_DEVICE'},
                             'provider_reported_qpu_seconds': .0000001},
                'wall_seconds': 2, 'classical': {'wall_seconds': 10}, 'build_seconds': 1,
                'ideal': {'wall_seconds': .3}, 'noisy': {'wall_seconds': .3}}
        report = comparison_accounting(data)
        self.assertEqual(report['evidence_kind'], 'REPORTED_HARDWARE_COMPARISON')
        self.assertEqual(report['practical_quantum_advantage'], 'NOT_DEMONSTRATED')
        self.assertIn('overlap', report['timing_relation'])

    def metrics(self, usage, timestamps=None):
        return _provider_metrics(SimpleNamespace(metrics=lambda: {'usage': usage, 'timestamps': timestamps or {}}))

    def test_usage_pending_estimated_or_conflicting_stays_unknown(self):
        for usage in ({'status':'pending', 'qpu_charge_time_seconds':0},
                      {'status':'estimated', 'qpu_charge_time_seconds':1},
                      {'qpu_charge_time_seconds':1},
                      {'status':'completed', 'qpu_charge_time_seconds':1, 'quantum_seconds':2},
                      {'status':'completed', 'qpu_charge_time_seconds':True}):
            self.assertIsNone(self.metrics(usage)['provider_reported_qpu_seconds'])
        report = self.metrics({'status':'completed', 'qpu_charge_time_seconds':1, 'quantum_seconds':2})
        self.assertTrue(report['provider_usage_conflict'])

    def test_partial_hardware_and_ambiguous_submission_are_not_called_simulations(self):
        data = {'hardware': {'quantum_hardware_used': True}, 'wall_seconds': 2,
                'classical': {'wall_seconds': 1}, 'build_seconds': .1,
                'ideal': {'wall_seconds': .1}, 'noisy': {'wall_seconds': .1}}
        self.assertEqual(comparison_accounting(data)['evidence_kind'], 'REPORTED_HARDWARE_RESULT_UNVERIFIED')
        data['hardware'] = {'provider_job_submitted': None, 'status': 'SUBMISSION_UNKNOWN'}
        self.assertEqual(comparison_accounting(data)['evidence_kind'], 'PROVIDER_EXECUTION_UNRESOLVED')

    def test_actual_zero_is_valid_but_not_a_monetary_price(self):
        report = self.metrics({'status':'completed', 'qpu_charge_time_seconds':0})
        self.assertEqual(report['provider_reported_qpu_seconds'], 0)
        self.assertIn('not a local estimate', report['provider_usage_source'])

    def test_bad_metrics_shape_and_ambiguous_time_remain_unknown(self):
        with self.assertRaises(ValueError):
            _provider_metrics(SimpleNamespace(metrics=lambda: []))
        report = self.metrics([], {'created':'2026-01-01T00:00:00', 'running':'2026-01-01T00:00:01'})
        self.assertIsNone(report['provider_reported_queue_seconds'])

    def test_cli_new_export_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'evidence'
            package_source = Path(__file__).resolve().parents[1]/'src'
            command = [sys.executable, '-I', '-B', '-c',
                       "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.argv[0]='austins-algorithm';runpy.run_module('austins_algorithm',run_name='__main__')",
                       str(package_source), 'practical-check', '--output', str(output), '--repeats', '2']
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output/'comparison.json').read_text())
            self.assertTrue(report['all_output_sets_verified'])
            self.assertEqual(len(json.loads((output/'manifest.json').read_text())['files']), 4)
            before = (output/'comparison.json').read_bytes()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, (output/'comparison.json').read_bytes())
