# Snakemake Executor Plugin for Huawei Donau Scheduler
# 华为多瑙 (Donau) 调度器 Snakemake 执行插件

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Snakemake](https://img.shields.io/badge/snakemake-9.0%2B-green)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

这是一个专为 **华为多瑙 (Huawei Donau)** 高性能计算调度器设计的 Snakemake 执行器插件。它允许 Snakemake 直接与 Donau 调度系统交互，实现作业的自动投递、状态监控和资源管理。

## ✨ 核心特性

- **原生适配**: 基于 `dsub`, `djob`, `dkill` 命令深度开发。
- **智能资源映射**: 自动将 Snakemake 的 `threads`, `mem_mb`, `runtime`, `npu`/`gpu` 转换为 Donau 的资源请求参数 (`-R cpu=X,mem=YMB`, `-T`).
- **健壮的状态检查**: 采用“活跃队列”+“历史队列”双重查询机制，防止因任务瞬间完成或调度器清理过快导致的状态误判。无论有多少活跃作业，每轮轮询最多只需 2 次 `djob` 批量调用。
- **失败自动诊断**: 作业失败时自动执行 `djob -ll` 提取退出信息、退出码、峰值内存、运行时长，并附上可操作的处理建议（如“增大 mem_mb”）。
- **可区分的作业名**: 作业名内嵌规则名、wildcard、Snakemake jobid 和运行标识（如 `smk_call_variant_sample-chr1_1234_a1b2c3`），在 `djob` 列表中可直接定位具体作业。
- **心跳日志**: 轮询循环即使无状态变化也会每 10 分钟输出一行心跳，日志“沉默”时也能区分是作业正常运行还是主进程挂死。
- **详细审计日志**: 集成 `loguru`，提供全链路的调试日志（命令构建、原始输出、状态变更），文件内无 ANSI 颜色码，可直接 grep。
- **安全取消**: 支持 Ctrl+C 触发批量、强制且非交互式的任务取消，每次 `dkill` 都有日志记录。

## 🛠️ 安装说明

确保您的环境已安装 Python 3.8+ 和 Snakemake 9.0+。

### pip 安装 (首选)

这是最简单的安装方式，直接从 PyPI 下载：

```bash
pip install snakemake-executor-plugin-donau
```

### 源码安装 (开发用)

由于 Snakemake 往往会和 Conda & Mamba 搭配使用，因此推荐激活 Snakemake 环境后再使用 pip 进行安装：

```bash
git clone https://github.com/xsx123123/snakemake_executor_donau.git
cd snakemake_executor_donau
pip install -e .
```

## 🚀 快速开始

### 1. 基础运行

在安装完成后，使用 `--executor donau` 参数即可启用本插件：

```bash
snakemake --executor donau --jobs 100
```

## 🧪 测试说明

项目根目录下提供了 `Test/` 文件夹用于功能测试。您可以使用以下命令验证插件是否正常工作：

```bash
# 在项目根目录下运行
snakemake --snakefile Test/snakefile --executor donau --jobs 10 --latency-wait 60
```

- `--jobs 10`: 限制最大并行任务数为 10。
- `--latency-wait 60`: 等待最多 60 秒以确保输出文件在文件系统中同步（HPC 共享文件系统建议开启）。

### 2. Snakefile 示例

在 `Snakefile` 中定义资源需求，插件会自动将其转换为调度器参数：

```python
rule complex_task:
    input:
        "data/raw.txt"
    output:
        "results/final.txt"
    # 1. 设置优先级 (对应 dsub -p)
    priority: 9999
    # 2. 设置其他资源
    resources:
        queue = "fat_node",       # 队列 (-q)
        mem_mb = 8192,            # 内存 (-R mem=8192MB)
        npu = 1,                  # NPU 卡 (-R ...,npu=1，AI 队列)
        runtime = 120,            # 运行时间 (-T 7200秒)
        nodes = 2,                # 副本/节点数 (-N 2)
        exclusive = True,         # 独占模式 (-x job)
        tag = "group=bio",        # 自定义标签 (--tag)
        account = "proj_01",      # 账户 (-A)
        mpi = "openmpi",          # MPI 类型 (--mpi)
        dsub_extra = "--env ALL"  # 任意额外 dsub 参数（逃生口）
    threads: 8                    # CPU核数 (-R cpu=8)
    shell:
        "echo 'Running on Donau' > {output}"
```

## ⚙️ 资源配置映射详解

插件会将 Snakemake 的资源定义映射为如下 `dsub` 参数：

| Snakemake 关键字 | 含义 | Donau 参数映射 | 说明 |
| :--- | :--- | :--- | :--- |
| `threads` | CPU 核心数 | `-R cpu=<threads>` | 默认为 1 |
| `priority` | 优先级 | `-p <int>` | 映射 Snakemake 优先级 (1-9999) |
| `resources.mem_mb` | 内存 (MB) | `-R mem=<mem_mb>MB` | 默认为 1024MB |
| `resources.queue` | 队列名称 | `-q <queue>` | 也支持 `partition` 关键字 |
| `resources.npu` | NPU 卡数量 | `-R ...,npu=<n>` | 与 cpu/mem 合并为一个 `-R` |
| `resources.gpu` | GPU 卡数量 | `-R ...,gpu=<n>` | 与 cpu/mem 合并为一个 `-R` |
| `resources.runtime` | 运行时间 (分钟) | `-T <seconds>` | 自动转换为秒。也支持 `time_min` |
| `resources.nodes` | 副本/节点数 | `-N <count>` | 也支持 `replica` 关键字 |
| `resources.exclusive` | 独占模式 | `-x job` | 设置为 True 或 1 开启 |
| `resources.tag` | 自定义标签 | `--tag <string>` | 例如 "key=value" |
| `resources.account` | 账户 | `-A <account>` | 计费或权限用 |
| `resources.mpi` | MPI 类型 | `--mpi <type>` | 如 `openmpi`, `intelmpi` |
| `resources.dsub_extra` | 任意额外 dsub 参数 | 原样追加到命令 | 未覆盖参数的逃生口；优先于插件级 `submit_extra_args` 设置 |

## 🔧 执行器设置

以下插件级选项可通过命令行传入（与规则级 resources 互补，resources 优先）：

| 命令行参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--donau-any-arch` | `true` | 与 `-q` 一起传 `-aa`。多瑙集群必需，否则作业永远 PENDING。 |
| `--donau-default-queue` | 无 | 作业未定义 `queue`/`partition` 资源时使用的默认队列 `-q`。 |
| `--donau-heartbeat-interval` | `600` | 轮询时心跳日志的间隔秒数，设为 `0` 关闭。 |
| `--donau-log-dir` | 工作目录 | `donau_executor.log` 的写入目录。 |
| `--donau-submit-extra-args` | 无 | 追加到每个 `dsub` 命令的额外参数，如 `--donau-submit-extra-args "--env ALL"`。 |

示例：

```bash
snakemake --executor donau --jobs 100 \
    --donau-default-queue x86 \
    --donau-heartbeat-interval 300
```

## 📝 日志与排错

### 1. 执行器系统日志 (Workdir)
调度行为日志会直接生成在您的工作目录下：
- **路径**: 自动在 workflow 的当前工作目录下创建（尊重 Snakefile 的 `workdir` 或 `--directory` 参数，也可用 `--donau-log-dir` 指定），按 50 MB 轮转，保留 7 天。
- **内容**: 
  - **精准溯源**: 每条日志包含精确到毫秒的时间戳，以及输出该日志的 **代码模块:函数:行号**，极大方便调试。
  - **详细记录**: 记录 UUID、执行的完整 Shell 命令 (`dsub`) 以及调度器的原始响应。
  - **纯文本**: 文件内不含 ANSI 颜色码，可直接 `grep`/`awk`；Snakemake 日志线程产生的无意义 `None` 空行已被过滤。

### 2. 心跳日志（区分“运行中”与“挂死”）
作业运行期间，成功的轮询不会产生任何日志。执行器每 10 分钟（可用 `--donau-heartbeat-interval` 调整）输出一行心跳：

```
[heartbeat] 140 job(s) active, queried 140 state(s): RUNNING=132, PENDING=8
```

只要心跳持续出现，说明轮询循环健康、作业仍在正常跑；如果日志长时间无输出**且心跳也停了**，主进程很可能挂死，可用 `py-spy dump --pid <snakemake_pid>` 抓栈诊断。

### 3. 失败自动诊断
作业进入失败状态时，执行器自动执行 `djob -ll <id>` 并把关键信息附加到报错中：

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

通常可以省掉手动 `djob -ll` 排查。常见提示：超时 → 调大 `resources.runtime`；内存超限（`errCode: 143`）→ 调大 `resources.mem_mb`。

### 4. 任务标准输出日志 (Per Rule)
每个具体任务的 stdout 和 stderr 会被重定向到：
- **路径**: `.snakemake/donau_logs/rule_<name>/<wildcards>/<jobid>.log`
- **用途**: 查看任务具体的运行报错或程序输出。

## 🔧 命令底层逻辑

本插件依赖以下 Donau 命令，请确保它们在 `$PATH` 中可用：

1.  **提交任务 (`dsub`)**
    *   使用 `-n` 指定任务名（内嵌规则名、wildcard、jobid 和运行标识，超长时自动裁减以适配调度器限制）。
    *   使用 `-oo` 同时捕获标准输出和错误。
    *   使用 `--cwd` 锁定工作目录。

2.  **查询状态 (`djob`)**
    *   命令：`djob -o "jobId state" --no-header <jobid...>`（活跃队列，**单次批量查询全部活跃作业**）；活跃队列查不到的 ID 再批量回退 `djob -D -o "jobId state" --no-header <id...>`（已完成/历史数据库）。
    *   逻辑：无论并发多少作业，一轮轮询最多 2 次 `djob` 调用。两轮都查不到时保持作业活跃等待下轮轮询，绝不误判为成功。

3.  **取消任务 (`dkill`)**
    *   命令：`dkill -y --force <id_list>`
    *   逻辑：使用 `-y` 跳过交互确认，使用 `--force` 确保任务被彻底清理。取消请求和结果都会写入日志。

## 📂 项目结构

遵循 Snakemake 官方插件规范：

```text
snakemake_executor_donau/
├── pyproject.toml                     # Poetry 配置文件 (定义依赖与插件入口)
├── README.md                          # 说明文档 (英文)
├── docs/
│   └── README_zh.md                   # 说明文档 (中文)
└── snakemake_executor_plugin_donau/   # 核心代码目录 (必须遵循命名规范)
    ├── __init__.py                    # 插件入口与配置
    ├── executor.py                    # 核心逻辑 (提交/查询/取消)
    └── logging.py                     # 日志模块配置
```

## 📦 开发与构建指南

如果您想开发自己的 Snakemake 插件或对本项目进行二次开发，请务必遵循以下规范：

### 1. 命名规范 (Strict Naming Convention)
Snakemake 的插件发现机制对命名有严格要求：
*   **代码目录名**: 必须命名为 `snakemake_executor_plugin_<name>` (例如: `snakemake_executor_plugin_donau`)。
*   **项目名称 (PyPI)**: 建议使用 `snakemake-executor-plugin-<name>`。

### 2. 配置文件 (pyproject.toml)
本项目采用 **Poetry** 标准格式，这是 Snakemake 官方推荐的方式。关键配置如下：

```toml
[tool.poetry.plugins."snakemake.executors"]
donau = "snakemake_executor_plugin_donau:Executor"
```
这行配置告诉 Snakemake：当用户使用 `--executor donau` 时，去加载 `snakemake_executor_plugin_donau` 模块中的 `Executor` 类。

### 3. 本地开发流程
1.  **克隆代码**:
    ```bash
    git clone https://github.com/xsx123123/snakemake_executor_donau.git
    cd snakemake_executor_donau
    ```
2.  **安装 (Editable Mode)**:
    在您的 Snakemake 环境中运行：
    ```bash
    pip install -e .
    ```
    *注意：无需手动安装 poetry 命令，pip 会自动识别 pyproject.toml 并构建。*
3.  **验证**:
    ```bash
    snakemake --help | grep donau
    ```
    如果输出包含 `donau`，说明插件已成功注册。

## ⚠️ 注意事项

*   **中断与重启**: Snakemake 被 Ctrl+C 终止时会自动通过 `dkill` 取消所有已提交的 Donau 作业（日志中可见 `Cancelling N Donau job(s)` 记录）。但如果主进程被 `kill -9` 杀死或终端直接关闭，**作业不会被取消**，将成为孤儿作业。重启相同工作流前，请先确认旧的 `smk_*` 作业已全部结束：
    ```bash
    djob -u $USER -o "jobId name state" | grep smk_
    ```
    旧运行的残留作业与新运行的作业可能会同时写同一批输出文件。
*   **大规模运行调优**: 数百并发作业的工作流建议调大 Snakemake 的轮询间隔，进一步降低调度器压力（插件本身已把每轮状态查询合并为至多 2 次 `djob` 调用）：
    ```bash
    snakemake --executor donau --jobs 500 --seconds-between-status-checks 30
    ```
*   **运行时间配置 (Runtime)**: **不建议**在 `resources` 中设置 `runtime` 或 `time_min`，除非确有必要。设置硬性的时间限制可能会导致长任务被调度器提前终止。此外，不当的时间配置可能会影响 Snakemake 的状态轮询行为。建议尽量让调度器使用默认时长。
*   **队列名称**: 请确保 Snakefile 中指定的 `queue` 在您的集群中真实存在。
*   **内存单位**: 插件强制使用 `MB` 作为单位与调度器交互。
*   **共享文件系统**: 默认配置假设所有计算节点共享文件系统。如果不是，请联系管理员配置存储插件。
