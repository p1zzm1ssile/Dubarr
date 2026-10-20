"""Listen to a track, read a few subtitle lines, and set a track's language tag.

MKV files are tagged in place with mkvpropedit: only the header changes, it
takes a moment and nothing is copied. MP4 files have no in-place editor, so
they get a quick copy with the new tag, checked and swapped like any change.
"""

import json
import os
import re
import subprocess

from core.probe import MediaInfo, ProbeError, probe
from core.remux import check_ids, mkvmerge_ids, temp_path
from utils.languages import LANGUAGES, NO_LANGUAGE, bibliographic, name
from utils.proc import ffmpeg_threads, wrap

# Subtitles that are text, so they can be shown. Picture-based ones (PGS, VobSub) can't.
TEXT_SUBTITLES = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "tx3g"}
TIMESTAMP = re.compile(r"^\d\d:\d\d:\d\d[,.]\d+ --> ")
MARKUP = re.compile(r"<[^>]+>|\{[^}]*\}")


class TagError(Exception):
    pass


def valid_tag(code: str) -> bool:
    return code in LANGUAGES or code == NO_LANGUAGE


def _run(command, timeout, binary=False):
    try:
        result = subprocess.run(wrap(command), capture_output=True, timeout=timeout, text=not binary)
    except subprocess.TimeoutExpired:
        raise TagError(f"{command[0]} took too long")
    except FileNotFoundError:
        raise TagError(f"{command[0]} isn't installed")
    return result


def _track(info: MediaInfo, index: int, kind=None):
    for t in info.tracks:
        if t.index == index and t.type in ("audio", "subtitle") and (kind is None or t.type == kind):
            return t
    raise TagError(f"Track {index} isn't an audio or subtitle track in this file")


# Where samples come from, as a share of the film. The first is about a quarter in, where people are usually
# talking; asking for another part moves on through these, for a sample that happened to have no speech.
SAMPLE_POSITIONS = (0.25, 0.4, 0.55, 0.7, 0.1, 0.85)


def sample_start(info: MediaInfo, seconds: int = 30, part: int = 0) -> int:
    """The second a sample starts at. Short files always start at the beginning."""
    if info.duration <= seconds * 3:
        return 0
    return int(info.duration * SAMPLE_POSITIONS[part % len(SAMPLE_POSITIONS)])


def audio_sample(info: MediaInfo, index: int, seconds: int = 30, part: int = 0) -> bytes:
    """A short MP3 of one audio track. part 0 is from about a quarter of the way in; later parts are elsewhere."""
    _track(info, index, "audio")
    start = sample_start(info, seconds, part)
    result = _run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(start), "-i", info.path, "-map", f"0:{index}",
                   "-t", str(seconds), "-vn", *ffmpeg_threads(), "-ac", "2", "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1"],
                  timeout=120, binary=True)
    if result.returncode != 0 or not result.stdout:
        errors = (result.stderr or b"").decode(errors="replace").strip().splitlines()
        raise TagError(errors[-1] if errors else "Couldn't make a sample")
    return result.stdout


def subtitle_lines(info: MediaInfo, index: int, limit: int = 12) -> list:
    """A few lines of dialogue from a text subtitle track."""
    track = _track(info, index, "subtitle")
    if track.codec not in TEXT_SUBTITLES:
        raise TagError("This subtitle track is made of pictures (like Blu-ray PGS), so it can't be shown as text. "
                       "Play the film with this track on to check it")
    # Look from a quarter of the way in first, then from the start. Only part of the file is read.
    starts = [int(info.duration * 0.25), 0] if info.duration > 1200 else [0]
    for start in starts:
        result = _run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(start), "-i", info.path, "-map", f"0:{index}",
                       "-t", "900", "-f", "srt", "pipe:1"], timeout=120)
        lines = []
        for line in (result.stdout or "").splitlines():
            line = MARKUP.sub("", line).strip()
            if not line or line.isdigit() or TIMESTAMP.match(line):
                continue
            lines.append(line)
            if len(lines) >= limit:
                break
        if lines:
            return lines
    raise TagError("No lines found in this subtitle track")


def describe_changes(info: MediaInfo, changes: dict) -> str:
    parts = []
    for index, code in sorted(changes.items()):
        t = _track(info, index)
        kind = "audio" if t.type == "audio" else "subtitles"
        parts.append(f"{kind} track {index} as {name(code)}")
    return "Tagged " + ", ".join(parts)


def tag_mkv(info: MediaInfo, changes: dict):
    """Set the language of MKV tracks in place."""
    check_ids(info, mkvmerge_ids(info.path))
    result = _run(["mkvmerge", "-J", info.path], timeout=120)
    numbers = {t["id"]: t["properties"].get("number") for t in json.loads(result.stdout).get("tracks", [])}
    command = ["mkvpropedit", "--quiet", info.path]
    for index, code in sorted(changes.items()):
        if not numbers.get(index):
            raise TagError(f"mkvmerge can't find track {index}")
        command += ["--edit", f"track:@{numbers[index]}", "--set", f"language={bibliographic(code)}"]
    result = _run(command, timeout=300)
    if result.returncode >= 2:
        raise TagError((result.stdout or result.stderr or "mkvpropedit failed").strip().splitlines()[-1])


def retag_mp4(info: MediaInfo, changes: dict) -> MediaInfo:
    """Write a copy of an MP4 with new language tags to temp_path(), and check it. Returns its info."""
    target = temp_path(info.path)
    # Data streams (timecode, chapter text) can't be copied into MP4. ffmpeg rebuilds them from the metadata
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", info.path, "-map", "0", "-map", "-0:d", "-c", "copy",
               "-map_metadata", "0", "-map_chapters", "0"]
    for index, code in sorted(changes.items()):
        command += [f"-metadata:s:{index}", f"language={bibliographic(code)}"]
    command += ffmpeg_threads() + ["-movflags", "+faststart", "-f", "mp4", target]
    try:
        result = _run(command, timeout=6 * 3600)
        if result.returncode != 0 or not os.path.exists(target):
            raise TagError((result.stderr or "ffmpeg failed").strip().splitlines()[-1])
        try:
            new = probe(target)
        except ProbeError as e:
            raise TagError(f"Couldn't read the new file: {e}")
        expected = [(t.type, t.codec, changes.get(t.index, t.language)) for t in info.tracks if t.type != "other"]
        got = [(t.type, t.codec, t.language) for t in new.tracks if t.type != "other"]
        if expected != got:
            raise TagError("The new file's tracks aren't what was expected, so the original is left as it was")
        if info.duration and abs(info.duration - new.duration) > max(2.0, info.duration * 0.005):
            raise TagError("The new file's length doesn't match the original")
        return new
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise

