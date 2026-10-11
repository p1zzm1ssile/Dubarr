"""Whether Dubarr is working, for the web UI and the Docker health check."""

import json
import os
import threading
import time

import utils.config as config

STATUS_FILE = os.path.join(config.config_dir, "dubarr-status.json")

# The main loop writes a heartbeat every minute. Missing a few means it's stuck.
STALE_SECONDS = 5 * 60

_lock = threading.Lock()
_status = {"started": time.time(), "heartbeat": time.time(), "problems": []}


def _write():
    # Written to a temporary file and renamed, so the health check never reads half a file
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_status, f)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def heartbeat(problems=None):
    with _lock:
        _status["heartbeat"] = time.time()
        _status["problems"] = list(problems or [])
        _write()


def evaluate(status: dict, now: float = None) -> dict:
    now = now or time.time()
    problems = list(status.get("problems") or [])
    if now - status["heartbeat"] > STALE_SECONDS:
        problems.insert(0, "Dubarr's main loop has stopped responding")
    return {
        "healthy": not problems,
        "problems": problems,
        "uptime_seconds": int(now - status["started"]),
    }


def health() -> dict:
    with _lock:
        return evaluate(dict(_status))


def health_from_file(path: str = STATUS_FILE) -> dict:
    with open(path) as f:
        return evaluate(json.load(f))
