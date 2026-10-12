"""Read the tracks inside a video file with ffprobe."""

import json
import subprocess
from dataclasses import asdict, dataclass, field

from utils.languages import normalise

# Raise when probe() starts recording something new, so cached results are read again
PROBE_VERSION = 2

TYPES = {"video": "video", "audio": "audio", "subtitle": "subtitle"}


@dataclass
class Track:
    index: int          # position in the file, the same number ffmpeg and mkvmerge use
    type: str           # video, audio, subtitle or other (fonts, chapters and data)
    codec: str = ""
    language: str = "und"
    raw_language: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    commentary: bool = False
    channels: int = 0
    size: int = 0       # bytes, when the file records it (mkvmerge writes this for every track)

    def to_dict(self):
        return asdict(self)


@dataclass
class MediaInfo:
    path: str
    container: str
    duration: float
    size: int
    tracks: list = field(default_factory=list)
    version: int = PROBE_VERSION

    def of(self, kind):
        return [t for t in self.tracks if t.type == kind]

    def to_dict(self):
        return {**asdict(self), "tracks": [t.to_dict() for t in self.tracks]}

    @classmethod
    def from_dict(cls, data):
        return cls(**{"version": 0, **data, "tracks": [Track(**t) for t in data.get("tracks", [])]})


class ProbeError(Exception):
    pass


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _estimate(stream, tags):
    """MP4 files don't record each track's size, but bitrate times length is close."""
    try:
        bitrate = float(stream.get("bit_rate") or tags.get("bps") or 0)
        duration = float(stream.get("duration") or 0)
    except ValueError:
        return 0
    return int(bitrate * duration / 8)


def parse(path: str, data: dict) -> MediaInfo:
    """Turn ffprobe's JSON into a MediaInfo. Separate from probe() so it can be tested."""
    fmt = data.get("format") or {}
    tracks = []
    for s in data.get("streams") or []:
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disposition = s.get("disposition") or {}
        kind = TYPES.get(s.get("codec_type"), "other")
        # Cover art is stored as a video stream, but it isn't the film
        if kind == "video" and disposition.get("attached_pic"):
            kind = "other"
        raw = tags.get("language", "")
        title = tags.get("title", "")
        tracks.append(Track(
            index=s["index"],
            type=kind,
            codec=s.get("codec_name") or tags.get("mimetype", ""),
            language=normalise(raw),
            raw_language=raw,
            title=title,
            default=bool(disposition.get("default")),
            forced=bool(disposition.get("forced")) or "forced" in title.lower(),
            hearing_impaired=bool(disposition.get("hearing_impaired")) or "sdh" in title.lower().split(),
            commentary=bool(disposition.get("comment")) or "commentary" in title.lower(),
            channels=_int(s.get("channels")),
            size=_int(tags.get("number_of_bytes") or tags.get("number_of_bytes-eng")) or _estimate(s, tags),
        ))
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    container = (fmt.get("format_name") or "").split(",")[0]
    return MediaInfo(path=path, container=container, duration=duration, size=_int(fmt.get("size")), tracks=tracks)


def probe(path: str, timeout: int = 120) -> MediaInfo:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe took too long to read the file")
    except FileNotFoundError:
        raise ProbeError("ffprobe isn't installed")
    if result.returncode != 0:
        raise ProbeError((result.stderr or "ffprobe couldn't read the file").strip().splitlines()[-1])
    try:
        return parse(path, json.loads(result.stdout))
    except (ValueError, KeyError) as e:
        raise ProbeError(f"Couldn't understand ffprobe's output ({e})")
