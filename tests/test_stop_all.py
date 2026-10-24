"""Stop all cancels the jobs waiting their turn behind the running one."""

import threading
import time

from core.engine import Busy, Engine


def queue(engine, name, ran, errors):
    def run():
        try:
            with engine.job(name, wait=True):
                ran.append(name)
        except Busy as e:
            errors.append(str(e))
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def wait_for(check):
    for _ in range(200):
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_stop_all_cancels_what_is_waiting():
    engine = Engine(store=None)
    ran, errors = [], []
    release = threading.Event()

    def running():
        with engine.job("Replacing files with no audio"):
            release.wait()

    first = threading.Thread(target=running)
    first.start()
    wait_for(engine.busy)
    threads = [queue(engine, "Tagging", ran, errors), queue(engine, "Tagging", ran, errors),
               queue(engine, "Scanning", ran, errors)]
    wait_for(lambda: sum(engine.waiting().values()) == 3)
    assert engine.waiting() == {"Tagging": 2, "Scanning": 1}
    assert engine.progress()["waiting"] == {"Tagging": 2, "Scanning": 1}

    engine.stop_all()
    release.set()
    for t in [first, *threads]:
        t.join(5)
    assert ran == [] and len(errors) == 3
    assert engine.waiting() == {} and not engine.busy()

    # Jobs asked for afterwards run as usual
    queue(engine, "Tagging", ran, errors).join(5)
    assert ran == ["Tagging"]


def test_plain_stop_leaves_waiting_jobs():
    engine = Engine(store=None)
    ran, errors = [], []
    release = threading.Event()

    def running():
        with engine.job("Changing files"):
            release.wait()

    first = threading.Thread(target=running)
    first.start()
    wait_for(engine.busy)
    waiting = queue(engine, "Tagging", ran, errors)
    wait_for(lambda: engine.waiting())
    engine.stop()
    release.set()
    first.join(5)
    waiting.join(5)
    assert ran == ["Tagging"]
