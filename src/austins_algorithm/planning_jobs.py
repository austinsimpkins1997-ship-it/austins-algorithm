"""Local signed job records, isolated workers, bounded recovery and portable exports."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import zipfile

from . import planning


def utc():
    return datetime.now(timezone.utc).isoformat()


def _read(path):
    for attempt in range(8):
        try:
            if path.is_symlink() or path.stat().st_size > 2_000_000:
                raise ValueError("Job file is linked or exceeds its size limit")
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            # Windows can briefly deny opening a file during atomic replacement.
            # Recheck type/size and bound the wait; permanent denial stays visible.
            if os.name!='nt' or attempt==7:raise
            time.sleep(.01)


def _write(path, value):
    if path.is_symlink():
        raise ValueError("Refuse to replace a linked file")
    raw = planning.canonical(value)
    if len(raw) > 2_000_000:
        raise ValueError("Job record exceeds its storage limit")
    temporary = path.with_name(path.name + "." + os.urandom(8).hex() + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary,path)
                break
            except PermissionError:
                if os.name!='nt' or attempt==7 or path.is_symlink():raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(directory):
    path = directory / "worker.lock"
    if path.is_symlink():
        raise ValueError("Linked worker lock")
    with path.open("a+b") as stream:
        stream.seek(0)
        if path.stat().st_size == 0:
            stream.write(b"0"); stream.flush(); stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("This job has an active worker; stop it before recovery") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _engine_pin():
    directory=Path(__file__).parent
    return {"engine":planning.ENGINE_VERSION,"sources":{name:sha256((directory/name).read_bytes()).hexdigest() if (directory/name).is_file() else None
            for name in ('planning.py','planning_jobs.py','optimization.py')}}


def _terminate(process):
    if process.poll() is not None:return
    if os.name == 'nt':
        subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/taskkill.exe'),'/PID',str(process.pid),'/T','/F'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=False)
    else:
        import signal
        os.killpg(process.pid,signal.SIGKILL)
    process.wait(timeout=5)


class JobStore:
    """Single-user local storage. The key grants integrity, not execution authority."""
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        if self.root.is_symlink():
            raise ValueError("Linked job store")
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes = {}
        self.stop_times = {}
        key = self.root / ".integrity-key"
        if key.is_symlink():
            raise ValueError("Linked integrity key")
        try:
            with key.open("xb") as stream:
                stream.write(os.urandom(32))
            if os.name != "nt":
                key.chmod(0o600)
        except FileExistsError:
            pass
        self.key = key.read_bytes()
        if len(self.key) != 32:
            raise ValueError("Invalid local integrity key")

    def directory(self, job_id):
        if not isinstance(job_id,str) or len(job_id)!=32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid job ID")
        path = self.root / job_id
        if path.is_symlink():
            raise ValueError("Linked job directory")
        return path

    def _save(self, record):
        value = {k:v for k,v in record.items() if k != "mac"}
        value["updated_utc"] = utc()
        value["mac"] = hmac.new(self.key,planning.canonical(value),"sha256").hexdigest()
        _write(self.directory(value["id"])/"job.json",value)
        return value

    def read(self, job_id):
        record = _read(self.directory(job_id)/"job.json")
        raw = {k:v for k,v in record.items() if k != "mac"}
        expected = hmac.new(self.key,planning.canonical(raw),"sha256").hexdigest()
        if not hmac.compare_digest(expected,record.get("mac", "")) or record.get("id") != job_id:
            raise ValueError("Job integrity failed; preserve the record and restore a trusted copy")
        return record

    def create(self, problem, *, budget=None, backend="branch-bound"):
        if backend not in ('branch-bound','scipy-milp'):raise ValueError('Unknown planning backend')
        p, limits = planning.problem_from_document(problem),planning.budget_from_document(budget)
        return self._create(p,limits,backend,'planning')

    def create_task(self, kind, payload, *, budget=None):
        """Adopt existing bounded CPU/IBM execution without changing their formats."""
        limits=planning.budget_from_document(budget)
        if kind=='weighted-simulation':
            from .optimization import problem_from_document
            payload=problem_from_document(payload)
        elif kind=='ibm-snapshot':
            if not isinstance(payload,dict) or set(payload)!={'model','shots','seed'} or payload['model'] not in ('manila','jakarta','torino'):
                raise ValueError('Snapshot tasks require model, shots and seed')
            planning.integer(payload['shots'],1,4096,'Shots');planning.integer(payload['seed'],0,2**31-1,'Seed')
        else:raise ValueError('Unknown managed task kind')
        return self._create(payload,limits,'existing-workflow',kind)

    def _create(self,p,limits,backend,kind):
        if backend not in ("branch-bound","scipy-milp","existing-workflow"):
            raise ValueError("Unknown backend")
        if shutil.disk_usage(self.root).free < 16 * 1024 * 1024:
            raise OSError("At least 16 MiB free disk space is required for a planning job")
        if len(list(self.root.glob("*/job.json"))) >= 1000:
            raise ValueError("Store limit reached: export and explicitly delete old jobs")
        job_id = os.urandom(16).hex()
        self.directory(job_id).mkdir()
        return self._save({"schema_version":planning.API_VERSION,"id":job_id,"problem":p,"budget":limits,
            "backend":backend,"workload_kind":kind,"engine_pin":_engine_pin(),"state":"READY","created_utc":utc(),
            "attempts":[],"checkpoint":None,"result":None,"error":None,"python":platform.python_version()})

    def start(self, job_id, *, resume=False, budget=None):
        directory = self.directory(job_id)
        with _lock(self.root), _lock(directory):
            record=self.read(job_id)
            active=sum(self.read(p.parent.name)['state'] in ('STARTING','RUNNING') for p in self.root.glob('*/job.json'))
            if active >= 2:
                raise RuntimeError('Store concurrency limit reached (2); stop or recover existing work')
            if record["engine_pin"] != _engine_pin():
                raise ValueError("Engine source changed; create a new job with retained inputs")
            if record["state"] in ("STARTING","RUNNING"):
                raise RuntimeError("Job is active; use explicit recovery after a worker failure")
            if len(record["attempts"]) >= 64:
                raise ValueError("At most 64 attempts per job")
            if resume and (record["backend"] != "branch-bound" or record["checkpoint"] is None):
                raise ValueError("No resumable branch-and-bound checkpoint")
            if not resume:
                record.update(checkpoint=None,result=None)
            if record["state"] == "COMPLETED":
                raise ValueError("Completed jobs cannot be rerun in place; create a new job")
            if budget is not None:
                record["budget"]=planning.budget_from_document(budget)
            if shutil.disk_usage(directory).free < 16*1024*1024:
                raise OSError("Insufficient job-storage space")
            (directory/"stop").unlink(missing_ok=True)
            token=os.urandom(16).hex()
            record.update(state="STARTING",error=None)
            record["attempts"].append({"token":token,"started_utc":utc(),"budget":record["budget"],"resume":resume})
            self._save(record)
        if getattr(sys,"frozen",False):
            command=[sys.executable,"--planning-worker",str(self.root),job_id,token]
        else:
            command=[sys.executable,"-I","-B","-c",
                "import sys;sys.path.insert(0,sys.argv.pop(1));from austins_algorithm.planning_jobs import worker;worker(*sys.argv[1:])",
                str(Path(__file__).resolve().parents[1]),str(self.root),job_id,token]
        try:
            with (directory/f"worker-{len(record['attempts'])}.log").open("xb") as log:
                process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),start_new_session=os.name!='nt')
        except BaseException as exc:
            with _lock(directory):
                record=self.read(job_id);record.update(state="FAILED",error=str(exc));self._save(record)
            raise
        self.processes[job_id]=(process,time.monotonic(),record["budget"]["seconds"])
        return self.read(job_id)

    def poll(self, job_id):
        owned=self.processes.get(job_id)
        if owned:
            process,started,seconds=owned
            if process.poll() is None and time.monotonic()-started > seconds+10:
                _terminate(process)
            if process.poll() is None and job_id in self.stop_times and time.monotonic()-self.stop_times[job_id] > 2:
                _terminate(process)
            if process.poll() is not None:
                self.processes.pop(job_id,None)
                self.stop_times.pop(job_id,None)
                record=self.read(job_id)
                if record["state"] in ("STARTING","RUNNING"):
                    return self.recover(job_id,reason=f"Worker exited {process.returncode}; last committed checkpoint retained")
        return self.read(job_id)

    def stop(self, job_id):
        record=self.read(job_id)
        if record["state"] not in ("STARTING","RUNNING"):
            return record
        path=self.directory(job_id)/"stop"
        if path.is_symlink():
            raise ValueError("Linked stop marker")
        path.touch(exist_ok=True)
        self.stop_times.setdefault(job_id,time.monotonic())
        return record

    def recover(self, job_id, *, reason="Explicit recovery after an interrupted worker"):
        with _lock(self.directory(job_id)):
            record=self.read(job_id)
            if record["state"] not in ("STARTING","RUNNING"):
                return record
            record.update(state="INTERRUPTED",error=reason)
            record["attempts"][-1].update(ended_utc=utc(),outcome="INTERRUPTED")
            return self._save(record)

    def close(self):
        # Only terminate workers launched by this manager; never kill an adopted PID.
        for job_id in list(self.processes):
            self.stop(job_id)
        deadline=time.monotonic()+2
        for job_id,(process,_,_) in list(self.processes.items()):
            try:
                process.wait(timeout=max(.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                _terminate(process)
            self.poll(job_id)

    def delete(self, job_id):
        with _lock(self.directory(job_id)):
            if self.read(job_id)["state"] in ("STARTING","RUNNING"):
                raise RuntimeError("Stop or recover the job before deletion")
        shutil.rmtree(self.directory(job_id))


def worker(root,job_id,token):
    store=JobStore(root)
    directory=store.directory(job_id)
    with _lock(directory):
        record=store.read(job_id)
        if record["state"] != "STARTING" or record["attempts"][-1]["token"] != token:
            return
        if record["engine_pin"] != _engine_pin():
            record.update(state="FAILED",error="Engine changed before worker launch");store._save(record);return
        record["state"]="RUNNING";store._save(record)
        started=time.monotonic()
        # A detached worker also has a final wall-time boundary. Last atomic
        # checkpoint survives; the owning manager reports the nonzero exit.
        import threading
        watchdog=threading.Timer(record['budget']['seconds']+10,lambda:os._exit(124))
        watchdog.daemon=True;watchdog.start()
        def progress(checkpoint):
            record["checkpoint"]=checkpoint
            store._save(record)
        try:
            if record.get('workload_kind','planning')=='planning':
                result=planning.solve(record["problem"],budget=record["budget"],backend=record["backend"],
                    checkpoint=record["checkpoint"] if record["attempts"][-1]["resume"] else None,
                    _trusted_checkpoint=True,stop=lambda:(directory/"stop").exists(),progress=progress)
            else:
                result=execute_task(record['workload_kind'],record['problem'])
            record.update(result=result,checkpoint=result["checkpoint"],
                state="COMPLETED" if result.get('task_completed') or result["optimality"]=="EXACT_FINITE_MODEL" else "PAUSED",error=None)
        except Exception as exc:
            record.update(state="FAILED",error=f"{type(exc).__name__}: {exc}")
        record["attempts"][-1].update(ended_utc=utc(),wall_seconds=time.monotonic()-started,outcome=record["state"])
        store._save(record)
        watchdog.cancel()


def export_job(store,job_id,path):
    record=store.read(job_id)
    if record["state"] in ("STARTING","RUNNING"):
        raise ValueError("Pause the job to export a coherent committed snapshot")
    path=Path(path)
    if path.exists():
        raise FileExistsError("Export destination exists")
    public={k:v for k,v in record.items() if k!="mac"}
    payloads={"project.json":planning.canonical({"schema_version":planning.API_VERSION,"problem":record["problem"],"budget":record["budget"],"backend":record["backend"],"workload_kind":record.get('workload_kind','planning')}),
              "job.json":planning.canonical(public),"overview.json":planning.canonical(job_overview(record)),
              "replay.py":b'import json\nfrom pathlib import Path\nfrom austins_algorithm.planning import solve\np=json.loads(Path(__file__).with_name("project.json").read_text(encoding="utf-8"))\nprint(json.dumps(solve(p["problem"],budget=p["budget"],backend=p["backend"]),indent=2))\n'}
    from . import __version__
    payloads['requirements.txt']=('chronorealm-austins-algorithm=='+__version__+'\n').encode('ascii')
    payloads['README.txt']=b'Inputs, result, checkpoint, attempts, runtime pins, worker logs and shared overview are retained. Install the pinned package, then run replay.py for a fresh bounded computation. Imported evidence or an advisory result does not grant authority. The local job HMAC key is intentionally excluded. Optional IBM/SciPy tasks also need their separately declared extras.\n'
    from html import escape
    payloads['report.html']=('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Planning review</title>'
        '<style>body{font:18px system-ui;max-width:60em;margin:2em auto;padding:1em;line-height:1.5}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        '<h1>What happened?</h1><pre>'+escape(planning.format_overview(job_overview(record)))+'</pre><p>Read-only recorded evidence. Verify the manifest and rerun the pinned replay before relying on an independently reproduced optimum.</p></html>').encode('utf-8')
    if record.get('workload_kind','planning')!='planning':
        payloads['replay.py']=b'import json\nfrom pathlib import Path\nfrom austins_algorithm.planning_jobs import execute_task\np=json.loads(Path(__file__).with_name("project.json").read_text(encoding="utf-8"))\nprint(json.dumps(execute_task(p["workload_kind"],p["problem"]),indent=2))\n'
    for log in store.directory(job_id).glob("worker-*.log"):
        if log.is_symlink() or log.stat().st_size > 1_000_000:
            raise ValueError("Worker log is linked or exceeds export limit")
        payloads[log.name]=log.read_bytes()
    manifest={"schema_version":planning.API_VERSION,"files":{n:{"bytes":len(v),"sha256":sha256(v).hexdigest()} for n,v in payloads.items()},
              "scope":"Recorded local evidence; re-run replay.py for an independent fresh solve. No authority or private integrity key is exported."}
    temporary=path.with_name(path.name+'.'+os.urandom(8).hex()+'.tmp')
    try:
        with zipfile.ZipFile(temporary,"x",zipfile.ZIP_DEFLATED) as archive:
            for name,raw in payloads.items(): archive.writestr(name,raw)
            archive.writestr("manifest.json",planning.canonical(manifest))
        # Publish without overwriting an existing user destination.
        os.link(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def verify_export(path):
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>100 or len({i.filename for i in entries})!=len(entries) or sum(i.file_size for i in entries)>70_000_000:
            raise ValueError("Export size or duplicate-entry limit exceeded")
        manifest=json.loads(archive.read("manifest.json"))
        if not isinstance(manifest,dict) or manifest.get('schema_version')!=planning.API_VERSION or not isinstance(manifest.get('files'),dict) or set(archive.namelist())!=set(manifest['files'])|{'manifest.json'}:
            raise ValueError("Export manifest/version differs")
        if not {'project.json','job.json','overview.json','replay.py','requirements.txt','README.txt'}<=set(manifest['files']):
            raise ValueError('Incomplete export')
        for name,item in manifest['files'].items():
            if '/' in name or '\\' in name:
                raise ValueError("Invalid export member")
            raw=archive.read(name)
            if {"bytes":len(raw),"sha256":sha256(raw).hexdigest()}!=item:
                raise ValueError("Export integrity differs")
        job=json.loads(archive.read('job.json'))
        project=json.loads(archive.read('project.json'))
        if project['problem']!=job['problem']:
            raise ValueError("Export project differs from job")
        if job.get('workload_kind','planning')=='planning':planning.problem_from_document(project['problem'])
        if job.get('workload_kind','planning')=='planning' and job.get('result') and job['result'].get('problem_sha256')!=planning.fingerprint(job['problem']):
            raise ValueError('Exported result belongs to another model')
        if job.get('workload_kind','planning')=='planning' and job['result'] and job['result']['bits'] is not None:
            if not planning.verify_candidate(job['problem'],job['result']['bits'],job['result']['cost'])['valid']:
                raise ValueError("Exported candidate is invalid")
    return {'status':'PLANNING_EXPORT_VERIFIED','members':len(entries),'optimality_independently_replayed':False,'action_authority':False}


def execute_task(kind,payload):
    if kind=='weighted-simulation':
        from .optimization import SearchSession
        session=SearchSession(payload,seed=7,max_shots=16,max_queries=64,max_rounds=4)
        session.run();report=session.report()
    elif kind=='ibm-snapshot':
        from .adapter_api import ExperimentRequest,WorkloadArtifact
        from .snapshot_adapter import IBMSnapshotAdapter
        report=IBMSnapshotAdapter().submit(ExperimentRequest(WorkloadArtifact('chronorealm.austin-snapshot/1',{'model':payload['model']},logical_width=3),
            requested_execution_mode='SIMULATOR',shots=payload['shots'],seed=payload['seed'])).result_payload
    else:raise ValueError('Unknown task kind')
    return {'task_completed':True,'task_kind':kind,'report':report,'checkpoint':None,'optimality':'NOT_APPLICABLE',
            'execution_mode':'SIMULATOR','action_authority':False}


def job_overview(record):
    if record.get('workload_kind','planning')=='planning':
        return planning.overview(record['problem'],record['result'],state=record['state'],error=record['error'])
    return {'schema_version':planning.API_VERSION,'goal':record['workload_kind'],'stage':record['state'],
            'status':record['state'],'execution_mode':'SIMULATOR','error':record['error'],
            'next_action':'Inspect the verified simulation report and export its inputs and evidence.',
            'uncertainty':['Local simulation does not establish physical-device performance.'],
            'attempts':record['attempts'],'action_authority':False}
