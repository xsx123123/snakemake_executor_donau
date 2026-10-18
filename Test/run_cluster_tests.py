#!/usr/bin/env python3
"""Run the Donau stress workflow with isolated outputs and auditable metrics.

Requires this plugin and Snakemake installed in the current Python environment.
Never deletes results or journals. --crash-after-submissions only kills the
Snakemake process started by this invocation, leaving cluster jobs for recovery.
"""
import argparse
import fcntl
import importlib.metadata
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULTS = {"samples":100, "jobs":100, "mode":"legacy", "submit_concurrency":16,
            "stress_scale":1.0, "chaos":"random", "hold_seconds":0.0,
            "runtime_minutes":60, "poll_seconds":30, "monitor_seconds":60,
            "latency_wait":60, "heartbeat_seconds":60}
ENV_KEYS = {"queue":"DONAU_TEST_QUEUE", "samples":"SAMPLES", "stress_scale":"DONAU_TEST_STRESS_SCALE",
            "hold_seconds":"DONAU_TEST_HOLD_SECONDS", "runtime_minutes":"DONAU_TEST_RUNTIME_MINUTES"}


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def instrument_tools(control, tools):
    """Instrument every vendor CLI call; preserve argument lists and output streams."""
    directory = control / "bin"
    directory.mkdir()
    template = '''#!{python}
import fcntl, json, os, subprocess, sys, time
from pathlib import Path
TOOLS = json.loads({tools})
LOG = {log}
tool = Path(sys.argv[0]).name
started = time.time()
rc = 127
error = None
try:
    rc = subprocess.call([TOOLS[tool]] + sys.argv[1:])
except OSError as exc:
    error = str(exc)
finally:
    event = {{"tool":tool,"argv":sys.argv[1:],"started_epoch":started,
             "ended_epoch":time.time(),"returncode":rc,"error":error}}
    with open(LOG, "a") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(event) + "\\n")
        output.flush()
sys.exit(rc if rc >= 0 else 128 - rc)
'''
    for tool in tools:
        path = directory / tool
        path.write_text(template.format(python=sys.executable, tools=repr(json.dumps(tools)),
                                        log=repr(str(control / "cli.jsonl"))))
        path.chmod(0o755)
    return directory


