"""Independent finite-reference, job failure/recovery and portable interface checks."""
import io
import json
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from austins_algorithm import planning
from austins_algorithm.optimization import exact_minimum
from austins_algorithm.planning_api import run_request,problem_schema
from austins_algorithm.planning_jobs import JobStore,export_job,verify_export,_terminate
from austins_algorithm.planning import example_problem
from austins_algorithm.planning_pilot import observation,summarize,STAGES


class PlanningTests(unittest.TestCase):
    def test_random_small_cases_match_independent_exhaustive_reference(self):
        rng=random.Random(710)
        for n in range(1,9):
            for repeat in range(12):
                p={'name':'Random bounded fixture','variables':[{'name':str(i),'cost':rng.randrange(9)} for i in range(n)],
                   'clauses':[[rng.choice([-1,1])*rng.randrange(1,n+1) for _ in range(rng.randrange(4))] for _ in range(rng.randrange(10))]}
                expected=exact_minimum(p);actual=planning.solve(p)
                self.assertEqual(actual['cost'],expected['cost'],(p,actual,expected))
                self.assertEqual(actual['status'],'OPTIMAL' if expected['status']=='OPTIMUM' else 'INFEASIBLE')
                if actual['bits']:self.assertTrue(planning.verify_candidate(p,actual['bits'])['valid'])

    def test_larger_model_and_legacy_migration_leave_simulator_limit_unchanged(self):
        p=example_problem(128);r=planning.solve(p)
        self.assertEqual(r['status'],'OPTIMAL')
        self.assertEqual(r['cost'],sum(min(p['variables'][i]['cost'],p['variables'][i+1]['cost']) for i in range(0,128,2)))
        from austins_algorithm.optimization import MAX_VARIABLES
        self.assertEqual(MAX_VARIABLES,8)
        self.assertEqual(problem_schema()['properties']['variables']['maxItems'],128)

    def test_budget_resume_and_untrusted_import_cannot_claim_proof(self):
        p=example_problem(16)
        first=planning.solve(p,budget={'nodes':1,'seconds':2})
        self.assertEqual(first['optimality'],'NOT_PROVED')
        resumed=planning.solve(p,checkpoint=first['checkpoint'])
        self.assertEqual(resumed['optimality'],'NOT_PROVED')
        self.assertEqual(resumed['cost'],planning.solve(p)['cost'])
        changed=example_problem(18)
        with self.assertRaises(ValueError):planning.solve(changed,checkpoint=first['checkpoint'])
        stopped=planning.solve(p,stop=lambda:True)
        self.assertEqual(stopped['termination'],'STOP_REQUESTED')

    def test_validation_and_api_result_are_shared(self):
        p=example_problem(4)
        request={'schema_version':planning.API_VERSION,'problem':p,'budget':{'nodes':100,'seconds':2},'backend':'branch-bound'}
        r=run_request(request)
        self.assertEqual(r['overview'],planning.overview(p,{k:v for k,v in r.items() if k!='overview'},state='COMPLETED'))
        for budget in ({'seconds':float('nan')},{'nodes':True},{'seconds':0}):
            with self.assertRaises(ValueError):planning.solve(p,budget=budget)
        with self.assertRaises(ValueError):planning.verify_candidate(p,'1111',0)
        self.assertFalse(planning.verify_candidate(p,'0000')['valid'])


