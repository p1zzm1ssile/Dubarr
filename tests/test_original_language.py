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


# Tagging films that only have their original language

@pytest.fixture
def services(monkeypatch):
    calls = []
    radarr_tags = [{"id": 3, "label": "4k"}]

    def request(method, url, headers=None, timeout=None, params=None, json=None, **kwargs):
        calls.append((method, url, json))
        if url.startswith("http://radarr"):
            path = url.split("/api/v3", 1)[1]
            if path == "/movie":
                return FakeResponse([{"id": 1, "path": "/data/movies/Akira (1988)", "title": "Akira",
                                      "originalLanguage": {"name": "Japanese"}, "tags": [3]}])
            if path == "/tag" and method == "GET":
                return FakeResponse(radarr_tags)
            if path == "/tag" and method == "POST":
                radarr_tags.append({"id": 9, "label": json["label"]})
                return FakeResponse(radarr_tags[-1])
            return FakeResponse({})
        path = url.split("8096", 1)[1]
        if path == "/Users":
            return FakeResponse([{"Id": "viewer", "Policy": {}}, {"Id": "admin", "Policy": {"IsAdministrator": True}}])
        if path == "/Items":
            assert params["userId"] == "admin"
            return FakeResponse({"Items": [{"Id": "m1", "Type": "Movie", "Name": "Akira",
                                            "Path": "/media/movies/Akira (1988)/Akira.mkv", "Tags": []}]})
        if path == "/Users/admin/Items/m1":
            return FakeResponse({"Id": "m1", "Name": "Akira", "Genres": ["Anime"], "Tags": ["favourite"]})
        return FakeResponse({})

    monkeypatch.setattr(requests, "request", request)
    for key, value in {"radarr_enabled": True, "radarr_url": "http://radarr:7878/api/v3", "radarr_api_key": "k",
                       "radarr_paths": [("/data/movies", "/movies")], "sonarr_enabled": False,
                       "jellyfin_enabled": True, "jellyfin_url": "http://jellyfin:8096", "jellyfin_api_key": "k",
                       "jellyfin_paths": [("/media/movies", "/movies")], "bazarr_enabled": False,
                       "keep_original_audio": True, "original_only_tag": "original-language-only"}.items():
        monkeypatch.setattr(config, key, value)
    return calls


def test_tags_in_radarr_and_jellyfin_once(services):
    engine = Engine(store=None)
    engine.tag_original_only("/movies/Akira (1988)/Akira.mkv")
    writes = [(m, u, j) for m, u, j in services if m in ("POST", "PUT")]
    assert writes[0] == ("POST", "http://radarr:7878/api/v3/tag", {"label": "original-language-only"})
    assert writes[1] == ("PUT", "http://radarr:7878/api/v3/movie/editor",
                         {"movieIds": [1], "tags": [9], "applyTags": "add"})
    # Jellyfin gets the whole item back, with nothing lost and the tag added
    assert writes[2] == ("POST", "http://jellyfin:8096/Items/m1",
                         {"Id": "m1", "Name": "Akira", "Genres": ["Anime"], "Tags": ["favourite", "original-language-only"]})
    assert len(writes) == 3

    services.clear()
    engine.tag_original_only("/movies/Akira (1988)/Akira.mkv")
    assert not [c for c in services if c[0] in ("POST", "PUT")]


def test_no_tags_when_the_tag_is_empty(services, monkeypatch):
    monkeypatch.setattr(config, "original_only_tag", "")
    Engine(store=None).tag_original_only("/movies/Akira (1988)/Akira.mkv")
    assert not services


def test_tag_name_is_checked():
    field = config.FIELDS_BY_KEY["original_only_tag"]
    assert config.coerce(field, "Anime-Only") == "anime-only"
    assert config.coerce(field, "") == ""
    with pytest.raises(ValueError):
        config.coerce(field, "anime only")


def test_checking_an_original_only_file_tags_it(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from core import plan as plans
    from core.store import Store

    plan = plans.Plan(plans.ORIGINAL_ONLY, "Only Japanese audio, the original language", original_only="jpn")
    stat = SimpleNamespace(st_size=1, st_mtime=1, st_nlink=1)
    engine = Engine(Store(str(tmp_path / "db")))
    tagged = []
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: (plan.status, plan.message, plan, None, stat))
    monkeypatch.setattr(engine, "tag_original_only", tagged.append)
    engine.check_file({"path": "/movies"}, "/movies/Akira/Akira.mkv")
    assert tagged == ["/movies/Akira/Akira.mkv"]
