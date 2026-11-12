from types import SimpleNamespace

import httpx2 as httpx  # openai>=3 builds its errors on httpx2
import openai
import pytest

from app.models import Recording
from app.transcriber import (
    MAX_PROMPT_CHARS,
    WHISPER_PREFIX,
    Transcriber,
    TranscriptionError,
    build_vocabulary_prompt,
    normalize_language,
)

REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")


def status_error(cls, code, headers=None):
    return cls("boom", response=httpx.Response(code, request=REQ, headers=headers or {}), body=None)


class FakeClient:
    """Mimics client.audio.transcriptions.create; plays back a script of results."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


def rec(audio=b"fLaC...", seconds=2.0):
    return Recording(audio=audio, sample_rate=16000, duration_s=seconds, speech_detected=True)


def make(client, **kw):
    sleeps = []
    t = Transcriber(client, sleep=sleeps.append, **kw)
    return t, sleeps


def test_success_sends_expected_request():
    client = FakeClient("  refactor parse_config in utils.py  ")
    t, sleeps = make(client, language="English", vocabulary=["pytest", "PySide6"])
    out = t.transcribe(rec())
    assert out.text == "refactor parse_config in utils.py"
    assert out.audio_seconds == 2.0
    assert out.model == "whisper-large-v3-turbo"
    call = client.calls[0]
    assert call["language"] == "en"
    assert "pytest" in call["prompt"] and "PySide6" in call["prompt"]
    assert call["file"][0] == "audio.flac"
    assert call["temperature"] == 0
    assert sleeps == []


def test_auto_language_omits_param():
    client = FakeClient("hi")
    make(client, language="auto")[0].transcribe(rec())
    assert "language" not in client.calls[0]


def test_retries_once_on_rate_limit_using_retry_after():
    client = FakeClient(status_error(openai.RateLimitError, 429, {"retry-after": "3"}), "ok")
    t, sleeps = make(client)
    assert t.transcribe(rec()).text == "ok"
    assert sleeps == [3.0]
    assert len(client.calls) == 2


def test_retry_wait_is_capped():
    client = FakeClient(status_error(openai.RateLimitError, 429, {"retry-after": "120"}), "ok")
    t, sleeps = make(client)
    t.transcribe(rec())
    assert sleeps == [10.0]


def test_rate_limit_twice_gives_clear_message():
    client = FakeClient(
        status_error(openai.RateLimitError, 429), status_error(openai.RateLimitError, 429)
    )
    t, _ = make(client)
    with pytest.raises(TranscriptionError, match="free-tier limit") as ei:
        t.transcribe(rec())
    assert ei.value.retryable
    assert len(client.calls) == 2


def test_timeout_then_success():
    client = FakeClient(openai.APITimeoutError(request=REQ), "done")
    assert make(client)[0].transcribe(rec()).text == "done"


def test_connection_error_twice():
    client = FakeClient(openai.APIConnectionError(request=REQ), openai.APIConnectionError(request=REQ))
    with pytest.raises(TranscriptionError, match="internet"):
        make(client)[0].transcribe(rec())


def test_bad_key_not_retried():
    client = FakeClient(status_error(openai.AuthenticationError, 401))
    t, sleeps = make(client)
    with pytest.raises(TranscriptionError, match="API key") as ei:
        t.transcribe(rec())
    assert not ei.value.retryable
    assert len(client.calls) == 1 and sleeps == []


def test_bad_request_not_retried():
    client = FakeClient(status_error(openai.BadRequestError, 400))
    with pytest.raises(TranscriptionError, match="400"):
        make(client)[0].transcribe(rec())
    assert len(client.calls) == 1


def test_empty_audio_rejected_without_call():
    client = FakeClient()
    with pytest.raises(TranscriptionError):
        make(client)[0].transcribe(rec(audio=b""))
    assert client.calls == []


def test_oversized_audio_rejected():
    client = FakeClient()
    with pytest.raises(TranscriptionError, match="25 MB"):
        make(client)[0].transcribe(rec(audio=b"x" * (25 * 1024 * 1024 + 1)))


def test_vocabulary_prompt_is_capped_and_deduped():
    terms = ["alpha", "alpha", " ", *[f"term{i:03d}" for i in range(500)]]
    prompt = build_vocabulary_prompt(terms)
    assert len(prompt) <= MAX_PROMPT_CHARS
    assert prompt.count("alpha") == 1
    assert prompt.startswith("Hey Claude")
    assert build_vocabulary_prompt([]) == WHISPER_PREFIX.strip()


@pytest.mark.parametrize(
    "given,expected",
    [("auto", None), ("", None), (None, None), ("English", "en"), ("hi", "hi"), ("Hindi", "hi")],
)
def test_normalize_language(given, expected):
    assert normalize_language(given) == expected
