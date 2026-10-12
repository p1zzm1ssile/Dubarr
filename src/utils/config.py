"""Dubarr settings.

Every setting is described once in FIELDS: its tab in the web UI, its label, its
type, the environment variable it can come from, and its default. Settings live
in settings.json in CONFIG_DIR and are edited in the web UI. On first start,
settings.json is created from the environment variables.

The rest of Dubarr reads plain module attributes (config.radarr_url and so on).
apply() fills those in from the saved values, so a change made in the web UI
takes effect without a restart.
"""

import json
import os
import secrets
import tempfile
import threading

from dotenv import load_dotenv

from utils import languages

load_dotenv()


def ensure_http(url):
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else "http://" + url


def split_labels(value):
    if isinstance(value, list):
        value = ",".join(value)
    return [l.strip() for l in (value or "").split(",") if l.strip()]


def env_bool(*names, default=False):
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value.strip().lower() == "true"
    return default


def env_str(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def env_int(*names, default=0):
    value = env_str(*names, default=None)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


# Only set by environment, since they're needed before the web UI can run
config_dir = os.getenv("CONFIG_DIR") or ("/config" if os.path.isdir("/config") else "config")
settings_file = os.path.join(config_dir, "settings.json")
database_file = os.path.join(config_dir, "dubarr.db")
token_file = os.path.join(config_dir, "webhook-token")
log_dir = os.path.join(config_dir, "logs")
log_file = os.path.join(log_dir, "dubarr.log")
log_output = os.getenv("LOG_OUTPUT", "all").lower()
web_host = os.getenv("WEB_HOST", "http://localhost").lower()
web_port = int(os.getenv("WEB_PORT", 9892))

TABS = {
    "general": "General",
    "libraries": "Libraries",
    "radarr": "Radarr",
    "sonarr": "Sonarr",
    "bazarr": "Bazarr",
    "jellyfin": "Jellyfin",
}


def _default_libraries():
    """The folders mapped in the Unraid template, when they exist."""
    libraries = []
    for path in env_str("LIBRARIES", default="/movies,/tv,/media").split(","):
        path = path.strip()
        if path and os.path.isdir(path):
            libraries.append({"path": path, "audio": "", "subtitles": ""})
    return libraries


def F(key, tab, label, type, default, help="", section="", group=None, **extra):
    """Describe one setting. type is bool, int, text, secret, select, labels, languages or libraries."""
    return dict(key=key, tab=tab, label=label, type=type, default=default, help=help,
                section=section, group=group, **extra)


FIELDS = [
    # General: what to keep
    F("audio_languages", "general", "Audio languages to keep", "languages",
      env_str("AUDIO_LANGUAGES", default="eng"), section="Languages",
      help="Audio in any other language is removed. A file with none of these is left alone and flagged. "
           "Libraries can have their own list."),
    F("subtitle_languages", "general", "Subtitle languages to keep", "languages",
      env_str("SUBTITLE_LANGUAGES", default="eng"), section="Languages",
      help="Subtitle tracks inside the file in any other language are removed, forced ones included. "
           "Subtitles with no language tag are always kept."),
    F("strip_audio", "general", "Remove unwanted audio", "bool",
      env_bool("STRIP_AUDIO", default=True), section="Languages"),
    F("strip_subtitles", "general", "Remove unwanted subtitles", "bool",
      env_bool("STRIP_SUBTITLES", default=True), section="Languages"),
    F("strip_commentary", "general", "Remove commentary tracks", "bool",
      env_bool("STRIP_COMMENTARY", default=True), section="Languages",
      help="Director's and cast commentaries, even in your languages, and the subtitles that go with them. "
           "The main audio is never removed."),
    F("one_audio_per_language", "general", "Keep only the best audio track per language", "bool",
      env_bool("ONE_AUDIO_PER_LANGUAGE", default=True), section="Languages",
      help="e.g. keeps Dolby Atmos and removes the extra 5.1 and stereo copies. Jellyfin converts the audio on the fly "
           "for devices that can't play it, without touching the video. Audio description tracks are never picked."),
    F("strip_subtitle_files", "general", "Remove subtitle files beside films in other languages", "bool",
      env_bool("STRIP_SUBTITLE_FILES", default=True), section="Languages",
      help="e.g. Film.fr.srt downloaded by Bazarr or Jellyfin. The language comes from the file name; files whose name "
           "doesn't say one are kept. Just a file delete, so it's done even while the film itself waits."),
    F("prefer_text_files", "general", "Remove picture subtitles when a text file is beside the film", "bool",
      env_bool("PREFER_TEXT_FILES", default=False), section="Languages",
      help="If Film.en.srt sits beside the film, its English picture subtitles (PGS) aren't needed, and Jellyfin "
           "won't have to transcode to show them. Forced tracks stay. Off by default, as it trusts the downloaded "
           "file to be a good match."),
    F("assume_unlabelled_is_full", "general", "Trust SDH labels when subtitle lengths aren't recorded", "bool",
      env_bool("ASSUME_UNLABELLED_IS_FULL", default=False), section="Languages",
      help="Some files don't record how long each subtitle track is, so a short forced track can't be told from a full one "
           "and every track is kept. On: when one track is labelled SDH, the unlabelled one is taken to be the full "
           "subtitles and the SDH copy is removed. Usually right, but it's a guess."),
    F("one_subtitle_per_language", "general", "Keep only one subtitle track per language, plus forced", "bool",
      env_bool("ONE_SUBTITLE_PER_LANGUAGE", default=True), section="Languages",
      help="Text subtitles (SRT) are kept over picture ones (PGS), since Jellyfin can show text without transcoding "
           "the video, and normal over SDH. Forced subtitles, which translate foreign-language scenes, are kept too."),

    # General: when
    F("report_only", "general", "Report only, don't change files", "bool",
      env_bool("REPORT_ONLY", default=True), section="Changing files",
      help="Dubarr lists what it would change on the Dashboard, where you can apply files one at a time "
           "or all at once. Turn off to let it change files by itself."),
    F("scan_interval_hours", "general", "Scan the libraries every (hours)", "int",
      env_int("SCAN_INTERVAL_HOURS", default=24), section="Changing files", min=1, max=720,
      help="Only new or changed files are read again, so later scans are quick."),
    F("active_from", "general", "Only change files from (hour)", "int",
      env_int("ACTIVE_FROM", default=0), section="Changing files", min=0, max=23,
      help="Rewriting files is heavy on the array. Set a window, e.g. 1 to 7 for overnight. "
           "The same hour in both means any time."),
    F("active_until", "general", "Until (hour)", "int",
      env_int("ACTIVE_UNTIL", default=0), section="Changing files", min=0, max=23),
    F("max_changes_per_pass", "general", "Most files changed at a time", "int",
      env_int("MAX_CHANGES_PER_PASS", default=25), section="Changing files", min=1, max=10000,
      help="Dubarr changes this many, then waits 10 minutes before the next batch."),
    F("min_saving_mb", "general", "Skip files that would save less than", "slider",
      env_int("MIN_SAVING_MB", default=0), section="Changing files",
      options=[0, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000],
      help="Removing any track means rewriting the whole file, which on a 4K film can be 60 GB of writing to save a few KB "
           "of subtitles. Files that would save less are listed under \"Not worth rewriting\" and can still be applied by hand."),
    F("workers", "general", "Workers", "int",
      env_int("WORKERS", default=1), section="Speed and CPU", min=1, max=8,
      help="How many films are changed at once. Each one is mostly disk work, so more workers only help when the "
           "films are on different disks; Dubarr never puts two on the same disk at once when it can tell."),
    F("ffmpeg_threads", "general", "CPU threads per worker", "int",
      env_int("FFMPEG_THREADS", default=0), section="Speed and CPU", min=0, max=64,
      help="0 lets ffmpeg decide. Removing tracks is copying rather than encoding, so it rarely uses much CPU."),
    F("low_priority", "general", "Run at low priority", "bool",
      env_bool("LOW_PRIORITY", default=True), section="Speed and CPU",
      help="Dubarr's ffmpeg and mkvmerge give way to everything else, like a Jellyfin transcode, for both CPU and disk."),
    F("process_hardlinked", "general", "Change files that are still seeding", "bool",
      env_bool("PROCESS_HARDLINKED", default=False), section="Changing files",
      help="A hardlinked file is usually still seeding in your torrent client. Changing it breaks the link, "
           "so the film takes up space twice until the torrent is removed. Off: wait until seeding stops."),

    # General: recycle folder
    F("recycle_days", "general", "Keep originals for (days)", "int",
      env_int("RECYCLE_DAYS", default=7), section="Recycle folder", min=0, max=365,
      help="Originals can be restored from History until then. 0 deletes them as soon as the new file checks out."),
    F("recycle_folder", "general", "Recycle folder", "text",
      env_str("RECYCLE_FOLDER", default=""), section="Recycle folder",
      help="Leave empty to use a hidden .dubarr-recycle folder at the top of each library. That keeps originals "
           "on the same disk, so moving them is instant."),

    # General: logs and login
    F("log_level", "general", "Log level", "select", env_str("LOG_LEVEL", default="INFO").upper(),
      section="Logs", options=["INFO", "WARNING", "ERROR"]),
    F("log_lines", "general", "Log lines shown", "int", env_int("LOG_LINES", default=500),
      section="Logs", min=50, max=10000),
    F("request_timeout", "general", "Request timeout (seconds)", "int",
      env_int("REQUEST_TIMEOUT", default=30), section="Logs", min=1),
    F("auth_lan_bypass", "general", "Skip login on my local network", "bool",
      env_bool("AUTH_LAN_BYPASS", default=False), section="Login",
      help="Don't ask for a password from the networks below. Never applies to requests that come through a reverse proxy."),
    F("local_networks", "general", "My local networks", "labels",
      env_str("LOCAL_NETWORKS", default="192.168.0.0/16, 10.0.0.0/8"), section="Login",
      help="Addresses that skip the login, separated by commas. 172.16.0.0/12 isn't included by default because Docker uses it, "
           "and traffic forwarded by Docker can appear to come from there."),

    # Libraries
    F("libraries", "libraries", "Libraries", "libraries", _default_libraries(), section="Libraries"),

    # Radarr, Sonarr and Jellyfin
    *[
        field
        for service, port, help_key in (
            ("radarr", 7878, "Settings > General > Security in Radarr"),
            ("sonarr", 8989, "Settings > General > Security in Sonarr"),
            ("bazarr", 6767, "Settings > General > Security in Bazarr"),
            ("jellyfin", 8096, "Dashboard > API Keys in Jellyfin"),
        )
        for field in (
            F(f"{service}_enabled", service, f"Use {service.capitalize()}", "bool",
              env_bool(f"{service.upper()}_ENABLED", default=bool(os.getenv(f"{service.upper()}_API_KEY"))),
              section="Connection"),
            F(f"{service}_url", service, "Address", "text",
              env_str(f"{service.upper()}_URL", default=f"localhost:{port}"), section="Connection",
              help=f"IP address or hostname with port, e.g. 192.168.1.10:{port}"),
            F(f"{service}_api_key", service, "API key", "secret", env_str(f"{service.upper()}_API_KEY"),
              section="Connection", help=help_key),
            F(f"{service}_path_map", service, "Folder mapping", "labels",
              env_str(f"{service.upper()}_PATH_MAP", default=""), section="Folders",
              help=f"Only if {service.capitalize()} sees your media under a different folder than Dubarr does. "
                   f"Write it as {service.capitalize()}'s folder=Dubarr's folder, e.g. /data/media/movies=/movies. "
                   "Separate several with commas."),
        )
    ],
    F("bazarr_search_missing", "bazarr", "Ask Bazarr for subtitles a film is left without", "bool",
      env_bool("BAZARR_SEARCH_MISSING", default=True), section="What Bazarr does",
      help="When a change leaves a film with no subtitles in your languages (say it only had French ones), Bazarr "
           "is asked to search for them."),
]

FIELDS_BY_KEY = {f["key"]: f for f in FIELDS}
SECRET_KEYS = {f["key"] for f in FIELDS if f["type"] == "secret"}

# The address each secret is sent to. A saved secret is never reused with a new
# address, or anyone with access could send it to a server of their choosing.
SECRET_URLS = {
    "radarr_api_key": "radarr_url", "sonarr_api_key": "sonarr_url", "jellyfin_api_key": "jellyfin_url",
    "bazarr_api_key": "bazarr_url",
}


def secrets_needing_reentry(changes: dict) -> dict:
    """{secret key: reason} for saved secrets whose address is being changed."""
    problems = {}
    for secret, url in SECRET_URLS.items():
        new_url = changes.get(url)
        if new_url is None or str(new_url).strip() == str(values.get(url, "")).strip():
            continue
        if changes.get(secret) is None and values.get(secret):
            label = FIELDS_BY_KEY[secret]["label"]
            problems[secret] = f"{label}: enter it again, since the address changed"
    return problems


_lock = threading.RLock()
load_error = None
values = {f["key"]: f["default"] for f in FIELDS}


def parse_path_map(value):
    """"/data/media=/media, /x=/y" -> [("/data/media", "/media"), ("/x", "/y")]."""
    pairs = []
    for item in split_labels(value):
        if "=" not in item:
            raise ValueError(f"needs an = between the two folders: {item}")
        remote, local = (p.strip().rstrip("/") for p in item.split("=", 1))
        if not remote or not local:
            raise ValueError(f"needs a folder on both sides of the =: {item}")
        pairs.append((remote, local))
    return pairs


def coerce_libraries(value):
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if not isinstance(value, list):
        raise ValueError("must be a list")
    clean, seen = [], set()
    for item in value:
        raw = str((item or {}).get("path", "")).strip()
        if not raw:
            continue  # an empty row
        path = raw.rstrip("/") or "/"
        if not path.startswith("/"):
            raise ValueError(f"{path} must be a full path inside the container, starting with /")
        if path == "/":
            raise ValueError("can't be the whole container (/)")
        if path in seen:
            raise ValueError(f"lists {path} twice")
        seen.add(path)
        entry = {"path": path}
        for key in ("audio", "subtitles"):
            try:
                entry[key] = ",".join(languages.parse_list(item.get(key, "")))
            except ValueError as e:
                raise ValueError(f"for {path} {e}")
        clean.append(entry)
    return clean


def coerce(field, value):
    """Turn a submitted value into the right type, or raise ValueError with a reason."""
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError("must be on or off")
    if t == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")
        if "min" in field and number < field["min"]:
            raise ValueError(f"must be at least {field['min']}")
        if "max" in field and number > field["max"]:
            raise ValueError(f"must be at most {field['max']}")
        return number
    if t == "slider":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if value not in field["options"]:
            raise ValueError(f"must be one of {', '.join(map(str, field['options']))}")
        return value
    if t == "select":
        if value not in field["options"]:
            raise ValueError(f"must be one of {', '.join(field['options'])}")
        return value
    if t == "libraries":
        try:
            return coerce_libraries(value)
        except (TypeError, AttributeError, json.JSONDecodeError):
            raise ValueError("couldn't be read")
    value = "" if value is None else str(value).strip()
    if len(value) > 2000:
        raise ValueError("is too long")
    if t == "text" and field["key"].endswith("_url") and any(c in value for c in " <>\"'"):
        raise ValueError("isn't a valid address")
    if t == "languages":
        codes = languages.parse_list(value)
        if not codes:
            raise ValueError("needs at least one language")
        return ",".join(codes)
    if field["key"].endswith("_path_map"):
        parse_path_map(value)
    if field["key"] == "recycle_folder" and value and not value.startswith("/"):
        raise ValueError("must be a full path inside the container, starting with /")
    if field["key"] == "local_networks":
        import ipaddress
        for item in split_labels(value):
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"has an invalid network: {item} (use a form like 192.168.1.0/24)")
    return value


