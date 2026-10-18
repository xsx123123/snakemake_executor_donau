import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "Test/run_cluster_tests.py"


def invoke(args, tmp_path):
    env = dict(os.environ, MOCK_DONAU_DIR=str(tmp_path / "scheduler"),
               PATH=str(ROOT / "tests/fixtures/cluster") + os.pathsep +
                    str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = subprocess.run([sys.executable, str(DRIVER)] + args, env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    return result


def latest_summary(workdir):
    pointer = json.loads((workdir / "latest-run.json").read_text())
    return json.loads((Path(pointer["control"]) / "summary.json").read_text())


def test_cluster_driver_dry_run_and_report_only(tmp_path):
    workdir = tmp_path / "dry"
    result = invoke(["--queue","mock","--samples","3","--dry-run","--monitor-seconds","0",
                     "--workdir",str(workdir)], tmp_path)
    assert result.returncode == 0, result.stdout
    summary = latest_summary(workdir)
    assert summary["run"]["config"]["samples"] == 3
    assert summary["run"]["dry_run"] is True
    assert summary["dsub_calls"] == 0
    assert invoke(["--report-only",str(workdir)], tmp_path).returncode == 0


def test_cluster_driver_real_chain_metrics_and_truth(tmp_path, sidecar_binary):
    workdir = tmp_path / "workflow"
    result = invoke(["--queue","mock","--samples","2","--jobs","2","--mode","sidecar",
                     "--sidecar-path",str(sidecar_binary),"--stress-scale","0.001","--chaos","off",
                     "--monitor-seconds","1","--poll-seconds","1","--require-state-sample","3",
                     "--workdir",str(workdir)], tmp_path)
    assert result.returncode == 0, result.stdout[-12000:]
    summary = latest_summary(workdir)
    assert summary["dsub_calls"] == 3
    assert len(summary["observed_job_ids"]) == 3
    assert summary["dsub_vendor_latency_seconds"]["p50"] > 0
    assert summary["state_verification"]["compared_terminal_reports"] == 3
    assert summary["state_verification"]["false_success"] == 0
    assert summary["state_verification"]["false_failure"] == 0
    assert summary["state_verification"]["unverified_report_ids"] == []
    assert summary["validation_exitcode"] == 0


def test_cluster_driver_crash_and_resume_legacy(tmp_path, sidecar_binary):
    workdir = tmp_path / "recovery"
    result = invoke(["--queue","mock","--samples","2","--jobs","2","--mode","sidecar",
                     "--sidecar-path",str(sidecar_binary),"--stress-scale","0.001","--chaos","off",
                     "--hold-seconds","4","--monitor-seconds","0","--poll-seconds","1",
                     "--crash-after-submissions","2","--workdir",str(workdir)], tmp_path)
    assert result.returncode == 3, result.stdout[-12000:]
    result = invoke(["--resume",str(workdir),"--unlock","--mode","legacy",
                     "--monitor-seconds","1","--require-state-sample","3"], tmp_path)
    assert result.returncode == 0, result.stdout[-12000:]
    with sqlite3.connect(str(tmp_path / "scheduler/jobs.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
    summary = latest_summary(workdir)
    assert summary["dsub_calls"] == 1  # Only the summary job is new after recovery.
    assert summary["state_verification"]["compared_terminal_reports"] == 3


def test_report_flags_false_terminal_and_missing_evidence(tmp_path):
    spec = importlib.util.spec_from_file_location("cluster_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "run.json").write_text(json.dumps({"config":{}}))
    (tmp_path / "snakemake.log").write_text(
        "Job a (ID: 1) finished successfully (State: FINISHED).\n"
        "Job b (ID: 2) failed (State: FAILED).\n"
        "Job c (ID: 3) finished successfully (State: FINISHED).\n")
    (tmp_path / "monitor.jsonl").write_text(json.dumps({"kind":"truth_summary","label":"final",
                                                      "states":{"1":"FAILED","2":"FINISHED"}}) + "\n")
    summary = module.summarize(tmp_path)
    assert summary["state_verification"]["false_success"] == 1
    assert summary["state_verification"]["false_failure"] == 1
    assert summary["state_verification"]["unverified_report_ids"] == ["3"]
    assert not summary["state_verification"]["minimum_500_satisfied"]


def test_cluster_driver_matrix_uses_isolated_directories(tmp_path, sidecar_binary):
    workdir = tmp_path / "matrix"
    result = invoke(["--queue","mock","--samples","1","--jobs","1","--matrix",
                     "--submit-concurrency","2","--sidecar-path",str(sidecar_binary),
                     "--stress-scale","0.001","--chaos","off","--monitor-seconds","0",
                     "--poll-seconds","1","--workdir",str(workdir)], tmp_path)
    assert result.returncode == 0, result.stdout[-12000:]
    comparison = json.loads((workdir / "comparison.json").read_text())
    assert [row["label"] for row in comparison] == ["serial","legacy","sidecar"]
    for label, concurrency in (("serial",1),("legacy",2),("sidecar",2)):
        summary = latest_summary(workdir / label)
        assert summary["run"]["config"]["submit_concurrency"] == concurrency
        assert summary["dsub_calls"] == 2
        assert summary["run"]["exitcode"] == 0
