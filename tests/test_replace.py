"""Replacing files with no audio through Radarr or Sonarr, against a stand-in for their API."""

import pytest
import requests

import utils.config as config
from conftest import make_video, needs_tools
from core import plan as plans
from core.engine import Engine
from core.store import REPLACED, Store
from services.connections import Arr
from test_bazarr import FakeResponse


@pytest.fixture
def arrs(monkeypatch, tmp_path):
    """Radarr has Film (2020), its file and the history of the download it came from. Sonarr has Show, with
    episode 1 of season 1 in its own file."""
    calls = []
    answers = {
        ("radarr", "GET", "/movie"): [{"id": 5, "path": "/data/movies/Film (2020)", "title": "Film"}],
        ("radarr", "GET", "/moviefile"): [{"id": 50, "path": "/data/movies/Film (2020)/Film.mkv"}],
        ("radarr", "GET", "/history/movie"): [
            {"id": 900, "eventType": "grabbed", "downloadId": "OLD", "date": "2024-01-01"},
            {"id": 901, "eventType": "grabbed", "downloadId": "ABC", "date": "2025-01-01"},
            {"id": 902, "eventType": "downloadFolderImported", "downloadId": "ABC", "date": "2025-01-02"},
        ],
        ("sonarr", "GET", "/series"): [{"id": 7, "path": "/data/tv/Show", "title": "Show"}],
        ("sonarr", "GET", "/episodefile"): [{"id": 70, "path": "/data/tv/Show/Season 01/Show S01E01.mkv"},
                                            {"id": 71, "path": "/data/tv/Show/Season 01/Show S01E02.mkv"}],
        ("sonarr", "GET", "/episode"): [{"id": 700, "episodeFileId": 70, "seasonNumber": 1, "episodeNumber": 1},
                                        {"id": 701, "episodeFileId": 71, "seasonNumber": 1, "episodeNumber": 2}],
        ("sonarr", "GET", "/history/series"): [],
    }

    def request(method, url, headers=None, timeout=None, params=None, json=None, **kwargs):
        kind = "radarr" if ":7878" in url else "sonarr"
        path = url.split("/api/v3", 1)[1]
        calls.append((kind, method, path, params or json))
        return FakeResponse(answers.get((kind, method, path)))

    monkeypatch.setattr(requests, "request", request)
    for key, value in {"radarr_enabled": True, "radarr_url": "http://radarr:7878/api/v3", "radarr_api_key": "k",
                       "radarr_paths": [("/data/movies", str(tmp_path / "movies"))],
                       "sonarr_enabled": True, "sonarr_url": "http://sonarr:8989/api/v3", "sonarr_api_key": "k",
                       "sonarr_paths": [("/data/tv", str(tmp_path / "tv"))],
                       "bazarr_enabled": False, "jellyfin_enabled": False, "keep_original_audio": False}.items():
        monkeypatch.setattr(config, key, value)
    return calls, answers


def writes(calls, kind):
    return [(m, p, body) for k, m, p, body in calls if k == kind and m != "GET"]


def test_radarr_blocklists_the_release_then_deletes_and_searches(arrs, tmp_path):
    calls, _ = arrs
    film = str(tmp_path / "movies" / "Film (2020)" / "Film.mkv")
    assert Arr("radarr", config).replace(film) == ("Film", True)
    assert writes(calls, "radarr") == [("POST", "/history/failed/901", None),
                                       ("DELETE", "/moviefile/50", None),
                                       ("POST", "/command", {"name": "MoviesSearch", "movieIds": [5]})]


def test_sonarr_searches_just_that_episode(arrs, tmp_path):
    calls, _ = arrs
    episode = str(tmp_path / "tv" / "Show" / "Season 01" / "Show S01E01.mkv")
    # No history of the download, so nothing is blocklisted, but it's still replaced
    assert Arr("sonarr", config).replace(episode) == ("Show S01E01", False)
    assert writes(calls, "sonarr") == [("DELETE", "/episodefile/70", None),
                                       ("POST", "/command", {"name": "EpisodeSearch", "episodeIds": [700]})]


def test_a_file_they_dont_have_is_left(arrs, tmp_path):
    calls, _ = arrs
    assert Arr("radarr", config).replace(str(tmp_path / "movies" / "Film (2020)" / "Extra.mkv")) is None
    assert Arr("radarr", config).replace(str(tmp_path / "movies" / "Other" / "Other.mkv")) is None
    assert not writes(calls, "radarr")


@pytest.fixture
def library(monkeypatch, tmp_path):
    movies = tmp_path / "movies"
    monkeypatch.setattr(config, "library_list", [{"path": str(movies), "audio": ["eng"], "subtitles": ["eng"]}])
    for key, value in {"report_only": True, "recycle_days": 7, "recycle_folder": "", "process_hardlinked": False,
                       "min_saving_mb": 0, "replace_no_audio": False}.items():
        monkeypatch.setattr(config, key, value)
    return movies


@needs_tools
def test_engine_replaces_only_files_with_no_audio(arrs, library, tmp_path):
    calls, _ = arrs
    silent = make_video(str(library / "Film (2020)" / "Film.mkv"), audio=())
    fine = make_video(str(library / "Fine (2021)" / "Fine.mkv"), audio=("eng",))
    engine = Engine(Store(str(tmp_path / "db")))
    engine.scan()
    assert engine.store.file(silent)["status"] == plans.NO_AUDIO
    calls.clear()

    engine.replace_no_audio(paths=[silent, fine])
    assert ("DELETE", "/moviefile/50", None) in writes(calls, "radarr")
    assert len([c for c in writes(calls, "radarr") if c[0] == "DELETE"]) == 1
    assert engine.store.file(silent) is None  # Radarr deletes it, so it's off the lists
    _, entries = engine.store.history()
    assert [e["state"] for e in entries] == [REPLACED]
    assert "won't pick the same release again" in entries[0]["message"]


@needs_tools
def test_by_itself_only_with_the_setting_on_and_report_only_off(arrs, library, tmp_path, monkeypatch):
    calls, _ = arrs
    make_video(str(library / "Film (2020)" / "Film.mkv"), audio=())
    engine = Engine(Store(str(tmp_path / "db")))
    engine.scan()
    engine.last_scan = engine.last_apply = engine.last_purge = engine.last_detect = 1e12
    monkeypatch.setattr(config, "scan_interval_hours", 24)

    def deletes():
        return [c for c in writes(calls, "radarr") if c[0] == "DELETE"]

    calls.clear()
    engine.tick()
    monkeypatch.setattr(config, "replace_no_audio", True)
    engine.tick()  # report only is still on
    assert not deletes()
    monkeypatch.setattr(config, "report_only", False)
    engine.tick()
    assert len(deletes()) == 1
