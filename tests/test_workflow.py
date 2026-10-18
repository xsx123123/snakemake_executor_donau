"""True Snakemake -> plugin -> executable CLI shim -> job -> poll integration."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode,count", [("legacy", 10), ("auto", 10), ("sidecar", 100)])
def test_workflow(tmp_path, mode, count):
    env = dict(os.environ, DONAU_MODE=mode, SMOKE_SAMPLES=str(count), MOCK_DONAU_DIR=str(tmp_path / "scheduler"),
               PATH=str(ROOT / "tests/fixtures/cluster") + os.pathsep +
                    str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    binary = ROOT / "rust/target/debug/donau-sidecar"
    if mode == "sidecar":
        subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--quiet"],
                       check=True, timeout=180)
    sidecar_path = str(binary) if mode == "sidecar" else str(tmp_path / "missing-binary")
    result = subprocess.run(
        [sys.executable, "-m", "snakemake", "-s", str(ROOT / "tests/fixtures/smoke.smk"),
         "--executor", "donau", "--donau-sidecar-path", sidecar_path, "--jobs", "10", "--seconds-between-status-checks", "1",
         "--max-status-checks-per-second", "100", "--latency-wait", "5"],
        cwd=tmp_path, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120)
    (tmp_path / "workflow.log").write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-8000:]
    assert len(list((tmp_path / "results").glob("*.txt"))) == count
    with sqlite3.connect(str(tmp_path / "scheduler/jobs.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == count
        assert conn.execute("SELECT count(*) FROM jobs WHERE state='FINISHED'").fetchone()[0] == count


@pytest.mark.parametrize("restart_mode", ["sidecar", "legacy"])
def test_workflow_kill_and_restart_without_duplicate_writers(tmp_path, restart_mode):
    import time
    binary = ROOT / "rust/target/debug/donau-sidecar"
    subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--quiet"],
                   check=True, timeout=180)
    env = dict(os.environ, DONAU_MODE="sidecar", MOCK_DONAU_DIR=str(tmp_path / "scheduler"),
               PATH=str(ROOT / "tests/fixtures/cluster") + os.pathsep +
                    str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    cmd = [sys.executable, "-m", "snakemake", "-s", str(ROOT / "tests/fixtures/recovery.smk"),
           "--executor", "donau", "--donau-sidecar-path", str(binary), "--jobs", "2",
           "--seconds-between-status-checks", "1", "--max-status-checks-per-second", "100",
           "--latency-wait", "5", "--rerun-incomplete"]
    with open(tmp_path / "before-kill.log", "w") as log:
        process = subprocess.Popen(cmd, cwd=tmp_path, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                journal = tmp_path / "donau_sidecar.journal"
                if journal.exists() and journal.read_text().count('"event":"job_submitted"') == 2:
                    break
                assert process.poll() is None, (tmp_path / "before-kill.log").read_text()
                assert time.monotonic() < deadline
                time.sleep(.02)
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    unlock = subprocess.run([sys.executable, "-m", "snakemake", "-s", str(ROOT / "tests/fixtures/recovery.smk"),
                             "--unlock"], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20)
    assert unlock.returncode == 0, unlock.stderr
    env["DONAU_MODE"] = restart_mode
    result = subprocess.run(cmd, cwd=tmp_path, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=60)
    (tmp_path / "after-restart.log").write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-8000:]
    with sqlite3.connect(str(tmp_path / "scheduler/jobs.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM jobs WHERE state='FINISHED'").fetchone()[0] == 2
    assert (tmp_path / "results/a.txt").read_text().strip() == "a"
    assert (tmp_path / "results/b.txt").read_text().strip() == "b"
    assert "[recover]" in (tmp_path / "donau_executor.log").read_text()


def test_original_stress_workflow_with_scaled_load_and_retry(tmp_path):
    binary = ROOT / "rust/target/debug/donau-sidecar"
    subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--quiet"],
                   check=True, timeout=180)
    env = dict(os.environ, DONAU_MODE="sidecar", MOCK_DONAU_DIR=str(tmp_path / "scheduler"),
               SAMPLES="10", DONAU_TEST_STRESS_SCALE="0.001", DONAU_TEST_QUEUE="mock",
               DONAU_TEST_DETERMINISTIC_FAILURES="1",
               PATH=str(ROOT / "tests/fixtures/cluster") + os.pathsep +
                    str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [sys.executable, "-m", "snakemake", "-s", str(ROOT / "Test/snakefile"),
         "--executor", "donau", "--donau-sidecar-path", str(binary), "--jobs", "10",
         "--seconds-between-status-checks", "1", "--max-status-checks-per-second", "100",
         "--latency-wait", "5"],
        cwd=tmp_path, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    (tmp_path / "stress-workflow.log").write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-12000:]
    assert len(list((tmp_path / "results").glob("*.result"))) == 10
    assert "10 / 10" in (tmp_path / "results/final_stress_report.txt").read_text()
    with sqlite3.connect(str(tmp_path / "scheduler/jobs.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE state='FINISHED'").fetchone()[0] == 11
    # Rule-level retries execute inside the remote Snakemake process; the
    # scheduler correctly sees one FINISHED job after its local retry succeeds.
    assert (tmp_path / ".snakemake/mock_failures/sample_010").exists()
    logs = list((tmp_path / ".snakemake/donau_logs/rule_hardcore_stress/sample-sample_010").glob("*.log"))
    text = logs[0].read_text()
    assert "Random Chaos Failure" in text
    assert "Status: Survived" in (tmp_path / "results/sample_010.result").read_text()
