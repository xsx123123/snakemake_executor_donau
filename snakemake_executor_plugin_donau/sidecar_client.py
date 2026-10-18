"""Thread-safe JSON Lines client. Never blindly replay a submit through legacy."""
import atexit
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .logging import logger


class SidecarUnavailable(RuntimeError):
    pass


class SidecarRequestError(RuntimeError):
    pass


class SubmissionFailed(RuntimeError):
    pass


def find_sidecar(configured=None):
    if configured:
        return str(Path(configured).expanduser().absolute())
    package = Path(__file__).resolve().parent
    for packaged in (package / "bin" / "donau-sidecar", package / "donau-sidecar"):
        if packaged.is_file():
            return str(packaged)
    return shutil.which("donau-sidecar")


class _Connection:
    def __init__(self, command, timeout):
        self.timeout = timeout
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0)
        self.pending = {}
        self.lock = threading.Lock()
        self.dead = False
        self.outgoing = queue.Queue(maxsize=1024)
        for target in (self._read, self._write, self._stderr):
            threading.Thread(target=target, daemon=True).start()

    def _fail(self, reason):
        with self.lock:
            if self.dead:
                return
            self.dead = True
            pending = list(self.pending.values())
            self.pending.clear()
        for response in pending:
            response.put_nowait(SidecarUnavailable(reason))

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("expected object")
                except (ValueError, UnicodeError):
                    logger.warning("Discarding malformed sidecar response")
                    continue
                if value.get("type") == "heartbeat":
                    logger.debug("[sidecar] metrics: {}", value)
                    continue
                if value.get("type") == "recovered":
                    jobs = value.get("jobs", [])
                    if jobs:
                        logger.warning("[recover] {} existing job(s): {}. Verify with "
                                       "djob -u $USER -o 'jobId name state'; absent states remain UNKNOWN.",
                                       len(jobs), [(job.get("id"), job.get("donau_jobid"),
                                                    job.get("state")) for job in jobs])
                    continue
                with self.lock:
                    response = self.pending.pop(value.get("request_id"), None)
                if response is not None:
                    response.put_nowait(value)
        except (OSError, ValueError) as error:
            logger.debug("Sidecar reader stopped: {}", error)
        finally:
            self._fail("sidecar stdout closed")

    def _write(self):
        try:
            while True:
                data = self.outgoing.get()
                if data is None:
                    break
                # FileIO.write may be partial for large messages.
                view = memoryview(data)
                while view:
                    count = self.process.stdin.write(view)
                    if not count:
                        raise OSError("sidecar stdin closed")
                    view = view[count:]
        except (OSError, ValueError) as error:
            self._fail(str(error))

    def _stderr(self):
        try:
            for line in self.process.stderr:
                logger.debug("[sidecar] {}", line.decode(errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def request(self, message):
        request_id = uuid.uuid4().hex
        response = queue.Queue(maxsize=1)
        deadline = time.monotonic() + self.timeout
        value = dict(message, request_id=request_id)
        data = (json.dumps(value, ensure_ascii=True) + "\n").encode()
        with self.lock:
            if self.dead:
                raise SidecarUnavailable("sidecar connection closed")
            self.pending[request_id] = response
        try:
            self.outgoing.put(data, timeout=self.timeout)
            result = response.get(timeout=max(0, deadline - time.monotonic()))
            if isinstance(result, Exception):
                raise result
            if result.get("type") == "error":
                error_type = SidecarRequestError if result.get("code") == "request_failed" else SidecarUnavailable
                raise error_type(result.get("msg", "sidecar protocol error"))
            return result
        except queue.Empty:
            raise SidecarUnavailable("sidecar request timed out")
        except queue.Full:
            raise SidecarUnavailable("sidecar write queue timed out")
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self, graceful=False):
        if graceful and self.process.poll() is None and not self.dead:
            try:
                self.request({"type": "shutdown"})
            except SidecarUnavailable:
                pass
        self._fail("sidecar connection closed")
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process.wait()
        try:
            self.outgoing.put_nowait(None)
        except queue.Full:
            pass
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


class SidecarClient:
    def __init__(self, path, journal, concurrency=16, recover_orphans=False,
                 timeout=30, max_restarts=3):
        self.command = [str(path), "--journal", str(journal), "--submit-concurrency",
                        str(concurrency)]
        if recover_orphans:
            self.command.append("--recover-orphans")
        self.timeout = timeout
        self.max_restarts = max_restarts
        self.restarts = 0
        self.lock = threading.Lock()
        self.connection = None
        self.closed = False
        self._start()
        atexit.register(self.close)

    def _start(self):
        connection = _Connection(self.command, self.timeout)
        try:
            hello = connection.request({"type": "hello", "proto": 1, "pid": os.getpid()})
            if hello.get("type") != "hello" or hello.get("proto") != 1 or hello.get("ok") is not True:
                raise SidecarUnavailable("sidecar protocol mismatch")
        except Exception:
            connection.close()
            raise
        self.connection = connection

    def request(self, message):
        while True:
            with self.lock:
                if self.closed:
                    raise SidecarUnavailable("sidecar client closed")
                connection = self.connection
            try:
                return connection.request(message)
            except SidecarUnavailable as error:
                with self.lock:
                    # Another caller may already have replaced the failed connection.
                    if self.connection is not connection:
                        continue
                    connection.close()
                    if self.restarts >= self.max_restarts:
                        raise SidecarUnavailable("sidecar restart limit reached: {}".format(error))
                    self.restarts += 1
                    logger.warning("Restarting Donau sidecar ({}/{}): {}",
                                   self.restarts, self.max_restarts, error)
                    try:
                        self._start()
                    except (OSError, SidecarUnavailable) as start_error:
                        if self.restarts >= self.max_restarts:
                            raise SidecarUnavailable(str(start_error))
                        # Keep closed connection as a sentinel; the next iteration retries.
                        continue

    def submit(self, request):
        result = self.request(dict(request, type="submit"))
        if result.get("type") == "submit_failed":
            raise SubmissionFailed(result.get("error", "submission failed"))
        if result.get("type") != "submitted" or not str(result.get("donau_jobid", "")).isdigit():
            raise SidecarUnavailable("invalid submitted response; outcome uncertain")
        if result.get("logfile"):
            request["logfile"] = result["logfile"]
        return str(result["donau_jobid"])

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.connection.close(graceful=True)
        atexit.unregister(self.close)
