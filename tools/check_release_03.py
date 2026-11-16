"""Verify source-copy parity and a built wheel before admitting a release."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXCLUDED={'.git','.build','.venv','venv','__pycache__','node_modules','build','dist','recovery','.pytest_cache'}


def files(root):
    import os
    result={}
    for directory,names,names_files in os.walk(root):
        names[:]=[n for n in names if n not in EXCLUDED and not n.endswith('.egg-info')]
        for name in names_files:
            p=Path(directory)/name
            if p.suffix in ('.pyc','.log','.sqlite3','.db') or p.is_symlink():continue
            result[p.relative_to(root).as_posix()]=sha256(p.read_bytes()).hexdigest()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peer',type=Path,required=True);parser.add_argument('--wheel',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();left,right=files(ROOT),files(args.peer.resolve())
    differences=sorted(k for k in left.keys()|right.keys() if left.get(k)!=right.get(k))
    if differences:raise SystemExit('Source-copy differences: '+json.dumps(differences))
    count=0
    with zipfile.ZipFile(args.wheel) as wheel:
        for name in wheel.namelist():
            if not name.startswith('austins_algorithm/') or name.endswith('/'):continue
            if wheel.read(name)!=(ROOT/'src'/name).read_bytes():raise SystemExit('Stale wheel runtime: '+name)
            count+=1
    report={'status':'SOURCE_AND_WHEEL_MATCH','authored_files':len(left),'runtime_files':count,'mismatches':[],
            'scope':'Local authored source and wheel bytes; excludes private state, historical recovery and generated output. Installed release must separately pass its own signed closure.'}
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps(report))
