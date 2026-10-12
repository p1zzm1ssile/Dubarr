"""Radarr, Sonarr and Jellyfin: tell them to look at a file again after Dubarr changes it."""

import threading
import time

import requests

from utils.paths import inside, to_remote

CACHE_SECONDS = 600


class ServiceError(Exception):
    pass


def _error(e, name):
    if isinstance(e, requests.exceptions.ConnectionError):
        return ServiceError(f"Can't reach {name}. Check the address")
    if isinstance(e, requests.exceptions.Timeout):
        return ServiceError(f"{name} didn't answer in time")
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        if e.response.status_code in (401, 403):
            return ServiceError(f"{name} rejected the API key")
        return ServiceError(f"{name} answered with error {e.response.status_code}")
    return ServiceError(f"{name}: {e}")


class Arr:
    """Radarr or Sonarr."""

    def __init__(self, kind, cfg):
        self.kind = kind
        self.name = kind.capitalize()
        self.url = getattr(cfg, f"{kind}_url")
        self.api_key = getattr(cfg, f"{kind}_api_key")
        self.paths = getattr(cfg, f"{kind}_paths")
        self.timeout = cfg.request_timeout
        self._items = None
        self._loaded = 0
        self._lock = threading.Lock()

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(method, self.url + path, headers={"X-Api-Key": self.api_key},
                                        timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        except (requests.RequestException, ValueError) as e:
            raise _error(e, self.name)

    def version(self):
        return self._request("GET", "/system/status").get("version", "")

    def items(self):
        """Every movie or series, with its folder. Kept for 10 minutes, as libraries can be big."""
        with self._lock:
            if self._items is None or time.time() - self._loaded > CACHE_SECONDS:
                endpoint = "/movie" if self.kind == "radarr" else "/series"
                self._items = [(i["id"], i.get("path") or "", i.get("title", "")) for i in self._request("GET", endpoint)]
                self._loaded = time.time()
            return self._items

    def find(self, local_path):
        """The movie or series a file belongs to, as (id, title), or None."""
        remote = to_remote(local_path, self.paths)
        matches = [(id, folder, title) for id, folder, title in self.items() if folder and inside(remote, folder)]
        if not matches:
            return None
        id, _, title = max(matches, key=lambda m: len(m[1]))
        return id, title

    def rescan(self, local_path):
        """Ask Radarr or Sonarr to read the file again. Returns the title, or None if it isn't theirs."""
        found = self.find(local_path)
        if not found:
            return None
        id, title = found
        if self.kind == "radarr":
            self._request("POST", "/command", json={"name": "RescanMovie", "movieId": id})
        else:
            self._request("POST", "/command", json={"name": "RescanSeries", "seriesId": id})
        return title


class Jellyfin:
    def __init__(self, cfg):
        self.url = cfg.jellyfin_url
        self.api_key = cfg.jellyfin_api_key
        self.paths = cfg.jellyfin_paths
        self.timeout = cfg.request_timeout

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f'MediaBrowser Token="{self.api_key}"'}
        try:
            response = requests.request(method, self.url + path, headers=headers, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None
        except (requests.RequestException, ValueError) as e:
            raise _error(e, "Jellyfin")

    def version(self):
        return self._request("GET", "/System/Info").get("Version", "")

    def updated(self, local_path):
        """Tell Jellyfin a file changed, so it reads its tracks again."""
        self._request("POST", "/Library/Media/Updated", json={
            "Updates": [{"Path": to_remote(local_path, self.paths), "UpdateType": "Modified"}]})


def webhook_paths(payload: dict, kind: str):
    """The files in a Radarr or Sonarr "On Import" webhook, as they see them."""
    paths = []
    if kind == "radarr":
        movie_file = payload.get("movieFile") or {}
        folder = (payload.get("movie") or {}).get("folderPath", "")
        if movie_file.get("path"):
            paths.append(movie_file["path"])
        elif folder and movie_file.get("relativePath"):
            paths.append(folder.rstrip("/") + "/" + movie_file["relativePath"])
    else:
        folder = (payload.get("series") or {}).get("path", "")
        files = payload.get("episodeFiles") or [payload.get("episodeFile") or {}]
        for episode_file in files:
            if episode_file.get("path"):
                paths.append(episode_file["path"])
            elif folder and episode_file.get("relativePath"):
                paths.append(folder.rstrip("/") + "/" + episode_file["relativePath"])
    return list(dict.fromkeys(paths))
