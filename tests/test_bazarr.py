"""Dubarr talking to Bazarr, against a stand-in for Bazarr's API."""

import os

import pytest
import requests

import utils.config as config
from conftest import make_video, needs_tools
from core.engine import NOT_BAZARR, Engine
from core.store import Store
from services.connections import Bazarr


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.content = b"x"

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


ANSWERS = {}
SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


@pytest.fixture
def fake_bazarr(monkeypatch, tmp_path):
    calls = []
    movie_path = str(tmp_path / "movies" / "Film" / "Film.mkv")
    answers = {
        ("GET", "/api/system/status"): {"data": {"bazarr_version": "1.5.1"}},
        ("GET", "/api/system/languages/profiles"): [
            {"name": "English", "items": [{"language": "en"}]},
            {"name": "Anime", "items": [{"language": "en"}, {"language": "ja"}, {"language": "fr"}]},
        ],
        ("GET", "/api/movies"): {"data": [{"radarrId": 42, "path": movie_path}]},
        ("GET", "/api/series"): {"data": [{"sonarrSeriesId": 7, "path": str(tmp_path / "tv" / "Show")}]},
    }
    ANSWERS.clear()
    ANSWERS.update(answers)
    answers = ANSWERS

    def request(method, url, headers=None, params=None, data=None, timeout=None, **kwargs):
        path = url.split("6767", 1)[1]
        assert headers["X-API-KEY"] == "secret"
        calls.append((method, path, dict(params or {})))
        return FakeResponse(answers.get((method, path), {}))

    monkeypatch.setattr(requests, "request", request)
    for key, value in {"bazarr_enabled": True, "bazarr_url": "http://bazarr:6767", "bazarr_api_key": "secret",
                       "bazarr_paths": [], "bazarr_search_missing": True, "radarr_enabled": False,
                       "sonarr_enabled": False, "jellyfin_enabled": False,
                       "bazarr_other_subtitles": config.OTHER_SUBTITLES_KEEP}.items():
        monkeypatch.setattr(config, key, value)
    return calls, movie_path


def test_profiles_and_clashes(fake_bazarr, monkeypatch):
    monkeypatch.setattr(config, "library_list", [{"path": "/movies", "audio": ["eng"], "subtitles": ["eng"]},
                                                 {"path": "/anime", "audio": ["jpn", "eng"], "subtitles": ["eng", "jpn"]}])
    engine = Engine(Store(":memory:"))
    assert engine.bazarr_clashes() == [("Anime", ["French"])]


def test_locate_without_radarr(fake_bazarr, tmp_path):
    _, movie_path = fake_bazarr
    bazarr = Bazarr(config)
    assert bazarr.locate(movie_path) == ("movie", 42)
    assert bazarr.locate(str(tmp_path / "tv" / "Show" / "S01" / "E1.mkv")) == ("series", 7)
    assert bazarr.locate("/elsewhere/x.mkv") is None