def summarize(control):
    metadata = json.loads((control / "run.json").read_text())
    events = []
    source = control / "cli.jsonl"
    if source.exists():
        for line in source.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    dsub = [event for event in events if event["tool"] == "dsub"]
    latency = sorted(event["ended_epoch"] - event["started_epoch"] for event in dsub)
    def percentile(fraction):
        return latency[min(len(latency) - 1, max(0, int(len(latency) * fraction + .999999) - 1))] if latency else None
    status = []
    diagnostics = 0
    recovery = 0
    for event in sorted(events, key=lambda event:event["started_epoch"]):
        if event["tool"] != "djob":
            continue
        args = event["argv"]
        if "-ll" in args:
            diagnostics += 1
        elif "jobId name state" in args:
            recovery += 1
        elif "jobId state" in args:
            status.append(event)
    rounds = []
    for event in status:
        if "-D" not in event["argv"]:
            rounds.append([event])
        elif rounds:
            rounds[-1].append(event)
        else:
            rounds.append([event])
    text = (control / "snakemake.log").read_text(errors="replace") if (control / "snakemake.log").exists() else ""
    callback_ids = set(re.findall(r"submitted successfully \(ID: (\d+)\)", text))
    truth = {}
    snapshots = control / "monitor.jsonl"
    if snapshots.exists():
        for line in snapshots.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "truth_summary" and row.get("label") == "final":
                truth = row["states"]
    success_states = {"FINISHED", "SUCCEEDED", "DONE", "0"}
    failure_states = {"FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT", "CANCELED", "CANCELLED"}
    reported = dict((jid, disposition) for jid, disposition in re.findall(
        r"\(ID: (\d+)\) (finished successfully|failed) \(State:", text))
    ids = sorted(callback_ids | set(reported), key=int)
    compared = {jid:state for jid,state in truth.items() if jid in reported and state != "UNKNOWN"}
    verification = {"compared_terminal_reports":len(compared),
                    "false_success":sum(reported[jid] == "finished successfully" and state not in success_states for jid,state in compared.items()),
                    "false_failure":sum(reported[jid] == "failed" and state not in failure_states for jid,state in compared.items()),
                    "unverified_report_ids":[jid for jid in reported if jid not in compared],
                    "minimum_500_satisfied":len(compared) >= 500}
    summary = {"state_verification":verification,"run":metadata, "observed_job_ids":ids, "observed_submission_callbacks":len(callback_ids),
               "dsub_calls":len(dsub), "dsub_nonzero":sum(event["returncode"] != 0 for event in dsub),
               "dsub_vendor_latency_seconds":{"p50":statistics.median(latency) if latency else None,
                                              "p90":percentile(.9), "p99":percentile(.99),
                                              "max":max(latency) if latency else None},
               "submission_span_including_dag_wait_seconds":
                   max(event["ended_epoch"] for event in dsub) - min(event["started_epoch"] for event in dsub) if dsub else None,
               "status_djob_calls":len(status), "diagnostic_djob_calls":diagnostics,
               "recovery_inventory_calls":recovery,
               "observed_status_round_max_calls":max(map(len, rounds)) if rounds else None,
               "backend_fallback_logged": "using legacy" in text.lower() or "legacy fallback" in text.lower(),
               "notes":["CLI latency excludes Python instrumentation startup; disable instrumentation for an uninstrumented control.",
                        "Submission span includes DAG waits and dependency stages, not pure submission service time.",
                        "Round grouping is inferred from serialized active/history CLI calls. Inspect cli.jsonl for overlapping/failure/recovery cases.",
                        "Monitor snapshots are separate from executor CLI counts. This report does not independently prove zero state misclassifications."]}
    write_json(control / "summary.json", summary)
    return summary