def parse_networks(value):
    import ipaddress

    networks = []
    for item in split_labels(value):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            pass
    return networks


def resolve(raw):
    """Every attribute the rest of Dubarr reads, worked out from the raw settings."""
    resolved = dict(raw)
    resolved["audio_language_codes"] = split_labels(raw["audio_languages"])
    resolved["subtitle_language_codes"] = split_labels(raw["subtitle_languages"])
    resolved["library_list"] = [
        {
            "path": lib["path"],
            "audio": split_labels(lib.get("audio")) or resolved["audio_language_codes"],
            "subtitles": split_labels(lib.get("subtitles")) or resolved["subtitle_language_codes"],
        }
        for lib in raw["libraries"]
    ]
    for service in ("radarr", "sonarr"):
        resolved[f"{service}_url"] = ensure_http(raw[f"{service}_url"]).rstrip("/") + "/api/v3"
    resolved["jellyfin_url"] = ensure_http(raw["jellyfin_url"]).rstrip("/")
    resolved["bazarr_url"] = ensure_http(raw["bazarr_url"]).rstrip("/")
    for service in ("radarr", "sonarr", "bazarr", "jellyfin"):
        try:
            resolved[f"{service}_paths"] = parse_path_map(raw[f"{service}_path_map"])
        except ValueError:
            resolved[f"{service}_paths"] = []
    resolved["local_networks"] = parse_networks(raw["local_networks"])
    return resolved


