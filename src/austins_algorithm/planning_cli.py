"""Common planning API, synchronous solve and persistent job commands."""
import argparse
import json
from pathlib import Path
import time

from . import planning
from .planning_jobs import JobStore, export_job, verify_export, job_overview


def read_problem(path):
    if path.stat().st_size > 1_000_000:
        raise ValueError("Planning input exceeds 1 MB")
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError("Duplicate JSON field: "+key)
            result[key]=value
        return result
    value=json.loads(path.read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
    if isinstance(value,dict) and set(value) in ({'schema_version','problem','budget','backend'},{'schema_version','problem','budget','backend','workload_kind'}):
        if value['schema_version']!=planning.API_VERSION: raise ValueError('Unsupported project version')
        if value.get('workload_kind','planning')!='planning':raise ValueError('This is a simulation task export; use its explicit replay route')
        return planning.problem_from_document(value['problem']),planning.budget_from_document(value['budget']),value['backend']
    return planning.problem_from_document(value),None,None


def main(argv=None):
    parser=argparse.ArgumentParser(prog='austins-algorithm plan',description=__doc__)
    parser.add_argument('--human',action='store_true',help='Print the shared readable overview instead of JSON')
    sub=parser.add_subparsers(dest='command',required=True)
    solve=sub.add_parser('solve');solve.add_argument('problem',type=Path)
    solve.add_argument('--nodes',type=int,default=100000);solve.add_argument('--seconds',type=float,default=30)
    solve.add_argument('--backend',choices=['branch-bound','scipy-milp'],default='branch-bound')
    solve.add_argument('--export',type=Path,help='Create an isolated job and export all committed evidence')
    solve.add_argument('--store',type=Path,help='Required with --export')
    for command in ('create','start','resume','status','stop','recover','export','delete','list'):
        p=sub.add_parser(command);p.add_argument('--store',required=True,type=Path)
        if command=='create':
            p.add_argument('problem',type=Path);p.add_argument('--nodes',type=int,default=100000);p.add_argument('--seconds',type=float,default=30)
            p.add_argument('--backend',choices=['branch-bound','scipy-milp'],default='branch-bound')
            p.add_argument('--kind',choices=['planning','weighted-simulation','ibm-snapshot'],default='planning')
        elif command!='list':
            p.add_argument('job_id')
        if command in ('start','resume'): p.add_argument('--wait',action='store_true')
        if command=='export': p.add_argument('destination',type=Path)
    verify=sub.add_parser('verify-export');verify.add_argument('archive',type=Path)
    args=parser.parse_args(argv)
    store=None
    try:
        if args.command=='verify-export':
            result=verify_export(args.archive)
        elif args.command=='solve' and not args.export:
            problem,_,_=read_problem(args.problem)
            result=planning.solve(problem,budget={'nodes':args.nodes,'seconds':args.seconds},backend=args.backend)
            result['overview']=planning.overview(problem,result,state='COMPLETED' if result['optimality']=='EXACT_FINITE_MODEL' else 'PAUSED')
        else:
            if args.store is None: raise ValueError('--store is required when exporting a managed solve')
            store=JobStore(args.store)
            if args.command in ('create','solve'):
                if getattr(args,'kind','planning')!='planning':
                    if args.problem.stat().st_size>1000000:raise ValueError('Task input exceeds 1 MB')
                    payload=json.loads(args.problem.read_text(encoding='utf-8'))
                    result=store.create_task(args.kind,payload,budget={'nodes':args.nodes,'seconds':args.seconds})
                else:
                    problem,_,_=read_problem(args.problem)
                    result=store.create(problem,budget={'nodes':args.nodes,'seconds':args.seconds},backend=args.backend)
                if args.command=='solve':
                    job_id=result['id'];store.start(job_id)
                    while True:
                        result=store.poll(job_id)
                        if result['state'] not in ('STARTING','RUNNING'): break
                        time.sleep(.05)
                    export_job(store,job_id,args.export)
            elif args.command=='list':
                result=[store.poll(p.parent.name) for p in sorted(store.root.glob('*/job.json'))]
            elif args.command in ('start','resume'):
                result=store.start(args.job_id,resume=args.command=='resume')
                if args.wait:
                    while result['state'] in ('STARTING','RUNNING'):
                        time.sleep(.05);result=store.poll(args.job_id)
            elif args.command=='status': result=store.poll(args.job_id)
            elif args.command=='stop': result=store.stop(args.job_id)
            elif args.command=='recover': result=store.recover(args.job_id)
            elif args.command=='export': result=export_job(store,args.job_id,args.destination)
            elif args.command=='delete': store.delete(args.job_id);result={'status':'DELETED','id':args.job_id}
            if isinstance(result,dict) and 'problem' in result:
                result['overview']=job_overview(result)
    except (ValueError,OSError,RuntimeError,ImportError) as exc:
        parser.error(str(exc))
    finally:
        # Waiting commands own their worker lifetime. Detached start is explicit.
        if store and (args.command=='solve' or getattr(args,'wait',False)):
            store.close()
    print(planning.format_overview(result['overview']) if args.human and isinstance(result,dict) and 'overview' in result else json.dumps(result,indent=2,allow_nan=False))
