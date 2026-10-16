"""
Snakemake Executor Plugin for Huawei Donau Scheduler
Author: Jian Zhang (Adapted by Hajimi)
"""

from typing import Optional

from pydantic import Field
from snakemake_interface_executor_plugins.settings import CommonSettings, ExecutorSettingsBase
from .executor import Executor

# 插件配置类
class ExecutorSettings(ExecutorSettingsBase):
    any_arch: bool = Field(
        default=True,
        metadata={
            "help": "Pass -aa (any-arch) to dsub together with -q. "
                    "Required by Donau clusters, otherwise jobs stay PENDING forever."
        },
    )
    heartbeat_interval: int = Field(
        default=600,
        metadata={
            "help": "Seconds between heartbeat log lines while polling job states. "
                    "Heartbeats distinguish 'jobs still running' from 'a stuck main "
                    "process' in donau_executor.log. Set to 0 to disable."
        },
    )
    default_queue: Optional[str] = Field(
        default=None,
        metadata={
            "help": "Default queue (-q) for jobs that do not define a 'queue' or "
                    "'partition' resource. On Donau clusters -q still has to be "
                    "combined with -aa (see any_arch)."
        },
    )
    log_dir: Optional[str] = Field(
        default=None,
        metadata={
            "help": "Directory for donau_executor.log. Defaults to the workflow "
                    "working directory."
        },
    )
    submit_extra_args: Optional[str] = Field(
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
