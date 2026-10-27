"""Real files through the whole cycle: scan, change, check, restore, recycle."""

import os
import subprocess
import time

import pytest

import utils.config as config
from conftest import make_pgs_video, make_video, needs_tools
from core import plan as plans
from core import swap, tagging
from core.engine import ERROR, SEEDING, Engine
from core.probe import probe
from core.remux import temp_path
from core.store import Store

pytestmark = needs_tools


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "movies"
    root.mkdir()
    monkeypatch.setattr(config, "library_list", [{"path": str(root), "audio": ["eng"], "subtitles": ["eng"]}])
    monkeypatch.setattr(config, "report_only", True)
    monkeypatch.setattr(config, "recycle_days", 7)
    monkeypatch.setattr(config, "recycle_folder", "")
    monkeypatch.setattr(config, "process_hardlinked", False)
    monkeypatch.setattr(config, "active_from", 0)
    monkeypatch.setattr(config, "active_until", 0)
    monkeypatch.setattr(config, "max_changes_per_pass", 25)
    for kind in ("radarr", "sonarr", "jellyfin"):
        monkeypatch.setattr(config, f"{kind}_enabled", False)
    return root


@pytest.fixture
def engine(tmp_path):
    return Engine(Store(str(tmp_path / "dubarr.db")))


def tracks(path, kind):
    return [t.language for t in probe(path).of(kind)]


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_strip_restore_cycle(library, engine, ext):
    film = make_video(str(library / "Film (2020)" / f"Film{ext}"),
                      audio=("fre", "eng", "ger"), subtitles=("eng", "fre", "spa"), default_audio=0)
    original_size = os.path.getsize(film)
    original_mtime = os.path.getmtime(film)

    # Report only: nothing changes, but the plan is recorded
    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == plans.CHANGE
    assert row["message"] == "Remove French + German audio and French + Spanish subtitles"
    engine.apply("scheduled")
    assert os.path.getmtime(film) == original_mtime

    # Applying by hand changes it
    engine.apply("manual", force=True)
    assert tracks(film, "audio") == ["eng"]
    assert tracks(film, "subtitle") == ["eng"]
    assert probe(film).of("audio")[0].default  # English took over as the default
    assert probe(film).of("video")
    assert not os.path.exists(temp_path(film))
    assert engine.store.file(film)["status"] == plans.CLEAN

    _, entries = engine.store.history()
    entry = entries[0]
    assert entry["state"] == "done"
    assert entry["original_size"] == original_size
    recycled = entry["backup_path"]
    assert recycled.startswith(str(library / ".dubarr-recycle"))
    assert os.path.getsize(recycled) == original_size

    # The recycle folder is never scanned
    engine.scan()
    _, rows = engine.store.files(limit=100)
    assert [r["path"] for r in rows] == [film]

    # Restore puts every track back and leaves the file alone from then on
    engine.restore(entry["id"])
    assert tracks(film, "audio") == ["fra", "eng", "deu"]
    assert os.path.getsize(film) == original_size
    assert not os.path.exists(recycled)
    assert engine.store.history_entry(entry["id"])["state"] == "restored"
    assert engine.store.file(film)["ignored"]
    engine.apply("manual", force=True)
    assert tracks(film, "audio") == ["fra", "eng", "deu"]


def test_automatic_mode_and_warnings(library, engine, monkeypatch):
    monkeypatch.setattr(config, "report_only", False)
    keep = make_video(str(library / "A" / "a.mkv"), audio=("eng",), subtitles=("eng",))
    french = make_video(str(library / "B" / "b.mkv"), audio=("fre",), subtitles=("fre",))
    untagged = make_video(str(library / "C" / "c.mkv"), audio=("eng", ""))
    strip = make_video(str(library / "D" / "d.mkv"), audio=("eng", "ita"))
    before = {p: os.path.getmtime(p) for p in (keep, french, untagged)}

    engine.scan()
    statuses = {r["path"]: (r["status"], r["message"]) for r in engine.store.files(limit=100)[1]}
    assert statuses[keep][0] == plans.CLEAN
    assert statuses[french] == (plans.NO_WANTED_AUDIO, "No English audio, only French")
    assert statuses[untagged][0] == plans.UNKNOWN_AUDIO
    assert statuses[strip][0] == plans.CHANGE

    engine.apply("scheduled")
    assert tracks(strip, "audio") == ["eng"]
    for path, mtime in before.items():
        assert os.path.getmtime(path) == mtime, f"{path} shouldn't have been touched"


