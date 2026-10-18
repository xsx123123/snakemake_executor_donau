from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from snakemake_executor_plugin_donau.sidecar_client import SidecarClient, SidecarUnavailable, SubmissionFailed

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "rust/target/debug/donau-sidecar"


@pytest.fixture
def client(tmp_path, monkeypatch, sidecar_binary):
    monkeypatch.setenv("DONAU_BIN_DIR", str(ROOT / "tests/fixtures/cluster"))
    monkeypatch.setenv("MOCK_DONAU_DIR", str(tmp_path / "mock"))
    monkeypatch.setenv("MOCK_EXECUTE", "0")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:" + os.environ["PATH"])
    instance = SidecarClient(sidecar_binary, tmp_path / "donau_sidecar.journal")
    yield instance
    instance.close()


def request(tmp_path, identifier="one"):
    return {"id":identifier, "name":"smk_" + identifier, "cwd":str(tmp_path),
            "logfile":str(tmp_path / "job.log"), "exec_cmd":"true", "run_uuid":"test-run"}


def count_jobs(tmp_path):
    path = tmp_path / "mock/jobs.sqlite"
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(str(path)) as conn:
            return conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    except sqlite3.OperationalError as error:
        # SQLite creates the file before the CLI has created its schema.
        if "no such table: jobs" in str(error):
            return 0
        raise


def test_concurrent_protocol_and_exactly_once_replay(client, tmp_path):
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda n: client.submit(request(tmp_path, str(n))), range(50)))
    assert len(set(ids)) == 50
    assert client.submit(request(tmp_path, "1")) == ids[1]
    assert count_jobs(tmp_path) == 50
    states = client.request({"type":"poll", "ids":ids + ["99999"]})["states"]
    assert states == dict.fromkeys(ids, "PENDING")
    client.request({"type":"cancel", "ids":[ids[0]]})
    assert client.request({"type":"poll", "ids":[ids[0]]})["states"][ids[0]] == "CANCELLED"


def test_killed_sidecar_recovers_same_identity(client, tmp_path):
    original = client.submit(request(tmp_path))
    client.connection.process.kill()
    client.connection.process.wait()
    assert client.submit(request(tmp_path)) == original
    assert client.restarts == 1
    assert count_jobs(tmp_path) == 1


def test_lost_response_reconciles_intent(tmp_path, monkeypatch, sidecar_binary):
    monkeypatch.setenv("DONAU_BIN_DIR", str(ROOT / "tests/fixtures/cluster"))
    monkeypatch.setenv("MOCK_DONAU_DIR", str(tmp_path / "mock"))
    monkeypatch.setenv("MOCK_EXECUTE", "0")
    monkeypatch.setenv("MOCK_AFTER_SUBMIT_DELAY", "1")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:" + os.environ["PATH"])
    client = SidecarClient(sidecar_binary, tmp_path / "donau_sidecar.journal")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.submit, request(tmp_path))
            deadline = time.monotonic() + 10
            while not count_jobs(tmp_path):
                assert time.monotonic() < deadline
                time.sleep(.01)
            client.connection.process.kill()
            assert future.result(timeout=10) == "1"
        assert count_jobs(tmp_path) == 1
        events = [json.loads(line) for line in (tmp_path / "donau_sidecar.journal").read_text().splitlines()]
        assert events[-1]["job"]["donau_jobid"] == "1"
    finally:
        client.close()


def test_unmatched_intent_is_not_resubmitted(client, tmp_path):
    # Write a crash-window intent with no discoverable Donau ID, then restart.
    client.close()
    journal = tmp_path / "donau_sidecar.journal"
    job = {"id":"one", "name":"smk_one", "run_uuid":"previous", "donau_jobid":None,
           "state":"UNKNOWN", "ts":int(time.time())}
    journal.write_text(json.dumps({"event":"submit_intent","job":job}) + "\n")
    recovered = SidecarClient(BINARY, journal)
    try:
        with pytest.raises(SubmissionFailed, match="ambiguous submission"):
            recovered.submit(request(tmp_path))
        assert count_jobs(tmp_path) == 0
    finally:
        recovered.close()


def test_retry_exhaustion(client, tmp_path, monkeypatch):
    # Child environment is set at spawn, so instantiate a new client for this case.
    client.close()
    monkeypatch.setenv("MOCK_DSUB_FAIL_N", "3")
    retrying = SidecarClient(BINARY, tmp_path / "retry.journal")
    try:
        with pytest.raises(SubmissionFailed, match="mock rejected"):
            retrying.submit(request(tmp_path))
        with sqlite3.connect(str(tmp_path / "mock/jobs.sqlite")) as conn:
            assert conn.execute("SELECT count(*) FROM events WHERE command='dsub'").fetchone()[0] == 3
        assert count_jobs(tmp_path) == 0
    finally:
        retrying.close()


