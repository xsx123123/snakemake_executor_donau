"""Conservative journal recovery for CLI fallback when the Rust proxy is absent.

Shares Rust's flock and event schema. Unknown submission intents are reconciled
by exact job name; absence is never treated as permission to resubmit.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from snakemake_interface_common.exceptions import WorkflowError

_TERMINAL = {"FINISHED", "SUCCEEDED", "DONE", "0", "FAILED", "ABORTED", "TIMEOUT",
             "NODE_FAIL", "TERMINATED", "EXIT", "CANCELED", "CANCELLED"}


class LegacyRecovery:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.identities = {}
        self.jobs = {}
        self.healthy = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ownership = open(self.path.with_suffix(".lock"), "a+b")
        try:
            fcntl.flock(self.ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = self.path.read_bytes() if self.path.exists() else b""
            lines = data.split(b"\n")
            for index, line in enumerate(lines):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    job = event["job"]
                    # Validate fields required for safe reuse before accepting this journal.
                    for field in ("id", "name", "run_uuid", "state", "donau_jobid"):
                        job[field]
                    self.jobs[job["id"]] = job
                except (ValueError, KeyError, TypeError):
                    if index != len(lines) - 1 or data.endswith(b"\n"):
                        raise WorkflowError("Corrupt Donau journal; reconcile manually before submission")
            temporary = self.path.with_suffix(".snapshot.tmp")
            with open(temporary, "wb") as output:
                for job in self.jobs.values():
                    output.write((json.dumps({"event":"snapshot", "job":job}) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(str(temporary), str(self.path))
            directory = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.output = open(self.path, "ab", buffering=0)
        except Exception:
            self.ownership.close()
            raise

    def _record(self, event, job):
        with self.lock:
            if not self.healthy:
                raise WorkflowError("Donau journal is unhealthy; submission stopped")
            job = dict(job, ts=int(time.time()))
            try:
                data = memoryview((json.dumps({"event":event, "job":job}) + "\n").encode())
                while data:
                    count = self.output.write(data)
                    if not count:
                        raise OSError("short journal write")
                    data = data[count:]
                os.fsync(self.output.fileno())
            except OSError:
                self.healthy = False
                raise
            self.jobs[job["id"]] = job

    @staticmethod
    def _query(args):
        tool = str(Path(os.environ["DONAU_BIN_DIR"]) / "djob") if os.environ.get("DONAU_BIN_DIR") else "djob"
        return subprocess.check_output([tool] + args, text=True, stderr=subprocess.STDOUT, timeout=30)

    def _reconcile(self, previous):
        matches = {}
        for historical in (False, True):
            args = (["-D"] if historical else []) + ["-o", "jobId name state", "--no-header"]
            for line in self._query(args).splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0].isdigit() and parts[1] == previous["name"]:
                    if parts[0] not in previous.get("previous_ids", []):
                        matches[parts[0]] = parts[2].upper()
        if len(matches) != 1:
            raise WorkflowError("Ambiguous Donau submission intent {} ({} matches); "
                                "inspect djob before retrying".format(previous["id"], len(matches)))
        jid, state = next(iter(matches.items()))
        previous = dict(previous, donau_jobid=jid, state=state)
        self._record("job_submitted", previous)
        return previous

    def submit(self, request, submit):
        with self.lock:
            identity_lock = self.identities.setdefault(request["id"], threading.Lock())
        with identity_lock:
            with self.lock:
                previous = self.jobs.get(request["id"])
            previous_ids = []
            if previous:
                if previous["donau_jobid"] is None and previous["state"].upper() not in _TERMINAL:
                    previous = self._reconcile(previous)
                    request["logfile"] = previous.get("logfile") or request["logfile"]
                    return previous["donau_jobid"]
                if previous["donau_jobid"] and previous["state"].upper() not in _TERMINAL:
                    states = {}
                    try:
                        for historical in (False, True):
                            args = (["-D"] if historical else []) + ["-o", "jobId state", "--no-header", previous["donau_jobid"]]
                            for line in self._query(args).splitlines():
                                parts = line.split()
                                if len(parts) >= 2 and parts[0] == previous["donau_jobid"]:
                                    states[parts[0]] = parts[1].upper()
                            if states:
                                break
                    except (OSError, subprocess.SubprocessError):
                        pass
                    previous = dict(previous, state=states.get(previous["donau_jobid"], "UNKNOWN"))
                    self._record("job_state", previous)
                same_attempt = (previous["run_uuid"] == request["run_uuid"] and
                                previous.get("attempt", 1) == request.get("attempt", 1))
                if previous["state"].upper() not in _TERMINAL or same_attempt:
                    if previous["donau_jobid"]:
                        request["logfile"] = previous.get("logfile") or request["logfile"]
                        return previous["donau_jobid"]
                    raise WorkflowError("Previous Donau submission failed; no job ID")
                previous_ids = list(previous.get("previous_ids", []))
                if previous["donau_jobid"]:
                    previous_ids.append(previous["donau_jobid"])
            job = {key:request[key] for key in ("id", "name", "run_uuid", "logfile")}
            job.update(attempt=request.get("attempt", 1), donau_jobid=None, state="UNKNOWN",
                       previous_ids=previous_ids)
            self._record("submit_intent", job)
            jid = submit()
            job.update(donau_jobid=jid, state="SUBMITTED")
            self._record("job_submitted", job)
            return jid

    def close(self):
        with self.lock:
            self.output.close()
            self.ownership.close()