def test_outside_the_window_nothing_changes(library, engine, monkeypatch):
    monkeypatch.setattr(config, "report_only", False)
    hour = time.localtime().tm_hour
    monkeypatch.setattr(config, "active_from", (hour + 1) % 24)
    monkeypatch.setattr(config, "active_until", (hour + 2) % 24)
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    engine.scan()
    engine.apply("scheduled")
    assert tracks(film, "audio") == ["eng", "ita"]


def test_hardlinked_files_wait(library, engine, tmp_path):
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    os.link(film, tmp_path / "seeding-copy.mkv")
    engine.scan()
    assert engine.store.file(film)["status"] == SEEDING
    engine.apply("manual", force=True)
    assert tracks(film, "audio") == ["eng", "ita"]
    os.remove(tmp_path / "seeding-copy.mkv")
    engine.scan()
    assert engine.store.file(film)["status"] == plans.CHANGE


def test_zero_days_deletes_the_original(library, engine, monkeypatch):
    monkeypatch.setattr(config, "recycle_days", 0)
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    engine.scan()
    engine.apply("manual", force=True)
    entry = engine.store.history()[1][0]
    assert entry["state"] == "deleted"
    assert not os.path.exists(library / ".dubarr-recycle")


def test_purge_after_the_set_days(library, engine):
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    engine.scan()
    engine.apply("manual", force=True)
    entry = engine.store.history()[1][0]
    assert swap.purge(engine.store, 7) == 0
    assert swap.purge(engine.store, 7, now=time.time() + 8 * 86400) == 1
    assert not os.path.exists(entry["backup_path"])
    assert engine.store.history_entry(entry["id"])["state"] == "purged"
    with pytest.raises(swap.SwapError, match="emptied"):
        engine.restore(entry["id"])


def test_restore_whole_batch(library, engine):
    films = [make_video(str(library / f"{n}.mkv"), audio=("eng", "ita")) for n in range(3)]
    engine.scan()
    engine.apply("manual", force=True)
    run_id = engine.store.last_run_with_restorable()
    engine.restore_run(run_id)
    for film in films:
        assert tracks(film, "audio") == ["eng", "ita"]
    assert engine.store.last_run_with_restorable() is None


def test_broken_file_is_reported_not_touched(library, engine):
    broken = library / "broken.mkv"
    broken.write_bytes(b"not a video")
    engine.scan()
    assert engine.store.file(str(broken))["status"] == ERROR
    assert broken.read_bytes() == b"not a video"


def test_recover_after_a_crash_mid_swap(library, engine):
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    # Pretend Dubarr died after step 1: the original renamed away, nothing in its place
    history_id = engine.store.add_history(1, film, swap.backup_path(film), os.path.getsize(film), "test", [])
    os.rename(film, swap.backup_path(film))
    engine.recover()
    assert os.path.exists(film)
    assert tracks(film, "audio") == ["eng", "ita"]
    assert engine.store.history_entry(history_id)["state"] == "failed"


def test_leftover_temp_files_are_cleaned_up(library, engine):
    film = make_video(str(library / "x.mkv"), audio=("eng",))
    leftover = temp_path(film)
    with open(leftover, "wb") as f:
        f.write(b"half a file")
    engine.scan()
    assert not os.path.exists(leftover)


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_listen_and_tag(library, engine, ext):
    film = make_video(str(library / f"x{ext}"), audio=("eng", ""), subtitles=("eng", "fre"))
    engine.scan()
    assert engine.store.file(film)["status"] == plans.UNKNOWN_AUDIO
    info = engine.known_file(film)

    sample = tagging.audio_sample(info, 2, seconds=2)
    assert sample[:3] == b"ID3" or sample[0] == 0xFF  # an MP3
    lines = tagging.subtitle_lines(info, 3)
    assert lines == ["hello"]
    with pytest.raises(tagging.TagError):
        tagging.audio_sample(info, 0)  # the video isn't an audio track

    inode = os.stat(film).st_ino
    message = engine.tag(film, {"2": "ita"})
    assert message == "Tagged audio track 2 as Italian"
    assert tracks(film, "audio") == ["eng", "ita"]
    assert os.stat(film).st_ino == inode  # tagged in place, nothing copied
    # Now Dubarr knows what it is, it's an ordinary unwanted track
    assert engine.store.file(film)["status"] == plans.CHANGE
    assert engine.tag(film, {"2": "ita"}) == "Nothing to change"
    with pytest.raises(tagging.TagError):
        engine.tag(film, {"0": "eng"})
    with pytest.raises(tagging.TagError):
        engine.tag(film, {"2": "elvish"})


