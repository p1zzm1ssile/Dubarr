"""Radarr, Sonarr, Bazarr and Jellyfin: tell them to look at a file again after Dubarr changes it."""

import posixpath
import threading
import time

import requests

from utils.languages import UNDEFINED, normalise
from utils.paths import inside, to_local, to_remote

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
        self._tags = {}
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
        """Every movie or series, as {id, folder, title, original (language code or None), tags (ids)}.
        Kept for 10 minutes, as libraries can be big."""
        with self._lock:
            if self._items is None or time.time() - self._loaded > CACHE_SECONDS:
                endpoint = "/movie" if self.kind == "radarr" else "/series"
                self._items = [{"id": i["id"], "folder": i.get("path") or "", "title": i.get("title", ""),
                                "original": original_language(i), "tags": list(i.get("tags") or [])}
                               for i in self._request("GET", endpoint)]
                self._loaded = time.time()
            return self._items

    def _match(self, local_path):
        remote = to_remote(local_path, self.paths)
        matches = [item for item in self.items() if item["folder"] and inside(remote, item["folder"])]
        return max(matches, key=lambda m: len(m["folder"])) if matches else None

    def find(self, local_path):
        """The movie or series a file belongs to, as (id, title), or None."""
        match = self._match(local_path)
        return (match["id"], match["title"]) if match else None

    def original_language(self, local_path):
        """The original language of the movie or series a file belongs to, as a code, or None."""
        match = self._match(local_path)
        return match["original"] if match else None

    def _tag_id(self, label):
        """The id of a tag, made if Radarr or Sonarr doesn't have it yet."""
        if label not in self._tags:
            for tag in self._request("GET", "/tag") or []:
                self._tags[tag.get("label", "").lower()] = tag["id"]
        if label not in self._tags:
            self._tags[label] = self._request("POST", "/tag", json={"label": label})["id"]
        return self._tags[label]

    def add_tag(self, local_path, label):
        """Tag the movie or series a file belongs to. Returns its title, or None if it isn't theirs
        or already has the tag."""
        match = self._match(local_path)
        if not match:
            return None
        tag = self._tag_id(label)
        if tag in match["tags"]:
            return None
        if self.kind == "radarr":
            self._request("PUT", "/movie/editor", json={"movieIds": [match["id"]], "tags": [tag], "applyTags": "add"})
        else:
            self._request("PUT", "/series/editor", json={"seriesIds": [match["id"]], "tags": [tag], "applyTags": "add"})
        match["tags"].append(tag)
        return match["title"]

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


def original_language(item):
    """Radarr and Sonarr (v4) give it as {"id": 8, "name": "Japanese"}, sometimes with a variant like
    "Portuguese (Brazil)". "Unknown", "Original" and "Any" mean they don't know."""
    language = item.get("originalLanguage") or {}
    label = (language.get("name") if isinstance(language, dict) else str(language)) or ""
    code = normalise(label.split("(")[0])
    return None if code == UNDEFINED else code


BAZARR_DELETED = 0  # the action in Bazarr's history for a subtitle it deleted


