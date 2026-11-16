"""Record caller-reported observations; compare only complete matched pairs."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from austins_algorithm.planning_pilot import observation,summarize
from austins_algorithm.planning_jobs import _write

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('record','summarize'));parser.add_argument('ledger',type=Path)
    parser.add_argument('--observation',type=Path,help='JSON with caller labels, stage seconds, input digest and check names')
    args=parser.parse_args()
    records=json.loads(args.ledger.read_text(encoding='utf-8')) if args.ledger.exists() else []
    if not isinstance(records,list) or len(records)>1000:parser.error('Invalid or oversized pilot ledger')
    if args.command=='record':
        if args.observation is None:parser.error('--observation is required')
        records.append(observation(**json.loads(args.observation.read_text(encoding='utf-8'))))
        summarize(records)  # Reject duplicate arms before publishing.
        _write(args.ledger,records)
    print(json.dumps(summarize(records),indent=2))
