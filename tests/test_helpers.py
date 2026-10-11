import datetime

import pytest

import utils.config as config
from core.engine import in_window
from services.connections import webhook_paths
from utils import languages
from utils.paths import inside, to_local, to_remote


def test_language_aliases():
    for value in ("fre", "fra", "fr", "French", "français", "fr-CA"):
        assert languages.normalise(value) == "fra"
    assert languages.normalise("") == "und"
    assert languages.normalise("qaa") == "und"
    assert languages.normalise("xyz") == "und"
    assert languages.normalise("mul") == "mul"


def test_language_list():
    assert languages.parse_list("English, jpn, fr, eng") == ["eng", "jpn", "fra"]
    with pytest.raises(ValueError, match="recognise: Klingonese"):
        languages.parse_list("eng, Klingonese")
    assert languages.describe(["kor", "spa"]) == "Korean + Spanish"


def test_path_mapping():
    pairs = [("/data/media", "/media"), ("/data/media/movies", "/movies")]
    assert to_local("/data/media/movies/A/a.mkv", pairs) == "/movies/A/a.mkv"
    assert to_local("/data/media/tv/B/b.mkv", pairs) == "/media/tv/B/b.mkv"
    assert to_local("/elsewhere/c.mkv", pairs) == "/elsewhere/c.mkv"
    assert to_remote("/movies/A/a.mkv", pairs) == "/data/media/movies/A/a.mkv"
    # A folder that only starts with the same letters isn't a match
    assert to_local("/data/mediaextra/x.mkv", pairs) == "/data/mediaextra/x.mkv"
    assert inside("/movies/A/a.mkv", "/movies/")
    assert not inside("/movies2/a.mkv", "/movies")


def test_time_window():
    at = lambda h: datetime.datetime(2026, 1, 1, h)
    assert in_window(0, 0, at(13))
    assert in_window(1, 7, at(1)) and in_window(1, 7, at(6)) and not in_window(1, 7, at(7))
    # Across midnight
    assert in_window(22, 6, at(23)) and in_window(22, 6, at(2)) and not in_window(22, 6, at(12))


def test_webhook_paths():
    radarr = {"eventType": "Download", "movie": {"folderPath": "/movies/Film (2020)"},
              "movieFile": {"relativePath": "Film.mkv"}}
    assert webhook_paths(radarr, "radarr") == ["/movies/Film (2020)/Film.mkv"]
    radarr["movieFile"]["path"] = "/movies/Film (2020)/Film.mkv"
    assert webhook_paths(radarr, "radarr") == ["/movies/Film (2020)/Film.mkv"]
    sonarr = {"eventType": "Download", "series": {"path": "/tv/Show"},
              "episodeFiles": [{"relativePath": "S01/E1.mkv"}, {"path": "/tv/Show/S01/E2.mkv"}]}
    assert webhook_paths(sonarr, "sonarr") == ["/tv/Show/S01/E1.mkv", "/tv/Show/S01/E2.mkv"]


def test_library_settings():
    libs = config.coerce(config.FIELDS_BY_KEY["libraries"], [
        {"path": "/movies/", "audio": "", "subtitles": ""},
        {"path": "/anime", "audio": "Japanese, English", "subtitles": "en"},
        {"path": "", "audio": "", "subtitles": ""},
    ])
    assert libs == [{"path": "/movies", "audio": "", "subtitles": ""},
                    {"path": "/anime", "audio": "jpn,eng", "subtitles": "eng"}]
    for bad in ([{"path": "movies"}], [{"path": "/"}], [{"path": "/a"}, {"path": "/a/"}], [{"path": "/a", "audio": "zz"}]):
        with pytest.raises(ValueError):
            config.coerce(config.FIELDS_BY_KEY["libraries"], bad)


def test_language_and_path_map_settings():
    assert config.coerce(config.FIELDS_BY_KEY["audio_languages"], "English, Japanese") == "eng,jpn"
    with pytest.raises(ValueError):
        config.coerce(config.FIELDS_BY_KEY["audio_languages"], "")
    with pytest.raises(ValueError):
        config.coerce(config.FIELDS_BY_KEY["radarr_path_map"], "/data/movies")
    assert config.coerce(config.FIELDS_BY_KEY["radarr_path_map"], "/data/movies=/movies") == "/data/movies=/movies"
