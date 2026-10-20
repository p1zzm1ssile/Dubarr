"""Guess the language of a track: Whisper listens to the audio, and subtitles are read by a small
built-in detector or by Ollama. A guess is only ever a suggestion. Nothing is tagged until you accept it.

Keys for Whisper and Ollama are optional, sent as a Bearer token, and only ever to an https:// address.
Redirects aren't followed, so a key can't be passed on to another server or downgraded to http://.
"""

import json
import subprocess
import threading

import requests

import utils.config as config
from core.probe import MediaInfo
from core.tagging import TagError, audio_sample, subtitle_lines
from services.connections import ServiceError, _error
from utils.languages import LANGUAGES, name, normalise
from utils.proc import wrap

# A guess this sure is shown as likely right; below it, the suggestion says to check by listening or reading
CONFIDENT = 0.8
# Samples Whisper hears before giving its best guess, for films where the first one has no talking
AUDIO_TRIES = 3
# Whisper on a CPU, loading its model on the first request, can take a while
MIN_TIMEOUT = 120
OLLAMA_PROMPT = (
    "Which language are these film subtitles written in? Reply with JSON only, like "
    '{"language": "eng", "confidence": 0.9}, where language is the ISO 639-2 code (eng, fra, deu, spa, jpn...) '
    "and confidence is from 0 to 1. The subtitles:\n\n"
)


class DetectError(Exception):
    pass


def audio_enabled(cfg=config) -> bool:
    return bool(cfg.whisper_enabled)


def subtitles_enabled(cfg=config) -> bool:
    return cfg.subtitle_detector != config.SUBTITLES_OFF


def _request(method, url, key, name, timeout, **kwargs):
    if key and not url.lower().startswith("https://"):
        raise DetectError(f"{name}'s API key is only sent to an https:// address")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, allow_redirects=False, **kwargs)
        if response.is_redirect:
            raise DetectError(f"{name} answered with a redirect. Use the address it redirects to")
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as e:
        raise _error(e, name)


def _address(url, name):
    if url in ("", "http://", "https://"):
        raise DetectError(f"Enter {name}'s address on the Language detection tab first")
    return url


def _timeout(cfg):
    return max(cfg.request_timeout, MIN_TIMEOUT)


def _guess(code, confidence, by, **extra):
    return {"language": code, "name": name(code), "confidence": round(confidence, 2), "by": by,
            "sure": confidence >= CONFIDENT, **extra}


def _whisper(cfg, sample: bytes) -> dict:
    url = _address(cfg.whisper_url, "Whisper")
    return _request("POST", url + "/detect-language", cfg.whisper_api_key, "Whisper", _timeout(cfg),
                    params={"encode": "true"}, files={"audio_file": ("sample.mp3", sample, "audio/mpeg")})


def detect_audio(info: MediaInfo, index: int, cfg=config) -> dict:
    """Whisper's best guess for an audio track, from up to AUDIO_TRIES samples."""
    if not audio_enabled(cfg):
        raise DetectError("Whisper isn't turned on. It's on the Language detection tab in Settings")
    best = None
    for part in range(AUDIO_TRIES):
        answer = _whisper(cfg, audio_sample(info, index, part=part))
        code = normalise(str(answer.get("language_code") or ""))
        try:
            confidence = min(max(float(answer.get("confidence") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if code in LANGUAGES and (best is None or confidence > best["confidence"]):
            best = _guess(code, confidence, "Whisper", part=part)
        if best and best["sure"]:
            break
    if not best:
        raise DetectError("Whisper couldn't tell the language. Listen to a sample instead")
    return best


_identifier = None
_identifier_lock = threading.Lock()


def _built_in(text: str) -> tuple:
    """(language code, confidence) from py3langid, which is loaded on first use."""
    global _identifier
    with _identifier_lock:
        if _identifier is None:
            from py3langid.langid import MODEL_FILE, LanguageIdentifier
            _identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    code, confidence = _identifier.classify(text)
    return normalise(code), float(confidence)


def _ollama(cfg, text: str) -> tuple:
    url = _address(cfg.ollama_url, "Ollama")
    answer = _request("POST", url + "/api/generate", cfg.ollama_api_key, "Ollama", _timeout(cfg), json={
        "model": cfg.ollama_model, "prompt": OLLAMA_PROMPT + text, "stream": False, "format": "json",
        "options": {"temperature": 0}})
    try:
        reply = json.loads(answer.get("response") or "{}")
        code = normalise(str(reply.get("language") or ""))
        confidence = min(max(float(reply.get("confidence") or 0), 0.0), 1.0)
    except (AttributeError, TypeError, ValueError):
        raise DetectError("Ollama's answer couldn't be read. Try another model")
    return code, confidence


def detect_subtitles(info: MediaInfo, index: int, cfg=config) -> dict:
    """The built-in detector's or Ollama's guess for a text subtitle track."""
    if not subtitles_enabled(cfg):
        raise DetectError("Subtitle detection is off. It's on the Language detection tab in Settings")
    # Only the words go out, at most a few thousand characters of them
    text = "\n".join(subtitle_lines(info, index, limit=60))[:3000]
    if len(text) < 20:
        raise DetectError("Too few words in this track to tell. Read it instead")
    if cfg.subtitle_detector == config.SUBTITLES_OLLAMA:
        code, confidence = _ollama(cfg, text)
        by = "Ollama"
    else:
        code, confidence = _built_in(text)
        by = "Dubarr"
    if code not in LANGUAGES:
        raise DetectError(f"{by} didn't recognise the language. Read the track instead")
    return _guess(code, confidence, by)


def detect(info: MediaInfo, index: int, cfg=config) -> dict:
    for t in info.tracks:
        if t.index == index and t.type == "audio":
            return detect_audio(info, index, cfg)
        if t.index == index and t.type == "subtitle":
            return detect_subtitles(info, index, cfg)
    raise TagError(f"Track {index} isn't an audio or subtitle track in this file")


def silent_sample() -> bytes:
    """A second of silence as MP3, for checking Whisper answers."""
    result = subprocess.run(wrap(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                  "-t", "1", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"]),
                            capture_output=True, timeout=30)
    if result.returncode != 0 or not result.stdout:
        raise DetectError("Couldn't make a test sample with ffmpeg")
    return result.stdout


def test_whisper(cfg) -> str:
    answer = _whisper(cfg, silent_sample())
    if "language_code" not in answer:
        raise DetectError("That address answered, but not like whisper-asr-webservice")
    return "Connected to Whisper. It answered a language check"


def test_ollama(cfg) -> str:
    url = _address(cfg.ollama_url, "Ollama")
    answer = _request("GET", url + "/api/tags", cfg.ollama_api_key, "Ollama", cfg.request_timeout)
    models = {m.get("name", "") for m in (answer or {}).get("models", [])}
    wanted = cfg.ollama_model if ":" in cfg.ollama_model else cfg.ollama_model + ":latest"
    if wanted not in models and cfg.ollama_model not in models:
        raise DetectError(f"Connected to Ollama, but it doesn't have {cfg.ollama_model}. "
                          f"Run ollama pull {cfg.ollama_model} on that server")
    return f"Connected to Ollama, with {cfg.ollama_model} ready"


__all__ = ["DetectError", "ServiceError", "detect", "test_whisper", "test_ollama"]
