"""
Logging configuration for Donau Executor
"""

import sys
import os
from loguru import logger

def setup_logger(workdir: str = ".", log_dir: str = None):
    """
    配置 loguru 日志系统
    """
    # 1. 确定日志文件路径：优先使用 --donau-log-dir 指定的目录，
    #    否则直接在工作目录下生成 donau_executor.log
    log_file = os.path.join(log_dir or workdir, "donau_executor.log")

    # 2. 过滤 snakemake rich-loguru 集成产生的 "None" 空消息行，
    # 避免文件里混入大量无意义行（不影响控制台输出）
    def _drop_none_messages(record):
        return record["message"] != "None"

    # 3. 我们不再主动清除处理器或配置控制台输出，
    # 这样插件会直接继承 Snakemake 主进程的 logger 配置 (例如 --logger rich-loguru)
    # logger.remove()

    # 4. 移除控制台输出配置，避免干扰主程序的日志样式
    # logger.add(
    #     sys.stderr,
    #     format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    #     level="INFO"
    # )

    # 5. 添加文件输出 (详细格式，保留用于排查问题；
    #    colorize=False 保证文件内无 ANSI 颜色码，可直接 grep)
    try:
        logger.add(
            log_file,
            rotation="50 MB",
            retention="7 days",
            level="DEBUG",
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            filter=_drop_none_messages,
            enqueue=True,
            colorize=False
        )
    except Exception as e:
        # 如果无法写入文件，仅在控制台提示警告
        print(f"Warning: Could not create log file at {log_file}: {e}", file=sys.stderr)

    return logger
