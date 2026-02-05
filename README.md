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
- **Seamless Logging**: Integrated with Snakemake's global logging system. Automatically adapts to `--logger rich-loguru` for high-quality terminal output while maintaining detailed local debug logs.
- **Safe Cancellation**: Supports batch, forced, and non-interactive job cancellation via Ctrl+C.
- **Async Performance**: Utilizes `asyncio` for non-blocking status polling, suitable for large-scale workflows.

## 🛠️ Installation

Ensure you have Python 3.8+ and Snakemake 8.0+ installed.

### pip Installation (Preferred)

This is the easiest way to install the plugin directly from PyPI:

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

## 🚀 Quick Start

### 1. Basic Usage

Once installed, use the `--executor donau` argument to enable this plugin:

```bash
snakemake --executor donau --jobs 100
```

## 🧪 Testing

A test environment is provided in the `Test/` directory. You can verify the plugin's functionality using the following command:

```bash
# Run from the project root
snakemake --snakefile Test/snakefile --executor donau --jobs 10 --latency-wait 60
```

- `--jobs 10`: Limits the maximum number of concurrent jobs to 10.
- `--latency-wait 60`: Wait up to 60 seconds for output files to appear on the filesystem (recommended for HPC shared filesystems).

### 2. Snakefile Example

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
        runtime = 120,            # -T 7200 (120 min -> seconds)
        nodes = 2,                # -N 2 (Replicas/Nodes)
        exclusive = True,         # -x (Exclusive mode)
        tag = "group=bio",        # --tag group=bio
        account = "proj_01",      # -A proj_01
        mpi = "openmpi"           # --mpi openmpi
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
| `resources.runtime` | Runtime (min) | `-T <seconds>` | Converted to seconds. `time_min` is also supported |
| `resources.nodes` | Replicas/Nodes | `-N <count>` | `replica` is also supported |
| `resources.exclusive` | Exclusive | `-x job` | Set to `True` or `1` to enable |
| `resources.tag` | Custom Tag | `--tag <string>` | e.g. "key=value" |
| `resources.account` | Account | `-A <account>` | For billing/permissions |
| `resources.mpi` | MPI Type | `--mpi <type>` | e.g., `openmpi`, `intelmpi` |

## 📝 Logging & Troubleshooting

### 1. Unified Terminal Output
The plugin automatically inherits Snakemake's global logger. If you use `--logger rich-loguru`, plugin logs (submission, success, etc.) will be rendered with the same high-quality formatting.

### 2. Local Debug Log (Workdir)
For detailed troubleshooting, the executor writes a persistent log to your working directory:
- **Path**: `./donau_executor.log`
- **Content**: Detailed timestamps, UUIDs, full shell commands (`dsub`), and raw scheduler responses.

### 3. Job Standard Output (Per Rule)
The stdout and stderr of each specific job are redirected to:
- **Path**: `.snakemake/donau_logs/rule_<name>/<wildcards>/<jobid>.log`

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

*   **Runtime Configuration**: It is **not recommended** to set `runtime` or `time_min` in your `resources` unless strictly necessary. Setting a hard limit might cause the scheduler to kill long-running jobs prematurely, or if misconfigured, might affect Snakemake's status polling behavior (though the plugin handles timeouts gracefully). Let the scheduler determine the default walltime when possible.
*   **Queue Names**: Ensure the `queue` specified in your Snakefile actually exists in your cluster.
*   **Memory Units**: The plugin enforces `MB` as the unit when interacting with the scheduler.
*   **Shared Filesystem**: The default configuration assumes all compute nodes share a filesystem. If not, storage plugins need to be configured.

