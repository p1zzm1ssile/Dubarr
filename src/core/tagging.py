"""Listen to a track, read a few subtitle lines, and set a track's language tag.

MKV files are tagged in place with mkvpropedit: only the header changes, it
takes a moment and nothing is copied. MP4 files are tagged in place too, by
rewriting the two bytes that hold each track's language. When an MP4 is laid
out in a way that can't be done safely, it gets a copy with the new tag
instead, checked and swapped like any change.
"""

import json
import os
import re
import struct
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


# MP4 boxes holding a track's language: moov > trak > mdia > mdhd, with hdlr saying what kind of track it is
AUDIO_HANDLERS = {b"soun"}
SUBTITLE_HANDLERS = {b"sbtl", b"subt", b"text", b"clcp"}


def _boxes(f, start, end):
    """(type, start, header length, end) of each box between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise TagError("The MP4's layout couldn't be read")
        yield kind, pos, header, pos + size
        pos += size


def _child(f, parent, kind):
    _, start, header, end = parent
    return next((b for b in _boxes(f, start + header, end) if b[0] == kind), None)


def _language_fields(f, size):
    """Where each audio and subtitle track's language is stored, in track order, as (offset, kind).
    None if the file isn't laid out in a way this can handle."""
    moov = next((b for b in _boxes(f, 0, size) if b[0] == b"moov"), None)
    if not moov:
        return None
    fields = []
    for trak in (b for b in _boxes(f, moov[1] + moov[2], moov[3]) if b[0] == b"trak"):
        mdia = _child(f, trak, b"mdia")
        if not mdia:
            return None
        hdlr, mdhd = _child(f, mdia, b"hdlr"), _child(f, mdia, b"mdhd")
        if not hdlr or not mdhd or _child(f, mdia, b"elng"):
            return None  # an extended language tag would be read instead of the one changed here
        f.seek(hdlr[1] + hdlr[2] + 8)
        handler = f.read(4)
        kind = "audio" if handler in AUDIO_HANDLERS else "subtitle" if handler in SUBTITLE_HANDLERS else None
        if not kind:
            continue
        f.seek(mdhd[1] + mdhd[2])
        version = f.read(1)[0]
        offset = mdhd[1] + mdhd[2] + (32 if version == 1 else 20)
        if offset + 2 > mdhd[3]:
            return None
        fields.append((offset, kind))
    return fields


def _packed(code: str) -> bytes:
    code = bibliographic(code)
    if len(code) != 3 or not code.isalpha() or not code.islower():
        raise ValueError(code)
    value = 0
    for c in code:
        value = (value << 5) | (ord(c) - 0x60)
    return struct.pack(">H", value)


def tag_mp4(info: MediaInfo, changes: dict) -> bool:
    """Set the language of MP4 tracks in place, in a moment. Returns False, having changed nothing, when the
    file can't be done that way and needs retag_mp4's copy instead."""
    tracks = [t for t in info.tracks if t.type in ("audio", "subtitle")]
    try:
        values = {index: _packed(code) for index, code in changes.items()}
    except ValueError:
        return False
    with open(info.path, "r+b") as f:
        try:
            fields = _language_fields(f, os.fstat(f.fileno()).st_size)
        except (TagError, struct.error, IndexError):
            return False
        # Matched up in order, so it only goes ahead when the tracks line up exactly with what ffprobe saw
        if fields is None or [kind for _, kind in fields] != [t.type for t in tracks]:
            return False
        offsets = {t.index: offset for t, (offset, _) in zip(tracks, fields)}
        old = {}
        for index in values:
            f.seek(offsets[index])
            old[index] = f.read(2)
            if struct.unpack(">H", old[index])[0] < 0x400:
                return False  # an old QuickTime language number, not a code
        for index, value in values.items():
            f.seek(offsets[index])
            f.write(value)
        f.flush()
        os.fsync(f.fileno())
        try:
            new = probe(info.path)
            got = {t.index: t.language for t in new.tracks}
            ok = all(got.get(t.index) == changes.get(t.index, t.language) for t in tracks)
        except ProbeError:
            ok = False
        if not ok:
            for index, value in old.items():
                f.seek(offsets[index])
                f.write(value)
            f.flush()
            os.fsync(f.fileno())
            return False
    return True


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

