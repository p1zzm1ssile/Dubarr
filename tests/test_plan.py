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


def test_commentary_subtitles_go_with_the_commentary():
    p = decide(media(VIDEO, audio("eng"), audio("eng", commentary=True), sub("eng"), sub("eng", commentary=True),
                     sub("eng", title="Commentary by the director", commentary=True)), ENGLISH)
    assert sorted(r["index"] for r in p.remove) == [2, 4, 5]
    assert p.message == "Remove 1 commentary track and 2 commentary subtitle tracks"
    kept = decide(media(VIDEO, audio("eng"), sub("eng", commentary=True)), Rules(["eng"], ["eng"], strip_commentary=False))
    assert kept.status == plans.CLEAN


def test_one_subtitle_per_language_prefers_text_and_keeps_forced():
    # Like A View to a Kill: Blu-ray picture subs, a text copy, SDH, a forced track, and commentary subs
    p = decide(media(VIDEO, audio("eng"),
                     sub("eng", codec="hdmv_pgs_subtitle", frames=1500, default=True),
                     sub("eng", codec="subrip", frames=1490),
                     sub("eng", codec="subrip", frames=1700, hearing_impaired=True),
                     sub("eng", codec="hdmv_pgs_subtitle", frames=35),            # forced, but not flagged
                     sub("eng", codec="subrip", commentary=True, frames=900)), ENGLISH)
    removed = {r["index"]: r["reason"] for r in p.remove}
    assert removed == {2: "Duplicate subtitles", 4: "Duplicate subtitles", 6: "Commentary subtitles"}
    assert p.message == "Remove 1 commentary subtitle track and 2 extra English subtitle tracks"


def test_sdh_is_kept_when_it_is_the_only_full_track():
    p = decide(media(VIDEO, audio("eng"), sub("eng", hearing_impaired=True, frames=1500), sub("eng", forced=True, frames=20)),
               ENGLISH)
    assert p.status == plans.CLEAN


def test_best_audio_per_language():
    p = decide(media(VIDEO,
                     audio("eng", codec="ac3", channels=6, default=True),
                     audio("eng", codec="truehd", channels=8, profile="Dolby TrueHD + Dolby Atmos"),
                     audio("eng", codec="aac", channels=2),
                     audio("eng", codec="eac3", channels=6, visual_impaired=True)), ENGLISH)
    assert sorted(r["index"] for r in p.remove) == [1, 3, 4]
    assert p.default_audio == 2  # the kept track becomes the default
    assert p.message == "Remove 3 extra English audio tracks"


def test_dts_hd_ma_beats_lossy():
    p = decide(media(VIDEO, audio("eng", codec="eac3", channels=6), audio("eng", codec="dts", channels=6,
                                                                           profile="DTS-HD MA")), ENGLISH)
    assert [r["index"] for r in p.remove] == [1]


def test_duplicates_per_language_for_anime():
    rules = Rules(["jpn", "eng"], ["eng"])
    p = decide(media(VIDEO, audio("jpn", codec="flac", channels=2), audio("jpn", codec="aac", channels=2),
                     audio("eng", codec="aac", channels=2), audio("eng", codec="ac3", channels=6)), rules)
    assert sorted(r["index"] for r in p.remove) == [2, 3]


def test_duplicate_rules_can_be_turned_off():
    tracks = (VIDEO, audio("eng", codec="ac3"), audio("eng", codec="aac"), sub("eng", frames=1000), sub("eng", frames=900))
    rules = Rules(["eng"], ["eng"], one_audio_per_language=False, one_subtitle_per_language=False)
    assert decide(media(*tracks), rules).status == plans.CLEAN


def test_parse_profile_and_lines():
    info = parse("/x.mkv", {"format": {}, "streams": [
        {"index": 0, "codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA", "disposition": {"visual_impaired": 1}},
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"NUMBER_OF_FRAMES": "42", "title": "English [CC]"},
         "disposition": {}},
    ]})
    assert info.tracks[0].profile == "DTS-HD MA" and info.tracks[0].visual_impaired
    assert info.tracks[1].frames == 42 and info.tracks[1].hearing_impaired


def test_subtitles_are_left_alone_when_their_length_is_unknown():
    # No line counts or sizes: a short forced track can't be spotted, so nothing is removed
    p = decide(media(VIDEO, audio("eng"), sub("eng", codec="hdmv_pgs_subtitle"), sub("eng", codec="subrip")), ENGLISH)
    assert p.status == plans.CLEAN