def test_mp4_with_timecode_track(library, engine):
    # Camera and QuickTime files carry a timecode data track that ffmpeg can't copy into MP4 (#17)
    plain = make_video(str(library / "plain.mp4"), audio=("eng", ""))
    film = str(library / "x.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", plain, "-map", "0", "-c", "copy",
                    "-timecode", "00:00:00:00", film], check=True)
    os.remove(plain)
    assert [s.type for s in probe(film).tracks][-1] == "other"
    engine.scan()
    assert engine.tag(film, {"2": "ita"}) == "Tagged audio track 2 as Italian"
    assert tracks(film, "audio") == ["eng", "ita"]

    from core.plan import Rules, decide
    from core.remux import remux
    info = probe(film)
    new = remux(info, decide(info, Rules(["eng"], ["eng"])))
    assert [t.language for t in new.of("audio")] == ["eng"]


@pytest.mark.parametrize("faststart", [False, True])
def test_mp4_tagged_in_place(library, engine, faststart):
    """Only the two bytes holding each language change, wherever the MP4 keeps its track list."""
    made = make_video(str(library / "made.mp4"), audio=("eng", "", ""), subtitles=("",))
    film = str(library / "x.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", made, "-map", "0", "-c", "copy"]
                   + (["-movflags", "+faststart"] if faststart else []) + [film], check=True)
    os.remove(made)
    with open(film, "rb") as f:
        before = f.read()
    engine.scan()
    engine.tag(film, {"2": "fra", "3": "jpn", "4": "eng"})
    assert tracks(film, "audio") == ["eng", "fra", "jpn"] and tracks(film, "subtitle") == ["eng"]
    with open(film, "rb") as f:
        after = f.read()
    assert len(after) == len(before)
    assert sum(a != b for a, b in zip(before, after)) <= 6
    assert engine.store.history()[0] == 0  # nothing to restore: nothing was copied


def test_mp4_copied_when_it_cant_be_tagged_in_place(library, engine, monkeypatch):
    film = make_video(str(library / "x.mp4"), audio=("eng", ""))
    engine.scan()
    monkeypatch.setattr(tagging, "tag_mp4", lambda info, changes: False)
    message = engine.tag(film, {"2": "ita"})
    assert tracks(film, "audio") == ["eng", "ita"]
    assert engine.store.history()[1][0]["message"] == message  # the original MP4 can be restored


def test_no_tagging_while_seeding(library, engine, tmp_path):
    film = make_video(str(library / "x.mkv"), audio=("",))
    os.link(film, tmp_path / "seeding.mkv")
    engine.scan()
    with pytest.raises(tagging.TagError, match="seeding"):
        engine.tag(film, {"1": "eng"})
    assert tracks(film, "audio") == ["und"]


def test_progress_estimate(engine):
    assert engine.progress() is None
    with engine.job("Changing files"):
        engine.activity.update(started=time.time() - 60, bytes_total=400, bytes_done=100, total=4, done=1)
        p = engine.progress()
    assert 175 <= p["eta_seconds"] <= 185  # a quarter done in a minute: three more minutes


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_duplicates_removed_and_forced_flagged(library, engine, ext, tmp_path):
    film = make_video(str(library / f"x{ext}"), audio=("eng", "eng"), subtitles=("eng", "eng"))
    info = probe(film)
    # Make the second subtitle look like a short forced track and the first like a full one
    from core.plan import Rules, decide
    for t, frames in zip(info.of("subtitle"), (900, 5)):
        t.frames = frames
    p = decide(info, Rules(["eng"], ["eng"]))
    assert p.mark_forced == [info.of("subtitle")[1].index]
    from core.remux import remux
    new = remux(info, p)
    assert [t.forced for t in new.of("subtitle")] == [False, True]
    assert len(new.of("audio")) == 1


