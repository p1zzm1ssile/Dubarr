import os
import shutil
import subprocess
import sys
import tempfile

import pytest

# Settings, logs and the database go to a throwaway folder
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="dubarr-test-config-")
os.environ["LOG_OUTPUT"] = "file"
os.environ["LIBRARIES"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "mkvmerge"))
needs_tools = pytest.mark.skipif(not TOOLS, reason="needs ffmpeg and mkvtoolnix")


def make_video(path, audio=("eng",), subtitles=(), default_audio=0, forced=()):
    """A tiny real video with an audio track per language, and subtitle tracks."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(dir=folder, prefix=".work-")
    inputs = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=24"]
    for n, _ in enumerate(audio):
        inputs += ["-f", "lavfi", "-i", f"sine=f={300 + 100 * n}"]
    maps = ["-map", "0:v"] + [x for n in range(len(audio)) for x in ("-map", f"{n + 1}:a")]
    meta = []
    for n, lang in enumerate(audio):
        if lang:
            meta += [f"-metadata:s:a:{n}", f"language={lang}"]
        meta += [f"-disposition:a:{n}", "default" if n == default_audio else "0"]
    base = os.path.join(work, "base" + (".mp4" if path.endswith(".mp4") else ".mkv"))
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-t", "3", *maps, "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", *meta, base], check=True)
    if path.endswith(".mp4"):
        if subtitles:
            srt = os.path.join(work, "s.srt")
            with open(srt, "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
            args = ["ffmpeg", "-v", "error", "-y", "-i", base]
            for _ in subtitles:
                args += ["-i", srt]
            args += ["-map", "0"] + [x for n in range(len(subtitles)) for x in ("-map", f"{n + 1}:s")]
            args += ["-c", "copy", "-c:s", "mov_text"]
            for n, lang in enumerate(subtitles):
                args += [f"-metadata:s:s:{n}", f"language={lang}"]
            subprocess.run(args + [path], check=True)
        else:
            shutil.move(base, path)
    else:
        args = ["mkvmerge", "-q", "-o", path, base]
        srt = os.path.join(work, "s.srt")
        with open(srt, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
        for n, lang in enumerate(subtitles):
            args += ["--language", f"0:{lang or 'und'}", "--forced-display-flag", f"0:{1 if n in forced else 0}", srt]
        subprocess.run(args, check=True)
    shutil.rmtree(work)
    return path


@pytest.fixture(autouse=True)
def quick_background(monkeypatch):
    """No waiting for a quiet minute before telling the other apps, or resting between files, in tests."""
    import core.engine
    monkeypatch.setattr(core.engine, "NOTIFY_QUIET_SECONDS", 0.05)
    monkeypatch.setattr(core.engine, "DETECT_PAUSE_SECONDS", 0)


def make_pgs_video(path, events=((1, 3), (4, 5.5))):
    """A small film with a Blu-ray style picture subtitle track (PGS), each subtitle a white box."""
    import struct

    def segment(kind, pts, data):
        return b"PG" + struct.pack(">IIBH", pts, pts, kind, len(data)) + data

    w, h, bw, bh = 320, 180, 100, 20
    x, y = (w - bw) // 2, h - bh - 10
    sup = b""
    for n, (start, end) in enumerate(events):
        p, q = int(start * 90000), int(end * 90000)
        sup += segment(0x16, p, struct.pack(">HHBHBBBB", w, h, 0x10, 2 * n, 0x80, 0, 0, 1) + struct.pack(">HBBHH", 0, 0, 0, x, y))
        sup += segment(0x17, p, struct.pack(">BBHHHH", 1, 0, x, y, bw, bh))
        sup += segment(0x14, p, struct.pack(">BB", 0, 0) + bytes([1, 235, 128, 128, 255]))
        rle = (bytes([0x00, 0xC0 | (bw >> 8), bw & 0xFF, 1]) + b"\0\0") * bh
        sup += segment(0x15, p, struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", bw, bh) + rle)
        sup += segment(0x80, p, b"")
        sup += segment(0x16, q, struct.pack(">HHBHBBBB", w, h, 0x10, 2 * n + 1, 0, 0, 0, 0))
        sup += segment(0x17, q, struct.pack(">BBHHHH", 1, 0, x, y, bw, bh))
        sup += segment(0x80, q, b"")
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(dir=folder, prefix=".work-")
    base, subs = os.path.join(work, "base.mkv"), os.path.join(work, "s.sup")
    with open(subs, "wb") as f:
        f.write(sup)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={2 * w}x{2 * h}:rate=24",
                    "-f", "lavfi", "-i", "sine=f=300", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-metadata:s:a:0", "language=eng", base], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", path, base, subs], check=True)
    shutil.rmtree(work)
    return path
