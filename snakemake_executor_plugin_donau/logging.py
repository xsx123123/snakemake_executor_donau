"""
Logging configuration for Donau Executor
"""

import sys
import os
from loguru import logger

def setup_logger(workdir: str = "."):
    """
    配置 loguru 日志系统
    """
    # 1. 确定日志文件路径：直接在工作目录下生成 donau_executor.log
    log_file = os.path.join(workdir, "donau_executor.log")
    
    # 2. 清除所有现有的 handler (包括默认的 stderr 输出)
    # 这样可以去除控制台中冗长的 "snakemake_executor_plugin_donau.executor:..." 前缀
    logger.remove()
    
    # 3. 添加控制台输出 (简化格式)
    # 格式示例: 15:56:30 | INFO | Job step1 submitted (ID: 123)
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"  # 控制台只显示 INFO 及以上，避免 DEBUG 刷屏
    )

    # 4. 添加文件输出 (详细格式，保留用于排查问题)
    try:
        logger.add(
            log_file, 
            rotation="10 MB", 
            retention="7 days",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            enqueue=True
        )
    except Exception as e:
        # 如果无法写入文件，仅在控制台提示警告
        print(f"Warning: Could not create log file at {log_file}: {e}", file=sys.stderr)
    
    return logger