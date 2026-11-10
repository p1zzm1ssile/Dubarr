"""Notifications to your phone (ntfy, Gotify, Discord), the daily summary, and the Unraid cache warning."""

import time

import pytest

import utils.config as config
from conftest import make_video, needs_tools
from core import swap
from core.engine import Engine
from core.store import Store
from services import push


class Answer:
    def __init__(self, code=200):
        self.status_code = code


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None, allow_redirects=True):
        calls.append({"url": url, "json": json})
        return Answer()

    monkeypatch.setattr(push.requests, "post", post)
    return calls


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = Engine(Store(str(tmp_path / "dubarr.db")))
    e.push_in_background = False
    monkeypatch.setattr(config, "push_url", "https://ntfy.sh/my-dubarr")
    for what in ("problems", "no_audio", "daily"):
        monkeypatch.setattr(config, f"push_{what}", True)
    return e


def test_kind_from_address():
    assert push.kind("https://discord.com/api/webhooks/1/abc") == "discord"
    assert push.kind("https://gotify.example/message?token=abc") == "gotify"
    assert push.kind("https://ntfy.sh/topic") == "ntfy"
    assert push.kind("http://192.168.1.10:8080/dubarr") == "ntfy"


def test_each_kind_is_sent_its_way(sent):
    push.send("https://ntfy.example/sub/my-topic", "Tïtle", "Body")
    assert sent[-1] == {"url": "https://ntfy.example/sub/",
                        "json": {"topic": "my-topic", "title": "Tïtle", "message": "Body", "tags": ["dubarr"]}}
    push.send("https://gotify.example/message?token=abc", "T", "B")
    assert sent[-1]["url"] == "https://gotify.example/message?token=abc"
    assert sent[-1]["json"]["title"] == "T"
    push.send("https://discord.com/api/webhooks/1/abc", "T", "B" * 3000)
    assert sent[-1]["json"]["content"].startswith("**T**\nB") and len(sent[-1]["json"]["content"]) == 2000


def test_errors_never_show_the_address(monkeypatch):
    monkeypatch.setattr(push.requests, "post", lambda *a, **k: Answer(403))
    with pytest.raises(push.PushError) as e:
        push.send("https://gotify.example/message?token=SECRET", "T", "B")
    assert "SECRET" not in str(e.value) and "403" in str(e.value)
    with pytest.raises(push.PushError):
        push.send("ntfy.sh/topic", "T", "B")
    with pytest.raises(push.PushError):
        push.send("https://ntfy.sh/", "T", "B")


def test_problems_are_sent_once_a_day(engine, sent, monkeypatch):
    engine.alert("Radarr", "Couldn't reach Radarr")
    engine.alert("Radarr", "Couldn't reach Radarr")
    engine._alerts.clear()  # the pop-up waits 10 minutes; the notification waits a day
    engine.alert("Radarr", "Couldn't reach Radarr")
    assert len(sent) == 1 and sent[0]["json"]["title"] == "Dubarr: Radarr"
    monkeypatch.setattr(config, "push_problems", False)
    engine.alert("Sonarr", "Couldn't reach Sonarr")
    assert len(sent) == 1


def test_nothing_sent_without_an_address(engine, sent, monkeypatch):
    monkeypatch.setattr(config, "push_url", "")
    assert engine.push("problems", "T", "B") is False
    assert not sent


def test_daily_summary(engine, sent, monkeypatch):
    monkeypatch.setattr(config, "push_daily_hour", 9)
    nine = time.mktime((2026, 11, 10, 9, 5, 0, 0, 0, -1))
    # Nothing happened: nothing sent
    assert engine.daily_summary(nine) is False
    engine.last_summary = ""
    run = engine.store.start_run("scheduled")
    engine.store.finish_run(run, 3, 1, 5 * 1024 ** 3)
    now = time.time()
    hour = time.localtime(now).tm_hour
    monkeypatch.setattr(config, "push_daily_hour", (hour + 1) % 24)
    assert engine.daily_summary(now) is False  # not its hour
    monkeypatch.setattr(config, "push_daily_hour", hour)
    assert engine.daily_summary(now) is True
    message = sent[-1]["json"]["message"]
    assert "Changed 3 file(s)" in message and "1 couldn't be changed" in message
    assert engine.daily_summary(now) is False  # once a day


def test_cache_warning_only_when_moved_off_the_array(engine, sent, monkeypatch):
    monkeypatch.setattr(swap, "unraid_disk", lambda path: "cache")
    engine.check_disk("/movies/Film/Film.mkv", None)       # not Unraid: nothing to say
    engine.check_disk("/movies/Film/Film.mkv", "cache")    # was on the pool already: fine
    assert not sent
    engine.check_disk("/movies/Film/Film.mkv", "disk3")
    assert "landing on the Unraid pool \"cache\"" in sent[-1]["json"]["message"]
    monkeypatch.setattr(swap, "unraid_disk", lambda path: "disk3")
    sent.clear()
    engine._pushed.clear()
    engine._alerts.clear()
    engine.check_disk("/movies/Film/Film.mkv", "disk3")
    assert not sent


def test_unraid_disk_elsewhere_is_none(tmp_path):
    f = tmp_path / "a"
    f.write_text("x")
    assert swap.unraid_disk(str(f)) is None
    assert swap.on_array("disk12") and not swap.on_array("cache") and not swap.on_array("diskless")


@needs_tools
def test_new_files_with_no_audio_are_sent_once(engine, sent, tmp_path, monkeypatch):
    library = tmp_path / "movies"
    make_video(str(library / "Film" / "Film.mkv"), audio=())
    make_video(str(library / "Other" / "Other.mkv"))
    monkeypatch.setattr(config, "library_list", [{"path": str(library), "audio": ["eng"], "subtitles": ["eng"]}])
    monkeypatch.setattr(engine, "check_mappings", lambda: None)
    engine.scan(wait=True)
    assert len(sent) == 1
    assert sent[0]["json"]["title"] == "Dubarr: 1 file with no audio"
    assert "Film.mkv" in sent[0]["json"]["message"]
    engine.scan(wait=True)
    assert len(sent) == 1  # already known
