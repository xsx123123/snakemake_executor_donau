"""Actual executor methods against a synthetic SQLite Donau scheduler.

No real cluster or workflow execution. Separate submission/verification/resource
measurements are reported; this is not Snakemake DAG end-to-end wall time.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import resource
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from loguru import logger
import psutil

from snakemake_executor_plugin_donau import Executor, ExecutorSettings
from snakemake_executor_plugin_donau.sidecar_client import SidecarClient

ROOT = Path(__file__).resolve().parents[1]
SUCCESS = {"FINISHED", "SUCCEEDED", "DONE", "0"}
FAILURE = {"FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT", "CANCELED", "CANCELLED"}
STATES = sorted(SUCCESS | FAILURE) + ["RUNNING", "PENDING", "UNKNOWN"]


def measure(count, mode):
    start_utc = datetime.now(timezone.utc).isoformat()
    source_hashes = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                     for folder in ("snakemake_executor_plugin_donau", "rust/src", "tests/fixtures")
                     for path in (ROOT / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    binary_hash = hashlib.sha256((ROOT / "rust/target/release/donau-sidecar").read_bytes()).hexdigest() if mode == "sidecar" else None
    with tempfile.TemporaryDirectory(prefix="donau-matrix-") as directory:
        directory = Path(directory)
        os.environ["MOCK_DONAU_DIR"] = str(directory / "mock")
        obj = object.__new__(Executor)
        obj.executor_settings = ExecutorSettings(mode=mode)
        obj._submit_concurrency = 1 if mode == "serial" else 16
        obj._submit_pool = ThreadPoolExecutor(max_workers=obj._submit_concurrency)
        obj._sidecar = None
        obj._journal_path = directory / "donau_sidecar.journal"
        obj.run_uuid = str(uuid.uuid4())
        obj._last_status_heartbeat = 0
        obj.run_job_pre = lambda job: None
        obj.format_job_exec = lambda job: "true"
        if mode == "sidecar":
            obj._sidecar = SidecarClient(ROOT / "rust/target/release/donau-sidecar", obj._journal_path)
        reports = []
        obj.report_job_submission = reports.append
        jobs = [SimpleNamespace(name="bench", jobid=i, wildcards_dict={"sample":str(i)},
                                is_group=lambda:False, priority=0, threads=1, attempt=1,
                                output=[str(directory / (str(i) + ".txt"))], resources={"mem_mb":64})
                for i in range(count)]
        peak = {"parent_rss_bytes":0, "sidecar_rss_bytes":0}
        stop = threading.Event()
        parent = psutil.Process()
        sidecar = psutil.Process(obj._sidecar.connection.process.pid) if obj._sidecar else None

        def sample():
            while not stop.wait(.1):
                peak["parent_rss_bytes"] = max(peak["parent_rss_bytes"], parent.memory_info().rss)
                if sidecar:
                    peak["sidecar_rss_bytes"] = max(peak["sidecar_rss_bytes"], sidecar.memory_info().rss)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_start = time.process_time()
            start = time.perf_counter()
            obj.run_jobs(jobs)
            submit_wall = time.perf_counter() - start
            parent_cpu = time.process_time() - cpu_start
            after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
            assert len(reports) == count
            with sqlite3.connect(str(directory / "mock/jobs.sqlite")) as conn:
                actual_count = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
            assert actual_count == count
            sidecar_cpu_percent = None
            if sidecar:
                cpu_before = sum(sidecar.cpu_times()[:2])
                start = time.perf_counter()
                time.sleep(6)
                sidecar_cpu_percent = 100 * (sum(sidecar.cpu_times()[:2]) - cpu_before) / (time.perf_counter() - start)
            truth = {}
            with sqlite3.connect(str(directory / "mock/jobs.sqlite")) as conn:
                for i, report in enumerate(reports):
                    state = STATES[i % len(STATES)] if i < 500 else "FINISHED"
                    conn.execute("UPDATE jobs SET state=? WHERE id=?", (state, report.external_jobid))
                    truth[report.external_jobid] = state
                # Scheduler omission: it must stay active, never report success/failure.
                missing_id = reports[-1].external_jobid
                conn.execute("DELETE FROM jobs WHERE id=?", (missing_id,))
                truth[missing_id] = "UNKNOWN"
                conn.commit()
            if sidecar:
                time.sleep(5.1)  # Expire the documented 5s poll cache after changing mock truth.
            successes, failures = [], []
            obj.report_job_success = lambda job:successes.append(job.external_jobid)
            obj.report_job_error = lambda job, **kwargs:failures.append(job.external_jobid)
            expected_success = {jid for jid, state in truth.items() if state in SUCCESS}
            expected_failure = {jid for jid, state in truth.items() if state in FAILURE}

            async def check():
                return [job.external_jobid async for job in obj.check_active_jobs(reports)]

            start = time.perf_counter()
            active = asyncio.run(check())
            check_wall = time.perf_counter() - start
            assert set(successes) == expected_success
            assert set(failures) == expected_failure
            assert set(active) == set(truth) - expected_success - expected_failure
            with sqlite3.connect(str(directory / "mock/jobs.sqlite")) as conn:
                calls = conn.execute("SELECT argv FROM events WHERE command='djob'").fetchall()
                status_calls = [json.loads(row[0]) for row in calls if "-ll" not in json.loads(row[0])]
            return {"start_utc":start_utc,"end_utc":datetime.now(timezone.utc).isoformat(),
                    "mode":mode,"count":count,"source_sha256":source_hashes,"sidecar_binary_sha256":binary_hash,"submit_delay_seconds":float(os.environ["MOCK_DSUB_DELAY"]),"submit_wall_seconds":submit_wall,
                    "submit_throughput":count / submit_wall,"parent_cpu_seconds":parent_cpu,
                    "direct_children_cpu_seconds":(after_children.ru_utime + after_children.ru_stime -
                                                   before_children.ru_utime - before_children.ru_stime),
                    "memory_peak":peak,"sidecar_cpu_percent_6s_pending":sidecar_cpu_percent,
                    "check_wall_seconds_including_diagnostics":check_wall,
                    "total_status_cli_calls_including_intervals":len(status_calls),
                    "verified_truth_jobs":len(truth),"false_success":len(set(successes) ^ expected_success),
                    "false_failure":len(set(failures) ^ expected_failure),"missing_id_kept_active":missing_id in active}
        finally:
            stop.set()
            sampler.join()
            obj._submit_pool.shutdown()
            if obj._sidecar:
                obj._sidecar.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--modes", nargs="+", choices=["serial", "legacy", "sidecar"])
    parser.add_argument("--submit-delay", type=float, default=.02)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/matrix.jsonl")
    args = parser.parse_args()
    logger.remove()
    os.environ["DONAU_BIN_DIR"] = str(ROOT / "tests/fixtures/cluster")
    os.environ["PATH"] = str(ROOT / "tests/fixtures/cluster") + os.pathsep + "/usr/bin:/bin:" + os.environ["PATH"]
    os.environ["MOCK_EXECUTE"] = "0"
    os.environ["MOCK_DSUB_DELAY"] = str(args.submit_delay)
    with args.output.open("w") as output:
        for count in args.counts:
            for mode in (args.modes or (["serial", "legacy", "sidecar"] if count == 1000 else ["legacy", "sidecar"])):
                result = measure(count, mode)
                output.write(json.dumps(result) + "\n")
                output.flush()
                print(json.dumps(result), flush=True)
