"""Run real local worker processes over a fenced SQLite practice job."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4

from .codec import canonical, digest, read_json
from .controller import domain, execute_program


SCHEMA = "future-reader.local-worker/1"
_MODEL = domain()
_MODEL_DIGEST = digest(_MODEL)
_EDGE = {(edge["source"], edge["target"]): edge for edge in _MODEL["edges"]}
_HEARTBEAT_S = 0.075
_MIN_LEASE_MS = 400
_MAX_GENERATION = 256
_MAX_RESTARTS = 4
_LAUNCH_RETRY_MS = 1000


def _job_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        raise ValueError("Use a job ID of 1–64 letters, digits, dots, dashes or underscores")
    return value


def _database(path):
    value = os.fspath(path)
    if not value or value == ":memory:":
        raise ValueError("Local worker jobs require a persistent SQLite file")
    return Path(value).resolve()


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


@contextmanager
def _db(path, *, create=False):
    target = path if create else f"{path.as_uri()}?mode=rw"
    connection = sqlite3.connect(target, uri=not create, timeout=1, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _transaction(connection, *, write=True):
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _wall_ms():
    return time.time_ns() // 1_000_000


def _record(connection, job):
    row = connection.execute("SELECT * FROM live_jobs WHERE job=?", (job,)).fetchone()
    if row is None:
        raise ValueError("Unknown local worker job")
    if row["schema_version"] != SCHEMA or row["model_digest"] != _MODEL_DIGEST:
        raise ValueError("The worker job needs its original model or an explicit migration")
    return row


def _plan(program, now, check_available):
    route = execute_program(program, now=now, check_available=check_available)
    if not route["eligible"] or len(route["path"]) < 2:
        raise ValueError(f"The local program has no eligible move sequence ({route['reason'] or 'NO_MOVES'})")
    steps = []
    tick = now
    for source, target in zip(route["path"], route["path"][1:]):
        edge = _EDGE[source, target]
        tick = max(tick, edge["opens"]) + edge["duration"]
        steps.append({"source": source, "target": target, "tick": tick, "cost": edge["cost"]})
    return steps


def create_job(store, job, program, *, now=0, check_available=True, work_ms=600):
    """Persist a validated practice program for a real local worker to run."""
    path, job = _database(store), _job_id(job)
    _integer(work_ms, 25, 5000, "Per-move fixture work duration in milliseconds")
    steps = _plan(program, now, check_available)
    checkpoint = {"state": steps[0]["source"], "tick": now, "cost": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    with _db(path, create=True) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS live_jobs (
                job TEXT PRIMARY KEY, schema_version TEXT NOT NULL, model_digest TEXT NOT NULL,
                program TEXT NOT NULL, program_digest TEXT NOT NULL,
                now INTEGER NOT NULL, check_available INTEGER NOT NULL, work_ms INTEGER NOT NULL,
                cursor INTEGER NOT NULL, state TEXT NOT NULL, tick INTEGER NOT NULL, cost INTEGER NOT NULL,
                checkpoint_digest TEXT NOT NULL, generation INTEGER NOT NULL, owner TEXT,
                worker_pid INTEGER, launcher_pid INTEGER, heartbeat_seq INTEGER NOT NULL, heartbeat_wall_ms INTEGER,
                heartbeat_elapsed_ms INTEGER, phase TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_effects (
                job TEXT NOT NULL, step INTEGER NOT NULL, generation INTEGER NOT NULL, worker_pid INTEGER NOT NULL,
                effect TEXT NOT NULL, PRIMARY KEY(job,step)
            );
            CREATE TABLE IF NOT EXISTS live_generations (
                job TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT NOT NULL,
                claimed_at_ms INTEGER NOT NULL, worker_pid INTEGER, start_ms INTEGER,
                PRIMARY KEY(job,generation)
            );
        """)
        with _transaction(connection):
            if "launcher_pid" not in {row["name"] for row in connection.execute("PRAGMA table_info(live_jobs)")}:
                connection.execute("ALTER TABLE live_jobs ADD COLUMN launcher_pid INTEGER")
            try:
                connection.execute("""
                    INSERT INTO live_jobs
                    (job,schema_version,model_digest,program,program_digest,now,check_available,work_ms,
                     cursor,state,tick,cost,checkpoint_digest,generation,heartbeat_seq,phase)
                    VALUES (?,?,?,?,?,?,?,?,0,?,?,0,?,0,0,'QUEUED')
                """, (job, SCHEMA, _MODEL_DIGEST, canonical(program).decode("ascii"), digest(program), now,
                      int(check_available), work_ms, checkpoint["state"], now, digest(checkpoint)))
            except sqlite3.IntegrityError as error:
                raise ValueError("This local worker job ID already exists") from error
    return status(path, job)


