"""Matched synthetic planning benchmark; never substitute for operator evidence."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, sleep

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from austins_algorithm.optimization import exact_minimum
from austins_algorithm.planning import solve,verify_candidate,problem_from_document,fingerprint
from austins_algorithm.planning import example_problem
from austins_algorithm.planning_jobs import JobStore,export_job,verify_export


def benchmark(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    cases=[]
    for size in (4,8,24,64,128):
        source=example_problem(size)
        expected=sum(min(source['variables'][i]['cost'],source['variables'][i+1]['cost']) for i in range(0,size,2))
        stages={}
        started=perf_counter();p=problem_from_document(source);stages['input_checks']=perf_counter()-started
        started=perf_counter();r=solve(p);stages['solve']=perf_counter()-started
        started=perf_counter();v=verify_candidate(p,r['bits'],r['cost']);stages['verification']=perf_counter()-started
        assert v['valid'] and r['status']=='OPTIMAL' and r['cost']==expected
        baseline=None
        if size<=8:
            legacy={k:v for k,v in p.items() if k!='schema_version'}
            started=perf_counter();b=exact_minimum(legacy);elapsed=perf_counter()-started
            assert b['cost']==r['cost']
            baseline={'method':'existing exhaustive exact reference','solve_seconds':elapsed,'assignments_checked':b['assignments_checked']}
        cases.append({'variables':size,'problem_sha256':fingerprint(p),'status':r['status'],'verified_cost':r['cost'],
                      'expected_cost_source':'Analytical minimum for disjoint positive pairs; separate from the general solver',
                      'nodes':r['work']['nodes_total'],'stages_seconds':stages,'baseline':baseline})
    store=JobStore(directory/'jobs')
    started=perf_counter();job=store.create(example_problem(24),budget={'nodes':1,'seconds':3});setup=perf_counter()-started
    try:
        started=perf_counter();store.start(job['id'])
        while store.poll(job['id'])['state'] in ('STARTING','RUNNING'):sleep(.02)
        first_wall=perf_counter()-started
        started=perf_counter();store.start(job['id'],resume=True,budget={'nodes':10000,'seconds':3})
        while store.poll(job['id'])['state'] in ('STARTING','RUNNING'):sleep(.02)
        resume_wall=perf_counter()-started
        final=store.read(job['id']);assert final['state']=='COMPLETED'
        started=perf_counter();export_job(store,job['id'],directory/'replay.zip');verify_export(directory/'replay.zip');export_verify=perf_counter()-started
    finally:store.close()
    report={'evidence_kind':'SYNTHETIC_LOCAL_MACHINE_BENCHMARK','recorded_utc':datetime.now(timezone.utc).isoformat(),
            'python':platform.python_version(),'platform':platform.platform(),'cases':cases,
            'managed_path_seconds':{'setup':setup,'first_budgeted_attempt_wall':first_wall,'resume_and_verification_wall':resume_wall,'export_and_integrity_check':export_verify},
            'operator_observations':0,'operator_benefit':'NOT_MEASURED','device_cost':'NOT_APPLICABLE_NO_QPU',
            'external_evidence_refresh':'NOT_EXERCISED_SYNTHETIC_PINNED_INPUTS','retries':'None; deliberate budget pause/resume measured separately',
            'limitations':['Single local run per size; timings are descriptive, not a general speedup.','Large cases use a structured analytical reference; exhaustive baseline only runs at <=8 variables.','No human productivity, quantum advantage or production reliability claim.']}
    (directory/'benchmark.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    print(json.dumps(benchmark(parser.parse_args().directory),indent=2))
