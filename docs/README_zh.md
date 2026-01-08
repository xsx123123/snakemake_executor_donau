# Snakemake Executor Plugin for Huawei Donau Scheduler
# 华为多瑙 (Donau) 调度器 Snakemake 执行插件

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Snakemake](https://img.shields.io/badge/snakemake-8.0%2B-green)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

这是一个专为 **华为多瑙 (Huawei Donau)** 高性能计算调度器设计的 Snakemake 执行器插件。它允许 Snakemake 直接与 Donau 调度系统交互，实现作业的自动投递、状态监控和资源管理。

## ✨ 核心特性

- **原生适配**: 基于 `dsub`, `djob`, `dkill` 命令深度开发。
- **智能资源映射**: 自动将 Snakemake 的 `threads`, `mem_mb`, `runtime` 转换为 Donau 的资源请求参数 (`-R cpu=X,mem=YMB`, `-T`).
- **健壮的状态检查**: 采用“活跃队列”+“历史队列”双重查询机制，防止因任务瞬间完成或调度器清理过快导致的状态误判。
- **详细审计日志**: 集成 `loguru`，提供全链路的调试日志（命令构建、原始输出、状态变更），方便运维排查。
- **安全取消**: 支持 Ctrl+C 触发批量、强制且非交互式的任务取消。

## 🛠️ 安装说明

确保您的环境已安装 Python 3.8+ 和 Snakemake 8.0+。

### 源码安装 (推荐)

由于该插件通常在特定集群环境使用，建议直接从源码安装：

```bash
git clone <repository-url> snakemake_executor_donau
cd snakemake_executor_donau
pip install -e .
```

## 🚀 快速开始

### 1. 基础运行

在安装完成后，使用 `--executor donau` 参数即可启用本插件：

```bash
snakemake --executor donau --jobs 100
```

### 2. Snakefile 示例

在 `Snakefile` 中定义资源需求，插件会自动将其转换为调度器参数：

```python
rule analysis:
    input:
        "data/raw.txt"
    output:
        "results/final.txt"
    resources:
        queue = "arm",        # 指定队列 (对应 dsub -q)
        mem_mb = 4096,        # 内存限制 (对应 -R mem=4096MB)
        runtime = 60          # 运行时间限制，单位分钟 (对应 dsub -T 3600)
    threads: 8                # CPU核数 (对应 -R cpu=8)
    shell:
        "echo 'Running on Donau' > {output}"
```

## ⚙️ 资源配置映射详解

插件会将 Snakemake 的资源定义映射为如下 `dsub` 参数：

| Snakemake 关键字 | 含义 | Donau 参数映射 | 说明 |
| :--- | :--- | :--- | :--- |
| `threads` | CPU 核心数 | `-R cpu=<threads>` | 默认为 1 |
| `resources.mem_mb` | 内存 (MB) | `-R mem=<mem_mb>MB` | 默认为 1024MB |
| `resources.queue` | 队列名称 | `-q <queue>` | 也支持 `partition` 关键字 |
| `resources.runtime` | 运行时间 (分钟) | `-T <seconds>` | 自动转换为秒。也支持 `time_min` |
| `resources.mem_mb_per_cpu` | 单核内存 | 自动计算总内存 | 转换为总内存后传给 `-R mem=...` |

### 实际生成的命令示例

如果规则定义如下：
```python
threads: 4
resources:
    mem_mb=8192,
    queue="fat_node",
    runtime=30
```

插件生成的提交命令将类似于：
```bash
dsub -n smk_rule_uuid -oo .snakemake/donau_logs/...
     --cwd /current/work/dir \
     -q fat_node \
     -R "cpu=4,mem=8192MB" \
     -T 1800 \
     ...
```

## 📝 日志与排错

### 1. 插件系统日志 (运维/调试用)
插件的所有调度行为（提交、查询结果、错误信息）都会记录在：
- **路径**: `.snakemake/donau_executor.log`
- **内容**: 包含详细的时间戳、UUID、执行的 Shell 命令及其标准输出。

### 2. 任务标准输出日志 (用户用)
每个具体任务的 stdout 和 stderr 会被重定向到：
- **路径**: `.snakemake/donau_logs/rule_<name>/<wildcards>/<jobid>.log`
- **用途**: 查看任务具体的运行报错或程序输出。

## 🔧 命令底层逻辑

本插件依赖以下 Donau 命令，请确保它们在 `$PATH` 中可用：

1.  **提交任务 (`dsub`)**
    *   使用 `-n` 指定任务名。
    *   使用 `-oo` 同时捕获标准输出和错误。
    *   使用 `--cwd` 锁定工作目录。

2.  **查询状态 (`djob`)**
    *   命令：`djob -o "jobid state" --no-header <id_list>`
    *   逻辑：优先查询活跃列表。如果 ID 不存在，自动追加 `-D` 参数查询已完成/历史作业数据库，确保状态判断准确无误。

3.  **取消任务 (`dkill`)**
    *   命令：`dkill -y --force <id_list>`
    *   逻辑：使用 `-y` 跳过交互确认，使用 `--force` 确保任务被彻底清理。

## 📂 项目结构

```text
snakemake_executor_donau/
├── pyproject.toml         # 项目依赖与元数据
├── README.md              # 说明文档
└── snakemake_executor_donau/
    ├── __init__.py        # 插件入口与配置
    ├── executor.py        # 核心逻辑 (提交/查询/取消)
    └── logging.py         # 日志模块配置
```

## ⚠️ 注意事项

*   **队列名称**: 请确保 Snakefile 中指定的 `queue` 在您的集群中真实存在。
*   **内存单位**: 插件强制使用 `MB` 作为单位与调度器交互。
*   **共享文件系统**: 默认配置假设所有计算节点共享文件系统。如果不是，请联系管理员配置存储插件。
