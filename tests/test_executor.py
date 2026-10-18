import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from snakemake_executor_plugin_donau import Executor, ExecutorSettings
from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo


def executor(workers=4):
    obj = object.__new__(Executor)
    obj._submit_concurrency = workers
    obj._submit_pool = ThreadPoolExecutor(max_workers=workers)
    obj._last_status_heartbeat = 0
    obj.run_job_pre = lambda job: None
    obj._build_submit_request = lambda job: {"logfile": "log", "job": job}
    return obj


def test_bounded_parallel_submissions_and_main_thread_callbacks():
    obj = executor()
    running = [0, 0]
    lock = threading.Lock()
    caller = threading.get_ident()
    reported = []

    def worker(request):
        with lock:
            running[0] += 1
            running[1] = max(running)
        time.sleep(.02)
        with lock:
            running[0] -= 1
        return str(request["job"])

    def report(job, request, jid):
        assert threading.get_ident() == caller
        reported.append((job, jid))

    obj._submit_worker = worker
    obj._report_submission = report
    try:
        obj.run_jobs(range(40))
        assert 1 < running[1] <= 4
        assert sorted(reported) == [(n, str(n)) for n in range(40)]
        obj.run_job(100)
        assert reported[-1] == (100, "100")
    finally:
        obj._submit_pool.shutdown()


def test_failed_submission_drains_and_reports_accepted_jobs():
    obj = executor(2)
    reported = []

    def worker(request):
        if request["job"] == 0:
            raise RuntimeError("submission failed")
        time.sleep(.02)
        return "1"

    obj._submit_worker = worker
    obj._report_submission = lambda job, request, jid: reported.append(job)
    try:
        with pytest.raises(RuntimeError, match="submission failed"):
            obj.run_jobs(range(10))
        assert reported == [1]
    finally:
        obj._submit_pool.shutdown()


def test_settings_registered():
    from dataclasses import fields
    settings = ExecutorSettings()
    assert settings.submit_concurrency == 16
    assert settings.any_arch is True
    assert len(fields(settings)) == 10


def test_status_truth_and_unknown_preserved():
    obj = executor()
    obj.executor_settings = SimpleNamespace(heartbeat_interval=0)
    states = ["FINISHED", "SUCCEEDED", "DONE", "0", "FAILED", "ABORTED",
              "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT", "CANCELED", "CANCELLED",
              "RUNNING", "PENDING", "UNKNOWN"]
    jobs = [SubmittedJobInfo(SimpleNamespace(name="test"), external_jobid=str(i),
                             aux={"logfile": "log"}) for i in range(500)]
    truth = {str(i): states[i % len(states)] for i in range(499)}
    successes, failures = [], []

    async def query(ids):
        return truth

    async def diag(jid):
        return {}

    obj._get_donau_job_status_async = query
    obj._get_job_diagnostics = diag
    obj.report_job_success = lambda job: successes.append(job.external_jobid)
    obj.report_job_error = lambda job, **kwargs: failures.append(job.external_jobid)

    async def check():
        return [job.external_jobid async for job in obj.check_active_jobs(jobs)]

    try:
        active = asyncio.run(check())
        assert set(successes) == {jid for jid, state in truth.items() if state in states[:4]}
        assert set(failures) == {jid for jid, state in truth.items() if state in states[4:12]}
        assert set(active) == set(str(i) for i in range(500)) - set(successes) - set(failures)
        assert "499" in active
    finally:
        obj._submit_pool.shutdown()


def test_two_query_polling_and_missing_ids(monkeypatch):
    obj = object.__new__(Executor)
    calls = []

    class Process:
        returncode = 0

        def __init__(self, args):
            self.args = args

        async def communicate(self):
            if "-D" in self.args:
                return b"2 FINISHED\nnoise warning\n", b""
            return b"1 RUNNING\nError text\n", b""

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(obj._get_donau_job_status_async({"1", "2", "3"}))
    assert result == {"1": "RUNNING", "2": "FINISHED"}
    assert len(calls) == 2
    assert "-D" in calls[1]
    assert "1" not in calls[1]


def test_resource_mapping_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    obj = object.__new__(Executor)
    obj.run_uuid = "abcdef-12345"
    obj.executor_settings = ExecutorSettings()
    obj.format_job_exec = lambda job: "echo 'hello world'"

    class Resources(dict):
        __getattr__ = dict.__getitem__

    job = SimpleNamespace(name="align", jobid=12, output=["result.txt"], wildcards_dict={"sample": "a/b"},
                          is_group=lambda: False, priority=10001, threads=4,
                          resources=Resources(queue="q", mem_mb_per_cpu=2000, npu=1,
                                              gpu=2, nodes=2, account="acct", mpi="openmpi",
                                              exclusive=1, tag="x=y", runtime=10,
                                              dsub_extra='--env "A=hello world"'))
    request = obj._build_submit_request(job)
    assert request["cmd"] == [
        "dsub", "-n", "smk_align_sample-a-b_12_abcdef", "-oo", request["logfile"],
        "--cwd", str(tmp_path), "-q", "q", "-aa", "-p", "9999", "-N", "2",
        "-A", "acct", "--mpi", "openmpi", "-x", "job", "--tag", "x=y",
        "-R", "cpu=4,mem=8000MB,npu=1,gpu=2", "-T", "600", "--env", "A=hello world",
        "echo 'hello world'",
    ]


def test_retry_count_and_original_backoff(monkeypatch):
    import subprocess
    obj = object.__new__(Executor)
    attempts, delays = [], []

    def fail(*args, **kwargs):
        attempts.append(args)
        raise subprocess.CalledProcessError(1, args[0], "failed")

    monkeypatch.setattr(subprocess, "check_output", fail)
    monkeypatch.setattr(time, "sleep", delays.append)
    with pytest.raises(subprocess.CalledProcessError):
        obj._run_cmd_with_retry(["dsub"])
    assert len(attempts) == 3
    assert delays == [2, 4]


def test_cli_settings_parse_true_and_false():
    from snakemake_executor_plugin_donau import _parse_bool
    for value in (True, "true", "yes", "1"):
        assert _parse_bool(value) is True
    for value in (False, "false", "no", "0"):
        assert _parse_bool(value) is False
    with pytest.raises(ValueError):
        _parse_bool("typo")


def test_failed_diagnostic_is_best_effort(monkeypatch):
    obj = object.__new__(Executor)

    class Process:
        returncode = 1

        async def communicate(self):
            return b"jobExitCode 0\n", b"error"

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(obj._get_job_diagnostics("123")) == {}


def test_sidecar_discovery_prefers_package_binary_then_module_directory(tmp_path, monkeypatch):
    from snakemake_executor_plugin_donau import sidecar_client
    monkeypatch.setattr(sidecar_client, "__file__", str(tmp_path / "sidecar_client.py"))
    sibling = tmp_path / "donau-sidecar"
    sibling.write_text("test fixture")
    assert sidecar_client.find_sidecar() == str(sibling)
    (tmp_path / "bin").mkdir()
    packaged = tmp_path / "bin/donau-sidecar"
    packaged.write_text("test fixture")
    assert sidecar_client.find_sidecar() == str(packaged)
    configured = tmp_path / "configured"
    assert sidecar_client.find_sidecar(str(configured)) == str(configured)
