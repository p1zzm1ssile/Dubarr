"""Dubarr talking to Bazarr, against a stand-in for Bazarr's API."""

import pytest
import requests

import utils.config as config
from conftest import make_video, needs_tools
from core.engine import Engine
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

    def request(method, url, headers=None, params=None, data=None, timeout=None, **kwargs):
        path = url.split("6767", 1)[1]
        assert headers["X-API-KEY"] == "secret"
        calls.append((method, path, dict(params or {})))
        return FakeResponse(answers.get((method, path), {}))

    monkeypatch.setattr(requests, "request", request)
    for key, value in {"bazarr_enabled": True, "bazarr_url": "http://bazarr:6767", "bazarr_api_key": "secret",
                       "bazarr_paths": [], "bazarr_search_missing": True, "radarr_enabled": False,
                       "sonarr_enabled": False, "jellyfin_enabled": False}.items():
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
    actions = [(m, p, c.get("action")) for m, p, c in calls if m == "PATCH"]
    assert actions == [("PATCH", "/api/movies", "scan-disk"), ("PATCH", "/api/movies", "search-missing")]
    assert all(c.get("radarrid") == 42 for m, p, c in calls if m == "PATCH")

    # With English subtitles left, it's only told to rescan
    (library / "Film" / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    engine.scan()
    calls.clear()
    engine.notify(movie_path)
    assert [c.get("action") for m, p, c in calls if m == "PATCH"] == ["scan-disk"]
