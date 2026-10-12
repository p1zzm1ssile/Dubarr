import threading
import time

import pytest

import utils.config as config
from core import engine as engine_module
from core.engine import run_by_disk
from utils import proc


def test_never_two_on_one_disk(monkeypatch):
    disks = {f"/m/{n}.mkv": f"disk{n % 2}" for n in range(8)}  # two disks, four films each
    monkeypatch.setattr(engine_module, "disk_of", lambda path: disks[path])
    lock = threading.Lock()
    active, seen_together, done = {}, set(), []

    def work(row):
        disk = disks[row["path"]]
        with lock:
            assert active.get(disk, 0) == 0, "two workers on one disk"
            active[disk] = 1
            seen_together.add(frozenset(d for d, n in active.items() if n))
        time.sleep(0.02)
        with lock:
            active[disk] = 0
            done.append(row["path"])

    run_by_disk([{"path": p} for p in disks], work, lambda: True, workers=4)
    assert sorted(done) == sorted(disks)
    assert frozenset({"disk0", "disk1"}) in seen_together  # the two disks did run side by side


def test_stopping_hands_out_no_more(monkeypatch):
    monkeypatch.setattr(engine_module, "disk_of", lambda path: path)
    done = []
    run_by_disk([{"path": str(n)} for n in range(10)], lambda row: done.append(row), lambda: len(done) < 3, workers=2)
    assert len(done) <= 4  # at most the ones already started


def test_one_worker_is_in_order():
    done = []
    run_by_disk([{"path": str(n)} for n in range(5)], lambda row: done.append(row["path"]), lambda: True, workers=1)
    assert done == ["0", "1", "2", "3", "4"]


def test_low_priority_and_threads(monkeypatch):
    monkeypatch.setattr(config, "low_priority", True)
    monkeypatch.setattr(config, "ffmpeg_threads", 2)
    wrapped = proc.wrap(["ffmpeg", "-i", "x"])
    assert wrapped[-3:] == ["ffmpeg", "-i", "x"]
    assert "nice" in wrapped
    assert proc.ffmpeg_threads() == ["-threads", "2"]
    monkeypatch.setattr(config, "low_priority", False)
    monkeypatch.setattr(config, "ffmpeg_threads", 0)
    assert proc.wrap(["ffmpeg"]) == ["ffmpeg"] and proc.ffmpeg_threads() == []
