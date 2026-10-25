"""Write a copy of a file without the unwanted tracks, then check the copy.

Nothing is re-encoded: the video and the tracks that stay are copied bit for bit.
MKV files go through mkvmerge, which keeps everything Matroska can hold (Dolby
Vision, fonts, chapters, tags). MP4 files go through ffmpeg.
"""

import json
import os
import subprocess

from core.plan import Plan
from core.probe import MediaInfo, ProbeError, probe
from utils.languages import bibliographic
from utils.proc import ffmpeg_threads, wrap

MKV = (".mkv", ".mk3d")
MP4 = (".mp4", ".m4v")
SUPPORTED = MKV + MP4

MKVMERGE_TYPES = {"video": "video", "audio": "audio", "subtitles": "subtitle"}


class RemuxError(Exception):
    pass


def supported(path: str) -> bool:
    return path.lower().endswith(SUPPORTED)


def temp_path(path: str) -> str:
    """Next to the original, so the swap is an instant rename on the same disk.
    Hidden, with an extension media servers ignore."""
    folder, name = os.path.split(path)
    return os.path.join(folder, f".{name}.dubarr.tmp")


def _run(command, timeout):
    try:
        return subprocess.run(wrap(command), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemuxError(f"{command[0]} took longer than {timeout // 60} minutes")
    except FileNotFoundError:
        raise RemuxError(f"{command[0]} isn't installed")


def _last_line(text, fallback):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else fallback


def charset(path: str):
    """A subtitle file's character set when it isn't UTF-8: older ones are often Windows-1252."""
    try:
        with open(path, "rb") as f:
            f.read().decode("utf-8")
        return None
    except UnicodeDecodeError:
        return "cp1252"
    except OSError:
        return None


def mkvmerge_ids(path: str, timeout: int = 120) -> dict:
    """track id -> type, as mkvmerge sees the file."""
    result = _run(["mkvmerge", "-J", path], timeout)
    if result.returncode == 2:
        raise RemuxError(_last_line(result.stdout or result.stderr, "mkvmerge couldn't read the file"))
    data = json.loads(result.stdout)
    return {t["id"]: MKVMERGE_TYPES.get(t["type"], "other") for t in data.get("tracks", [])}


def check_ids(info: MediaInfo, ids: dict):
    """ffprobe and mkvmerge both number tracks in file order. Make sure they agree
    before trusting ffprobe's numbers to tell mkvmerge what to drop."""
    for t in info.tracks:
        if t.type in ("audio", "subtitle", "video") and ids.get(t.index) != t.type:
            raise RemuxError(f"Track {t.index} is {t.type} to ffprobe but {ids.get(t.index, 'missing')} to mkvmerge, so it's left alone")


def mkvmerge_command(source: str, target: str, info: MediaInfo, plan: Plan) -> list:
    kept = set(plan.keep)
    audio = [t.index for t in info.of("audio") if t.index in kept]
    subs = [t.index for t in info.of("subtitle") if t.index in kept]
    command = ["mkvmerge", "--quiet", "-o", target]
    command += ["--audio-tracks", ",".join(map(str, audio))]
    command += ["--subtitle-tracks", ",".join(map(str, subs))] if subs else ["--no-subtitles"]
    if plan.default_audio is not None:
        for index in audio:
            command += ["--default-track-flag", f"{index}:{1 if index == plan.default_audio else 0}"]
    for index in plan.mark_forced:
        command += ["--forced-display-flag", f"{index}:1"]
    command += [source]
    # Subtitle files beside the film go in after its own tracks
    for f in plan.add_files:
        command += ["--language", f"0:{bibliographic(f['language'])}"]
        if f.get("forced"):
            command += ["--forced-display-flag", "0:1"]
        if f.get("hearing_impaired"):
            command += ["--track-name", "0:SDH"]
        if charset(f["path"]):
            command += ["--sub-charset", f"0:{charset(f['path'])}"]
        command.append(f["path"])
    return command


def ffmpeg_command(source: str, target: str, info: MediaInfo, plan: Plan) -> list:
    removed = {r["index"] for r in plan.remove}
    # Data streams (timecode, chapter text) can't be copied into MP4. ffmpeg rebuilds them from the metadata
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source]
    for f in plan.add_files:
        if charset(f["path"]):
            command += ["-sub_charenc", charset(f["path"])]
        command += ["-i", f["path"]]
    command += ["-map", "0", "-map", "-0:d"]
    for index in sorted(removed):
        command += ["-map", f"-0:{index}"]
    for n, _ in enumerate(plan.add_files, start=1):
        command += ["-map", f"{n}:0"]
    command += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"]
    if plan.default_audio is not None:
        kept_audio = [t.index for t in info.of("audio") if t.index not in removed]
        for position, index in enumerate(kept_audio):
            command += [f"-disposition:a:{position}", "default" if index == plan.default_audio else "0"]
    kept_subs = [t.index for t in info.of("subtitle") if t.index not in removed]
    for position, index in enumerate(kept_subs):
        if index in plan.mark_forced:
            command += [f"-disposition:s:{position}", "+forced"]
    # MP4 holds text subtitles as mov_text, so files beside the film are converted (text only, nothing re-encoded)
    for n, f in enumerate(plan.add_files):
        position = len(kept_subs) + n
        command += [f"-c:s:{position}", "mov_text", f"-metadata:s:s:{position}", f"language={bibliographic(f['language'])}",
                    f"-disposition:s:{position}", "forced" if f.get("forced") else "0"]
    command += ffmpeg_threads() + ["-movflags", "+faststart", "-f", "mp4"]
    return command + [target]