def truth_snapshot(tools, wanted, control, label):
    states = {}
    with open(control / "monitor.jsonl", "a") as output:
        for extra in ([], ["-D"]):
            missing = [jid for jid in wanted if jid not in states]
            if not missing:
                break
            start = time.monotonic()
            try:
                result = subprocess.run([tools["djob"]] + extra + ["-o", "jobId state", "--no-header"] + missing,
                                        capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        fields = line.split()
                        if len(fields) >= 2 and fields[0].isdigit():
                            states[fields[0]] = fields[1].upper()
                output.write(json.dumps({"utc":utc(),"kind":"independent_truth", "label":label,
                                         "history":bool(extra),"duration_seconds":time.monotonic() - start,
                                         "returncode":result.returncode,"stdout":result.stdout,"stderr":result.stderr}) + "\n")
            except (OSError, subprocess.TimeoutExpired) as error:
                output.write(json.dumps({"utc":utc(),"kind":"monitor_error","label":label,"error":str(error)}) + "\n")
        output.write(json.dumps({"utc":utc(),"kind":"truth_summary","label":label,"states":states,
                                 "missing_ids":[jid for jid in wanted if jid not in states]}) + "\n")
    return states


def monitor(process, tools, control, ids, ids_lock, stop, interval):
    """Independent scheduler truth snapshots; optional and separately labelled."""
    observed = None
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            observed = psutil.Process(process.pid)
        except psutil.Error:
            pass
    next_query = time.monotonic()
    with open(control / "resources.jsonl", "a") as output:
        while not stop.is_set():
            sample = {"utc":utc(), "kind":"resources"}
            if observed:
                try:
                    cpu = observed.cpu_times()
                    sample.update(main_cpu_seconds=cpu.user + cpu.system, main_rss_bytes=observed.memory_info().rss)
                    sample["sidecars"] = [{"pid":child.pid,"rss_bytes":child.memory_info().rss,
                                           "cpu_seconds":sum(child.cpu_times()[:2])}
                                          for child in observed.children() if "donau-sidecar" in child.name()]
                except (OSError, psutil.Error):
                    pass
            output.write(json.dumps(sample) + "\n")
            output.flush()
            if interval and time.monotonic() >= next_query:
                with ids_lock:
                    wanted = sorted(ids)
                if wanted:
                    truth_snapshot(tools, wanted, control, "periodic")
                next_query = time.monotonic() + interval
            stop.wait(1)


def wait_for_journal(workdir, timeout=65):
    lock = workdir / "logs/donau_sidecar.lock"
    if not lock.exists():
        return
    started = time.monotonic()
    with open(lock, "a+b") as ownership:
        while True:
            try:
                fcntl.flock(ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(ownership, fcntl.LOCK_UN)
                return
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise ValueError("Old sidecar still owns the journal; wait for its drain before resuming")
                time.sleep(1)


def execute(config, workdir, args):
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = workdir / "cluster-test.json"
    if manifest.exists() and not args.resume:
        raise ValueError("Workdir already has a test run; use --resume or choose an empty directory")
    if not manifest.exists():
        if any(path.name not in ("cluster-test.json",) for path in workdir.iterdir()):
            raise ValueError("A new test requires an empty workdir")
        write_json(manifest, {"created_utc":utc(),"config":config})
    control = workdir / "control" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    control.mkdir(parents=True)
    tools = {tool:shutil.which(tool) for tool in ("dsub", "djob", "dkill")}
    if not args.dry_run and not all(tools.values()):
        raise ValueError("Load the Donau environment first; missing CLI: " + ", ".join(tool for tool,path in tools.items() if not path))
    env = dict(os.environ)
    for key, variable in ENV_KEYS.items():
        env[variable] = str(config[key])
    env["DONAU_MODE"] = config["mode"]
    env["DONAU_TEST_DETERMINISTIC_FAILURES"] = "1" if config["chaos"] == "once" else "0"
    env["DONAU_TEST_CHAOS_RATE"] = "0" if config["chaos"] == "off" else "0.1"
    # The selected Snakefile controls workload, not the login-node environment.
    if not args.no_cli_metrics and not args.dry_run:
        directory = instrument_tools(control, tools)
        env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
        env["DONAU_BIN_DIR"] = str(directory)
    else:
        # Do not inherit a previous mock/instrumentation directory into a real run.
        env.pop("DONAU_BIN_DIR", None)
    command = [sys.executable, "-m", "snakemake", "--snakefile", str(SCRIPT_DIR / "snakefile"),
               "--executor", "donau", "--jobs", str(config["jobs"]),
               "--donau-mode", config["mode"], "--donau-submit-concurrency", str(config["submit_concurrency"]),
               "--donau-default-queue", config["queue"], "--donau-log-dir", str(workdir / "logs"),
               "--seconds-between-status-checks", str(config["poll_seconds"]),
               "--donau-heartbeat-interval", str(config["heartbeat_seconds"]),
               "--latency-wait", str(config["latency_wait"])]
    if config.get("sidecar_path"):
        command += ["--donau-sidecar-path", config["sidecar_path"]]
    if args.dry_run:
        command.append("--dry-run")
    if args.resume:
        command.append("--rerun-incomplete")
    if args.resume:
        wait_for_journal(workdir)
    if args.unlock:
        unlocked = subprocess.run([sys.executable,"-m","snakemake","--snakefile",str(SCRIPT_DIR / "snakefile"),"--unlock"],
                                  cwd=workdir, env=env, text=True, capture_output=True)
        (control / "unlock.log").write_text(unlocked.stdout + unlocked.stderr)
        if unlocked.returncode:
            raise ValueError("Unlock failed; inspect " + str(control / "unlock.log"))
    versions = {}
    for package in ("snakemake", "snakemake-interface-executor-plugins", "snakemake-executor-plugin-donau"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    metadata = {"versions":versions,"config":config,"hostname":socket.gethostname(),"python":sys.version,"argv":command,
                "tools":tools,"dry_run":args.dry_run,"instrumented":not args.no_cli_metrics and not args.dry_run,
                "start_utc":utc(),"end_utc":None,"exitcode":None,"pid":None,
                "intentional_crash":False,"workdir":str(workdir)}
    write_json(control / "run.json", metadata)
    write_json(workdir / "latest-run.json", {"control":str(control)})
    started = time.monotonic()
    ids, ids_lock, stop = set(), threading.Lock(), threading.Event()
    process = subprocess.Popen(command, cwd=workdir, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    metadata["pid"] = process.pid
    write_json(control / "run.json", metadata)
    sampler = threading.Thread(target=monitor, args=(process,tools,control,ids,ids_lock,stop,
                                                    config["monitor_seconds"] if not args.dry_run else 0), daemon=True)
    sampler.start()
    interrupted = False
    with open(control / "snakemake.log", "w") as log:
        while True:
            try:
                line = process.stdout.readline()
                if not line:
                    break
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                submission = re.search(r"submitted successfully \(ID: (\d+)\)", line)
                terminal = re.search(r"\(ID: (\d+)\) (?:finished successfully|failed) \(State:", line)
                match = submission or terminal
                if match:
                    with ids_lock:
                        ids.add(match.group(1))
                    if submission and args.crash_after_submissions and len(ids) >= args.crash_after_submissions and not metadata["intentional_crash"]:
                        metadata["intentional_crash"] = True
                        write_json(control / "run.json", metadata)
                        process.kill()  # This invocation's Snakemake only; preserve Donau jobs and journal.
            except KeyboardInterrupt:
                interrupted = True
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
    returncode = process.wait()
    workflow_ended = utc()
    workflow_wall = time.monotonic() - started
    stop.set()
    # A snapshot may have an active and a historical query, each bounded by 30s.
    sampler.join(timeout=65)
    if config["monitor_seconds"] and not args.dry_run:
        truth_snapshot(tools, sorted(ids), control, "final")
    metadata.update(end_utc=workflow_ended, wall_seconds=workflow_wall, exitcode=returncode,
                    interrupted=interrupted)
    write_json(control / "run.json", metadata)
    summary = summarize(control)
    print("\nResults: {}\nSummary: {}".format(workdir, control / "summary.json"))
    if metadata["intentional_crash"]:
        print("Intentional crash complete. Resume the same workdir with --resume PATH --unlock.")
        return 3, summary
    verification = summary["state_verification"]
    gate = 0
    if verification["false_success"] or verification["false_failure"]:
        gate = 4
    if config["mode"] == "sidecar" and summary["backend_fallback_logged"]:
        gate = 4
    if args.require_state_sample and (verification["compared_terminal_reports"] < args.require_state_sample or verification["unverified_report_ids"]):
        gate = 4
    summary["validation_exitcode"] = gate
    write_json(control / "summary.json", summary)
    if gate:
        print("State verification gate failed or lacks required evidence; inspect summary and monitor logs.")
    return (returncode if returncode > 0 else (128 - returncode if returncode < 0 else gate)), summary


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--queue", help="Donau queue; required for a new run")
    for name in ("samples","jobs","submit_concurrency","runtime_minutes","poll_seconds","monitor_seconds","latency_wait","heartbeat_seconds"):
        result.add_argument("--"+name.replace("_","-"), type=int, help="Default: {}".format(DEFAULTS[name]))
    result.add_argument("--mode", choices=("legacy","sidecar","auto"))
    result.add_argument("--sidecar-path", type=lambda value:str(Path(value).expanduser().resolve()))
    result.add_argument("--stress-scale", type=float, help="Scale allocation/IO/CPU load only; default 1 (1.5/3 GiB allocation per job)")
    result.add_argument("--chaos", choices=("random","once","off"), help="random=10%%; once=first attempt of every tenth sample; off=performance control")
    result.add_argument("--hold-seconds", type=float, help="Delay each task for crash-recovery tests; default 0")
    result.add_argument("--workdir", type=Path)
    result.add_argument("--resume", type=Path, help="Reuse the original workdir, results and journal")
    result.add_argument("--unlock", action="store_true", help="Run Snakemake --unlock after confirming the old main process has stopped")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--matrix", action="store_true", help="Separate serial/legacyN/sidecarN runs with identical workload settings")
    result.add_argument("--no-cli-metrics", action="store_true", help="Disable timing shims for an uninstrumented control")
    result.add_argument("--crash-after-submissions", type=int, metavar="N", help="Kill this invocation's main process after N accepted jobs (sidecar only); exit 3 is expected")
    result.add_argument("--require-state-sample", type=int, default=0, metavar="N", help="Fail the validation gate unless N terminal reports match independent scheduler truth; use 500 for acceptance")
    result.add_argument("--report-only", type=Path, help="Refresh the latest summary without running jobs")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.report_only:
        latest = json.loads((args.report_only / "latest-run.json").read_text())
        summary = summarize(Path(latest["control"]))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.resume and (args.workdir or args.matrix):
        raise ValueError("--resume cannot be combined with --workdir or --matrix")
    config = dict(DEFAULTS)
    if args.resume:
        config.update(json.loads((args.resume / "cluster-test.json").read_text())["config"])
        latest = args.resume / "latest-run.json"
        if latest.exists():
            old = json.loads((Path(json.loads(latest.read_text())["control"]) / "run.json").read_text())
            if old.get("hostname") == socket.gethostname() and old.get("exitcode") is None and old.get("pid"):
                proc = Path("/proc") / str(old["pid"])
                try:
                    if proc.exists() and (proc / "cwd").resolve() == args.resume.resolve():
                        raise ValueError("The previous Snakemake process is still running; refusing resume/unlock")
                except OSError:
                    pass
    for key in list(DEFAULTS) + ["queue","sidecar_path"]:
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
    if not config.get("queue"):
        raise ValueError("--queue is required for a new test")
    for key in ("samples","jobs","runtime_minutes","poll_seconds","latency_wait"):
        if config[key] < 1:
            raise ValueError(key + " must be positive")
    if not 1 <= config["submit_concurrency"] <= 64 or config["stress_scale"] <= 0 or config["hold_seconds"] < 0 or config["monitor_seconds"] < 0 or config["heartbeat_seconds"] < 0:
        raise ValueError("Invalid concurrency, stress scale, hold or monitoring interval")
    if args.require_state_sample < 0 or (args.require_state_sample and config["monitor_seconds"] == 0):
        raise ValueError("State verification requires a nonnegative sample count and enabled monitoring")
    if args.crash_after_submissions is not None and (args.crash_after_submissions < 1 or config["mode"] != "sidecar" or args.matrix or args.dry_run):
        raise ValueError("Crash injection requires --mode sidecar and a positive threshold, without --matrix/--dry-run")
    if config["mode"] == "sidecar" or args.matrix:
        from snakemake_executor_plugin_donau.sidecar_client import find_sidecar
        binary = find_sidecar(config.get("sidecar_path"))
        if not binary or not os.access(binary, os.X_OK):
            raise ValueError("Build/install donau-sidecar or pass --sidecar-path before a sidecar test")
        config["sidecar_path"] = binary
    workdir = (args.resume or args.workdir or SCRIPT_DIR / "runs" / (datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+uuid.uuid4().hex[:6])).resolve()
    if args.matrix:
        workdir.mkdir(parents=True, exist_ok=True)
        comparison = []
        code = 0
        for label, mode, concurrency in (("serial","legacy",1),("legacy","legacy",config["submit_concurrency"]),("sidecar","sidecar",config["submit_concurrency"])):
            current = dict(config, mode=mode, submit_concurrency=concurrency)
            rc, summary = execute(current, workdir / label, args)
            comparison.append({"label":label,"exitcode":rc,"wall_seconds":summary["run"].get("wall_seconds"),
                               "dsub_vendor_latency_seconds":summary["dsub_vendor_latency_seconds"],
                               "backend_fallback_logged":summary["backend_fallback_logged"]})
            write_json(workdir / "comparison.json", comparison)
            code = code or rc
            if rc:
                break
        print("Comparison: " + str(workdir / "comparison.json"))
        return code
    return execute(config, workdir, args)[0]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print("Test setup failed: {}".format(error), file=sys.stderr)
        raise SystemExit(2)