def _verified(connection, row):
    program = json.loads(row["program"])
    if (digest(program) != row["program_digest"] or row["check_available"] not in (0, 1)
            or row["phase"] not in ("QUEUED", "RUNNING", "COMPLETE")):
        raise ValueError("The persisted program differs from its fingerprint")
    plan = _plan(program, row["now"], bool(row["check_available"]))
    effects = [dict(entry) for entry in connection.execute("""
        SELECT step,generation,worker_pid,effect FROM live_effects WHERE job=? ORDER BY step
    """, (row["job"],))]
    if len(effects) != row["cursor"] or len(effects) > len(plan):
        raise ValueError("The effect count disagrees with the durable checkpoint")
    checkpoint = {"state": plan[0]["source"], "tick": row["now"], "cost": 0}
    for index, entry in enumerate(effects):
        effect = json.loads(entry["effect"])
        generation = entry["generation"]
        if (entry["step"] != index or canonical(effect) != canonical(plan[index])
                or not 1 <= generation <= row["generation"] or entry["worker_pid"] <= 0):
            raise ValueError("The effect journal diverges from the verified program")
        checkpoint = {"state": effect["target"], "tick": effect["tick"], "cost": checkpoint["cost"] + effect["cost"]}
        entry["effect"] = effect
    observed = {"state": row["state"], "tick": row["tick"], "cost": row["cost"]}
    if (observed != checkpoint or digest(checkpoint) != row["checkpoint_digest"]
            or (row["phase"] == "COMPLETE") != (len(effects) == len(plan))):
        raise ValueError("The observed checkpoint disagrees with effect replay")
    return plan, effects


def status(store, job):
    """Read the job and independently verify its committed effects."""
    with _db(_database(store)) as connection, _transaction(connection, write=False):
        row = _record(connection, _job_id(job))
        plan, effects = _verified(connection, row)
        generations = [dict(entry) for entry in connection.execute("""
            SELECT generation,reason,claimed_at_ms,worker_pid,start_ms FROM live_generations WHERE job=?
            ORDER BY generation
        """, (row["job"],))]
        return {
            "schema": SCHEMA, "mode": "REAL_LOCAL_PROCESS_WITH_SIMULATED_EFFECTS", "job": row["job"],
            "status": row["phase"], "plan": plan, "generation": row["generation"],
            "worker_pid": row["worker_pid"], "launcher_pid": row["launcher_pid"],
            "heartbeat_seq": row["heartbeat_seq"],
            "heartbeat_wall_ms": row["heartbeat_wall_ms"], "heartbeat_elapsed_ms": row["heartbeat_elapsed_ms"],
            "checkpoint": {"state": row["state"], "tick": row["tick"], "cost": row["cost"],
                           "sha256": row["checkpoint_digest"]},
            "cursor": row["cursor"], "effects": effects, "generations": generations,
            "program_digest": row["program_digest"],
        }


def _claim(connection, job, *, baseline, reason):
    """Acquire a newer fencing generation only if its observed baseline still holds."""
    with _transaction(connection):
        row = _record(connection, job)
        if row["phase"] == "COMPLETE" or (row["generation"], row["heartbeat_seq"]) != baseline:
            return None
        _verified(connection, row)
        generation = row["generation"] + 1
        if generation > _MAX_GENERATION:
            raise ValueError("The local job reached its recovery generation limit")
        token = uuid4().hex
        connection.execute("""
            UPDATE live_jobs SET generation=?,owner=?,worker_pid=NULL,launcher_pid=NULL,
                heartbeat_seq=heartbeat_seq+1,heartbeat_wall_ms=NULL,heartbeat_elapsed_ms=NULL,phase='RUNNING'
            WHERE job=?
        """, (generation, token, job))
        connection.execute("""
            INSERT INTO live_generations (job,generation,reason,claimed_at_ms) VALUES (?,?,?,?)
        """, (job, generation, reason, _wall_ms()))
        return generation, token


def _fence(row, generation, token, pid):
    return row["generation"] == generation and row["owner"] == token and row["worker_pid"] in (None, pid)


def _heartbeat(connection, job, generation, token, pid, elapsed_ms, *, begin=False, launcher_pid=None):
    with _transaction(connection):
        row = _record(connection, job)
        if not _fence(row, generation, token, pid) or row["phase"] == "COMPLETE":
            return False
        stamp = _wall_ms()
        connection.execute("""
            UPDATE live_jobs SET worker_pid=?,heartbeat_seq=heartbeat_seq+1,
                heartbeat_wall_ms=?,heartbeat_elapsed_ms=? WHERE job=?
        """, (pid, stamp, elapsed_ms, job))
        if begin:
            connection.execute("UPDATE live_jobs SET launcher_pid=? WHERE job=?", (launcher_pid, job))
            connection.execute("""
                UPDATE live_generations SET worker_pid=?,start_ms=?
                WHERE job=? AND generation=?
            """, (pid, stamp, job, generation))
        return True


