"""Finding a film's original language in Radarr or Sonarr, against a stand-in for their API."""

import pytest
import requests

import utils.config as config
from core.engine import Engine
from services.connections import Arr, original_language
from test_bazarr import FakeResponse


def test_reads_radarr_and_sonarr_language_names():
    assert original_language({"originalLanguage": {"id": 8, "name": "Japanese"}}) == "jpn"
    assert original_language({"originalLanguage": {"id": 18, "name": "Portuguese (Brazil)"}}) == "por"
    assert original_language({"originalLanguage": {"id": 0, "name": "Unknown"}}) is None
    assert original_language({"originalLanguage": {"id": -2, "name": "Original"}}) is None
    assert original_language({}) is None  # Sonarr v3 doesn't say


@pytest.fixture
def radarr(monkeypatch):
    calls = []
    state = {"down": False}

    def request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url))
        if state["down"]:
            raise requests.exceptions.ConnectionError()
        return FakeResponse([
            {"id": 1, "path": "/data/movies/Spirited Away (2001)", "title": "Spirited Away",
             "originalLanguage": {"id": 8, "name": "Japanese"}},
            {"id": 2, "path": "/data/movies/Heat (1995)", "title": "Heat",
             "originalLanguage": {"id": 1, "name": "English"}},
        ])

    monkeypatch.setattr(requests, "request", request)
    for key, value in {"radarr_enabled": True, "radarr_url": "http://radarr:7878/api/v3", "radarr_api_key": "k",
                       "radarr_paths": [("/data/movies", "/movies")], "sonarr_enabled": False,
                       "keep_original_audio": True}.items():
        monkeypatch.setattr(config, key, value)
    return calls, state


def test_arr_finds_the_original_language_through_the_folder_mapping(radarr):
    arr = Arr("radarr", config)
    assert arr.original_language("/movies/Spirited Away (2001)/Spirited Away.mkv") == "jpn"
    assert arr.original_language("/movies/Heat (1995)/Heat.mkv") == "eng"
    assert arr.original_language("/movies/Unknown/film.mkv") is None


def test_engine_puts_it_in_the_rules(radarr):
    engine = Engine(store=None)
    library = {"path": "/movies", "audio": ["eng"], "subtitles": ["eng"]}
    rules = engine.rules(library, "/movies/Spirited Away (2001)/Spirited Away.mkv")
    assert rules.keep_original_audio and rules.original_language == "jpn"


def test_engine_doesnt_ask_when_the_setting_is_off(radarr, monkeypatch):
    calls, _ = radarr
    monkeypatch.setattr(config, "keep_original_audio", False)
    assert Engine(store=None).original_language("/movies/Spirited Away (2001)/a.mkv") is None
    assert not calls


def test_engine_waits_before_asking_an_unreachable_radarr_again(radarr):
    calls, state = radarr
    state["down"] = True
    engine = Engine(store=None)
    assert engine.original_language("/movies/Spirited Away (2001)/a.mkv") is None
    assert engine.original_language("/movies/Heat (1995)/b.mkv") is None
    assert len(calls) == 1
