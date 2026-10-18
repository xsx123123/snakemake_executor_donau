#!/usr/bin/env python3
"""SQLite-backed Donau CLI shim. No network or real scheduler is used."""
import json
import fcntl
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time


def db():
    root = Path(os.environ["MOCK_DONAU_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    # Serialize first-time schema creation. The state file can appear before
    # tables exist, and concurrent WAL transitions otherwise cause spurious CLI failures.
    with open(root / "schema.lock", "a+b") as ownership:
        fcntl.flock(ownership, fcntl.LOCK_EX)
        conn = sqlite3.connect(str(root / "jobs.sqlite"), timeout=30)
        ready = conn.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone()
        if not ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, state TEXT, pid INTEGER, code INTEGER, argv TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, command TEXT, argv TEXT)")
            conn.commit()
    return conn


def main():
    args = sys.argv[1:]
    conn = db()
    if args and args[0] == "--worker":
        jid, cwd, logfile, command = args[1:]
        conn.execute("UPDATE jobs SET state='RUNNING', pid=? WHERE id=?", (os.getpid(), jid))
        conn.commit()
        with open(logfile, "a") as log:
            result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, stdout=log, stderr=log)
        conn.execute("UPDATE jobs SET state=?, code=? WHERE id=? AND state!='CANCELLED'",
                     ("FINISHED" if result.returncode == 0 else "FAILED", result.returncode, jid))
        conn.commit()
        return
    tool = Path(sys.argv[0]).name
    conn.execute("INSERT INTO events VALUES (?, ?, ?)", (time.time(), tool, json.dumps(args)))
    conn.commit()
    if tool == "dsub":
        time.sleep(float(os.environ.get("MOCK_DSUB_DELAY", "0")))
        attempts = conn.execute("SELECT count(*) FROM events WHERE command='dsub'").fetchone()[0]
        if attempts <= int(os.environ.get("MOCK_DSUB_FAIL_N", "0")):
            print("mock rejected submission", file=sys.stderr)
            sys.exit(1)
        def value(flag, default):
            return args[args.index(flag) + 1] if flag in args else default
        name = value("-n", "mock")
        cursor = conn.execute("INSERT INTO jobs(name,state,argv) VALUES (?, 'PENDING', ?)", (name, json.dumps(args)))
        jid = cursor.lastrowid
        conn.commit()
        if os.environ.get("MOCK_EXECUTE", "1") == "1":
            logfile = value("-oo", str(Path(os.environ["MOCK_DONAU_DIR"]) / "job.log"))
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(jid),
                                        value("--cwd", os.getcwd()), logfile, args[-1]],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            conn.execute("UPDATE jobs SET pid=? WHERE id=?", (process.pid, jid))
            conn.commit()
        print("Job <{}> submitted".format(jid), flush=True)
        # Fault injection: accepted submission, but response may not reach the caller.
        time.sleep(float(os.environ.get("MOCK_AFTER_SUBMIT_DELAY", "0")))
    elif tool == "djob":
        time.sleep(float(os.environ.get("MOCK_DJOB_DELAY", "0")))
        if os.environ.get("MOCK_DJOB_ERROR") == "1":
            sys.exit(1)
        rows = conn.execute("SELECT id,name,state,code FROM jobs ORDER BY id").fetchall()
        ids = {arg for arg in args if arg.isdigit()}
        if "-ll" in args:
            for jid, name, state, code in rows:
                if str(jid) in ids:
                    print("jobExitCode {}\nexitMessage mock {}".format(code or 0, state))
            return
        for jid, name, state, code in rows:
            if ids and str(jid) not in ids:
                continue
            terminal = state in ("FINISHED", "SUCCEEDED", "DONE", "0", "FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT", "CANCELED", "CANCELLED")
            if terminal != ("-D" in args):
                continue
            if "jobId name state" in args:
                print(jid, name, state)
            else:
                print(jid, state)
    elif tool == "dkill":
        for arg in args:
            if arg.isdigit():
                row = conn.execute("SELECT pid FROM jobs WHERE id=?", (arg,)).fetchone()
                if row and row[0]:
                    try:
                        os.killpg(row[0], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                conn.execute("UPDATE jobs SET state='CANCELLED' WHERE id=?", (arg,))
        conn.commit()
        print("Cancelled")
    else:
        raise SystemExit("Unknown mock CLI " + tool)


if __name__ == "__main__":
    main()
