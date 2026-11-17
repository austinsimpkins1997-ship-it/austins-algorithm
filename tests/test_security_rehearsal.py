import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from austins_algorithm import security_rehearsal as lab
from austins_algorithm import search_sequence as seq
from austins_algorithm.planning_jobs import JobStore, export_job, verify_export, execute_task


class SecurityRehearsalTests(unittest.TestCase):
    def test_reader_bounds_allocation_before_parsing(self):
        from austins_algorithm.security_cli import read_document
        class Probe(io.BytesIO):
            def read(self,n=-1):
                if n<0 or n>32769:raise AssertionError('Unbounded input allocation')
                return super().read(n)
        with patch.object(Path,'open',return_value=Probe(b'x'*100000)):
            with self.assertRaises(ValueError):read_document(Path('synthetic-input.json'))

    def test_declared_default_cases_and_counterexamples(self):
        good=lab.run(lab.example_document())
        self.assertEqual(good['status'],'MODEL_CHECKS_PASSED')
        self.assertEqual(len(good['fixtures']),5)
        self.assertFalse(good['external_network_used'])
        weak=lab.example_document();weak['policy']['shared_budget']=False;weak['policy']['preserve_restart']=False
        bad=lab.run(weak)
        failures={r['fixture'] for r in bad['fixtures'] if r['status']=='FAIL'}
        self.assertEqual(failures,{'shared-paths','restart'})

    def test_exact_window_boundary_and_success_reset(self):
        p=lab.example_document()['policy'];p['max_failures']=1;p['window_ms']=1000
        def e(t,valid=False):return {'t_ms':t,'account':'synthetic-a','path':'login','valid':valid,'restart':False}
        trace=lab.simulate(p,[e(0),e(999,True),e(1000,True),e(1001)])
        self.assertEqual([r['admitted'] for r in trace],[True,False,True,True])

    def test_equal_timestamp_is_atomic_ordered_model(self):
        p=lab.example_document()['policy'];p['max_failures']=2
        event={'t_ms':0,'account':'synthetic-a','path':'login','valid':False,'restart':False}
        self.assertEqual([r['admitted'] for r in lab.simulate(p,[event]*4)],[True,True,False,False])

    def test_delay_and_account_isolation(self):
        p=lab.example_document()['policy'];p['delay_ms']=100
        def e(t,account='synthetic-a'):return {'t_ms':t,'account':account,'path':'login','valid':False,'restart':False}
        self.assertEqual([r['admitted'] for r in lab.simulate(p,[e(0),e(99),e(99,'synthetic-b'),e(100)])],[True,False,True,True])

    def test_strict_scope_and_bounds(self):
        for key in ('url','password','hashes','host','oracle'):
            value=lab.example_document();value[key]='not-supported'
            with self.assertRaises(ValueError):lab.document(value)
        value=lab.example_document();value['sequence']['rounds']=[{}]
        with self.assertRaises(ValueError):lab.document(value)
        for invalid in (True,0,11):
            p=lab.example_document()['policy'];p['max_failures']=invalid
            with self.assertRaises(ValueError):lab.policy(p)

    def test_job_export_and_fresh_replay(self):
        with tempfile.TemporaryDirectory() as d:
            store=JobStore(Path(d)/'jobs')
            try:
                job=store.create_task('security-rehearsal',lab.example_document())
                store.start(job['id']);deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    job=store.poll(job['id'])
                    if job['state'] not in ('STARTING','RUNNING'):break
                    time.sleep(.02)
                self.assertEqual(job['state'],'COMPLETED',job.get('error'))
                archive=Path(d)/'evidence.zip';export_job(store,job['id'],archive)
                self.assertEqual(verify_export(archive)['status'],'PLANNING_EXPORT_VERIFIED')
                fresh=execute_task('security-rehearsal',job['problem'])['report']
                self.assertEqual(fresh['fixtures'],job['result']['report']['fixtures'])
                job['engine_pin']['sources']['workbench_assets/security_sources.json']='0'*64
                store._save(job)
                with self.assertRaises(ValueError):export_job(store,job['id'],Path(d)/'changed-source.zip')
            finally:store.close()

    def test_stop_does_not_claim_completed_checks(self):
        report=lab.run(lab.example_document(),stop=lambda:True)
        self.assertEqual(report['status'],'CANCELLED')
        self.assertFalse(execute_task('security-rehearsal',lab.example_document(),stop=lambda:True)['task_completed'])

    def test_stop_during_sequence_does_not_claim_overall_completion(self):
        calls=0
        def stop():
            nonlocal calls
            calls+=1
            return calls>=6
        self.assertEqual(lab.run(lab.example_document(),stop=stop)['status'],'CANCELLED')


class SequenceTests(unittest.TestCase):
    def test_independent_verification_and_held_out_seed(self):
        result=seq.experiment({'rounds':[0,1,2,3],'shots':32,'seed':17})
        self.assertEqual(result['selected_rounds'],1)
        self.assertEqual(result['held_out_seed'],18)
        self.assertEqual(result['status'],'VERIFIED_SAMPLES')
        self.assertEqual(result['work']['cpu_preprocessing_evaluations'],40)
        self.assertEqual(result['quantum_advantage'],'NOT_DEMONSTRATED')
        self.assertEqual(seq.stages(1),['prepare','oracle','diffuse','measure','verify','checkpoint'])

    def test_no_claim_from_unamplified_samples(self):
        result=seq.experiment({'rounds':[0],'shots':256,'seed':7})
        self.assertEqual(result['status'],'UNVERIFIED_SAMPLES_PRESENT')
        self.assertFalse(all(result['independent_sample_verification'].values()))

    def test_optional_qiskit_sequence_matches_public_predicate(self):
        try:from qiskit import QuantumCircuit
        except ImportError:self.skipTest('Optional Qiskit unavailable')
        from austins_algorithm.grover_search import build_austin_phase_oracle,build_grover_search_circuit
        from qiskit.quantum_info import Statevector
        circuit=build_grover_search_circuit(build_austin_phase_oracle(),3,iterations=1)
        state=Statevector.from_instruction(circuit.remove_final_measurements(inplace=False))
        probabilities=state.probabilities_dict()
        self.assertAlmostEqual(sum(p for bits,p in probabilities.items() if bits in ('011','101')),1.0)
        self.assertIn('OPENQASM 3',seq.circuit_qasm(1))