def test_minimum_saving(library, engine, monkeypatch):
    from core.engine import TOO_SMALL, estimated_saving
    film = make_video(str(library / "x.mkv"), audio=("eng", "ita"))
    monkeypatch.setattr(config, "report_only", False)
    monkeypatch.setattr(config, "min_saving_mb", 10)
    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == TOO_SMALL
    assert "less than the 10.0 MB set" in row["message"]
    engine.apply("scheduled")
    engine.apply("manual", force=True)  # "Apply all" leaves them too
    assert tracks(film, "audio") == ["eng", "ita"]
    engine.apply("manual", paths=[film], force=True)  # chosen by hand
    assert tracks(film, "audio") == ["eng"]

    # An audio track of unknown size could be huge, so it isn't skipped
    plan = type("P", (), {"remove": [{"type": "audio", "size": 0}], "saved_bytes": 0})
    assert estimated_saving(plan) is None


def test_slider_setting():
    field = config.FIELDS_BY_KEY["min_saving_mb"]
    assert config.coerce(field, "50") == 50
    with pytest.raises(ValueError):
        config.coerce(field, 51)


def test_subtitle_files_beside_films(library, engine, tmp_path):
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng", "ita"))
    french = library / "Film" / "Film.fr.srt"
    english = library / "Film" / "Film.en.srt"
    french.write_text("1\n00:00:01,000 --> 00:00:02,000\nBonjour\n")
    english.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    os.link(film, tmp_path / "seeding.mkv")  # the film itself has to wait

    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == plans.CHANGE
    assert row["message"].startswith("Remove 1 French subtitle file. The rest waits")
    engine.apply("manual", force=True)
    assert not french.exists() and english.exists()
    assert tracks(film, "audio") == ["eng", "ita"]  # untouched while seeding
    assert engine.store.file(film)["status"] == SEEDING

    # Restoring the file puts it back, and the film is then left alone
    entry = engine.store.history()[1][0]
    assert entry["video"] == film and entry["state"] == "done"
    engine.restore(entry["id"])
    assert french.read_text().endswith("Bonjour\n")
    assert engine.store.file(film)["ignored"]


def test_several_workers(library, engine, monkeypatch):
    monkeypatch.setattr(config, "workers", 3)
    monkeypatch.setattr(config, "low_priority", True)
    monkeypatch.setattr(config, "ffmpeg_threads", 1)
    films = [make_video(str(library / f"{n}{'.mp4' if n % 2 else '.mkv'}"), audio=("eng", "ita")) for n in range(5)]
    engine.scan()
    engine.apply("manual", force=True)
    for film in films:
        assert tracks(film, "audio") == ["eng"]
    assert engine.store.runs()[0]["changed"] == 5


def test_recheck_of_a_deleted_file_removes_it(library, engine):
    broken = library / "gone.mp4"
    broken.write_bytes(b"\0\0\0 ftypmp42 cut short")
    engine.scan()
    assert engine.store.file(str(broken))["status"] == ERROR
    broken.unlink()
    engine.scan("manual", [str(broken)])  # what Re-check does
    assert engine.store.file(str(broken)) is None


def test_apply_all_in_one_library(library, engine, tmp_path, monkeypatch):
    anime = tmp_path / "anime"
    monkeypatch.setattr(config, "library_list", config.library_list + [
        {"path": str(anime), "audio": ["eng"], "subtitles": ["eng"]}])
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng", "fre"))
    show = make_video(str(anime / "Show" / "Show.mkv"), audio=("eng", "fre"))
    engine.scan()
    total, rows = engine.store.files(statuses=[plans.CHANGE], library=str(anime))
    assert total == 1 and rows[0]["path"] == show

    engine.apply("manual", force=True, library=str(anime))
    assert tracks(show, "audio") == ["eng"]
    assert tracks(film, "audio") == ["eng", "fra"]  # the other library is left for now


def test_remove_tracks_by_hand(library, engine):
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng", "fre"), subtitles=("eng", "fre"))
    srt = library / "Film" / "Film.en.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    engine.scan()
    with pytest.raises(ValueError, match="audio track has to stay"):
        engine.plan_by_hand(film, [1, 2], [])
    with pytest.raises(ValueError, match="isn't an audio or subtitle"):
        engine.plan_by_hand(film, [0], [])
    with pytest.raises(ValueError, match="Tick at least one"):
        engine.plan_by_hand(film, [], [])

    # English audio and the English subtitle track go, though they're languages you keep
    engine.remove_by_hand(film, [1, 3], [str(srt)])
    assert tracks(film, "audio") == ["fra"] and tracks(film, "subtitle") == ["fra"]
    assert not srt.exists()
    _, entries = engine.store.history(limit=10)
    assert sorted(e["message"] for e in entries) == ["Removed a subtitle file beside the film by hand",
                                                     "Removed by hand: English audio, English subtitles and 1 subtitle file"]