class Bazarr:
    """Bazarr knows films by their Radarr id and series by their Sonarr id, so Dubarr finds those first:
    through Radarr or Sonarr when they're connected, otherwise from Bazarr's own list."""

    def __init__(self, cfg):
        self.name = "Bazarr"
        self.url = cfg.bazarr_url
        self.api_key = cfg.bazarr_api_key
        self.paths = cfg.bazarr_paths
        self.timeout = cfg.request_timeout
        self._lists = {}
        self._lock = threading.Lock()

    def _request(self, method, path, params=None):
        try:
            response = requests.request(method, self.url + "/api" + path, headers={"X-API-KEY": self.api_key},
                                        params=params, data=params if method != "GET" else None,
                                        timeout=self.timeout)
            response.raise_for_status()
            return response.json() if response.content else None
        except (requests.RequestException, ValueError) as e:
            raise _error(e, self.name)

    @staticmethod
    def _data(answer):
        return answer.get("data", answer) if isinstance(answer, dict) else answer

    def version(self):
        return (self._data(self._request("GET", "/system/status")) or {}).get("bazarr_version", "")

    def profiles(self):
        """[(profile name, [language codes])] from Bazarr's language profiles."""
        result = []
        for profile in self._data(self._request("GET", "/system/languages/profiles")) or []:
            codes = [item.get("language", "") for item in profile.get("items", [])]
            result.append((profile.get("name", "?"), [c for c in codes if c]))
        return result

    def _list(self, kind):
        with self._lock:
            loaded, items = self._lists.get(kind, (0, None))
            if items is None or time.time() - loaded > CACHE_SECONDS:
                items = self._data(self._request("GET", f"/{kind}", {"start": 0, "length": -1})) or []
                self._lists[kind] = (time.time(), items)
            return items

    def downloaded_subtitles(self):
        """The subtitle files Bazarr downloaded, as Dubarr's paths, from Bazarr's history: downloaded, upgraded,
        uploaded, synced, translated or extracted. One whose latest entry says Bazarr deleted it isn't counted.
        Empty if Bazarr's history is empty."""
        with self._lock:
            loaded, files = self._lists.get("history", (0, None))
            if files is not None and time.time() - loaded <= CACHE_SECONDS:
                return files
        latest = {}
        for kind in ("movies", "episodes"):
            # Newest first
            for entry in self._data(self._request("GET", f"/{kind}/history", {"start": 0, "length": -1})) or []:
                if entry.get("subtitles_path"):
                    path = posixpath.normpath(to_local(entry["subtitles_path"].replace("\\", "/"), self.paths))
                    latest.setdefault(path, entry.get("action"))
        files = frozenset(path for path, action in latest.items() if action != BAZARR_DELETED)
        with self._lock:
            self._lists["history"] = (time.time(), files)
        return files

    def locate(self, local_path):
        """("movie", Radarr id) or ("series", Sonarr id) for a file, from Bazarr's own lists, or None."""
        remote = to_remote(local_path, self.paths)
        for movie in self._list("movies"):
            if movie.get("path") and (movie["path"] == remote or inside(remote, movie["path"])):
                return "movie", movie.get("radarrId")
        for show in self._list("series"):
            if show.get("path") and inside(remote, show["path"]):
                return "series", show.get("sonarrSeriesId")
        return None

    def action(self, kind, item_id, action):
        """kind is "movie" or "series"; action is "scan-disk" or "search-missing"."""
        if kind == "movie":
            self._request("PATCH", "/movies", {"radarrid": item_id, "action": action})
        else:
            self._request("PATCH", "/series", {"seriesid": item_id, "action": action})


class Jellyfin:
    def __init__(self, cfg):
        self.name = "Jellyfin"
        self.url = cfg.jellyfin_url
        self.api_key = cfg.jellyfin_api_key
        self.paths = cfg.jellyfin_paths
        self.timeout = cfg.request_timeout
        self._items = None
        self._loaded = 0
        self._user = None
        self._lock = threading.Lock()

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

    def items(self):
        """Every movie and series, with its path and tags. Kept for 10 minutes."""
        with self._lock:
            if self._items is None or time.time() - self._loaded > CACHE_SECONDS:
                answer = self._request("GET", "/Items", params={
                    "userId": self._admin(), "Recursive": "true", "IncludeItemTypes": "Movie,Series", "Fields": "Path,Tags"}) or {}
                self._items = [{"id": i["Id"], "type": i.get("Type"), "path": i.get("Path") or "",
                                "title": i.get("Name", ""), "tags": list(i.get("Tags") or [])}
                               for i in answer.get("Items", [])]
                self._loaded = time.time()
            return self._items

    def _match(self, local_path):
        """The movie (its own file) or series (its folder) a file belongs to."""
        remote = to_remote(local_path, self.paths)
        matches = [i for i in self.items() if i["path"]
                   and (i["path"] == remote if i["type"] == "Movie" else inside(remote, i["path"]))]
        return max(matches, key=lambda i: len(i["path"])) if matches else None

    def _admin(self):
        """Editing an item goes through a user who can edit metadata: the first administrator."""
        if self._user is None:
            users = self._request("GET", "/Users") or []
            admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
            if not admins:
                raise ServiceError("Jellyfin has no administrator to edit tags with")
            self._user = admins[0]["Id"]
        return self._user

    def add_tag(self, local_path, label):
        """Tag the movie or series a file belongs to, so it can be filtered by in Jellyfin.
        Returns its title, or None if Jellyfin doesn't know it or it already has the tag."""
        match = self._match(local_path)
        if not match or any(t.lower() == label for t in match["tags"]):
            return None
        # Jellyfin saves the whole item, so send it back as it is with the tag added
        item = self._request("GET", f"/Users/{self._admin()}/Items/{match['id']}")
        item["Tags"] = list(item.get("Tags") or []) + [label]
        self._request("POST", f"/Items/{match['id']}", json=item)
        match["tags"].append(label)
        return match["title"]


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
