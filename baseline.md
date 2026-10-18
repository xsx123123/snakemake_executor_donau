# Donau 优化基线与 Phase 1 记录

测量日期：2026-10-18；环境：本机 Linux、Python 3.12.3、Snakemake 9.27.0、executor interface 9.4.0。未安装 Donau CLI，无真实队列、节点或调度器负载数据。以下全部是 synthetic/mock 实测，不能用于宣称真实集群验收通过。

## 复现

```sh
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/python benchmarks/submission.py > benchmarks/baseline.json
.venv/bin/python benchmarks/plugin_submission.py > benchmarks/phase1.json
.venv/bin/pytest -q
SAMPLES=1000 .venv/bin/snakemake --snakefile Test/snakefile --executor donau --jobs 100 --dry-run
```

mock dsub 每次 sleep 20ms，输出进程 PID 模拟 job ID，不运行工作流。`submission.py` 通过脚本 shebang 调用系统 PATH 中的 python3（本机包含 pyenv shim 开销）；`plugin_submission.py` 显式使用当前 Python，测量实际插件 `run_jobs` 路径。两组数据的绝对耗时不可交叉比较。JSON 保留完整精度。

## Phase 1 实际插件结果

| 提交数量 | 并发 | 提交耗时(s) | 吞吐(job/s) |
|---|---|---|---|
| 1000 | 1 | 36.9319 | 27.0769 |
| 1000 | 16 | 5.2813 | 189.3481 |

优化后为串行的 14.30%，约 6.99 倍加速。在此合成条件下满足 ≤1/5；不是 Donau 集群吞吐上限。该脚本只覆盖提交阶段，不是工作流端到端压测。

## 方案纠正与依据

安装的 `snakemake_interface_executor_plugins/executors/base.py` 的 `run_jobs` 默认逐项调用 `run_job`，并明确支持覆盖批量提交。因此仅在 `run_job` 中 `pool.submit(...).result()` 不会并发。插件覆盖 `run_jobs`，最多保留 N 个在途 future，命令构建及回调在调度线程执行；`run_job` 单独调用时仍在返回前拿到 external ID 并上报。未改变核心调度器。

实际 interface 9.x 的 `ExecutorSettingsBase` 使用 dataclass，原仓库 Pydantic Field 无法注册 CLI。已按实际接口修复。默认 true 的 bool 字段还触发当前 argparse-dataclass 的未加插件前缀 dest 问题，因此 any_arch 使用 Optional[bool] 配合严格 true/false 解析，保留文档参数名与默认资源行为。

原重试保持 3 次、间隔 2s/4s；资源命令顺序及映射保持；批量状态查询函数保持；失败诊断有界并发，超时后回收进程。线程池优雅关闭。

## 已验证与待验证

- Phase 1 首轮 9 项 pytest 回归通过，涵盖 500 条合成作业状态。不是与真实 djob 的 500 作业抽样。
- 1000 样本 Snakemake dry-run：1000 个压力作业、1 个汇总、1 个 all，参数解析成功。
- Python 3.8 AST 语法检查通过；未在 Python 3.8 运行 Snakemake 9（其运行版本需求须单独验证）。
- 真实 dsub P50/P90/P99、真实轮询间隔、py-spy 三次采样、节点背景负载、I/O 等待占比 >90% 均未验证。父进程 CPU 占比不能等同 I/O 等待占比，子进程 CPU 也须计入。
- Rust sidecar、恢复/去重、故障注入与本机二进制 wheel 已实现并有首轮验证；完整当前状态见 docs/26.10.19/实施进度.md 和验收报告.md。真实集群灰度尚未完成。

## 原方案同步线程池反例（已完成的基线）

以下来自 benchmarks/baseline.json，使用 PATH 中 pyenv shim 的 synthetic dsub；与上文显式 Python 的简单 CLI 数据、后续 SQLite mock 数据分开比较。

| 数量 | worker | 并发调用方 | 耗时(s) | 吞吐(job/s) | P50(ms) | P90(ms) | P99(ms) | max(ms) |
|---|---|---|---|---|---|---|---|---|
| 100 | 1 | False | 9.538 | 10.484 | 93.097 | 100.123 | 117.851 | 169.930 |
| 500 | 1 | False | 55.923 | 8.941 | 114.225 | 119.885 | 123.818 | 126.533 |
| 1000 | 1 | False | 112.461 | 8.892 | 108.349 | 117.675 | 382.811 | 448.694 |
| 1000 | 16 | False | 112.537 | 8.886 | 114.508 | 119.433 | 123.059 | 134.594 |
| 1000 | 16 | True | 25.984 | 38.486 | 414.756 | 459.159 | 500.551 | 547.602 |

串行调用 `submit().result()` 时 1 worker 与 16 worker 的 1000 次耗时分别为 112.461s 与 112.537s，实证没有并发收益。需要批量入口提供多个在途请求。该旧脚本未记录精确开始/结束 UTC；保留原始值，不补造时间窗口。

## SQLite mock 矩阵

`.venv/bin/python benchmarks/matrix.py --counts 1000 10000 --submit-delay 0.02`
输出 benchmarks/matrix.jsonl；环境与边界见 benchmarks/matrix-metadata.json。此场景同时包含 Python CLI 启动、SQLite 状态写入及 20ms 人工等待，更接近 CPU/调度器客户端开销占比较高的情况。1000 次串行提交 74.682s（父 CPU 0.990s、直接子进程 CPU 44.551s）；不能支持“I/O 等待 >90%”结论。16 并发 legacy 17.248s、sidecar 19.967s，在该场景未达到 ≤1/5，真实 Donau 上限未测。不以简单 mock 的 6.99 倍结果替代这个未通过项。

矩阵验证每组全部作业状态（≥1000），含至少 500 个多种成功/失败/活跃状态以及一个调度器缺失 ID。完整误判数、单独提交耗时、含诊断的检查时间与内存记录在 JSONL 中。`total_status_cli_calls_including_intervals` 是整个用例期间的总调用次数，不能当成每轮次数。单轮 ≤2 的直接计数另由批量轮询测试提供。

## 200ms 模型与最终本地复测

`benchmarks/matrix-200ms.jsonl`：

| 路径 | 1000次提交(s) | 相对串行加速 |
|---|---|---|
| 串行 | 259.114 | 1.00 |
| legacy16 | 17.443 | 14.85 |
| sidecar16 | 18.575 | 13.95 |

这组受控模拟等待为200ms，每组1000条状态全量核对无误判，满足本机mock的≤1/5。不是Donau吞吐上限或真实I/O占比证明。

最终journal滚动修复后的 `benchmarks/matrix-final-smoke.jsonl` 重测：

| 路径 | 1000次提交(s) | 假成功 | 假失败 |
|---|---|---|---|
| legacy16 | 17.793 | 0 | 0 |
| sidecar16 | 18.168 | 0 | 0 |

性能记录带执行时的生产代码/夹具hash与二进制hash。后续仅补充启动时同目录二进制发现和设置帮助文字，不影响这里直接调用CLI/协议的热路径。20ms数据属于更早构建的参考测量，继续保留其未通过G1的结论。数据中的约1e-14量级负CPU差值是浮点累计差分误差，不解释为负耗时。

真实集群验收由用户执行，入口及采样命令见 Test/集群测试说明.md；未测到的真实指标不记通过。
