"""The Dubarr web UI and its JSON API."""

import glob
import hmac
import io
import json
import os
import posixpath
import re
import time
import zipfile
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import utils.auth as auth
import utils.config as config
import utils.status as status
from core import plan as plans
from core.engine import ERROR, NOT_BAZARR, SEEDING, TOO_SMALL, Busy, in_window
from core import detect as detection
from core.swap import SwapError
from core.tagging import TagError, audio_sample, subtitle_lines
from services.connections import Arr, Bazarr, Jellyfin, ServiceError, webhook_paths
from utils.languages import LANGUAGES, name, normalise
from utils.logger import logger
from utils.paths import to_local

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC = {
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon.png": ("icon.png", "image/png"),
}
COOKIE = "dubarr_session"
LOG_LINE = re.compile(r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>[A-Z]+)\] (?P<message>.*)$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# The order the Dashboard lists them in
STATUSES = {
    plans.CHANGE: "Will be changed",
    NOT_BAZARR: "Subtitles not from Bazarr",
    SEEDING: "Waiting for seeding to stop",
    TOO_SMALL: "Not worth rewriting",
    plans.NO_WANTED_AUDIO: "No wanted audio",
    plans.UNKNOWN_AUDIO: "Untagged audio",
    plans.UNKNOWN_SUBTITLES: "Untagged subtitles",
    plans.UNKNOWN_ORIGINAL: "Original language unknown",
    plans.ORIGINAL_ONLY: "Original language only",
    ERROR: "Problems",
    plans.NO_AUDIO: "No audio",
    plans.CLEAN: "Already clean",
    "ignored": "Left alone (restored)",
}

engine = None  # set by serve()
_clash_cache = (0.0, [])  # Bazarr's language profiles change rarely, so they're checked every 10 minutes


