"""
Snakemake Executor Plugin for Huawei Donau Scheduler
Author: Jian Zhang (Adapted by Hajimi)
"""

from snakemake_interface_executor_plugins.settings import CommonSettings, ExecutorSettingsBase
from .executor import Executor

# 定义插件配置类（即使暂时为空也必须存在）
class ExecutorSettings(ExecutorSettingsBase):
    pass

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