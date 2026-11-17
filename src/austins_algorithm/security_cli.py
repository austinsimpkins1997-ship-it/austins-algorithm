"""Explicit offline security rehearsal and public toy-circuit sequence commands."""
import argparse
import json
from pathlib import Path
import time

from . import security_rehearsal as lab
from .planning_jobs import JobStore, export_job
from .search_sequence import circuit_qasm


def read_document(path):
    with Path(path).open('rb') as stream:
        raw=stream.read(32769)
    if len(raw)>32768:raise ValueError('Security lab input exceeds 32 KiB')
    def pairs(items):
        value={}
        for name,item in items:
            if name in value:raise ValueError('Duplicate JSON key')
            value[name]=item
        return value
    return lab.document(json.loads(raw,object_pairs_hook=pairs))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('example')
    run=sub.add_parser('run');run.add_argument('input',type=Path);run.add_argument('--store',type=Path,required=True);run.add_argument('--export',type=Path)
    circuit=sub.add_parser('circuit');circuit.add_argument('--rounds',type=int,default=1);circuit.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='example':print(json.dumps(lab.example_document(),indent=2));return
        if args.command=='circuit':
            data=circuit_qasm(args.rounds)
            with args.output.open('x',encoding='utf-8') as stream:stream.write(data)
            print(json.dumps({'status':'TOY_CIRCUIT_EXPORTED','path':str(args.output),'provider_submission':False}));return
        store=JobStore(args.store)
        try:
            job=store.create_task('security-rehearsal',read_document(args.input))
            store.start(job['id'])
            while True:
                job=store.poll(job['id'])
                if job['state'] not in ('STARTING','RUNNING'):break
                time.sleep(.02)
            if job['state']!='COMPLETED':raise RuntimeError(job.get('error') or job['state'])
            if args.export:export_job(store,job['id'],args.export)
            print(lab.summary(job['result']['report']))
        finally:store.close()
    except (OSError,ValueError,RuntimeError,ImportError) as exc:parser.error(str(exc))