def _commit(connection, job, generation, token, pid, expected_cursor):
    """Commit the local effect and checkpoint in one transaction."""
    with _transaction(connection):
        row = _record(connection, job)
        if not _fence(row, generation, token, pid) or row["worker_pid"] != pid:
            return "FENCED"
        if row["cursor"] > expected_cursor:
            return "ALREADY_COMMITTED"
        if row["phase"] == "COMPLETE" or row["cursor"] != expected_cursor:
            raise ValueError("Invalid work cursor")
        plan, _ = _verified(connection, row)
        effect = plan[expected_cursor]
        cursor = expected_cursor + 1
        checkpoint = {"state": effect["target"], "tick": effect["tick"], "cost": row["cost"] + effect["cost"]}
        phase = "COMPLETE" if cursor == len(plan) else "RUNNING"
        connection.execute("""
            INSERT INTO live_effects (job,step,generation,worker_pid,effect) VALUES (?,?,?,?,?)
        """, (job, expected_cursor, generation, pid, canonical(effect).decode("ascii")))
        connection.execute("""
            UPDATE live_jobs SET cursor=?,state=?,tick=?,cost=?,checkpoint_digest=?,
                phase=?,heartbeat_seq=heartbeat_seq+1 WHERE job=?
        """, (cursor, checkpoint["state"], checkpoint["tick"], checkpoint["cost"],
              digest(checkpoint), phase, job))
        return phase


def _worker(store, job, generation, token, disconnected):
    pid = os.getpid()
    launcher_pid = os.getppid() if getattr(sys, "frozen", False) else pid
    started = time.monotonic_ns()
    with _db(_database(store)) as connection:
        if not _heartbeat(connection, job, generation, token, pid, 0, begin=True, launcher_pid=launcher_pid):
            return 0
        while not disconnected.is_set():
            row = _record(connection, job)
            if not _fence(row, generation, token, pid) or row["phase"] == "COMPLETE":
                return 0
            finish = time.monotonic_ns() + row["work_ms"] * 1_000_000
            while (remaining := finish - time.monotonic_ns()) > 0:
                if disconnected.wait(min(_HEARTBEAT_S, remaining / 1_000_000_000)):
                    return 0
                elapsed = (time.monotonic_ns() - started) // 1_000_000
                if not _heartbeat(connection, job, generation, token, pid, elapsed):
                    return 0
            if disconnected.is_set():
                return 0
            result = _commit(connection, job, generation, token, pid, row["cursor"])
            if result in ("COMPLETE", "FENCED"):
                return 0
    return 0


def _start_worker(store, job, claim):
    generation, token = claim
    command = [sys.executable]
    if getattr(sys, "frozen", False):
        command.append("--future-reader-worker")
    else:
        command.extend(("-B", "-m", "future_reader.live_failover"))
    command.extend(("_worker", str(store), job, str(generation)))
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Inherit the claim through a private pipe, keeping it out of command lines.
    try:
        process.stdin.write(token.encode("ascii"))
        process.stdin.flush()
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise
    return generation, process


def _reap(child):
    if child is not None:
        process = child[1]
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        finally:
            process.stdin.close()


