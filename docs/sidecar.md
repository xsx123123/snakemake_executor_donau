# 并发、sidecar 与恢复操作

## 构建和选择后端

```sh
cargo test --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path rust/Cargo.toml
snakemake -s Test/snakefile --executor donau --jobs 100 \
  --donau-submit-concurrency 16 --donau-mode sidecar \
  --donau-sidecar-path "$PWD/rust/target/release/donau-sidecar"
```

- `--donau-submit-concurrency`：1–64，插件默认 16。`1` 为串行对照。
- `--donau-mode auto|legacy|sidecar`：默认 auto。兼容别名 `--donau-sidecar-mode`。环境变量 `DONAU_MODE` 优先级最高。
- `--donau-sidecar-path`：显式路径，缺省探测包内 `bin/donau-sidecar`、模块同目录或 PATH。
- `--donau-recover-orphans true|false`：默认 false。启动总会对账并列出历史存量；false 时仅新工作流重新请求的身份恢复轮询，其他孤儿保留清单；true 时持续轮询所有存量至终态。不会把缺失状态判成功。
- `--donau-any-arch true|false`：默认 true，保持 `-aa` 语义。

Rust 独立运行的默认提交并发为 32、批量轮询间隔 5s、指标心跳 30s，支持 `--submit-concurrency`、`--poll-interval`、`--heartbeat-interval` 和 `--journal`。插件传入自己的并发配置。Snakemake 侧仍可用 `--seconds-between-status-checks` 调整检查节奏；sidecar 共享 5s 缓存与周期查询，正常单轮状态查询至多活跃/历史两次 `djob`，失败诊断另计。

默认 journal 位于 `--donau-log-dir`（缺省工作目录）的 `donau_sidecar.journal`。同一日志目录同一时刻仅允许一个代理/恢复实例，使用 advisory flock 排除重复写入。多个独立工作流使用各自工作目录/日志目录。日志格式与原有 executor 心跳保持，代理 stderr 以 `[sidecar]` 前缀进入调试日志。

## 崩溃后恢复

1. 保留工作目录、journal 和调度器中的旧作业。正常 Ctrl+C 仍调用 `dkill`；`kill -9` 不会撤销集群任务。
2. 若 Snakemake 提示旧锁，确认主进程已终止后按 Snakemake 流程执行 `snakemake -s <Snakefile> --unlock`。
3. 以相同工作目录、日志目录重启，必要时加 `--rerun-incomplete`。插件使用输出集合构造稳定身份，重启时的瞬态 jobid/attempt 变化不会绕过仍在写入的旧作业。没有输出的规则使用 rule/wildcards 身份。
4. 查看 `[recover]` 存量清单，使用 `djob -u "$USER" -o "jobId name state"` 核对。原作业仍在运行/状态缺失时复用 ID 并等待；只有确认终态后才允许新的运行或重试代次提交。

提交前先 fsync `submit_intent`；拿到 Donau ID 后 fsync `job_submitted`；终态 fsync，非终态每秒批量同步。首段50MiB、之后每追加50MiB事件做原子快照/归档，归档保留7天（快照大小另计，避免十万级活跃快照超过阈值时每条记录都触发重写），活跃/UNKNOWN 身份不会因过期删除。可忽略崩溃留下的非完整最后一行；完整坏行会拒绝恢复，避免静默丢失去重状态。

响应丢失时使用原作业名在活跃与历史库精确对账。唯一匹配才恢复；没有匹配或匹配不唯一时停止该提交并要求人工核对，绝不把查询缺失当作可重提依据。重试代次保留旧 ID 排除清单，避免同名历史记录混淆。额外参数若覆盖 `-n` 等身份参数，自动对账可能无法完成；参数透传行为保持，但需要人工核对。

协议连接故障最多重启 3 次。启动不可用会告警并降级；运行中故障耗尽重启后，legacy 路径先读取同一 journal、获得同一 flock、对账身份，再允许 CLI 提交。旧代理仍占有 journal 或无法确认提交结果时会停止相关提交，不绕过去重。轮询、诊断和取消可以直接回退 CLI。回滚：

```sh
DONAU_MODE=legacy snakemake -s <Snakefile> --executor donau --jobs 100
# 或 --donau-mode legacy
```

保留 journal，不要以删除 journal 作为处理 UNKNOWN 的办法。需要清理孤儿时，人工核对清单后针对明确的 ID 执行 `dkill -y --force <id...>`，再确认调度器终态。

## 协议与 mock

stdin/stdout JSON Lines，首条 hello proto=1；支持 submit/poll/cancel/diag/shutdown，stdout 仅协议，stderr 仅日志。所有请求增加 `request_id` 相关字段；`submit.id` 是稳定身份，`run_uuid` 与 `attempt` 表示代次。兼容设计字段形式的资源描述，插件使用 `argv` 扩展传递已规范化的参数向量，以确保边界情况与 legacy 完全一致。`recovered` 扩展消息输出存量清单；submitted 同时返回原 logfile，便于恢复后找到旧日志。未知/无效单行只丢弃/响应错误，不终止后续有效请求。

```sh
.venv/bin/pytest -q
# 真实 Snakemake -> 执行型 mock CLI，含100任务及 kill -9 / legacy回滚
.venv/bin/pytest -q tests/test_workflow.py
# 实际插件方法压测；仅提交/状态阶段，不执行真实工作流
.venv/bin/python benchmarks/matrix.py --counts 1000 10000
```

`DONAU_BIN_DIR=$PWD/tests/fixtures/cluster` 可替换 Rust 使用的 CLI；legacy 同时需要把该目录放在 PATH 前面。mock 的 `MOCK_DONAU_DIR` 为 SQLite 状态目录；支持 `-D`、`-ll`、批量状态、执行实际任务、失败次数、提交前/提交后延迟等故障注入。

## 打包

普通 Python wheel 可使用 PATH 中独立安装的代理，也可使用显式路径。若需把二进制随包分发：

```sh
.venv/bin/pip install poetry-core
.venv/bin/python scripts/build_sidecar_wheel.py
.venv/bin/pip install dist/snakemake_executor_plugin_donau-0.2.0-py3-none-linux_x86_64.whl
```

构建脚本编译 Rust release，把可执行文件放到包内 bin，并生成宿主平台标签及完整 RECORD。产物只适用于兼容本机构建平台/libc 的主机；没有声明 manylinux 兼容，也没有上传发布。源码包包含 Rust/Cargo.lock 与构建脚本，可在集群相应平台重建。Python 插件语法保持 3.8 兼容；实际运行 Snakemake 9 需要满足该版本自身的 Python 要求（本次使用 3.12）。

## 验收边界

本机没有真实 Donau 集群。mock 能验证协议、映射、恢复与故障路径，不能证明调度器吞吐上限、真实队列限流、节点负载、真实 CLI 格式/一致性、10k 实际工作流耗时或真实灰度通过。上线前按原优化文档 Phase 0/3 在目标集群采集数据并核对至少 500 个真实作业；详见根目录 baseline.md 和验收报告。

真实集群验收由用户执行；已提供 [测试入口与完整步骤](../Test/集群测试说明.md)，可自动汇总 CLI 延迟、真值、资源和恢复证据。