def apply(target, raw):
    """Set the attributes the rest of Dubarr reads, on target, from raw values.

    Background threads read them without a lock, so each attribute is only ever
    set to its final value: never, even briefly, to the raw text it comes from.
    """
    for key, value in resolve(raw).items():
        setattr(target, key, value)


def load():
    """Read settings.json, creating it from the environment on first start."""
    global values, load_error
    with _lock:
        saved = {}
        if os.path.exists(settings_file):
            try:
                with open(settings_file) as f:
                    saved = json.load(f)
            except (OSError, ValueError) as e:
                # Keep the broken file for the user to fix, rather than overwrite it later
                broken = f"{settings_file}.broken-{int(__import__('time').time())}"
                os.replace(settings_file, broken)
                load_error = f"settings.json couldn't be read ({e}). It was moved to {broken} and defaults are being used"
                saved = {}

        merged = {f["key"]: f["default"] for f in FIELDS}
        for key, value in saved.items():
            if key in FIELDS_BY_KEY:
                try:
                    merged[key] = coerce(FIELDS_BY_KEY[key], value)
                except ValueError:
                    pass
        values = merged
        apply(globals_namespace, values)

        if not os.path.exists(settings_file):
            _write(values)


def _write(data):
    os.makedirs(config_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config_dir, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)  # holds API keys
        os.replace(tmp, settings_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def update(changes: dict):
    """Validate and save changed settings. Returns {key: reason} for any rejected."""
    global values
    errors = {}
    clean = {}
    for key, value in changes.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            errors[key] = "isn't a setting"
            continue
        # Secrets are never sent to the browser; an untouched one comes back as None
        if field["type"] == "secret" and value is None:
            continue
        try:
            clean[key] = coerce(field, value)
        except ValueError as e:
            errors[key] = f"{field['label']} {e}"

    errors.update(secrets_needing_reentry(changes))
    if errors:
        return errors

    with _lock:
        new_values = {**values, **clean}
        _write(new_values)
        values = new_values
        apply(globals_namespace, values)
    return {}


def preview(changes: dict):
    """A copy of the current settings with unsaved changes applied, for testing a connection."""
    from types import SimpleNamespace

    problems = secrets_needing_reentry(changes)
    if problems:
        raise ValueError("; ".join(problems.values()))
    raw = dict(values)
    for key, value in changes.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None or (field["type"] == "secret" and value is None):
            continue
        raw[key] = coerce(field, value)
    target = SimpleNamespace(**{k: v for k, v in globals().items() if not k.startswith("_")})
    apply(target, raw)
    return target


def public_values():
    """Settings safe to send to the browser: secrets are replaced by whether they're set."""
    with _lock:
        return {
            key: ({"set": bool(value)} if key in SECRET_KEYS else value)
            for key, value in values.items()
        }


def webhook_token():
    """The secret in the webhook address Radarr and Sonarr call. Made on first use."""
    try:
        with open(token_file) as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    os.makedirs(config_dir, exist_ok=True)
    with open(token_file, "w") as f:
        f.write(token)
    os.chmod(token_file, 0o600)
    return token


class _Module:
    """Lets apply() set attributes on this module."""

    def __setattr__(self, key, value):
        globals()[key] = value

    def __getattr__(self, key):
        try:
            return globals()[key]
        except KeyError:
            raise AttributeError(key)


globals_namespace = _Module()
load()
