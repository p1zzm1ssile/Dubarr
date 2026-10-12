"""How Dubarr starts ffmpeg, ffprobe and mkvmerge: at low priority if set, with a thread limit."""

import shutil

import utils.config as config


def wrap(command):
    """The command, run at idle CPU and disk priority when "Run at low priority" is on, so a Jellyfin
    transcode or anything else on the server comes first."""
    if not getattr(config, "low_priority", False):
        return list(command)
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix + list(command)


def ffmpeg_threads():
    """ffmpeg's -threads option, or nothing to let it decide."""
    threads = getattr(config, "ffmpeg_threads", 0)
    return ["-threads", str(threads)] if threads else []