def _supervise(store, job, *, timeout_s, lease_ms, fault_after=None):
    path, job = _database(store), _job_id(job)
    _integer(timeout_s, 1, 120, "Supervisor time budget in seconds")
    _integer(lease_ms, _MIN_LEASE_MS, 30_000, "Heartbeat loss window in milliseconds")
    finish = time.monotonic() + timeout_s
    seen = None
    changed = time.monotonic()
    child = None
    injected = None
    attempts = 0
    terminal = False
    try:
        with _db(path) as connection:
            while time.monotonic() < finish:
                row = _record(connection, job)
                if row["phase"] == "COMPLETE":
                    terminal = True
                    break
                baseline = row["generation"], row["heartbeat_seq"]
                now = time.monotonic()
                if baseline != seen:
                    seen, changed = baseline, now
                owns_generation = child is not None and child[0] == row["generation"]
                if (fault_after is not None and injected is None and row["cursor"] == fault_after
                        and owns_generation and child[1].poll() is None and row["worker_pid"] is not None
                        and child[1].pid in (row["launcher_pid"], row["worker_pid"])):
                    worker_pid = row["worker_pid"]
                    child[1].kill()
                    exit_code = child[1].wait(timeout=5)
                    injected = {"pid": child[1].pid, "worker_pid": worker_pid, "exit_code": exit_code,
                                "checkpoint_cursor": row["cursor"]}
                reason = None
                if row["generation"] == 0:
                    reason = "START"
                elif owns_generation and child[1].poll() is not None:
                    reason = "OWNED_PROCESS_EXITED"
                elif now - changed >= (max(lease_ms, _LAUNCH_RETRY_MS) if row["worker_pid"] is None else lease_ms) / 1000:
                    reason = "HEARTBEAT_STALLED"
                if reason is not None:
                    if attempts >= _MAX_RESTARTS:
                        break
                    claim = _claim(connection, job, baseline=baseline, reason=reason)
                    if claim is not None:
                        _reap(child)
                        child = _start_worker(path, job, claim)
                        attempts += 1
                time.sleep(min(_HEARTBEAT_S / 2, max(0, finish - time.monotonic())))
    finally:
        if terminal and child is not None:
            try:
                child[1].wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
        _reap(child)
    result = status(path, job)
    result["supervisor_outcome"] = "COMPLETE" if result["status"] == "COMPLETE" else "PAUSED"
    if injected is not None:
        result["injected_process_loss"] = injected
    return result


def run(store, job, *, timeout_s=30, lease_ms=1200):
    """Start, watch or recover a job through the bounded local supervisor."""
    return _supervise(store, job, timeout_s=timeout_s, lease_ms=lease_ms)


def demo(store):
    """Kill the owned worker after its first durable effect and verify recovery."""
    job = f"failover-{uuid4().hex[:12]}"
    program = {"schema": "future-reader.program/1", "steps": [
        {"op": "Start", "state": "S"}, {"op": "Move", "to": "B"}, {"op": "Move", "to": "G"},
        {"op": "Goal", "state": "G"},
    ]}
    create_job(store, job, program, work_ms=600)
    result = _supervise(store, job, timeout_s=25, lease_ms=1500, fault_after=1)
    failure = result.get("injected_process_loss")
    if (not failure or result["supervisor_outcome"] != "COMPLETE"
            or [entry["generation"] for entry in result["effects"]] != [1, 2]
            or result["checkpoint"]["state"] != "G"):
        raise RuntimeError("The failover exercise did not observe the expected two-generation continuation")
    return result


def _parent_pipe():
    if sys.stdin is not None:
        return os.dup(sys.stdin.fileno())
    if os.name == "nt":
        from ctypes import WinDLL, byref, wintypes
        import msvcrt

        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel32.GetStdHandle(wintypes.DWORD(-10).value)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        current = kernel32.GetCurrentProcess()
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, wintypes.LPHANDLE,
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        duplicate = wintypes.HANDLE()
        if not handle or not kernel32.DuplicateHandle(current, handle, current, byref(duplicate), 0, False, 2):
            raise OSError("Could not duplicate the private worker standard-input handle")
        try:
            return msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(duplicate)
            raise
    raise ValueError("The private worker standard-input pipe is unavailable")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("create")
    create.add_argument("store", type=Path)
    create.add_argument("job")
    create.add_argument("program", type=Path)
    create.add_argument("--work-ms", type=int, default=600)
    for action in ("run", "status", "demo", "_worker"):
        command = commands.add_parser(action)
        command.add_argument("store", type=Path)
        if action != "demo":
            command.add_argument("job")
        if action == "run":
            command.add_argument("--timeout-s", type=int, default=30)
            command.add_argument("--lease-ms", type=int, default=1200)
        if action == "_worker":
            command.add_argument("generation", type=int)
    args = parser.parse_args(argv)
    try:
        if args.operation == "create":
            result = create_job(args.store, args.job, read_json(args.program), work_ms=args.work_ms)
        elif args.operation == "run":
            result = run(args.store, args.job, timeout_s=args.timeout_s, lease_ms=args.lease_ms)
        elif args.operation == "status":
            result = status(args.store, args.job)
        elif args.operation == "demo":
            result = demo(args.store)
        else:
            parent_pipe = _parent_pipe()
            source = os.fdopen(parent_pipe, "rb")
            try:
                token = source.read(32).decode("ascii")
                if re.fullmatch(r"[0-9a-f]{32}", token) is None:
                    raise ValueError("Missing internal worker claim")
            except BaseException:
                source.close()
                raise
            disconnected = Event()

            def watch_parent():
                try:
                    source.read(1)
                finally:
                    source.close()
                    disconnected.set()

            Thread(target=watch_parent, daemon=True).start()
            return _worker(args.store, args.job, args.generation, token, disconnected)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as error:
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