def verify(original: MediaInfo, new: MediaInfo, plan: Plan):
    """Raise RemuxError unless the new file is the old one minus exactly the planned tracks."""
    kept = set(plan.keep)

    def summary(tracks):
        return [(t.type, t.codec, t.language) for t in tracks]

    for kind in ("video", "audio", "subtitle"):
        expected = summary([t for t in original.of(kind) if t.index in kept])
        got = summary(new.of(kind))
        if kind == "subtitle" and plan.add_files:
            # Files put into the film come after its own tracks, in their languages
            added = [t.language for t in new.of(kind)[len(expected):]]
            if added != [f["language"] for f in plan.add_files]:
                raise RemuxError("The subtitle files didn't go into the new file as planned")
            got = got[:len(expected)]
        if expected != got:
            raise RemuxError(f"The new file's {kind} tracks aren't what was planned "
                             f"(expected {len(expected)}, found {len(got)})")

    if original.duration and new.duration:
        allowed = max(2.0, original.duration * 0.005)
        if abs(original.duration - new.duration) > allowed:
            raise RemuxError(f"The new file is {new.duration:.0f}s long but the original is {original.duration:.0f}s")

    if new.size <= 0:
        raise RemuxError("The new file is empty")
    if plan.saved_bytes:
        expected_size = original.size - plan.saved_bytes
        if abs(new.size - expected_size) > max(expected_size * 0.1, 50_000_000):
            raise RemuxError(f"The new file is {new.size:,} bytes, expected about {expected_size:,}")
    elif new.size < original.size * 0.2:
        raise RemuxError("The new file is far smaller than expected")


def remux(info: MediaInfo, plan: Plan, timeout: int = 6 * 3600) -> MediaInfo:
    """Write the cleaned copy to temp_path() and check it. Returns the new file's info.
    The original is never touched here. On any problem the copy is deleted."""
    source = info.path
    target = temp_path(source)
    try:
        if source.lower().endswith(MKV):
            check_ids(info, mkvmerge_ids(source))
            command = mkvmerge_command(source, target, info, plan)
        elif source.lower().endswith(MP4):
            command = ffmpeg_command(source, target, info, plan)
        else:
            raise RemuxError("Only MKV and MP4 files are supported")

        result = _run(command, timeout)
        # mkvmerge exits with 1 for warnings, which are fine; 2 is an error
        failed = result.returncode >= 2 if command[0] == "mkvmerge" else result.returncode != 0
        if failed or not os.path.exists(target):
            raise RemuxError(_last_line(result.stderr or result.stdout, f"{command[0]} failed"))

        try:
            new = probe(target)
        except ProbeError as e:
            raise RemuxError(f"Couldn't read the new file: {e}")
        verify(info, new, plan)
        return new
    except BaseException:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
