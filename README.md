# Snakemake Executor Plugin for Huawei Donau Scheduler

<p align="center">
  <img src="docs/logo.png" alt="Snakemake Donau executor logo" width="320">
</p>

![Python](https://img.shields.io/badge/tested_Python-3.12-blue)
![Snakemake](https://img.shields.io/badge/snakemake-9.0%2B-green)
![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Validation](https://img.shields.io/badge/validation-local_mock_passed-yellow)

[中文文档 (Chinese Documentation)](docs/README_zh.md)

This is a Snakemake executor plugin designed specifically for the **Huawei Donau** High Performance Computing (HPC) scheduler. It enables Snakemake to interact directly with the Donau scheduling system, handling job submission, status monitoring, and resource management automatically.

## ✨ Key Features

- **Bounded concurrent submission**: Submit through Snakemake's batch `run_jobs` interface with up to 16 concurrent `dsub` processes by default (configurable from 1–64). Request construction and callbacks stay on the scheduler thread; CLI work runs in a bounded pool.
- **Optional Rust sidecar**: A persistent process handles submission, shared status polling, cancellation and diagnostics over a JSON Lines protocol. Use `auto`, `legacy` or `sidecar` mode without changing your Snakefile.
- **Crash recovery**: The sidecar records durable submission intents and accepted job IDs. Restarts reconcile existing jobs and reuse active IDs; uncertain submissions stop for reconciliation rather than being blindly submitted again. Legacy rollback can read the same journal.
- **Batch status queries**: Query the active queue, then history for missing IDs, with at most two normal status-query calls per round. Missing states remain active; recovery and failure diagnostics make separate calls.
- **Resource mapping**: Map CPU, memory, queue, runtime, GPU/NPU, nodes, account and MPI resources to Donau arguments.
- **Operational visibility**: Readable job names, executor logs, heartbeat messages, per-job logs and automatic `djob -ll` failure diagnostics.
- **Cluster validation tools**: Compare serial, concurrent Python and sidecar modes; collect CLI timings, resource usage and independent scheduler state; inject main-process crashes and test recovery.

Local mock tests cover submission, resource mapping, polling, cancellation, retries and crash recovery. **Real Donau cluster validation is pending.** See [benchmark results](baseline.md), the [validation report](验收报告.md) and the [cluster testing guide](Test/集群测试说明.md).

## 🛠️ Installation

Use a Python environment supported by your Snakemake 9 installation. Local validation used Python 3.12.3 and Snakemake 9.27.0. Load the cluster environment so that `dsub`, `djob` and `dkill` are available in `PATH`; workflow files and outputs must be accessible from compute nodes.

### Released Package

To install the published package (its version may differ from this checkout):

```bash
pip install snakemake-executor-plugin-donau
```

### Source Installation (Development)

Since Snakemake is often used with Conda & Mamba, it is recommended to install the plugin via pip after activating your Snakemake environment:

```bash
git clone https://github.com/xsx123123/snakemake_executor_donau.git
cd snakemake_executor_donau
pip install -e .
```

### Optional Rust Sidecar

The Python backend works without Rust. To build the sidecar from this checkout, install Rust 1.85+ and run:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
```

To build a wheel containing the executable:

```bash
python -m pip install poetry-core
python scripts/build_sidecar_wheel.py
```

The wheel targets the build host's platform/libc; build on a system compatible with your cluster. See the [build and deployment guide](docs/sidecar.md).

## 🚀 Quick Start

Replace `fat_x86` with an existing queue. `--jobs` limits in-flight workflow jobs; `--donau-submit-concurrency` limits simultaneous submissions.

```bash
# Concurrent Python backend
snakemake --executor donau --jobs 100 \
  --donau-default-queue fat_x86 \
  --donau-mode legacy --donau-submit-concurrency 16 \
  --latency-wait 60

# Rust sidecar backend
snakemake --executor donau --jobs 100 \
  --donau-default-queue fat_x86 \
  --donau-mode sidecar --donau-submit-concurrency 16 \
  --donau-sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --latency-wait 60
```

The default `auto` mode uses a discovered sidecar, otherwise the Python backend. Discovery checks an explicit path, package `bin/`, the module directory and `PATH`. Even explicit `sidecar` mode can fall back with a warning if startup fails; inspect logs to confirm the selected backend. `DONAU_MODE` overrides the CLI mode.

### Snakefile Example

Define resources in your `Snakefile`, and the plugin will automatically convert them to scheduler parameters:

```python
rule complex_task:
    input:
        "data/raw.txt"
    output:
        "results/final.txt"
    # 1. Set Job Priority (Maps to dsub -p)
    priority: 9999
    # 2. Set Resources
    resources:
        queue = "fat_node",       # -q fat_node
        mem_mb = 8192,            # -R mem=8192MB
        npu = 1,                  # -R npu=1 (AI/NPU queues)
        runtime = 120,            # -T 7200 (120 min -> seconds)
        nodes = 2,                # -N 2 (Replicas/Nodes)
        exclusive = True,         # -x (Exclusive mode)
        tag = "group=bio",        # --tag group=bio
        account = "proj_01",      # -A proj_01
        mpi = "openmpi",          # --mpi openmpi
        dsub_extra = "--env ALL"  # any extra dsub flags (escape hatch)
    threads: 8                    # -R cpu=8
    shell:
        "echo 'Running on Donau' > {output}"
```

## ⚙️ Resource Mapping Details

The plugin maps Snakemake resource definitions to `dsub` parameters as follows:

| Snakemake Keyword | Meaning | Donau Parameter | Notes |
| :--- | :--- | :--- | :--- |
| `threads` | CPU Cores | `-R cpu=<threads>` | Defaults to 1 |
| `priority` | Priority | `-p <int>` | Maps Snakemake priority (1-9999) |
| `resources.mem_mb` | Memory (MB) | `-R mem=<mem_mb>MB` | Defaults to 1024MB |
| `resources.queue` | Queue Name | `-q <queue>` | `partition` is also supported |
| `resources.npu` | NPU Cards | `-R ...,npu=<n>` | Combined with cpu/mem in one `-R` |
| `resources.gpu` | GPU Cards | `-R ...,gpu=<n>` | Combined with cpu/mem in one `-R` |
| `resources.runtime` | Runtime (min) | `-T <seconds>` | Converted to seconds. `time_min` is also supported |
| `resources.nodes` | Replicas/Nodes | `-N <count>` | `replica` is also supported |
| `resources.exclusive` | Exclusive | `-x job` | Set to `True` or `1` to enable |
| `resources.tag` | Custom Tag | `--tag <string>` | e.g. "key=value" |
| `resources.account` | Account | `-A <account>` | For billing/permissions |
| `resources.mpi` | MPI Type | `--mpi <type>` | e.g., `openmpi`, `intelmpi` |
| `resources.dsub_extra` | Extra dsub Args | parsed into argument tokens | Escape hatch for unmapped flags; overrides the plugin-level `submit_extra_args` setting |

## 🔧 Executor Settings

The following plugin-level options are available on the command line (each takes precedence over or complements the per-rule resources):

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--donau-mode` | `auto` | Backend: `auto`, `legacy` or `sidecar`. `DONAU_MODE` overrides this setting. |
| `--donau-sidecar-mode` | none | Compatibility alias for `--donau-mode`. |
| `--donau-sidecar-path` | auto-discovered | Path to the `donau-sidecar` executable. |
| `--donau-submit-concurrency` | `16` | Maximum concurrent submissions, 1–64. Use `1` with legacy mode for a serial baseline. |
| `--donau-recover-orphans` | `false` | Continue polling all recovered jobs. By default, only identities requested again by the workflow resume polling. Startup reconciliation runs in either case. |
| `--donau-any-arch` | `true` | Pass `-aa` together with `-q`; explicitly set `true` or `false` for your site. |
| `--donau-default-queue` | none | Default `-q` for jobs that do not define a `queue`/`partition` resource. |
| `--donau-heartbeat-interval` | `600` | Seconds between heartbeat log lines while polling. Set to `0` to disable. |
| `--donau-log-dir` | workdir | Directory for `donau_executor.log` and `donau_sidecar.journal`. Use a separate directory per independent workflow. |
| `--donau-submit-extra-args` | none | Extra arguments appended to every `dsub` command, e.g. `--donau-submit-extra-args "--env ALL"`. |

Example:

```bash
snakemake --executor donau --jobs 100 \
    --donau-default-queue x86 \
    --donau-heartbeat-interval 300
```

## 📝 Logging & Troubleshooting

### 1. Unified Terminal Output
The plugin automatically inherits Snakemake's global logger. If you use `--logger rich-loguru`, plugin logs (submission, success, etc.) will be rendered with the same high-quality formatting.

### 2. Local Debug Log (Workdir)
For detailed troubleshooting, the executor writes a persistent log to your working directory:
- **Path**: Created in the workflow's current working directory (respects Snakefile `workdir` or `--directory`), rotated at 50 MB with 7-day retention.
- **Content**: Precise timestamps, source code location (module:function:line), UUIDs, full shell commands (`dsub`), and raw scheduler responses.
- **Plain text**: No ANSI color codes, so you can `grep`/`awk` it directly. Meaningless empty `None` lines from Snakemake's logging thread are filtered out.

### 3. Heartbeat Lines
While jobs are running, the log would otherwise be silent (a successful poll produces no output). Every 10 minutes the executor prints one line like:

```
[heartbeat] 140 job(s) active, queried 140 state(s): RUNNING=132, PENDING=8
```

If this line keeps appearing, the polling loop is healthy and jobs are simply still running. If the log goes silent **and** heartbeats stop, the main process is likely stuck — capture a stack with `py-spy dump --pid <snakemake_pid>` to diagnose.

### 4. Failure Diagnostics
When a job reaches a failed state, the executor automatically runs `djob -ll <id>` and includes the result in the error report, e.g.:

```
Donau job 40587533 failed with state FAILED.
Donau job details (djob -ll):
  jobExitCode: 1
  systemExitCode: 10001
  exitMessage: Job command execution failed, errCode: 1
  totalMaxMem: 0
  jobWallclockDuration: 13
  execNodes: agent-X86-56
Hint: job command exited with code 1, check the job log for details
```

This usually saves you a manual `djob -ll` lookup. Typical hints: walltime limit → increase `resources.runtime`; memory limit (`errCode: 143`) → increase `resources.mem_mb`.

### 5. Job Standard Output (Per Rule)
The stdout and stderr of each specific job are redirected to:
- **Path**: `.snakemake/donau_logs/rule_<name>/<wildcards>/<jobid>.log`

## 🔧 Underlying Logic

This plugin relies on the following Donau commands (ensure they are available in `$PATH`):

1.  **Job Submission (`dsub`)**
    *   Uses `-n` to specify the job name (embeds rule, wildcards, job ID, and run tag; truncated to fit scheduler limits).
    *   Uses `-oo` to capture both stdout and stderr.
    *   Uses `--cwd` to lock the working directory.
    *   Includes automatic retry logic for network stability.

2.  **Status Query (`djob`)**
    *   Command: `djob -o "jobId state" --no-header <jobid...>` — a **single batch query** covering all active jobs (active queue), falling back to `djob -D -o "jobId state" --no-header <missing_ids...>` (completed/history database) for IDs absent from the active queue.
    *   Logic: One round of polling costs at most 2 `djob` invocations regardless of concurrency. Jobs missing from both queries stay active for the next poll and are never misreported as successful.

3.  **Job Cancellation (`dkill`)**
    *   Command: `dkill -y --force <id_list>`
    *   Logic: Uses `-y` to skip interactive confirmation and `--force` to ensure jobs are thoroughly cleaned up. Both the request and the result are logged.

## 🧪 Testing on a Donau Cluster

Install this checkout and build the sidecar as above. Start with a dry-run and a small sidecar run:

```bash
python Test/run_cluster_tests.py --queue fat_x86 --samples 100 --dry-run

python Test/run_cluster_tests.py --queue fat_x86 --samples 100 --jobs 20 \
  --mode sidecar --submit-concurrency 16 --chaos off \
  --sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --workdir Test/runs/sidecar_100
```

Each new run requires an empty work directory. The workflow retains the original memory, I/O and CPU stress: samples request 2048/4096 MB and allocate 1.5/3 GiB. `--chaos off` disables injected failures for performance comparisons; `--chaos once` provides reproducible retry checks. To check only the scheduling path, add `--stress-scale 0.001`; this reduces actual workload, but keeps resource requests unchanged.

After the small run, compare all three modes and require at least 500 independently verified terminal reports:

```bash
python Test/run_cluster_tests.py --queue fat_x86 --samples 1000 --jobs 100 \
  --matrix --submit-concurrency 16 --chaos off \
  --sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --monitor-seconds 60 --require-state-sample 500 \
  --workdir Test/runs/matrix_1000
```

The matrix runs serial legacy, concurrent legacy and sidecar in separate directories. Results include `control/<run>/summary.json`, CLI latency records, resource samples and independent scheduler truth; the matrix root contains `comparison.json`. CLI instrumentation adds overhead, so use identical settings across modes and optionally repeat with `--no-cli-metrics`.

For planned crashes, resume, legacy rollback, exit codes and evidence collection, follow the [full cluster testing guide](Test/集群测试说明.md). Keep the entire work directory, including `logs/`, journal and `control/`.

## 📊 Local Performance and Validation

These are **synthetic submission-stage measurements**, not end-to-end Donau workflow timings. Both concurrent modes used 16 submission workers:

| Mock scenario (1000 submissions) | Serial legacy | Concurrent legacy | Sidecar |
| :--- | ---: | ---: | ---: |
| SQLite CLI + 20 ms artificial delay | 74.682 s | 17.248 s | 19.967 s |
| SQLite CLI + 200 ms artificial delay | 259.114 s | 17.443 s | 18.575 s |

Results depend on CLI overhead and waiting time. The 20 ms scenario did **not** meet the target of reducing time to one fifth of serial; the 200 ms scenario did. Sidecar recovery and shared polling do not imply faster submission than the Python pool in every workload. Local state comparisons reported no false successes or failures; this does not establish real cluster correctness or throughput.

See [baseline.md](baseline.md) for raw-data links and methodology, and [验收报告.md](验收报告.md) for completed checks and remaining cluster acceptance work.

## 📂 Project Structure

```text
snakemake_executor_donau/
├── snakemake_executor_plugin_donau/
│   ├── __init__.py          # Entry point and executor settings
│   ├── executor.py          # Concurrent submission, query and cancellation
│   ├── sidecar_client.py    # Protocol client and process lifecycle
│   ├── recovery.py          # Journal-aware legacy recovery
│   └── logging.py
├── rust/                   # Rust sidecar, locked dependencies and tests
├── scripts/                # Binary wheel builder
├── tests/                  # Unit/integration tests and mock Donau CLI
├── Test/
│   ├── snakefile           # Configurable stress workflow
│   ├── run_cluster_tests.py
│   └── 集群测试说明.md
├── benchmarks/             # Synthetic benchmarks and recorded evidence
├── docs/sidecar.md          # Backend, journal, recovery and deployment guide
├── baseline.md
└── 验收报告.md
```

## 📦 Development & Building Guide

If you intend to develop your own Snakemake plugin or contribute to this project, please adhere to the following standards:

### 1. Naming Convention (Strict)
Snakemake's plugin discovery mechanism enforces strict naming:
*   **Code Directory**: Must be named `snakemake_executor_plugin_<name>` (e.g., `snakemake_executor_plugin_donau`).
*   **Project Name (PyPI)**: Recommended to be `snakemake-executor-plugin-<name>`.

### 2. Configuration (pyproject.toml)
This project uses the **Poetry** standard format, which is recommended by Snakemake. The key configuration is:

```toml
[tool.poetry.plugins."snakemake.executors"]
donau = "snakemake_executor_plugin_donau:Executor"
```
This line tells Snakemake: "When the user specifies `--executor donau`, load the `Executor` class from the `snakemake_executor_plugin_donau` module."

### 3. Local Development Flow
1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/xsx123123/snakemake_executor_donau.git
    cd snakemake_executor_donau
    ```
2.  **Install in Editable Mode**:
    Within your Snakemake environment, run:
    ```bash
    pip install -e .
    ```
    *Note: You do not need to install `poetry` explicitly; `pip` handles the build via `pyproject.toml`.*
3.  **Verify**:
    ```bash
    snakemake --help | grep donau
    ```
    If `donau` appears in the output, the plugin is successfully registered.

## ♻️ Recovery and Operational Notes

- **Graceful cancellation**: Ctrl+C follows Snakemake cancellation and calls `dkill`. A hard kill does not cancel remote jobs.
- **Resume with the journal**: Keep the same working directory and log directory. Confirm the old main process has stopped, unlock Snakemake if needed, then restart with `--rerun-incomplete` when appropriate. Active or unknown job identities are reused or reconciled; confirmed terminal jobs may start a new generation. Never delete the journal to bypass uncertain submissions.
- **Legacy rollback**: Use `DONAU_MODE=legacy snakemake ...` with the same directories. The fallback reads the existing journal before submitting. Legacy runs without a journal require manual checks for surviving `smk_*` jobs before restarting.
- **Journal ownership**: A file lock permits one sidecar/recovery writer per log directory. The journal synchronizes submission intents, accepted IDs and terminal states, with atomic checkpoints and seven-day archive retention. Do not share the directory across independent workflows.
- **Polling**: `--seconds-between-status-checks` controls Snakemake checks; the sidecar independently polls with a default five-second shared cache. Recovery and diagnostics are additional scheduler queries.
- **Site settings**: Use valid queues, choose runtime limits for the actual workload and verify shared filesystem access. Memory requests use MB. Custom `dsub_extra` arguments overriding job identity flags can prevent automatic reconciliation.

For exact restart steps, ambiguous submission handling and orphan tracking, see [sidecar configuration and recovery](docs/sidecar.md).