ENGLISH_SRT = ("1\n00:00:00,500 --> 00:00:01,500\nWhere were you last night? I waited for hours.\n\n"
               "2\n00:00:01,600 --> 00:00:02,500\nI'm sorry, the train was late and my phone died.\n")


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_subtitle_files_put_into_the_film(library, engine, monkeypatch, ext):
    monkeypatch.setattr(config, "mux_subtitle_files", True)
    film = make_video(str(library / "Film" / f"Film{ext}"), audio=("eng",))
    named = library / "Film" / "Film.en.srt"
    named.write_text(ENGLISH_SRT)
    unnamed = library / "Film" / "Film.srt"  # no language in the name: read from the text
    unnamed.write_text(ENGLISH_SRT.replace("Where", "Who").encode("cp1252").decode("cp1252"), encoding="cp1252")
    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == plans.CHANGE
    assert "Put the English subtitle file beside it into the film" in row["message"]
    engine.apply("manual", force=True)
    assert tracks(film, "subtitle") == ["eng"]  # one English track: the second file isn't needed
    assert not named.exists()
    assert unnamed.exists()
    _, entries = engine.store.history()
    assert any("Put the English subtitle file into the film" in e["message"] for e in entries)
    assert engine.store.file(film)["status"] == plans.UNKNOWN_SUBTITLES  # the unnamed file left beside it


def test_subtitle_files_left_beside_the_film_by_default(library, engine):
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng",))
    (library / "Film" / "Film.en.srt").write_text(ENGLISH_SRT)
    engine.scan()
    assert engine.store.file(film)["status"] == plans.CLEAN


def test_subtitle_file_language_read_from_its_text(library, engine, monkeypatch):
    """A language read from the text is a guess: the track goes in untagged, with it as the suggestion."""
    monkeypatch.setattr(config, "mux_subtitle_files", True)
    monkeypatch.setattr(config, "auto_tag_confidence", 0)
    film = make_video(str(library / "Café" / "Café.mkv"), audio=("eng",))
    (library / "Café" / "Café.srt").write_bytes(ENGLISH_SRT.replace("hours", "hours at the café").encode("cp1252"))
    engine.scan()
    engine.apply("manual", force=True)
    assert tracks(film, "subtitle") == ["und"]
    assert "café" in "\n".join(tagging.subtitle_lines(probe(film), 2))  # Windows-1252 read correctly
    row = engine.store.file(film)
    assert row["status"] == plans.UNKNOWN_SUBTITLES
    assert {i: (g["language"], g["by"]) for i, g in engine.guesses(row).items()} == {2: ("eng", "Dubarr")}
    _, entries = engine.store.history()
    assert any("untagged, as its language is a guess" in e["message"] for e in entries)


def test_a_guessed_subtitle_file_is_tagged_when_tag_automatically_allows(library, engine, monkeypatch):
    monkeypatch.setattr(config, "mux_subtitle_files", True)
    monkeypatch.setattr(config, "auto_tag_confidence", 50)
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng",))
    (library / "Film" / "Film.srt").write_text(ENGLISH_SRT)
    engine.scan()
    engine.apply("manual", force=True)
    assert tracks(film, "subtitle") == ["eng"]


def test_subtitle_files_put_in_by_hand_from_any_list(library, engine, monkeypatch):
    """With the setting off, or on a list such as Untagged audio, the button puts them in."""
    monkeypatch.setattr(config, "mux_subtitle_files", False)
    monkeypatch.setattr(config, "auto_tag_confidence", 0)
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("",))
    named = library / "Film" / "Film.en.srt"
    named.write_text(ENGLISH_SRT)
    (library / "Film" / "Film.fr.srt").write_text(ENGLISH_SRT)  # French isn't kept, so it stays out
    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == plans.UNKNOWN_AUDIO
    assert row["plan"]["can_add"] == [str(named)]
    assert engine.add_subtitle_files(film) == "Put 1 subtitle file into the film"
    assert tracks(film, "subtitle") == ["eng"] and not named.exists()
    row = engine.store.file(film)
    assert row["status"] == plans.UNKNOWN_AUDIO and not row["plan"]["can_add"]
    assert engine.add_subtitle_files(film) == "No subtitle files to put in"


