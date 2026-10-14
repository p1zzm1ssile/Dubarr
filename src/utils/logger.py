# logger.py
from loguru import logger
import glob
import re
import sys
import os
import utils.config as config

if os.name == "nt":
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


logger.remove()

# Keys and passwords never go in the log, even inside an error message that quotes a web address
SECRET_IN_ADDRESS = re.compile(r"(?i)\b(api_?key|token|password|passwd|pass|x-plex-token)=([^&\s'\"]+)")


def redact(record):
    message = SECRET_IN_ADDRESS.sub(r"\1=***", record["message"])
    for key in config.SECRET_KEYS:
        value = str(config.values.get(key) or "")
        if len(value) >= 4:
            message = message.replace(value, "***")
    record["message"] = message


def private(path, flags):
    """Log files can only be read by the account the app runs as."""
    return os.open(path, flags, 0o600)


logger.configure(patcher=redact)
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
for existing in glob.glob(os.path.join(config.log_dir, "*.log*")):
    try:
        os.chmod(existing, 0o600)
    except OSError:
        pass
logger.add(
    config.log_file,
    format="{time:YYYY-MM-DD HH:mm:ss} [{level}] {message}",
    filter=custom_filter,
    rotation="10 MB",
    retention=5,
    colorize=False,
    enqueue=True,
    opener=private,
)

__all__ = ["logger"]
