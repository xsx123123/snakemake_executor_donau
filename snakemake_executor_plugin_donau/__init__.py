"""
Snakemake Executor Plugin for Huawei Donau Scheduler
Author: Jian Zhang (Adapted by Hajimi)
"""

from typing import Optional

from dataclasses import dataclass, field
from snakemake_interface_executor_plugins.settings import CommonSettings, ExecutorSettingsBase
from .executor import Executor

def _parse_bool(value):
    if isinstance(value, bool):
        return value
    lowered = value.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError("expected true or false")


# 插件配置类
@dataclass
class ExecutorSettings(ExecutorSettingsBase):
    mode: str = field(
        default="auto",
        metadata={"help": "Execution backend: auto, legacy or sidecar (DONAU_MODE overrides).",
                  "choices": ["auto", "legacy", "sidecar"]},
    )
    sidecar_mode: Optional[str] = field(
        default=None,
        metadata={"help": "Alias for --donau-mode.", "choices": ["auto", "legacy", "sidecar"]},
    )
    sidecar_path: Optional[str] = field(
        default=None,
        metadata={"help": "Path to donau-sidecar; defaults to package bin/, module directory or PATH."},
    )
    recover_orphans: Optional[bool] = field(
        default=False,
        metadata={"help": "Continue tracking recovered jobs (true/false). Never blindly resubmit UNKNOWN jobs.",
                  "type": _parse_bool},
    )
    submit_concurrency: int = field(
        default=16,
        metadata={"help": "Maximum concurrent dsub submissions (1-64, default 16)."},
    )
    any_arch: Optional[bool] = field(
        default=True,
        metadata={
            "type": _parse_bool,
            "help": "Pass -aa (any-arch) to dsub together with -q. "
                    "Required by Donau clusters, otherwise jobs stay PENDING forever."
        },
    )
    heartbeat_interval: int = field(
        default=600,
        metadata={
            "help": "Seconds between heartbeat log lines while polling job states. "
                    "Heartbeats distinguish 'jobs still running' from 'a stuck main "
                    "process' in donau_executor.log. Set to 0 to disable."
        },
    )
    default_queue: Optional[str] = field(
        default=None,
        metadata={
            "help": "Default queue (-q) for jobs that do not define a 'queue' or "
                    "'partition' resource. On Donau clusters -q still has to be "
                    "combined with -aa (see any_arch)."
        },
    )
    log_dir: Optional[str] = field(
        default=None,
        metadata={
            "help": "Directory for donau_executor.log. Defaults to the workflow "
                    "working directory."
        },
    )
    submit_extra_args: Optional[str] = field(
        default=None,
        metadata={
            "help": "Extra arguments appended verbatim to every dsub command, "
                    "e.g. '--env ALL'. Job-level 'dsub_extra' resource takes "
                    "precedence."
        },
    )

# 1. 通用配置：定义这是一个远程执行器
common_settings = CommonSettings(
    non_local_exec=True,
    implies_no_shared_fs=False,  # 只有在云端且没有共享存储时才设为 True
    job_deploy_sources=False,
    pass_default_storage_provider_args=True,
    pass_default_resources_args=True,
    pass_envvar_declarations_to_cmd=False,
    auto_deploy_default_storage_provider=False,
    init_seconds_before_status_checks=5, # 提交后等5秒再开始查状态，加快测试反馈
    pass_group_args=True,
)
