"""Language detection with Whisper, Ollama and the built-in detector, against fake servers."""

import json
import threading
import time
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


def test_detect_again_listens_elsewhere(monkeypatch):
    film = SimpleNamespace(tracks=[SimpleNamespace(index=1, type="audio")])
    heard = []
    monkeypatch.setattr(detect, "audio_sample", lambda info, index, part: heard.append(part) or b"mp3")
    monkeypatch.setattr(detect, "_whisper", lambda cfg, sample: {"language_code": "en", "confidence": 0.2})
    detect.detect(film, 1, settings(), start=3)
    assert heard == [3, 4, 5]


# Detection in the background, automatic tagging and the wait before tracks are removed

@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "movies"
    root.mkdir()
    monkeypatch.setattr(config, "library_list", [{"path": str(root), "audio": ["eng"], "subtitles": ["eng"]}])
    for key, value in dict(report_only=True, recycle_days=7, recycle_folder="", process_hardlinked=False,
                           active_from=0, active_until=0, max_changes_per_pass=25, keep_original_audio=False,
                           bazarr_other_subtitles=config.OTHER_SUBTITLES_KEEP, min_saving_mb=0,
                           whisper_enabled=True, whisper_api_key="", subtitle_detector=config.SUBTITLES_OFF,
                           detect_in_background=True, auto_tag_confidence=0, trust_confidence=97).items():
        monkeypatch.setattr(config, key, value)
    for kind in ("radarr", "sonarr", "jellyfin", "bazarr"):
        monkeypatch.setattr(config, f"{kind}_enabled", False)
    return root


@pytest.fixture
def engine(tmp_path):
    from core.engine import Engine
    from core.store import Store
    return Engine(Store(str(tmp_path / "dubarr.db")))


def whisper_says(monkeypatch, code, confidence):
    fake = Fake([(200, {"language_code": code, "confidence": confidence})] * 10)
    monkeypatch.setattr(config, "whisper_url", fake.url)
    return fake


@needs_tools
def test_background_detection_keeps_suggestions_only(library, engine, monkeypatch):
    from core import plan as plans
    film = make_video(str(library / "x.mkv"), audio=("eng", ""))
    engine.scan()
    fake = whisper_says(monkeypatch, "it", 0.95)
    try:
        engine.detect_pass()
        engine.detect_pass()  # already guessed: Whisper isn't asked again
    finally:
        fake.close()
    assert len(fake.requests) == 1
    row = engine.store.file(film)
    assert row["status"] == plans.UNKNOWN_AUDIO  # a suggestion, nothing tagged
    assert {i: (g["language"], g["by"]) for i, g in engine.guesses(row).items()} == {2: ("ita", "Whisper")}


@needs_tools
def test_auto_tag_waits_for_a_check_unless_very_sure(library, engine, monkeypatch):
    from core import plan as plans
    from core.engine import AUTO_TAGGED
    unsure = make_video(str(library / "a.mkv"), audio=("eng", ""))
    engine.scan()
    monkeypatch.setattr(config, "auto_tag_confidence", 90)
    fake = whisper_says(monkeypatch, "it", 0.93)
    try:
        engine.detect_pass()
    finally:
        fake.close()
    row = engine.store.file(unsure)
    assert [t["language"] for t in row["info"]["tracks"] if t["type"] == "audio"] == ["eng", "ita"]
    # Italian isn't wanted, but the guess could be wrong: nothing is removed until it's checked
    assert row["status"] == AUTO_TAGGED and "93% sure" in row["message"]
    assert engine.store.history()[1][0]["state"] == "tagged"
    engine.looks_right(unsure, wait=True)
    assert engine.store.file(unsure)["status"] == plans.CHANGE

    sure = make_video(str(library / "b.mkv"), audio=("eng", ""))
    engine.scan()
    fake = whisper_says(monkeypatch, "it", 0.99)
    try:
        engine.detect_pass()
    finally:
        fake.close()
    assert engine.store.file(sure)["status"] == plans.CHANGE  # sure enough to skip the wait


@needs_tools
def test_accept_all_and_tagging_by_hand(library, engine, monkeypatch):
    from core import plan as plans
    from core.engine import AUTO_TAGGED
    low = make_video(str(library / "low.mkv"), audio=("eng", ""))
    high = make_video(str(library / "high.mkv"), audio=("eng", ""))
    engine.scan()
    fake = whisper_says(monkeypatch, "it", 0.8)
    try:
        engine.guess_file(engine.store.file(low))
    finally:
        fake.close()
    fake = whisper_says(monkeypatch, "it", 0.92)
    try:
        engine.guess_file(engine.store.file(high))
    finally:
        fake.close()
    engine.accept_guesses(90)
    assert engine.store.file(low)["status"] == plans.UNKNOWN_AUDIO  # 80% stays a suggestion
    assert engine.store.file(high)["status"] == AUTO_TAGGED
    # Tagging by hand means you've looked at it
    engine.tag(high, {"2": "fra"})
    assert engine.store.file(high)["status"] == plans.CHANGE


@needs_tools
def test_stop_drops_automatic_tagging_queued_behind_a_job(library, engine, monkeypatch):
    """Stop during a job used to let automatic tagging waiting for its turn start straight after."""
    from core import plan as plans
    first = make_video(str(library / "first.mkv"), audio=("eng", ""))
    second = make_video(str(library / "second.mkv"), audio=("eng", ""))
    engine.scan()
    fake = whisper_says(monkeypatch, "it", 0.92)
    try:
        for path in (first, second):
            engine.guess_file(engine.store.file(path))
    finally:
        fake.close()

    # Accept all stops at the next file
    real_tag = engine.tag

    def tag_then_stop(*args, **kwargs):
        result = real_tag(*args, **kwargs)
        engine.stop()
        return result

    monkeypatch.setattr(engine, "tag", tag_then_stop)
    engine.accept_guesses(90)
    statuses = sorted(engine.store.file(p)["status"] for p in (first, second))
    assert statuses.count(plans.UNKNOWN_AUDIO) == 1
    monkeypatch.setattr(engine, "tag", real_tag)

    # Tagging that was waiting while Stop was pressed is dropped; tags you save yourself still go
    left = next(p for p in (first, second) if engine.store.file(p)["status"] == plans.UNKNOWN_AUDIO)
    asked = time.time() - 1
    engine.stop()
    guesses = engine.guesses(engine.store.file(left))
    assert engine._tag_guesses(left, guesses, asked) == "Stopped"
    assert engine.store.file(left)["status"] == plans.UNKNOWN_AUDIO
    engine.tag(left, {i: g["language"] for i, g in guesses.items()})
    assert engine.store.file(left)["status"] != plans.UNKNOWN_AUDIO
