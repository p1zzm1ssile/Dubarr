from core.engine import Engine
from core.store import Store
from utils.logger import logger
import utils.config as config
import utils.status as status
import utils.web as web
import shutil
import signal
import sys
import threading
import time

__version__ = "0.7.0"


def problems():
    found = []
    if config.load_error:
        found.append(config.load_error)
    for tool in ("ffprobe", "ffmpeg", "mkvmerge", "mkvpropedit"):
        if not shutil.which(tool):
            found.append(f"{tool} isn't installed")
    return found


def main():
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    engine = Engine(Store(config.database_file))
    # Finish or undo any change that was cut short by a restart
    engine.recover()

    logger.log("STARTUP", "Starting Dubarr web UI")
    threading.Thread(target=web.serve, args=(engine,), daemon=True).start()

    if not config.library_list:
        logger.log("STARTUP", "No libraries set up yet. Add your movie and TV folders in Settings > Libraries")
    mode = "report only (no files are changed)" if config.report_only else "changing files"
    logger.log("STARTUP", f"Dubarr is running, {mode}. Open the web UI on port {config.web_port}")

    while True:
        status.heartbeat(problems())
        threading.Thread(target=engine.tick, daemon=True).start()
        time.sleep(60)


def handle_sigint(signum, frame):
    sys.exit(0)


if __name__ == "__main__":
    logger.info(f"Dubarr version {__version__} starting")
    main()
