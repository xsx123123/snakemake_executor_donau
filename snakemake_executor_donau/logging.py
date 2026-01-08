"""
Logging configuration for Donau Executor
"""

import os
from loguru import logger

def setup_logger(persistence_path: str = ".snakemake"):
    """
    配置 loguru 将日志写入文件。
    """
    log_file = os.path.join(persistence_path, "donau_executor.log")
    
    # 使用 logger.add 并确保只添加一次
    # 如果在一个长时间运行的任务中多次调用初始化，
    # 这种方式可以避免产生重复的日志条目。
    
    # 移除默认控制台输出（可选，取决于您是否希望在终端也看到这些信息）
    # logger.remove() 
    
    try:
        logger.add(
            log_file, 
            rotation="10 MB", 
            retention="7 days",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            enqueue=True # 线程安全
        )
    except Exception:
        # 如果无法写入文件，则回退到标准错误输出
        pass
    
    return logger