class JobTests(unittest.TestCase):
    def setUp(self):
        build=ROOT/'.build';build.mkdir(exist_ok=True)
        self.temp=TemporaryDirectory(dir=build,prefix='planning-test-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=JobStore(self.root/'jobs');self.addCleanup(self.store.close)

    def wait(self,job_id):
        end=time.monotonic()+15
        while time.monotonic()<end:
            record=self.store.poll(job_id)
            if record['state'] not in ('STARTING','RUNNING'):return record
            time.sleep(.03)
        self.fail('Worker did not stop within test ceiling')

    def test_worker_budget_restart_resume_export_and_independent_replay(self):
        r=self.store.create(example_problem(16),budget={'nodes':1,'seconds':3});job_id=r['id']
        self.store.start(job_id);first=self.wait(job_id)
        self.assertEqual(first['state'],'PAUSED')
        other=JobStore(self.root/'jobs');other.start(job_id,resume=True,budget={'nodes':100000,'seconds':3})
        try:
            while other.poll(job_id)['state'] in ('STARTING','RUNNING'):time.sleep(.03)
        finally:other.close()
        r=self.store.read(job_id);self.assertEqual(r['state'],'COMPLETED')
        self.assertEqual(r['result']['cost'],planning.solve(r['problem'])['cost'])
        destination=self.root/'all.zip';export_job(self.store,job_id,destination)
        self.assertEqual(verify_export(destination)['status'],'PLANNING_EXPORT_VERIFIED')
        self.assertFalse(verify_export(destination)['optimality_independently_replayed'])
        import zipfile
        with zipfile.ZipFile(destination) as archive:
            self.assertNotIn('.integrity-key',archive.namelist())
            self.assertIn('overview.json',archive.namelist());self.assertIn('replay.py',archive.namelist())
            self.assertIn(b'What happened?',archive.read('report.html'))
            self.assertIn(planning.format_overview(planning.overview(r['problem'],r['result'],state=r['state'],error=r['error'])).splitlines()[0].encode(),archive.read('report.html'))
        with self.assertRaises(FileExistsError):export_job(self.store,job_id,destination)

    def test_tampered_checkpoint_or_model_is_rejected(self):
        r=self.store.create(example_problem(8));path=self.store.directory(r['id'])/'job.json'
        data=json.loads(path.read_bytes());data['checkpoint']={'frontier':[]};path.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'integrity'):self.store.start(r['id'],resume=True)

    def test_native_worker_exit_and_explicit_recovery_preserve_record(self):
        # Exit before entry is a real subprocess failure, without a fake success receipt.
        r=self.store.create(example_problem(8));job_id=r['id']
        import subprocess
        real=subprocess.Popen
        def fail(command,**kwargs):return real([sys.executable,'-I','-B','-c','raise SystemExit(17)'],**kwargs)
        with patch('austins_algorithm.planning_jobs.subprocess.Popen',side_effect=fail):self.store.start(job_id)
        r=self.wait(job_id);self.assertEqual(r['state'],'INTERRUPTED');self.assertIn('17',r['error'])
        self.store.start(job_id);self.assertEqual(self.wait(job_id)['state'],'COMPLETED')

    def test_low_disk_interrupted_atomic_save_and_unsafe_identifiers(self):
        with patch('austins_algorithm.planning_jobs.shutil.disk_usage',return_value=type('D',(),{'free':0})()):
            with self.assertRaises(OSError):self.store.create(example_problem(8))
        r=self.store.create(example_problem(8));path=self.store.directory(r['id'])/'job.json';before=path.read_bytes()
        with patch('austins_algorithm.planning_jobs.os.replace',side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):self.store._save(r)
        self.assertEqual(path.read_bytes(),before)
        for key in ('../outside','a'*31,'g'*32):
            with self.assertRaises(ValueError):self.store.read(key)

    def test_stop_marker_and_close_do_not_orphan_workers(self):
        r=self.store.create(example_problem(128));job_id=r['id'];self.store.start(job_id);self.store.stop(job_id)
        r=self.wait(job_id)
        self.assertIn(r['state'],('PAUSED','COMPLETED'))
        self.store.close();self.assertFalse(self.store.processes)

    def test_existing_weighted_simulation_uses_the_same_worker_and_export(self):
        p=example_problem(4);p.pop('schema_version')
        r=self.store.create_task('weighted-simulation',p,budget={'seconds':3})
        self.store.start(r['id']);r=self.wait(r['id'])
        self.assertEqual(r['state'],'COMPLETED')
        self.assertEqual(r['result']['execution_mode'],'SIMULATOR')
        export_job(self.store,r['id'],self.root/'simulation.zip')
        self.assertEqual(verify_export(self.root/'simulation.zip')['status'],'PLANNING_EXPORT_VERIFIED')


class PilotTests(unittest.TestCase):
    def test_empty_and_unmatched_observations_never_claim_benefit(self):
        self.assertEqual(summarize([])['conclusion'],'NO_MATCHED_OPERATOR_EVIDENCE')
        args=dict(operator='caller-labelled test actor',pair_id='synthetic-validation-only',project_sha256='0'*64,
                  seconds={k:1 for k in STAGES},required_checks=['input','verify'],completed_checks=['input','verify'],output_verified=True)
        a=observation(arm='baseline',**args);b=observation(arm='planning',**args)
        self.assertEqual(len(summarize([a])['held_pairs']),1)
        self.assertEqual(len(summarize([a,b])['accepted_pairs']),1)
        b['completed_checks']=['input'];self.assertEqual(len(summarize([a,b])['held_pairs']),1)


if __name__=='__main__':unittest.main()
