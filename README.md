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

Since Snakemake is often used with Conda & Mamba, it is recommended to install the plugin via pip after activating your Snakemake environment:

```bash
git clone https://github.com/xsx123123/snakemake_executor_donau.git
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

Following the official Snakemake plugin conventions:

```text
snakemake_executor_donau/
├── pyproject.toml                     # Poetry configuration (deps & entry points)
├── README.md                          # Documentation (English)
├── docs/
│   └── README_zh.md                   # Documentation (Chinese)
└── snakemake_executor_plugin_donau/   # Core code directory (must follow strict naming)
    ├── __init__.py                    # Plugin entry point
    ├── executor.py                    # Core logic (submit/query/cancel)
    └── logging.py                     # Logging configuration
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

## ⚠️ Notes

*   **Queue Names**: Ensure the `queue` specified in your Snakefile actually exists in your cluster.
*   **Memory Units**: The plugin enforces `MB` as the unit when interacting with the scheduler.
*   **Shared Filesystem**: The default configuration assumes all compute nodes share a filesystem. If not, storage plugins need to be configured.