def test_restart_limit_falls_back_through_journal_without_resubmission(client, tmp_path):
    import threading
    from snakemake_executor_plugin_donau import Executor
    original = request(tmp_path)
    jid = client.submit(original)
    client.max_restarts = 0
    client.connection.process.kill()
    client.connection.process.wait()
    obj = object.__new__(Executor)
    obj._sidecar = client
    obj._backend_lock = threading.Lock()
    obj._legacy_recovery = None
    obj._journal_path = tmp_path / "donau_sidecar.journal"
    obj._legacy_submit_worker = lambda req: pytest.fail("known live job must not be submitted again")
    try:
        assert obj._submit_worker({"sidecar":original, "name":"task", "logfile":"log"}) == jid
        assert obj._sidecar is None
        assert count_jobs(tmp_path) == 1
    finally:
        if obj._legacy_recovery is not None:
            obj._legacy_recovery.close()


def test_attempt_change_cannot_duplicate_active_writer(client, tmp_path):
    original = request(tmp_path)
    original["attempt"] = 2
    jid = client.submit(original)
    restarted_request = dict(original, attempt=1, run_uuid="new-workflow-run")
    assert client.submit(restarted_request) == jid
    assert count_jobs(tmp_path) == 1
    client.request({"type":"cancel", "ids":[jid]})
    client.request({"type":"poll", "ids":[jid]})
    next_attempt = dict(original, attempt=3)
    assert client.submit(next_attempt) != jid
    assert count_jobs(tmp_path) == 2


@pytest.mark.parametrize("count", [500, 10000])
def test_batch_poll_uses_at_most_two_cli_calls_and_matches_truth(client, tmp_path, count):
    # Initialize the independent mock database without submitting any task.
    subprocess.run([str(ROOT / "tests/fixtures/cluster/djob"), "-o", "jobId state", "--no-header"],
                   check=True, capture_output=True)
    with sqlite3.connect(str(tmp_path / "mock/jobs.sqlite")) as conn:
        conn.executemany("INSERT INTO jobs(id,name,state) VALUES (?,?,?)",
                         [(i, "smk_" + str(i), "FINISHED" if i % 2 else "RUNNING") for i in range(1, count + 1)])
        conn.commit()
        before = conn.execute("SELECT count(*) FROM events WHERE command='djob'").fetchone()[0]
    ids = [str(i) for i in range(1, count + 2)]
    states = client.request({"type":"poll", "ids":ids})["states"]
    assert states == {str(i):("FINISHED" if i % 2 else "RUNNING") for i in range(1, count + 1)}
    with sqlite3.connect(str(tmp_path / "mock/jobs.sqlite")) as conn:
        after = conn.execute("SELECT count(*) FROM events WHERE command='djob'").fetchone()[0]
    assert after - before == 2
    assert str(count + 1) not in states


@pytest.mark.parametrize("follow", [False, True])
def test_recovered_orphan_tracking_is_opt_in(tmp_path, monkeypatch, sidecar_binary, follow):
    monkeypatch.setenv("DONAU_BIN_DIR", str(ROOT / "tests/fixtures/cluster"))
    monkeypatch.setenv("MOCK_DONAU_DIR", str(tmp_path / "mock"))
    monkeypatch.setenv("MOCK_EXECUTE", "0")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:" + os.environ["PATH"])
    journal = tmp_path / "donau_sidecar.journal"
    original = SidecarClient(sidecar_binary, journal)
    jid = original.submit(request(tmp_path))
    original.close()
    recovered = SidecarClient(sidecar_binary, journal, recover_orphans=follow)
    try:
        # Requests are processed after startup reconciliation: diag acts as a barrier.
        recovered.request({"type":"diag", "id":jid})
        with sqlite3.connect(str(tmp_path / "mock/jobs.sqlite")) as conn:
            conn.execute("UPDATE jobs SET state='FINISHED' WHERE id=?", (jid,))
            conn.commit()
        time.sleep(6)
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        last = [row["job"] for row in rows if row["job"]["id"] == "one"][-1]
        assert (last["state"] == "FINISHED") is follow
        assert recovered.submit(dict(request(tmp_path), run_uuid="test-run")) == jid
        assert count_jobs(tmp_path) == 1
    finally:
        recovered.close()


def test_request_timeout_recovers_accepted_submit_without_duplicate(tmp_path, monkeypatch, sidecar_binary):
    monkeypatch.setenv("DONAU_BIN_DIR", str(ROOT / "tests/fixtures/cluster"))
    monkeypatch.setenv("MOCK_DONAU_DIR", str(tmp_path / "mock"))
    monkeypatch.setenv("MOCK_EXECUTE", "0")
    monkeypatch.setenv("MOCK_AFTER_SUBMIT_DELAY", "2")
    monkeypatch.setenv("PATH", "/usr/bin:/bin:" + os.environ["PATH"])
    client = SidecarClient(sidecar_binary, tmp_path / "donau_sidecar.journal", timeout=1)
    try:
        assert client.submit(request(tmp_path)) == "1"
        assert client.restarts == 1
        assert count_jobs(tmp_path) == 1
    finally:
        client.close()