class Handler(BaseHTTPRequestHandler):
    server_version = "Dubarr"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    # Helpers

    @property
    def ip(self):
        return self.client_address[0]

    def token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def user(self):
        """The signed-in user, "(local network)" with the LAN bypass, or None."""
        user = auth.session_user(self.token())
        if user:
            return user
        if auth.has_account() and auth.lan_bypass(self.ip, self.headers):
            return "(local network)"
        return None

    def send(self, code, body=b"", content_type="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location):
        self.send(302, b"", "text/plain", {"Location": location})

    def page(self, name):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            self.send(200, f.read(), "text/html; charset=utf-8")

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("Request too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise ValueError("Invalid JSON")

    def session_cookie(self, token, max_age):
        return f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"

    # Routing

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        try:
            if path in STATIC:
                name, content_type = STATIC[path]
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    return self.send(200, f.read(), content_type)

            if path == "/health":
                result = status.health()
                code = 200 if result["healthy"] else 503
                # Without a login, only say whether Dubarr is healthy
                return self.send(code, result if self.user() else {"healthy": result["healthy"]})

            if path == "/login":
                if not auth.has_account():
                    return self.redirect("/setup")
                if self.user():
                    return self.redirect("/")
                return self.page("login.html")

            if path == "/setup":
                if auth.has_account():
                    return self.redirect("/login")
                return self.page("setup.html")

            if path == "/api/session":
                return self.send(200, {
                    "user": self.user(),
                    "has_account": auth.has_account(),
                    "needs_setup": not auth.has_account(),
                    "password_rules": [{"id": i, "text": t} for i, t, _ in auth.PASSWORD_RULES],
                    "locked_for": auth.locked_for(self.ip),
                })

            # Everything below needs a login
            if not self.user():
                if path.startswith("/api/"):
                    return self.send(401, {"error": "Please log in"})
                return self.redirect("/login" if auth.has_account() else "/setup")

            if path == "/":
                return self.page("app.html")
            if path == "/api/status":
                return self.send(200, status_report())
            if path == "/api/dashboard":
                return self.send(200, dashboard())
            if path == "/api/files":
                return self.send(200, files(query))
            if path == "/api/history":
                return self.send(200, history(query))
            if path == "/api/settings":
                return self.send(200, {
                    "tabs": config.TABS,
                    # Without defaults: they come from environment variables and can
                    # hold API keys. Only values, with secrets masked, go to the browser.
                    "fields": [{k: v for k, v in f.items() if k != "default"} for f in config.FIELDS],
                    "values": config.public_values(),
                    "languages": {code: name for code, (name, _) in LANGUAGES.items()},
                    "username": auth.username(),
                    "webhook_path": f"/api/webhook?token={config.webhook_token()}",
                })
            if path == "/api/sample":
                info = engine.known_file(query.get("path", ""))
                sample = audio_sample(info, int(query.get("index", -1)), part=int(query.get("part", 0)))
                return self.send(200, sample, "audio/mpeg")
            if path == "/api/subtitle-lines":
                info = engine.known_file(query.get("path", ""))
                return self.send(200, {"lines": subtitle_lines(info, int(query.get("index", -1)))})
            if path == "/api/logs":
                return self.send(200, read_logs(query.get("filter", "all")))
            if path == "/api/logs/download":
                return self.send(200, zip_logs(), "application/zip", {
                    "Content-Disposition": f'attachment; filename="dubarr-logs-{time.strftime("%Y%m%d-%H%M%S")}.zip"'
                })

            self.send(404, {"error": "Not found"})
        except (TagError, ValueError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            logger.error(f"Web UI error on {path} | {e}")
            self.send(500, {"error": str(e)})

    def do_POST(self):
        self.handle_write()

    def do_PUT(self):
        self.handle_write()

    def handle_write(self):
        url = urlparse(self.path)
        path = url.path

        # Radarr and Sonarr can't log in or add headers, so the webhook carries its own secret
        if path == "/api/webhook":
            token = parse_qs(url.query).get("token", [""])[0]
            if not hmac.compare_digest(token, config.webhook_token()):
                return self.send(403, {"error": "Wrong webhook token"})
            try:
                payload = self.body()
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                return self.send(200, webhook(payload))
            except ValueError as e:
                return self.send(400, {"error": str(e)})

        # Only accept writes from Dubarr's own pages: a form on another site
        # can't set this header, and the session cookie is SameSite=Strict too
        if self.headers.get("X-Dubarr") != "1":
            return self.send(403, {"error": "Missing request header"})

        try:
            data = self.body()

            if path == "/api/setup":
                if auth.has_account():
                    return self.send(403, {"error": "An account already exists"})
                if auth.locked_for(self.ip):
                    return self.send(429, {"error": "Too many attempts. Try again in a few minutes"})
                if not auth.can_create_account(data.get("code")):
                    # Count it like a failed login, so the code can't be guessed
                    auth.login(self.ip, "", "")
                    logger.warning(f"Wrong setup code entered from {self.ip}")
                    return self.send(403, {"error": "That setup code isn't right. It's in Dubarr's log"})
                auth.set_password(data.get("username"), data.get("password"))
                logger.info(f"Created web UI account {auth.username()}")
                token = auth.login(self.ip, data.get("username"), data.get("password"))
                return self.send(200, {"ok": True}, headers={"Set-Cookie": self.session_cookie(token, auth.SESSION_SECONDS)})

            if path == "/api/login":
                if wait := auth.locked_for(self.ip):
                    return self.send(429, {"error": f"Too many attempts. Try again in {wait // 60 + 1} minute(s)"})
                token = auth.login(self.ip, data.get("username"), data.get("password"))
                if not token:
                    logger.warning(f"Failed web UI login from {self.ip}")
                    if wait := auth.locked_for(self.ip):
                        return self.send(429, {"error": f"Too many attempts. Try again in {wait // 60 + 1} minute(s)"})
                    return self.send(401, {"error": "Wrong username or password"})
                return self.send(200, {"ok": True}, headers={"Set-Cookie": self.session_cookie(token, auth.SESSION_SECONDS)})

            if path == "/api/logout":
                auth.logout(self.token())
                return self.send(200, {"ok": True}, headers={"Set-Cookie": self.session_cookie("", 0)})

            if not self.user():
                return self.send(401, {"error": "Please log in"})

            if path == "/api/settings":
                changes = data.get("values") or {}
                errors = config.update(changes)
                if errors:
                    return self.send(400, {"errors": errors})
                logger.info(f"Settings saved ({', '.join(sorted(changes.keys()))})")
                # Languages or the Bazarr connection may have changed: check the profiles again
                global _clash_cache
                _clash_cache = (0.0, [])
                # New languages or libraries change what's wanted: re-check (quick, nothing is re-read)
                if {"audio_languages", "subtitle_languages", "strip_audio", "strip_subtitles", "strip_commentary",
                        "one_audio_per_language", "one_subtitle_per_language", "assume_unlabelled_is_full",
                        "min_saving_mb", "strip_subtitle_files", "prefer_text_files", "keep_original_audio", "original_only_tag",
                        "libraries", "process_hardlinked"} & changes.keys():
                    engine.background(engine.scan, "settings", wait=True)
                return self.send(200, {"ok": True, "values": config.public_values()})

            if path == "/api/test":
                return self.send(200, test_connection(data.get("target", ""), data.get("values") or {}))

            if path == "/api/password":
                if self.user() == "(local network)" and not auth.session_user(self.token()):
                    return self.send(403, {"error": "Log in with your password to change it"})
                if not auth.check_password(auth.username(), data.get("current", "")):
                    return self.send(400, {"error": "Current password is wrong"})
                auth.set_password(data.get("username") or auth.username(), data.get("password"))
                logger.info("Web UI password changed")
                token = auth.login(self.ip, data.get("username") or auth.username(), data.get("password"))
                return self.send(200, {"ok": True}, headers={"Set-Cookie": self.session_cookie(token, auth.SESSION_SECONDS)})

            try:
                if path == "/api/scan":
                    engine.background(engine.scan, "manual")
                    return self.send(200, {"ok": True, "message": "Scan started"})
                if path == "/api/apply":
                    paths = data.get("paths") or None
                    library = data.get("library") or None
                    if library and library not in [lib["path"] for lib in config.library_list]:
                        raise ValueError("That library isn't set up in Dubarr")
                    status = NOT_BAZARR if data.get("status") == NOT_BAZARR else None
                    engine.background(engine.apply, "manual", paths, True, library=library, status=status)
                    return self.send(200, {"ok": True, "message": "Changing files"})
                if path == "/api/remove":
                    file, tracks, files = data.get("path", ""), data.get("tracks") or [], data.get("files") or []
                    if not isinstance(tracks, list) or not isinstance(files, list):
                        raise ValueError("tracks and files must be lists")
                    try:
                        tracks = [int(t) for t in tracks]
                    except (TypeError, ValueError):
                        raise ValueError("tracks must be track numbers")
                    files = [str(f) for f in files]
                    engine.plan_by_hand(file, tracks, files)  # says straight away if the choice can't be made
                    engine.background(engine.remove_by_hand, file, tracks, files)
                    return self.send(200, {"ok": True, "message": "Removing them. It shows under History when it's done"})
                if path == "/api/stop":
                    engine.stop()
                    return self.send(200, {"ok": True, "message": "Stopping after the current file"})
                if path == "/api/recheck":
                    engine.background(engine.scan, "manual", [data.get("path", "")])
                    return self.send(200, {"ok": True})
                if path == "/api/tag":
                    tags = data.get("tags") or {}
                    if not isinstance(tags, dict):
                        raise ValueError("tags must be an object")
                    message = engine.tag(data.get("path", ""), tags)
                    return self.send(200, {"ok": True, "message": message})
                if path == "/api/detect":
                    info = engine.known_file(data.get("path", ""))
                    try:
                        index = int(data.get("index", -1))
                    except (TypeError, ValueError):
                        raise ValueError("index must be a track number")
                    return self.send(200, detection.detect(info, index))
                if path == "/api/ignore":
                    engine.store.set_ignored(data.get("path", ""), bool(data.get("ignored")))
                    return self.send(200, {"ok": True})
                if path == "/api/restore":
                    engine.restore(int(data.get("id", 0)))
                    return self.send(200, {"ok": True, "message": "Original restored"})
                if path == "/api/restore-run":
                    run_id = data.get("run_id") or engine.store.last_run_with_restorable()
                    if not run_id:
                        return self.send(400, {"error": "Nothing to restore"})
                    engine.background(engine.restore_run, int(run_id))
                    return self.send(200, {"ok": True, "message": "Restoring the whole batch"})
            except Busy as e:
                return self.send(409, {"error": str(e)})
            except (SwapError, TagError, detection.DetectError, ServiceError) as e:
                return self.send(400, {"error": str(e)})

            self.send(404, {"error": "Not found"})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            logger.error(f"Web UI error on {path} | {e}")
            self.send(500, {"error": str(e)})


# API helpers


def status_report():
    report = status.health()
    report["activity"] = engine.progress()
    report["report_only"] = config.report_only
    report["in_window"] = in_window(config.active_from, config.active_until)
    report["last_scan"] = engine.last_scan or None
    report["next_scan"] = (engine.last_scan + config.scan_interval_hours * 3600) if engine.last_scan else None
    return report


def dashboard():
    counts = engine.store.counts()
    return {
        "statuses": [{"key": k, "label": label, **counts.get(k, {"files": 0, "saved_bytes": 0})}
                     for k, label in STATUSES.items()],
        "saved": engine.store.total_saved(),
        "libraries": [{"path": lib["path"], "exists": os.path.isdir(lib["path"]),
                       "audio": lib["audio"], "subtitles": lib["subtitles"]} for lib in config.library_list],
        "restorable_run": engine.store.last_run_with_restorable(),
        "report_only": config.report_only,
        "bazarr_clashes": bazarr_clashes(),
    }


def bazarr_clashes():
    global _clash_cache
    if time.time() - _clash_cache[0] > 600:
        try:
            clashes = engine.bazarr_clashes()
        except ServiceError:
            clashes = _clash_cache[1]
        _clash_cache = (time.time(), clashes)
    return [{"profile": p, "languages": langs} for p, langs in _clash_cache[1]]


def files(query):
    wanted = query.get("status", plans.CHANGE)
    try:
        offset = max(0, int(query.get("offset", 0)))
    except ValueError:
        offset = 0
    search = query.get("search", "")[:200]
    library = query.get("library") or None
    if wanted == "ignored":
        total, rows = engine.store.files(ignored=True, search=search, offset=offset, limit=200, library=library)
    else:
        total, rows = engine.store.files(statuses=[wanted], ignored=False, search=search, offset=offset, limit=200,
                                         library=library)
    out = []
    sources = engine.subtitle_sources() if rows else None
    for r in rows:
        info = r["info"] or {}
        out.append({
            "path": r["path"], "status": r["status"], "message": r["message"], "saved_bytes": r["saved_bytes"],
            "size": r["size"], "checked_at": r["checked_at"], "ignored": r["ignored"],
            "tracks": [t for t in info.get("tracks", []) if t["type"] in ("audio", "subtitle")],
            "remove": [t["index"] for t in (r["plan"] or {}).get("remove", [])],
            "mark_forced": (r["plan"] or {}).get("mark_forced", []),
            "files": [with_source(f, sources) for f in info.get("external", [])],
            "remove_files": [f["path"] for f in (r["plan"] or {}).get("remove_files", [])],
        })
    return {"total": total, "files": out,
            "detect": {"audio": detection.audio_enabled(), "subtitles": detection.subtitles_enabled()}}


def with_source(file, sources):
    """A subtitle file beside a film, with where it came from: from_bazarr is True and source names the provider
    (e.g. "opensubtitlescom") when Bazarr downloaded it, False when it didn't, None when Bazarr can't say."""
    if sources is None:
        return {**file, "from_bazarr": None, "source": ""}
    path = posixpath.normpath(file["path"])
    return {**file, "from_bazarr": path in sources, "source": sources.get(path, "")}


def history(query):
    try:
        offset = max(0, int(query.get("offset", 0)))
    except ValueError:
        offset = 0
    total, rows = engine.store.history(limit=200, offset=offset, search=query.get("search", "")[:200])
    return {"total": total, "entries": rows, "runs": engine.store.runs(), "recycle_days": config.recycle_days}


def webhook(payload: dict):
    """Radarr or Sonarr "On Import". Checks the new file straight away."""
    event = payload.get("eventType", "")
    kind = "radarr" if "movie" in payload else "sonarr" if "series" in payload else None
    if event == "Test":
        logger.info(f"Webhook test received from {kind.capitalize() if kind else 'an app'}")
        return {"ok": True, "message": "Dubarr received the test"}
    if event != "Download" or not kind:
        return {"ok": True, "message": f"Ignored {event or 'unknown'} event"}
    paths = [to_local(p, getattr(config, f"{kind}_paths")) for p in webhook_paths(payload, kind)]
    if not paths:
        return {"ok": True, "message": "No file in the event"}
    logger.info(f"{kind.capitalize()} imported {', '.join(paths)}")
    engine.background(engine.imported, paths, wait=True)
    return {"ok": True, "message": f"Checking {len(paths)} file(s)"}


def read_logs(kind):
    lines = []
    try:
        with open(config.log_file, errors="replace") as f:
            # Only keep the end of the file, rather than reading all of it into memory
            raw = deque(f, maxlen=max(config.log_lines * 5, 2000))
    except FileNotFoundError:
        raw = []
    for line in raw:
        match = LOG_LINE.match(line.rstrip("\n"))
        entry = match.groupdict() if match else {"time": "", "level": "INFO", "message": line.rstrip("\n")}
        if kind == "issues" and entry["level"] not in ("ERROR", "CRITICAL", "WARNING"):
            continue
        if kind == "actions" and entry["level"] != "ACTION":
            continue
        lines.append(entry)
    return {"lines": lines[-config.log_lines:]}


def zip_logs():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(glob.glob(os.path.join(config.log_dir, "*.log*"))):
            z.write(path, os.path.basename(path))
    return buffer.getvalue()


def test_connection(target, values):
    """Try a connection with the settings on screen, saved or not."""
    try:
        cfg = config.preview(values)
        if target == "whisper":
            return {"ok": True, "message": detection.test_whisper(cfg)}
        if target == "ollama":
            return {"ok": True, "message": detection.test_ollama(cfg)}
        if target not in ("radarr", "sonarr", "bazarr", "jellyfin"):
            return {"ok": False, "message": "Unknown service"}
        if not getattr(cfg, f"{target}_api_key"):
            return {"ok": False, "message": "Enter the API key first"}
        if target == "bazarr":
            bazarr = Bazarr(cfg)
            version = bazarr.version()
            kept = {code for lib in cfg.library_list for code in lib["subtitles"]}
            clashes = []
            for profile, codes in bazarr.profiles():
                extra = sorted({normalise(c) for c in codes} - kept - {"und"})
                if extra:
                    clashes.append(f"{profile} ({', '.join(name(c) for c in extra)})")
            message = f"Connected to Bazarr {version}"
            if clashes:
                return {"ok": False, "message": f"{message}, but these language profiles download subtitles Dubarr "
                                                f"removes: {'; '.join(clashes)}. Remove those languages in Bazarr, "
                                                "or add them to Dubarr's subtitle languages"}
            return {"ok": True, "message": f"{message}. Its language profiles match Dubarr's"}
        if target == "jellyfin":
            return {"ok": True, "message": f"Connected to Jellyfin {Jellyfin(cfg).version()}"}
        arr = Arr(target, cfg)
        version = arr.version()
        what = "movies" if target == "radarr" else "series"
        return {"ok": True, "message": f"Connected to {arr.name} {version} ({len(arr.items())} {what})"}
    except (ServiceError, detection.DetectError, ValueError) as e:
        return {"ok": False, "message": str(e)}


def serve(e):
    global engine
    engine = e
    server = ThreadingHTTPServer(("", config.web_port), Handler)
    server.daemon_threads = True
    logger.info(f"Starting web UI at {config.web_host}:{config.web_port}")
    if not auth.has_account():
        logger.log("STARTUP", f"First run: open the web UI and use setup code {auth.SETUP_CODE} to create your login")
    server.serve_forever()
