"""Login for the web UI.

The account lives in auth.json in CONFIG_DIR, with the password stored as a
scrypt hash. To reset a forgotten password, delete auth.json and restart: the
web UI will ask to create an account again.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import utils.config as config

AUTH_FILE = os.path.join(config.config_dir, "auth.json")
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 5 * 60
FAILURE_MEMORY_SECONDS = 15 * 60  # failures older than this are forgotten

# Headers a reverse proxy adds. A request carrying any of them never gets the LAN
# bypass, since behind a proxy every visitor appears to come from the proxy's
# own (often private) address.
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host",
                 "X-Forwarded-Proto", "CF-Connecting-IP", "True-Client-IP")

PASSWORD_RULES = [
    ("length", "At least 8 characters", lambda p: len(p) >= 8),
    ("upper", "An uppercase letter", lambda p: re.search(r"[A-Z]", p)),
    ("lower", "A lowercase letter", lambda p: re.search(r"[a-z]", p)),
    ("number", "A number", lambda p: re.search(r"[0-9]", p)),
    ("special", "A special character, like ! ? # or @", lambda p: re.search(r"[^A-Za-z0-9]", p)),
]

_lock = threading.Lock()
_sessions = {}  # token -> (username, expires)
_failures = {}  # ip -> (count, last_failure, locked_until)
_account = None  # cached contents of auth.json
_account_loaded = False

# A one-time code, printed in the log, that's needed to create the first account.
# Being on the local network isn't proof enough: a reverse proxy that adds no
# forwarding headers, or Docker's own port forwarding, can make a visitor from
# the internet look local.
SETUP_CODE = "-".join(secrets.token_hex(2).upper() for _ in range(3))


def password_problems(password: str) -> list:
    return [text for _, text, check in PASSWORD_RULES if not check(password or "")]


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64)


def _load():
    global _account, _account_loaded
    if not _account_loaded:
        try:
            with open(AUTH_FILE) as f:
                _account = json.load(f)
        except (OSError, ValueError):
            _account = None
        _account_loaded = True
    return _account


def _save(data):
    os.makedirs(config.config_dir, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)  # in case an older version left it behind
    os.replace(tmp, AUTH_FILE)
    global _account, _account_loaded
    _account, _account_loaded = data, True


def has_account() -> bool:
    return _load() is not None


def username() -> str:
    return (_load() or {}).get("username", "")


def set_password(user: str, password: str):
    """Create the account, or change its password. Raises ValueError if not allowed."""
    user = (user or "").strip()
    if not 1 <= len(user) <= 64:
        raise ValueError("Username must be 1 to 64 characters")
    problems = password_problems(password)
    if problems:
        raise ValueError("Password needs: " + ", ".join(p.lower() for p in problems))
    salt = secrets.token_bytes(16)
    _save({"username": user, "salt": salt.hex(), "hash": _hash(password, salt).hex()})
    with _lock:
        _sessions.clear()  # sign everyone out after a password change


def check_password(user: str, password: str) -> bool:
    data = _load()
    if not data:
        return False
    candidate = _hash(password or "", bytes.fromhex(data["salt"]))
    user_ok = hmac.compare_digest((user or "").strip().lower(), data["username"].lower())
    return hmac.compare_digest(candidate, bytes.fromhex(data["hash"])) and user_ok


def _failure_entry(ip, now):
    count, last, until = _failures.get(ip, (0, 0, 0))
    if until <= now and now - last > FAILURE_MEMORY_SECONDS:
        count = 0
    return count, last, until


def locked_for(ip: str) -> int:
    with _lock:
        _, _, until = _failure_entry(ip, time.time())
        return max(0, int(until - time.time()))


def login(ip: str, user: str, password: str):
    """Return a session token, or None. Locks an address out after repeated failures.

    The attempt is counted before the (slow) password check, so many attempts
    sent at once can't all slip in before the lockout starts.
    """
    now = time.time()
    with _lock:
        count, last, until = _failure_entry(ip, now)
        if until > now:
            return None
        count += 1
        until = now + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0
        _failures[ip] = (0 if until else count, now, until)

    if not check_password(user, password):
        return None

    with _lock:
        _failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        _sessions[token] = (username(), time.time() + SESSION_SECONDS)
        return token


def session_user(token: str):
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if entry is None:
            return None
        user, expires = entry
        if expires < time.time():
            _sessions.pop(token, None)
            return None
        return user


def logout(token: str):
    with _lock:
        _sessions.pop(token, None)


def is_proxied(headers) -> bool:
    return any(headers.get(h) for h in PROXY_HEADERS)


def _address(ip: str):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def lan_bypass(ip: str, headers) -> bool:
    """Only for the networks listed in the settings, and never through a proxy."""
    if not config.auth_lan_bypass or is_proxied(headers):
        return False
    address = _address(ip)
    return address is not None and any(address in net for net in config.local_networks)


def can_create_account(code: str) -> bool:
    return not has_account() and hmac.compare_digest((code or "").strip().upper(), SETUP_CODE)
