# Snakemake Executor Plugin for Huawei Donau Scheduler

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Snakemake](https://img.shields.io/badge/snakemake-8.0%2B-green)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

[中文文档 (Chinese Documentation)](docs/README_zh.md)

This is a Snakemake executor plugin designed specifically for the **Huawei Donau** High Performance Computing (HPC) scheduler. It enables Snakemake to interact directly with the Donau scheduling system, handling job submission, status monitoring, and resource management automatically.

## ✨ Key Features

- **Native Adaptation**: deeply integrated with `dsub`, `djob`, and `dkill` commands.
- **Smart Resource Mapping**: Automatically translates Snakemake resources (`threads`, `mem_mb`, `runtime`, `account`, `mpi`) into Donau resource request parameters (e.g., `-R cpu=X,mem=YMB`, `-T`, `-A`, `--mpi`).
- **Robust Status Checking**: Implements a dual-query mechanism ("Active Queue" + "History Database") to prevent false status judgments caused by jobs finishing instantly or rapid scheduler cleanup.
- **Detailed Audit Logs**: Integrated with `loguru` to provide full-link debugging logs (command construction, raw output, status changes) for easy troubleshooting.
- **Safe Cancellation**: Supports batch, forced, and non-interactive job cancellation via Ctrl+C.
- **Async Performance**: Utilizes `asyncio` for non-blocking status polling, suitable for large-scale workflows.

## 🛠️ Installation

Ensure you have Python 3.8+ and Snakemake 8.0+ installed.

### Source Installation (Recommended)

Since this plugin is typically used in specific cluster environments, installing directly from the source is recommended:

```bash
git clone <repository-url> snakemake_executor_donau
cd snakemake_executor_donau
pip install -e .
```

## 🚀 Quick Start

### 1. Basic Usage

Once installed, use the `--executor donau` argument to enable this plugin:

```bash
snakemake --executor donau --jobs 100
```

### 2. Snakefile Example

Define resources in your `Snakefile`, and the plugin will automatically convert them to scheduler parameters:

```python
rule analysis:
    input:
        "data/raw.txt"
    output:
        "results/final.txt"
    resources:
        queue = "arm",        # Specify queue (maps to dsub -q)
        mem_mb = 4096,        # Memory limit (maps to -R mem=4096MB)
        runtime = 60,         # Runtime limit in minutes (maps to dsub -T 3600)
        account = "proj_01",  # Billing account (maps to dsub -A)
        mpi = "openmpi"       # MPI type (maps to dsub --mpi)
    threads: 8                # CPU cores (maps to -R cpu=8)
    shell:
        "echo 'Running on Donau' > {output}"
```

## ⚙️ Resource Mapping Details

The plugin maps Snakemake resource definitions to `dsub` parameters as follows:

| Snakemake Keyword | Meaning | Donau Parameter | Notes |
| :--- | :--- | :--- | :--- |
| `threads` | CPU Cores | `-R cpu=<threads>` | Defaults to 1 |
| `resources.mem_mb` | Memory (MB) | `-R mem=<mem_mb>MB` | Defaults to 1024MB |
| `resources.queue` | Queue Name | `-q <queue>` | `partition` is also supported |
| `resources.runtime` | Runtime (min) | `-T <seconds>` | Converted to seconds. `time_min` is also supported |
| `resources.account` | Account | `-A <account>` | For billing/permissions |
| `resources.mpi` | MPI Type | `--mpi <type>` | e.g., `openmpi`, `intelmpi` |

## 📝 Logging & Troubleshooting

### 1. Plugin System Log (For Ops/Debug)
All scheduling actions (submissions, query results, errors) are recorded in:
- **Path**: `.snakemake/donau_executor.log`
- **Content**: Detailed timestamps, UUIDs, executed shell commands, and their raw outputs.

### 2. Job Standard Output Log (For Users)
The stdout and stderr of each specific job are redirected to:
- **Path**: `.snakemake/donau_logs/rule_<name>/<wildcards>/<jobid>.log`
- **Usage**: To check specific job errors or program outputs.

## 🔧 Underlying Logic

This plugin relies on the following Donau commands (ensure they are available in `$PATH`):

1.  **Job Submission (`dsub`)**
    *   Uses `-n` to specify the job name.
    *   Uses `-oo` to capture both stdout and stderr.
    *   Uses `--cwd` to lock the working directory.
    *   Includes automatic retry logic for network stability.

2.  **Status Query (`djob`)**
    *   Command: `djob -o "jobid state" --no-header <id_list>`
    *   Logic: Prioritizes querying the active list. If an ID is missing, it automatically appends the `-D` flag to query the completed/history database, ensuring accurate status retrieval.

3.  **Job Cancellation (`dkill`)**
    *   Command: `dkill -y --force <id_list>`
    *   Logic: Uses `-y` to skip interactive confirmation and `--force` to ensure jobs are thoroughly cleaned up.

## 📂 Project Structure

```text
snakemake_executor_donau/
├── pyproject.toml                 # Dependencies and metadata
├── README.md                      # Documentation (English)
├── docs/
│   └── README_zh.md               # Documentation (Chinese)
└── snakemake_executor_donau/
    ├── __init__.py                # Plugin entry point
    ├── executor.py                # Core logic (submit/query/cancel)
    └── logging.py                 # Logging configuration
```

## ⚠️ Notes

*   **Queue Names**: Ensure the `queue` specified in your Snakefile actually exists in your cluster.
*   **Memory Units**: The plugin enforces `MB` as the unit when interacting with the scheduler.
*   **Shared Filesystem**: The default configuration assumes all compute nodes share a filesystem. If not, storage plugins need to be configured.
