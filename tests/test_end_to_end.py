"""Real files through the whole cycle: scan, change, check, restore, recycle."""

import os
import time

import pytest

import utils.config as config
from conftest import make_video, needs_tools
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
    if ext == ".mkv":
        assert os.stat(film).st_ino == inode  # tagged in place, nothing copied
    else:
        assert engine.store.history()[1][0]["message"] == message  # the original MP4 can be restored
    # Now Dubarr knows what it is, it's an ordinary unwanted track
    assert engine.store.file(film)["status"] == plans.CHANGE
    assert engine.tag(film, {"2": "ita"}) == "Nothing to change"
    with pytest.raises(tagging.TagError):
        engine.tag(film, {"0": "eng"})
    with pytest.raises(tagging.TagError):
        engine.tag(film, {"2": "elvish"})


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
