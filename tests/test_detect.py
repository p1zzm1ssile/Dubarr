"""Language detection with Whisper, Ollama and the built-in detector, against fake servers."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import utils.config as config
from conftest import make_video, needs_tools
from core import detect
from core.probe import probe


class Fake:
    """A pretend Whisper or Ollama: answers come from a list, requests are kept to look at."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                code, answer = fake.answers.pop(0)
                data = json.dumps(answer).encode()
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "http://elsewhere.example/")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def settings(**changes):
    values = dict(whisper_enabled=True, whisper_url="", whisper_api_key="", subtitle_detector=config.SUBTITLES_BUILT_IN,
                  ollama_url="", ollama_model="qwen2.5:3b", ollama_api_key="", request_timeout=5)
    values.update(changes)
    return SimpleNamespace(**values)


@needs_tools
def test_whisper_tries_another_part_when_unsure(tmp_path):
    film = probe(make_video(str(tmp_path / "x.mkv"), audio=("",)))
    fake = Fake([(200, {"language_code": "en", "confidence": 0.3}), (200, {"language_code": "fr", "confidence": 0.95})])
    try:
        guess = detect.detect(film, 1, settings(whisper_url=fake.url))
    finally:
        fake.close()
    assert guess == {"language": "fra", "name": "French", "confidence": 0.95, "by": "Whisper", "sure": True, "part": 1}
    assert [r["path"] for r in fake.requests] == ["/detect-language?encode=true"] * 2
    assert b"audio/mpeg" in fake.requests[0]["body"]  # the sample is sent as an MP3
    assert "Authorization" not in fake.requests[0]["headers"]  # no key, no header


@needs_tools
def test_whisper_unsure_after_every_try(tmp_path):
    film = probe(make_video(str(tmp_path / "x.mkv"), audio=("",)))
    fake = Fake([(200, {"language_code": "en", "confidence": 0.4}), (200, {"language_code": "xx", "confidence": 0.9}),
                 (200, {"language_code": "de", "confidence": 0.2})])
    try:
        guess = detect.detect(film, 1, settings(whisper_url=fake.url))
    finally:
        fake.close()
    assert (guess["language"], guess["sure"], guess["part"]) == ("eng", False, 0)  # the best one, marked unsure
    assert len(fake.requests) == 3


def test_subtitles_built_in(monkeypatch):
    film = SimpleNamespace(tracks=[SimpleNamespace(index=2, type="subtitle")])
    monkeypatch.setattr(detect, "subtitle_lines", lambda info, index, limit: [
        "Je ne sais pas ce que tu veux dire.", "On se voit demain matin ?", "Oui, bien sûr."])
    guess = detect.detect(film, 2, settings())
    assert (guess["language"], guess["by"], guess["sure"]) == ("fra", "Dubarr", True)

    monkeypatch.setattr(detect, "subtitle_lines", lambda info, index, limit: ["Ok."])
    with pytest.raises(detect.DetectError, match="Too few words"):
        detect.detect(film, 2, settings())
    with pytest.raises(detect.DetectError, match="off"):
        detect.detect(film, 2, settings(subtitle_detector=config.SUBTITLES_OFF))


def test_subtitles_with_ollama(monkeypatch):
    film = SimpleNamespace(tracks=[SimpleNamespace(index=2, type="subtitle")])
    monkeypatch.setattr(detect, "subtitle_lines", lambda info, index, limit: ["Wo bist du gewesen? Ich habe dich gesucht."])
    fake = Fake([(200, {"response": json.dumps({"language": "ger", "confidence": 0.9})}),
                 (200, {"response": json.dumps({"language": "elvish", "confidence": 1})}),
                 (200, {"models": [{"name": "qwen2.5:3b"}]}), (200, {"models": [{"name": "llama3.2:latest"}]})])
    cfg = settings(subtitle_detector=config.SUBTITLES_OLLAMA, ollama_url=fake.url)
    try:
        guess = detect.detect(film, 2, cfg)
        assert (guess["language"], guess["by"]) == ("deu", "Ollama")
        sent = json.loads(fake.requests[0]["body"])
        assert sent["model"] == "qwen2.5:3b" and sent["stream"] is False and "gesucht" in sent["prompt"]
        with pytest.raises(detect.DetectError, match="didn't recognise"):
            detect.detect(film, 2, cfg)
        assert "ready" in detect.test_ollama(cfg)
        with pytest.raises(detect.DetectError, match="ollama pull qwen2.5:3b"):
            detect.test_ollama(cfg)
    finally:
        fake.close()


def test_key_never_sent_over_http(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing should be sent")
    monkeypatch.setattr(detect.requests, "request", refuse)
    with pytest.raises(detect.DetectError, match="https"):
        detect._request("POST", "http://192.168.1.10:9000/detect-language", "s3cret-key", "Whisper", 5)


def test_key_sent_as_bearer_over_https_without_redirects(monkeypatch):
    seen = {}

    def answer(method, url, **kwargs):
        seen.update(kwargs, url=url)
        return SimpleNamespace(is_redirect=False, raise_for_status=lambda: None, json=lambda: {"ok": True})
    monkeypatch.setattr(detect.requests, "request", answer)
    detect._request("GET", "https://ollama.example/api/tags", "s3cret-key", "Ollama", 5)
    assert seen["headers"] == {"Authorization": "Bearer s3cret-key"}
    assert seen["allow_redirects"] is False
    assert "s3cret-key" not in seen["url"]


def test_redirect_isnt_followed():
    fake = Fake([(302, {})])
    try:
        with pytest.raises(detect.DetectError, match="redirect"):
            detect._request("GET", fake.url + "/api/tags", "", "Ollama", 5)
    finally:
        fake.close()
    assert len(fake.requests) == 1


@pytest.fixture
def saved_settings(monkeypatch):
    """Settings saved by a test are put back afterwards."""
    original = dict(config.values)
    monkeypatch.setattr(config, "values", dict(original))
    yield
    config._write(original)
    config.apply(config.globals_namespace, original)


def test_settings_refuse_a_key_for_an_http_address(saved_settings):
    errors = config.update({"whisper_url": "http://192.168.1.10:9000", "whisper_api_key": "s3cret-key"})
    assert "https" in errors["whisper_api_key"]
    with pytest.raises(ValueError, match="https"):
        config.preview({"ollama_url": "http://192.168.1.10:11434", "ollama_api_key": "s3cret-key"})
    assert config.update({"whisper_url": "http://192.168.1.10:9000"}) == {}  # no key, http is fine
    assert config.update({"whisper_url": "https://whisper.example", "whisper_api_key": "s3cret-key"}) == {}
    # The saved key isn't sent to the browser, and isn't reused with a new address
    assert config.public_values()["whisper_api_key"] == {"set": True}
    assert "whisper_api_key" in config.update({"whisper_url": "https://other.example"})
    assert {"whisper_api_key", "ollama_api_key"} <= config.SECRET_KEYS  # so the logger masks them


def test_keys_masked_in_the_log(monkeypatch):
    from utils.logger import redact
    monkeypatch.setattr(config, "values", {**config.values, "ollama_api_key": "s3cret-key"})
    record = {"message": "Ollama rejected s3cret-key at https://x/?token=abc"}
    redact(record)
    assert "s3cret-key" not in record["message"] and "abc" not in record["message"]