@needs_tools
def test_told_after_a_change_and_asked_to_search(fake_bazarr, tmp_path, monkeypatch):
    calls, movie_path = fake_bazarr
    library = tmp_path / "movies"
    monkeypatch.setattr(config, "library_list", [{"path": str(library), "audio": ["eng"], "subtitles": ["eng"]}])
    for key, value in {"report_only": True, "recycle_days": 7, "recycle_folder": "", "process_hardlinked": False,
                       "min_saving_mb": 0, "active_from": 0, "active_until": 0}.items():
        monkeypatch.setattr(config, key, value)
    make_video(movie_path, audio=("eng", "ita"), subtitles=("fre",))  # no English subtitles at all
    engine = Engine(Store(str(tmp_path / "db")))
    engine.scan()
    calls.clear()
    engine.apply("manual", force=True)
    engine.wait_for_notices()
    actions = [(m, p, c.get("action")) for m, p, c in calls if m == "PATCH"]
    assert actions == [("PATCH", "/api/movies", "scan-disk"), ("PATCH", "/api/movies", "search-missing")]
    assert all(c.get("radarrid") == 42 for m, p, c in calls if m == "PATCH")

    # With English subtitles left, it's only told to rescan
    (library / "Film" / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    engine.scan()
    calls.clear()
    engine.notify(movie_path)
    engine.wait_for_notices()
    assert [c.get("action") for m, p, c in calls if m == "PATCH"] == ["scan-disk"]


def history(movie_path, *entries):
    """Bazarr's movie history, newest first, as (action, subtitle file beside the film)."""
    folder = os.path.dirname(movie_path)
    ANSWERS[("GET", "/api/movies/history")] = {"data": [
        {"action": action, "radarrId": 42, "subtitles_path": os.path.join(folder, name)} for action, name in entries]}


def test_which_subtitles_bazarr_downloaded(fake_bazarr, monkeypatch):
    _, movie_path = fake_bazarr
    history(movie_path, (0, "Film.fr.srt"), (1, "Film.fr.srt"), (3, "Film.en.srt"), (1, "Film.en.srt"))
    ANSWERS[("GET", "/api/episodes/history")] = {"data": [{"action": 2, "subtitles_path": "/data/tv/Show/S01/E1.en.srt"}]}
    monkeypatch.setattr(config, "bazarr_paths", [("/data/tv", "/tv")])
    folder = os.path.dirname(movie_path)
    # Downloaded then deleted: not Bazarr's any more. Bazarr's own folders are turned into Dubarr's
    assert Bazarr(config).downloaded_subtitles() == {f"{folder}/Film.en.srt", "/tv/Show/S01/E1.en.srt"}


@pytest.fixture
def subtitled(fake_bazarr, tmp_path, monkeypatch):
    """A film Bazarr looks after with its own English subtitles and two others, and one Bazarr doesn't know."""
    _, movie_path = fake_bazarr
    library = tmp_path / "movies"
    monkeypatch.setattr(config, "library_list", [{"path": str(library), "audio": ["eng"], "subtitles": ["eng"]}])
    for key, value in {"report_only": True, "recycle_days": 7, "recycle_folder": "", "process_hardlinked": False,
                       "min_saving_mb": 0, "active_from": 0, "active_until": 0, "strip_subtitle_files": True,
                       "max_changes_per_pass": 10}.items():
        monkeypatch.setattr(config, key, value)
    make_video(movie_path, subtitles=("eng",))
    for name in ("Film.en.srt", "Film.en.forced.srt", "Film.srt"):
        (library / "Film" / name).write_text(SRT)
    other = make_video(str(library / "Other" / "Other.mkv"), subtitles=("eng",))
    (library / "Other" / "Other.srt").write_text(SRT)
    history(movie_path, (1, "Film.en.srt"))
    return Engine(Store(str(tmp_path / "db"))), library / "Film", other


@needs_tools
def test_subtitles_bazarr_did_not_download_are_listed_and_removed_by_hand(subtitled, monkeypatch):
    engine, film, other = subtitled
    engine.scan()
    assert engine.store.file(str(film / "Film.mkv"))["status"] == "clean"  # off by default
    monkeypatch.setattr(config, "bazarr_other_subtitles", config.OTHER_SUBTITLES_LIST)
    engine.scan()
    row = engine.store.file(str(film / "Film.mkv"))
    assert row["status"] == NOT_BAZARR
    assert row["message"] == "Remove 2 subtitle files Bazarr didn't download"
    assert engine.store.file(other)["status"] == "clean"  # Bazarr doesn't know it

    # Not by themselves, even with Report only off
    monkeypatch.setattr(config, "report_only", False)
    engine.tick()
    assert (film / "Film.srt").exists()

    engine.apply("manual", force=True, status=NOT_BAZARR)
    assert sorted(os.listdir(film)) == ["Film.en.srt", "Film.mkv"]
    assert os.path.exists(other.replace(".mkv", ".srt"))
    assert engine.store.file(str(film / "Film.mkv"))["status"] == "clean"
    _, entries = engine.store.history(limit=10)
    assert len(entries) == 2 and all("Bazarr didn't download" in e["message"] for e in entries)


@needs_tools
def test_subtitles_bazarr_did_not_download_are_removed_by_themselves(subtitled, monkeypatch):
    engine, film, _ = subtitled
    monkeypatch.setattr(config, "bazarr_other_subtitles", config.OTHER_SUBTITLES_AUTO)
    engine.scan()
    engine.tick()  # Report only is on, but this was chosen separately
    assert sorted(os.listdir(film)) == ["Film.en.srt", "Film.mkv"]


@needs_tools
def test_nothing_is_removed_while_bazarrs_history_is_empty(subtitled, monkeypatch):
    engine, film, _ = subtitled
    ANSWERS[("GET", "/api/movies/history")] = {"data": []}
    monkeypatch.setattr(config, "bazarr_other_subtitles", config.OTHER_SUBTITLES_AUTO)
    engine.scan()
    engine.tick()
    assert len(os.listdir(film)) == 4
    assert engine.store.file(str(film / "Film.mkv"))["status"] == "clean"


def test_where_subtitle_files_came_from(fake_bazarr):
    _, movie_path = fake_bazarr
    history(movie_path, (5, "Film.en.srt"), (1, "Film.en.srt"))
    ANSWERS[("GET", "/api/movies/history")]["data"][1]["provider"] = "opensubtitlescom"
    folder = os.path.dirname(movie_path)
    # A sync keeps the provider of the download it started from
    assert Bazarr(config).subtitle_sources() == {f"{folder}/Film.en.srt": "opensubtitlescom"}

    import utils.web as web
    sources = {f"{folder}/Film.en.srt": "opensubtitlescom"}
    assert web.with_source({"path": f"{folder}/Film.en.srt"}, sources)["source"] == "opensubtitlescom"
    assert web.with_source({"path": f"{folder}/Film.srt"}, sources)["from_bazarr"] is False
    assert web.with_source({"path": f"{folder}/Film.srt"}, None)["from_bazarr"] is None


def test_bazarr_taking_its_time_isnt_an_error(fake_bazarr, monkeypatch):
    def slow(*args, **kwargs):
        raise requests.exceptions.ReadTimeout()
    monkeypatch.setattr(requests, "request", slow)
    Bazarr(config).action("series", 7, "scan-disk")  # Bazarr has it; it answers when it's done
