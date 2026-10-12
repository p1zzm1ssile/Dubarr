"""Translate folders between what Radarr, Sonarr or Jellyfin see and what Dubarr sees."""

import posixpath


def _swap(path, pairs, src, dst):
    path = posixpath.normpath(path)
    best = None
    for pair in pairs:
        prefix = posixpath.normpath(pair[src])
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if best is None or len(prefix) > len(posixpath.normpath(best[src])):
                best = pair
    if best is None:
        return path
    rest = path[len(posixpath.normpath(best[src])):]
    return posixpath.normpath(best[dst] + rest)


def to_local(path, pairs):
    """Their path -> Dubarr's path. pairs are (theirs, ours)."""
    return _swap(path, pairs, 0, 1)


def to_remote(path, pairs):
    """Dubarr's path -> their path."""
    return _swap(path, pairs, 1, 0)


def inside(path, folder):
    path, folder = posixpath.normpath(path), posixpath.normpath(folder)
    return path == folder or path.startswith(folder.rstrip("/") + "/")
