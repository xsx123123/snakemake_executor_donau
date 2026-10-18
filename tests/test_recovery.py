import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from snakemake_executor_plugin_donau.recovery import LegacyRecovery
from snakemake_interface_common.exceptions import WorkflowError


def test_fallback_journal_lock_and_concurrent_identity_replay(tmp_path, monkeypatch):
    journal = tmp_path / "donau_sidecar.journal"
    recovery = LegacyRecovery(journal)
    monkeypatch.setattr(recovery, "_query", lambda args:"123 RUNNING\n")
    request = {"id":"one", "name":"smk_one", "logfile":"old.log", "run_uuid":"run", "attempt":1}
    submitted = []
    try:
        with pytest.raises(OSError):
            LegacyRecovery(journal)
        def submit():
            submitted.append(1)
            return "123"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:recovery.submit(dict(request), submit), range(20)))
        assert results == ["123"] * 20
        assert len(submitted) == 1
    finally:
        recovery.close()
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert records[0]["event"] == "submit_intent"
    assert records[-1]["job"]["donau_jobid"] == "123"


def test_fallback_unknown_intent_cannot_trigger_cli_submit(tmp_path, monkeypatch):
    journal = tmp_path / "donau_sidecar.journal"
    job = {"id":"one", "name":"smk_one", "logfile":"old.log", "run_uuid":"run",
           "state":"UNKNOWN", "donau_jobid":None}
    journal.write_text(json.dumps({"event":"submit_intent", "job":job}) + "\n")
    recovery = LegacyRecovery(journal)
    monkeypatch.setattr(recovery, "_query", lambda args:"")
    try:
        with pytest.raises(WorkflowError, match="Ambiguous"):
            recovery.submit(dict(job), lambda:pytest.fail("must not submit unknown intent"))
    finally:
        recovery.close()
