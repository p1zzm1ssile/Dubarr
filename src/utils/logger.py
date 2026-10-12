# logger.py
from loguru import logger
import sys
import os
import utils.config as config

if os.name == "nt":
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


logger.remove()
logger.level("STARTUP", no=25, color="<cyan><bold>")
logger.level("ACTION", no=25, color="<yellow><bold>")

level_priority = {
    "STARTUP": 0,
    "CRITICAL": 1,
    "ERROR": 2,
    "ACTION": 3,
    "WARNING": 4,
    "WARN": 4,
    "INFO": 5,
}


def custom_filter(record):
    lvl = record["level"].name
    if lvl == "STARTUP":
        return True
    # Read each time, so a log level changed in the web UI applies straight away
    return level_priority.get(lvl, 100) <= level_priority.get(config.log_level, 5)


if config.log_output in ["all", "stdout"]:
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>[{level}]</level> <cyan>{message}</cyan>",
        filter=custom_filter,
        colorize=True,
    )

# Always written, for the Logs page and the log download
os.makedirs(config.log_dir, exist_ok=True)
logger.add(
    config.log_file,
    format="{time:YYYY-MM-DD HH:mm:ss} [{level}] {message}",
    filter=custom_filter,
    rotation="10 MB",
    retention=5,
    colorize=False,
    enqueue=True,
)

__all__ = ["logger"]
