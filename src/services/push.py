"""Notifications to your phone: ntfy, Gotify or a Discord webhook, worked out from the address.

  https://discord.com/api/webhooks/...      Discord
  https://gotify.example/message?token=...  Gotify
  anything else, e.g. https://ntfy.sh/topic  ntfy (a user:password@ in the address is sent as a login)

The address is a secret (it's enough to send to you), so it's never logged.
"""

from urllib.parse import urlsplit, urlunsplit

import requests

DISCORD_LIMIT = 2000


class PushError(Exception):
    pass


def kind(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.endswith(("discord.com", "discordapp.com")) and "/api/webhooks/" in parts.path:
        return "discord"
    if parts.path.rstrip("/").endswith("/message") and "token=" in parts.query:
        return "gotify"
    return "ntfy"


def send(url: str, title: str, message: str, timeout: int = 15):
    """Send one notification. Raises PushError, with a reason that doesn't include the address."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise PushError("The address needs to start with http:// or https://")
    how = kind(url)
    if how == "discord":
        text = f"**{title}**\n{message}"
        request = dict(url=url, json={"content": text[:DISCORD_LIMIT]})
    elif how == "gotify":
        request = dict(url=url, json={"title": title, "message": message, "priority": 5})
    else:
        # ntfy takes JSON at its root address, naming the topic, which keeps titles with any letters in them working
        parts = urlsplit(url)
        topic = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if not topic:
            raise PushError("The ntfy address needs a topic at the end, e.g. https://ntfy.sh/my-dubarr")
        root = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/")[:-len(topic)] or "/", "", ""))
        request = dict(url=root, json={"topic": topic, "title": title, "message": message, "tags": ["dubarr"]})
    try:
        response = requests.post(**request, timeout=timeout, allow_redirects=False)
    except requests.RequestException as e:
        raise PushError(f"Couldn't reach {urlsplit(url).hostname}: {type(e).__name__}") from None
    if response.status_code >= 300:
        raise PushError(f"{how.capitalize()} answered {response.status_code}"
                        + (" (check the address or token)" if response.status_code in (401, 403, 404) else ""))
    return how