def test_forced_spotted_by_size_when_lines_are_unknown():
    p = decide(media(VIDEO, audio("eng"), sub("eng", codec="hdmv_pgs_subtitle", size=30_000_000),
                     sub("eng", codec="hdmv_pgs_subtitle", size=400_000),
                     sub("eng", codec="hdmv_pgs_subtitle", size=31_000_000, hearing_impaired=True)), ENGLISH)
    assert [r["index"] for r in p.remove] == [4]  # the SDH copy; the small forced one stays


def test_sizes_of_text_and_picture_subtitles_are_not_compared():
    # Text is far smaller than pictures, so without line counts the SRT could be mistaken for forced
    p = decide(media(VIDEO, audio("eng"), sub("eng", codec="hdmv_pgs_subtitle", size=30_000_000),
                     sub("eng", codec="subrip", size=90_000)), ENGLISH)
    assert p.status == plans.CLEAN


def test_unflagged_forced_subtitles_get_flagged():
    p = decide(media(VIDEO, audio("eng"), audio("fra"), sub("eng", frames=1200), sub("eng", frames=12)), ENGLISH)
    assert p.mark_forced == [4]
    flagged = decide(media(VIDEO, audio("eng"), audio("fra"), sub("eng", frames=1200), sub("eng", frames=12, forced=True)),
                     ENGLISH)
    assert flagged.mark_forced == []


def test_trusting_sdh_labels_when_lengths_are_unknown():
    tracks = (VIDEO, audio("eng"), sub("eng", codec="subrip"), sub("eng", codec="subrip", hearing_impaired=True),
              sub("eng", codec="subrip", forced=True))
    assert decide(media(*tracks), ENGLISH).status == plans.CLEAN  # off: can't tell, so everything stays
    trusting = Rules(["eng"], ["eng"], assume_unlabelled_is_full=True)
    p = decide(media(*tracks), trusting)
    assert [r["index"] for r in p.remove] == [3]  # the SDH copy; the full and the flagged forced one stay
    # Nothing labelled SDH (or everything is): no basis for a guess
    assert decide(media(VIDEO, audio("eng"), sub("eng"), sub("eng")), trusting).status == plans.CLEAN
    assert decide(media(VIDEO, audio("eng"), sub("eng", hearing_impaired=True), sub("eng", hearing_impaired=True)),
                  trusting).status == plans.CLEAN


def test_sidecar_names(tmp_path):
    from core.probe import find_sidecars
    (tmp_path / "Film (2020).mkv").write_bytes(b"")
    for name in ("Film (2020).en.srt", "Film (2020).fr.srt", "Film (2020).eng.forced.srt", "Film (2020).en.hi.srt",
                 "Film (2020).German.sdh.ass", "Film (2020).srt", "Film (2020).hi.srt", "Film (2020).en.sup",
                 "Other Film.fr.srt", "Film (2020).nfo", ".Film (2020).fr.srt"):
        (tmp_path / name).write_text("x")
    found = {f["path"].split("/")[-1]: (f["language"], f["forced"], f["hearing_impaired"], f["text"])
             for f in find_sidecars(str(tmp_path / "Film (2020).mkv"))}
    assert found == {
        "Film (2020).en.srt": ("eng", False, False, True),
        "Film (2020).fr.srt": ("fra", False, False, True),
        "Film (2020).eng.forced.srt": ("eng", True, False, True),
        "Film (2020).en.hi.srt": ("eng", False, True, True),  # "hi" after the language means hearing impaired
        "Film (2020).German.sdh.ass": ("deu", False, True, True),
        "Film (2020).srt": ("und", False, False, True),        # no language in the name: kept
        "Film (2020).hi.srt": ("hin", False, False, True),     # first, it's Hindi
        "Film (2020).en.sup": ("eng", False, False, False),
    }


def sidecar(lang, text=True, forced=False, size=50_000):
    return {"path": f"/m/film.{lang}.srt", "language": lang, "format": "srt" if text else "sup", "text": text,
            "forced": forced, "hearing_impaired": False, "size": size}


def test_unwanted_subtitle_files_are_removed():
    info = media(VIDEO, audio("eng"), sub("eng"))
    info.external = [sidecar("eng"), sidecar("fra"), sidecar("deu"), sidecar("und")]
    p = decide(info, ENGLISH)
    assert p.status == plans.CHANGE and not p.rewrites
    assert [f["language"] for f in p.remove_files] == ["fra", "deu"]
    assert p.message == "Remove 2 French + German subtitle files"
    assert decide(info, Rules(["eng"], ["eng"], strip_subtitle_files=False)).status == plans.CLEAN


