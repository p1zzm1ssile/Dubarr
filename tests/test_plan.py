from core import plan as plans
from core.plan import Rules, decide
from core.probe import MediaInfo, Track, parse
from utils import languages


def media(*tracks):
    return MediaInfo(path="/m/film.mkv", container="matroska", duration=100, size=1000,
                     tracks=[Track(index=i, **t) for i, t in enumerate(tracks)])


VIDEO = {"type": "video", "codec": "hevc"}


def audio(lang, **kw):
    return {"type": "audio", "codec": "eac3", "language": lang, **kw}


def sub(lang, **kw):
    return {"type": "subtitle", "codec": "subrip", "language": lang, **kw}


ENGLISH = Rules(["eng"], ["eng"])


def test_strips_other_audio_and_subtitles():
    p = decide(media(VIDEO, audio("eng", default=True), audio("fra", size=100), audio("deu", size=50),
                     sub("eng"), sub("fra", size=5), sub("spa")), ENGLISH)
    assert p.status == plans.CHANGE
    assert [r["index"] for r in p.remove] == [2, 3, 5, 6]
    assert p.keep == [0, 1, 4]
    assert p.message == "Remove French + German audio and French + Spanish subtitles"
    assert p.saved_bytes == 155
    assert p.default_audio is None  # English was already the default


def test_only_unwanted_audio_is_left_alone_with_a_warning():
    p = decide(media(VIDEO, audio("fra"), sub("eng")), ENGLISH)
    assert p.status == plans.NO_WANTED_AUDIO
    assert p.message == "No English audio, only French"
    assert not p.remove


def test_names_every_language_in_the_warning():
    p = decide(media(VIDEO, audio("kor"), audio("spa")), ENGLISH)
    assert p.message == "No English audio, only Korean + Spanish"


def test_untagged_audio_means_hands_off():
    p = decide(media(VIDEO, audio("eng"), audio("und"), sub("fra")), ENGLISH)
    assert p.status == plans.UNKNOWN_AUDIO
    assert not p.remove
    assert "1 audio track has no language tag, plus English" in p.message


def test_clean_file():
    p = decide(media(VIDEO, audio("eng"), sub("eng", forced=True), sub("eng")), ENGLISH)
    assert p.status == plans.CLEAN


def test_no_audio():
    assert decide(media(VIDEO), ENGLISH).status == plans.NO_AUDIO


def test_untagged_subtitles_are_kept():
    p = decide(media(VIDEO, audio("eng"), sub("und"), sub("fra")), ENGLISH)
    assert [r["index"] for r in p.remove] == [3]


def test_forced_subtitles_in_other_languages_go():
    p = decide(media(VIDEO, audio("eng"), sub("eng", forced=True), sub("fra", forced=True)), ENGLISH)
    assert [r["index"] for r in p.remove] == [3]


def test_several_wanted_languages_for_anime():
    rules = Rules(["jpn", "eng"], ["eng"])
    p = decide(media(VIDEO, audio("jpn", default=True), audio("eng"), audio("ger"), sub("eng"), sub("jpn")), rules)
    assert [r["index"] for r in p.remove] == [3, 5]


def test_default_moves_to_a_kept_track():
    rules = Rules(["eng", "jpn"], ["eng"])
    p = decide(media(VIDEO, audio("fra", default=True), audio("jpn"), audio("eng", commentary=True), audio("eng")), rules)
    # English beats Japanese (order of the setting), and a commentary is never picked
    assert p.default_audio == 4


def test_multiple_languages_track_counts_as_wanted():
    p = decide(media(VIDEO, audio("mul"), audio("fra")), ENGLISH)
    assert p.status == plans.CHANGE
    assert [r["index"] for r in p.remove] == [2]


def test_switches():
    tracks = (VIDEO, audio("eng"), audio("fra"), sub("fra"))
    p = decide(media(*tracks), Rules(["eng"], ["eng"], strip_audio=False))
    assert [r["index"] for r in p.remove] == [3]
    p = decide(media(*tracks), Rules(["eng"], ["eng"], strip_subtitles=False))
    assert [r["index"] for r in p.remove] == [2]
    p = decide(media(*tracks), Rules(["eng"], ["eng"], strip_audio=False, strip_subtitles=False))
    assert p.status == plans.CLEAN


def test_bibliographic_codes_match():
    # Files say "fre" and "ger"; settings might say "fra" or "French"
    p = decide(media(VIDEO, audio(languages.normalise("ger")), audio(languages.normalise("fre"))),
               Rules(languages.parse_list("French"), ["eng"]))
    assert [r["index"] for r in p.remove] == [1]


def test_parse_ffprobe():
    info = parse("/x.mkv", {
        "format": {"format_name": "matroska,webm", "duration": "5.0", "size": "1234"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 6,
             "tags": {"language": "fre", "NUMBER_OF_BYTES": "999"}, "disposition": {"default": 1}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"title": "English Forced"},
             "disposition": {}},
            {"index": 3, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"index": 4, "codec_type": "attachment", "tags": {"mimetype": "font/ttf"}},
        ],
    })
    assert info.container == "matroska"
    assert [t.type for t in info.tracks] == ["video", "audio", "subtitle", "other", "other"]
    a = info.tracks[1]
    assert (a.language, a.raw_language, a.default, a.channels, a.size) == ("fra", "fre", True, 6, 999)
    s = info.tracks[2]
    assert s.language == "und" and s.forced


def test_old_cached_results_are_read_again():
    info = MediaInfo.from_dict({"path": "/x.mkv", "container": "matroska", "duration": 1, "size": 1, "tracks": []})
    assert info.version == 0
    assert MediaInfo(path="/x", container="", duration=0, size=0).version > 0


def test_commentary_is_removed():
    p = decide(media(VIDEO, audio("eng", default=True), audio("eng", commentary=True), audio("fra"), sub("eng")), ENGLISH)
    assert [r["index"] for r in p.remove] == [3, 2]
    assert p.message == "Remove French audio and 1 commentary track"


def test_commentary_can_be_kept():
    rules = Rules(["eng"], ["eng"], strip_commentary=False)
    p = decide(media(VIDEO, audio("eng"), audio("eng", commentary=True)), rules)
    assert p.status == plans.CLEAN


def test_the_only_wanted_audio_is_never_removed_even_if_marked_commentary():
    p = decide(media(VIDEO, audio("eng", commentary=True), audio("fra")), ENGLISH)
    assert [r["index"] for r in p.remove] == [2]


def test_three_parts_read_as_a_list():
    p = decide(media(VIDEO, audio("eng"), audio("eng", commentary=True), audio("fra"), sub("spa")), ENGLISH)
    assert p.message == "Remove French audio, 1 commentary track and Spanish subtitles"


def test_untagged_subtitles_are_flagged_when_nothing_else_to_do():
    p = decide(media(VIDEO, audio("eng"), sub("und"), sub("und")), ENGLISH)
    assert p.status == plans.UNKNOWN_SUBTITLES
    assert p.message.startswith("2 subtitle tracks have no language tag")
    assert not p.remove
