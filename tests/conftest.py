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