def test_files_and_tracks_together():
    info = media(VIDEO, audio("eng"), audio("ita"))
    info.external = [sidecar("ita")]
    p = decide(info, ENGLISH)
    assert p.rewrites and len(p.remove_files) == 1
    assert p.message == "Remove Italian audio and 1 Italian subtitle file"
    # Only the files, when the video has to wait
    files_only = decide(info, ENGLISH, tracks=False)
    assert not files_only.rewrites and len(files_only.remove_files) == 1


def test_picture_subtitles_go_when_a_text_file_is_beside_the_film():
    info = media(VIDEO, audio("eng"), sub("eng", codec="hdmv_pgs_subtitle", frames=1500),
                 sub("eng", codec="hdmv_pgs_subtitle", forced=True, frames=20))
    info.external = [sidecar("eng")]
    assert decide(info, ENGLISH).status == plans.CLEAN  # off by default
    p = decide(info, Rules(["eng"], ["eng"], prefer_text_files=True))
    assert [r["index"] for r in p.remove] == [2]  # the forced picture track stays
    assert "1 picture subtitle track (a text file replaces it)" in p.message


# Keeping the original language

ORIGINAL = Rules(["eng"], ["eng"], keep_original_audio=True)


def with_original(code):
    return Rules(["eng"], ["eng"], keep_original_audio=True, original_language=code)


def test_keeps_the_original_language_from_radarr():
    p = decide(media(VIDEO, audio("eng", default=True), audio("jpn"), audio("fra"), sub("jpn")), with_original("jpn"))
    assert p.status == plans.CHANGE
    assert [r["index"] for r in p.remove] == [3, 4]
    assert p.message == "Remove French audio and Japanese subtitles"


def test_original_language_is_ignored_when_the_setting_is_off():
    rules = Rules(["eng"], ["eng"], original_language="jpn")
    p = decide(media(VIDEO, audio("eng", default=True), audio("jpn")), rules)
    assert [r["index"] for r in p.remove] == [2]


def test_original_already_wanted_changes_nothing():
    p = decide(media(VIDEO, audio("eng", default=True), audio("fra")), with_original("eng"))
    assert [r["index"] for r in p.remove] == [2]


def test_track_flagged_original_stands_in_for_radarr():
    p = decide(media(VIDEO, audio("eng", default=True), audio("kor", original=True), audio("spa")), ORIGINAL)
    assert [r["index"] for r in p.remove] == [3]
    assert p.message == "Remove Spanish audio"


def test_radarr_wins_over_the_flag():
    p = decide(media(VIDEO, audio("eng", default=True), audio("kor", original=True), audio("jpn")),
               with_original("jpn"))
    assert [r["index"] for r in p.remove] == [2]


def test_unknown_original_keeps_other_audio_and_says_why():
    p = decide(media(VIDEO, audio("eng", default=True), audio("jpn")), ORIGINAL)
    assert p.status == plans.UNKNOWN_ORIGINAL
    assert p.message == "Keeps Japanese audio, as the original language isn't known"
    assert not p.remove


def test_unknown_original_still_removes_subtitles():
    p = decide(media(VIDEO, audio("eng", default=True), audio("jpn"), sub("fra")), ORIGINAL)
    assert p.status == plans.CHANGE
    assert [r["index"] for r in p.remove] == [3]
    assert p.message == "Remove French subtitles. Keeps Japanese audio, as the original language isn't known"


def test_flagged_tracks_that_disagree_mean_unknown():
    p = decide(media(VIDEO, audio("eng", original=True), audio("jpn", original=True)), ORIGINAL)
    assert p.status == plans.UNKNOWN_ORIGINAL


def test_original_alone_is_still_flagged_as_no_wanted_audio():
    p = decide(media(VIDEO, audio("jpn"), audio("fra")), with_original("jpn"))
    assert p.status == plans.NO_WANTED_AUDIO


def test_one_audio_per_original_language():
    p = decide(media(VIDEO, audio("eng", default=True), audio("jpn", codec="truehd"), audio("jpn", codec="ac3")),
               with_original("jpn"))
    assert [(r["index"], r["reason"]) for r in p.remove] == [(3, "Duplicate audio")]


def test_your_language_stays_the_default_over_the_original():
    p = decide(media(VIDEO, audio("fra", default=True), audio("jpn"), audio("eng")), with_original("jpn"))
    assert p.default_audio == 3


def test_reads_the_original_flag():
    info = parse("/m/f.mkv", {"streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac",
                                           "tags": {"language": "jpn"}, "disposition": {"original": 1}}]})
    assert info.tracks[0].original