def test_unnamed_subtitle_files_are_never_removed_for_their_guess(library, engine, monkeypatch):
    monkeypatch.setattr(config, "strip_subtitle_files", True)
    make_video(str(library / "Film" / "Film.mkv"), audio=("eng",))
    unnamed = library / "Film" / "Film.srt"
    unnamed.write_text("1\n00:00:00,500 --> 00:00:01,500\nOù étais-tu hier soir ? Je t'ai attendu pendant des heures.\n")
    engine.scan()
    engine.apply("manual", force=True)
    assert unnamed.exists()


def test_progress_counts_the_copy_being_written(library, engine, monkeypatch):
    """Removing tracks from one big film used to show "0 file(s) so far" until it was done."""
    import core.engine as engine_module
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng", "fre"))
    engine.scan()
    real_remux = engine_module.remux
    seen = []

    def remux_watched(info, plan):
        # Half of the copy written so far
        with open(temp_path(info.path), "wb") as f:
            f.write(b"\0" * ((info.size - plan.saved_bytes) // 2))
        seen.append(engine.progress())
        return real_remux(info, plan)

    monkeypatch.setattr(engine_module, "remux", remux_watched)
    engine.remove_by_hand(film, [2], [])
    a = seen[0]
    assert a["bytes_total"] > 0
    assert 0.4 < a["bytes_done"] / a["bytes_total"] < 0.6
    assert a["eta_seconds"] is not None
    assert not engine._writing


def test_picture_subtitles_show_as_stills(library, engine):
    from core.tagging import subtitle_stills
    film = make_pgs_video(str(library / "Film" / "Film.mkv"))
    info = probe(film)
    pgs = info.of("subtitle")[0]
    assert pgs.codec == "hdmv_pgs_subtitle"
    stills = subtitle_stills(info, pgs.index)
    assert len(stills) == 2 and all(s[:2] == b"\xff\xd8" for s in stills)  # JPEGs
    with pytest.raises(tagging.TagError, match="pictures"):
        tagging.subtitle_lines(info, pgs.index)


def test_naming_a_subtitle_file_settles_its_language(library, engine):
    """An unnamed file keeps the film under Untagged subtitles until its language is chosen, which renames it."""
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("eng",))
    unnamed = library / "Film" / "Film.srt"
    unnamed.write_text(ENGLISH_SRT)
    (library / "Film" / "Film.forced.srt").write_text(ENGLISH_SRT)  # "forced" isn't a language
    engine.scan()
    row = engine.store.file(film)
    assert row["status"] == plans.UNKNOWN_SUBTITLES
    assert "2 subtitle files beside it don't say their language" in row["message"]
    guessed = {f["path"]: f.get("guess") for f in row["info"]["external"]}
    assert guessed[str(unnamed)] == "eng"
    with pytest.raises(tagging.TagError, match="isn't a subtitle file beside"):
        engine.files_wanted(film, {str(library / "Other.srt"): "eng"})
    engine.tag(film, {}, files={str(unnamed): "eng", str(library / "Film" / "Film.forced.srt"): "fra"})
    assert sorted(os.listdir(library / "Film")) == ["Film.en.srt", "Film.fr.forced.srt", "Film.mkv"]
    assert engine.store.file(film)["status"] == plans.CHANGE  # French isn't kept, so that file can go now
    with pytest.raises(tagging.TagError, match="already a Film.en.srt"):
        (library / "Film" / "Film.srt").write_text(ENGLISH_SRT)
        engine.files_wanted(film, {str(library / "Film" / "Film.srt"): "eng"})


def test_put_in_any_subtitle_file_you_choose_and_suggestions_survive(library, engine, monkeypatch):
    """Put in from the panel takes the file you pick, even in a language the film has already. Suggestions for
    the film's own tracks carry over to the new copy."""
    monkeypatch.setattr(config, "auto_tag_confidence", 0)
    film = make_video(str(library / "Film" / "Film.mkv"), audio=("", "eng"), subtitles=("eng",))
    named = library / "Film" / "Film.en.srt"
    named.write_text(ENGLISH_SRT)
    engine.scan()
    row = engine.store.file(film)
    engine.store.save_guess(film, 1, row["size"], row["mtime"], "ita", 0.9, "Whisper")
    assert engine.add_subtitle_files(film, files=[str(named)]) == "Put 1 subtitle file into the film"
    assert tracks(film, "subtitle") == ["eng", "eng"] and not named.exists()
    row = engine.store.file(film)
    assert {i: g["language"] for i, g in engine.guesses(row).items()} == {1: "ita"}
