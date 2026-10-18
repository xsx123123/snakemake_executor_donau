# 华为多瑙 (Donau) 调度器 Snakemake 执行插件

<p align="center">
  <img src="logo.png" alt="Snakemake 多瑙执行器 Logo" width="320">
</p>

![Python](https://img.shields.io/badge/tested_Python-3.12-blue)
![Snakemake](https://img.shields.io/badge/snakemake-9.0%2B-green)
![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Validation](https://img.shields.io/badge/validation-local_mock_passed-yellow)

[English Documentation](../README.md)

这是一个专为 **华为多瑙 (Huawei Donau)** 高性能计算调度器设计的 Snakemake 执行器插件。它允许 Snakemake 直接与 Donau 调度系统交互，实现作业的自动投递、状态监控和资源管理。

## ✨ 核心特性

- **有界并发提交**：通过 Snakemake 的 `run_jobs` 批量接口提交作业，默认最多同时运行 16 个 `dsub`，可配置为 1–64。请求构建和回调留在调度线程，CLI 调用在线程池中执行。
- **可选 Rust sidecar**：常驻进程通过 JSON Lines 协议处理提交、共享状态轮询、取消和诊断。通过 `auto`、`legacy`、`sidecar` 模式切换，无需修改 Snakefile。
- **崩溃恢复**：持久记录提交意图和已接收的作业 ID。重启时对账并复用仍活跃的 ID；无法确认的提交停止等待核对，避免盲目重复提交。legacy 回滚路径可读取同一 journal。
- **批量状态查询**：先查询活跃队列，再查询缺失 ID 的历史状态，每轮常规状态查询最多两次 `djob`。查不到状态时继续等待；恢复对账和失败诊断另计。
- **资源映射**：将 CPU、内存、队列、运行时间、GPU/NPU、节点、账户和 MPI 配置转换为 Donau 参数。
- **运行可观测性**：提供可区分的作业名、执行器日志、心跳、作业日志及自动 `djob -ll` 失败诊断。
- **集群验证工具**：对比串行、Python 并发和 sidecar，采集 CLI 延迟、资源用量和独立调度器状态，支持主进程故障注入与恢复测试。

本地 mock 测试覆盖提交、资源映射、轮询、取消、重试和崩溃恢复。**真实多瑙集群验证尚未完成。** 详见[性能基线](../baseline.md)、[验收报告](../验收报告.md)和[集群测试说明](../Test/集群测试说明.md)。

## 🛠️ 安装说明

请使用满足所安装 Snakemake 9 版本要求的 Python 环境。本地验证使用 Python 3.12.3 和 Snakemake 9.27.0。加载集群环境，确保 `dsub`、`djob`、`dkill` 在 `PATH` 中，且计算节点能访问工作流文件和输出目录。

### 安装已发布版本

从 PyPI 安装的版本可能与当前源码不同：

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

### 可选 Rust sidecar

Python 后端无需 Rust。从源码构建 sidecar 时，需要 Rust 1.85+：

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
```

构建携带二进制的 wheel：

```bash
python -m pip install poetry-core
python scripts/build_sidecar_wheel.py
```

wheel 适用于构建主机对应的平台和 libc，请在兼容目标集群的环境中构建。详见[构建与部署说明](sidecar.md)。

## 🚀 快速开始

以下命令在项目根目录运行，将 `fat_x86` 替换为真实队列。`--jobs` 控制在途作业上限，`--donau-submit-concurrency` 控制同时提交的数量。

```bash
# Python 并发后端
snakemake --executor donau --jobs 100 \
  --donau-default-queue fat_x86 \
  --donau-mode legacy --donau-submit-concurrency 16 \
  --latency-wait 60

# Rust sidecar 后端
snakemake --executor donau --jobs 100 \
  --donau-default-queue fat_x86 \
  --donau-mode sidecar --donau-submit-concurrency 16 \
  --donau-sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --latency-wait 60
```

默认 `auto` 模式使用探测到的 sidecar，否则使用 Python 后端。依次检查显式路径、包内 `bin/`、模块同目录和 `PATH`。即使显式指定 `sidecar`，启动失败也可能告警降级，请通过日志确认实际后端。环境变量 `DONAU_MODE` 优先于命令行模式。

### Snakefile 示例

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
| `resources.dsub_extra` | 任意额外 dsub 参数 | 解析为参数列表后追加 | 未覆盖参数的扩展入口；优先于插件级 `submit_extra_args` 设置 |

## 🔧 执行器设置

以下插件级选项可通过命令行传入（与规则级 resources 互补，resources 优先）：

| 命令行参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--donau-mode` | `auto` | 后端：`auto`、`legacy` 或 `sidecar`。`DONAU_MODE` 优先。 |
| `--donau-sidecar-mode` | 无 | `--donau-mode` 的兼容别名。 |
| `--donau-sidecar-path` | 自动探测 | `donau-sidecar` 可执行文件路径。 |
| `--donau-submit-concurrency` | `16` | 同时提交数量，范围 1–64。legacy 模式设为 `1` 可作为串行基线。 |
| `--donau-recover-orphans` | `false` | 是否持续轮询全部恢复作业。默认仅恢复当前工作流重新请求的身份；两种配置启动时都会对账。 |
| `--donau-any-arch` | `true` | 与 `-q` 一起传 `-aa`，可按站点要求显式设置 `true` 或 `false`。 |
| `--donau-default-queue` | 无 | 作业未定义 `queue`/`partition` 资源时使用的默认队列 `-q`。 |
| `--donau-heartbeat-interval` | `600` | 轮询时心跳日志的间隔秒数，设为 `0` 关闭。 |
| `--donau-log-dir` | 工作目录 | `donau_executor.log` 和 `donau_sidecar.journal` 的目录；独立工作流应使用各自目录。 |
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

## 🧪 多瑙集群测试

按上文安装当前源码并构建 sidecar。在项目根目录先 dry-run，再做小规模验证：

```bash
python Test/run_cluster_tests.py --queue fat_x86 --samples 100 --dry-run

python Test/run_cluster_tests.py --queue fat_x86 --samples 100 --jobs 20 \
  --mode sidecar --submit-concurrency 16 --chaos off \
  --sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --workdir Test/runs/sidecar_100
```

每次新测试需要空工作目录。工作流保留原有内存、I/O 和 CPU 压力：样本申请 2048/4096 MB，实际分配 1.5/3 GiB。`--chaos off` 关闭故障注入，适合性能对照；`--chaos once` 提供可复现的重试检查。仅验证调度链路时，可加 `--stress-scale 0.001`，缩小实际工作负载，但不缩小申请的资源。

小规模通过后，运行三组对照，并要求至少 500 条终态报告完成独立比对：

```bash
python Test/run_cluster_tests.py --queue fat_x86 --samples 1000 --jobs 100 \
  --matrix --submit-concurrency 16 --chaos off \
  --sidecar-path "$PWD/rust/target/release/donau-sidecar" \
  --monitor-seconds 60 --require-state-sample 500 \
  --workdir Test/runs/matrix_1000
```

矩阵分别在独立目录运行 legacy 串行、legacy 并发和 sidecar。结果包含 `control/<run>/summary.json`、CLI 延迟记录、资源采样和独立调度器状态，矩阵根目录生成 `comparison.json`。CLI 采集包装会增加开销，各组应保持相同配置，也可增加一次 `--no-cli-metrics` 对照。

计划性崩溃、恢复、legacy 回滚、退出码及证据采集步骤见[完整集群测试说明](../Test/集群测试说明.md)。请保留整个工作目录，包括 `logs/`、journal 和 `control/`。

## 📊 本地性能与验证范围

以下是**合成环境的提交阶段测量**，不代表真实 Donau 工作流端到端耗时。两种并发后端均使用 16 个提交并发：

| Mock 场景（1000 次提交） | legacy 串行 | legacy 并发 | sidecar |
| :--- | ---: | ---: | ---: |
| SQLite CLI + 20 ms 人工延迟 | 74.682 s | 17.248 s | 19.967 s |
| SQLite CLI + 200 ms 人工延迟 | 259.114 s | 17.443 s | 18.575 s |

性能取决于 CLI 开销和等待时间。20 ms 场景**未达到**耗时降至串行五分之一的目标，200 ms 场景达到。sidecar 的恢复与共享轮询能力不意味着它在所有负载下都比 Python 线程池提交更快。本地状态比对没有发现假成功或假失败，但不能据此确认真实集群的正确性与吞吐。

原始数据和测量方法见[性能基线](../baseline.md)，已完成检查及待完成集群验收见[验收报告](../验收报告.md)。

## 📂 项目结构

```text
snakemake_executor_donau/
├── snakemake_executor_plugin_donau/
│   ├── __init__.py          # 插件入口与执行器配置
│   ├── executor.py          # 并发提交、查询和取消
│   ├── sidecar_client.py    # 协议客户端与进程生命周期
│   ├── recovery.py          # 基于 journal 的 legacy 恢复
│   └── logging.py
├── rust/                   # Rust sidecar、锁定依赖和测试
├── scripts/                # 二进制 wheel 构建脚本
├── tests/                  # 单元/集成测试与 mock Donau CLI
├── Test/
│   ├── snakefile           # 可配置压力工作流
│   ├── run_cluster_tests.py
│   └── 集群测试说明.md
├── benchmarks/             # 合成基准和测量记录
├── docs/sidecar.md          # 后端、journal、恢复和部署说明
├── baseline.md
└── 验收报告.md
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

## ♻️ 恢复与运行注意事项

- **正常取消**：Ctrl+C 按 Snakemake 的取消流程执行 `dkill`。强制杀死主进程不会取消远端作业。
- **保留 journal 恢复**：使用相同的工作目录和日志目录。确认旧主进程停止，必要时解除 Snakemake 锁，并按需添加 `--rerun-incomplete` 重启。活跃或状态不明的身份会复用或对账；确认终态后可开始新的运行代次。不要删除 journal 来绕过不确定的提交。
- **legacy 回滚**：在相同目录下使用 `DONAU_MODE=legacy snakemake ...`，回退路径会先读取已有 journal。没有 journal 的 legacy 运行重启前，需要人工确认残留的 `smk_*` 作业。
- **journal 独占**：每个日志目录通过文件锁限制为一个 sidecar/恢复写入实例。journal 同步记录提交意图、已接收 ID 和终态，采用原子检查点及七天归档保留。独立工作流不要共享日志目录。
- **轮询节奏**：`--seconds-between-status-checks` 控制 Snakemake 检查间隔；sidecar 独立使用默认五秒的共享缓存和周期轮询。恢复对账和失败诊断会产生额外查询。
- **站点配置**：使用有效队列，按实际工作负载选择运行时间限制，并确认共享文件系统可用。内存请求单位为 MB。自定义 `dsub_extra` 若覆盖作业身份参数，可能导致自动对账失败。

完整重启步骤、不确定提交处理和孤儿作业跟踪见[sidecar 配置与恢复说明](sidecar.md)。
