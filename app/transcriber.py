"""Speech to text through Groq Whisper (OpenAI-compatible API)."""

from __future__ import annotations

import logging
import time
from typing import Callable

import openai

from app.api_errors import classify
from app.models import Recording, Transcript

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PROMPT_CHARS = 600  # Whisper reads at most ~224 prompt tokens

_LANGUAGE_ALIASES = {"english": "en", "hindi": "hi"}


class TranscriptionError(Exception):
    """Shown to the user. ``retryable`` says whether trying again may help."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def build_vocabulary_prompt(terms: list[str]) -> str | None:
    """Comma list of technical terms so Whisper spells them right."""
    picked: list[str] = []
    length = len("Technical terms: .")
    for term in terms:
        term = term.strip()
        if not term or term in picked:
            continue
        if length + len(term) + 2 > MAX_PROMPT_CHARS:
            break
        picked.append(term)
        length += len(term) + 2
    return f"Technical terms: {', '.join(picked)}." if picked else None


def normalize_language(language: str | None) -> str | None:
    """'auto' → None (Whisper detects); 'English' → 'en'; ISO codes pass through."""
    if not language:
        return None
    lang = language.strip().lower()
    if lang in ("", "auto"):
        return None
    return _LANGUAGE_ALIASES.get(lang, lang)


def make_groq_client(api_key: str, base_url: str, timeout: float = 30.0) -> openai.OpenAI:
    # Retries are handled here (once, with a clear message), not by the SDK.
    return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)


class Transcriber:
    def __init__(
        self,
        client,
        model: str = "whisper-large-v3-turbo",
        language: str | None = "auto",
        vocabulary: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.model = model
        self.language = normalize_language(language)
        self.prompt = build_vocabulary_prompt(vocabulary or [])
        self._sleep = sleep

    def transcribe(self, recording: Recording) -> Transcript:
        if not recording.audio:
            raise TranscriptionError("No audio was recorded.", retryable=False)
        if len(recording.audio) > MAX_UPLOAD_BYTES:
            raise TranscriptionError("Recording is larger than Groq's 25 MB limit.", retryable=False)

        started = time.monotonic()
        text = self._call_with_retry(recording)
        return Transcript(
            text=text.strip(),
            audio_seconds=recording.duration_s,
            latency_s=round(time.monotonic() - started, 2),
            model=self.model,
        )

    def _call_with_retry(self, recording: Recording) -> str:
        for attempt in (1, 2):
            try:
                return self._call(recording)
            except openai.OpenAIError as exc:
                failure = classify(exc)
                if failure.transient and attempt == 1:
                    log.warning("Transcription failed (%s); retrying once in %.1fs", failure.message, failure.wait)
                    self._sleep(failure.wait)
                    continue
                suffix = " Press Retry." if failure.retryable else ""
                raise TranscriptionError(failure.message + suffix, failure.retryable) from exc
        raise AssertionError("unreachable")

    def _call(self, recording: Recording) -> str:
        kwargs = {
            "model": self.model,
            "file": (recording.filename, recording.audio),
            "response_format": "json",
            "temperature": 0,
        }
        if self.language:
            kwargs["language"] = self.language
        if self.prompt:
            kwargs["prompt"] = self.prompt
        result = self.client.audio.transcriptions.create(**kwargs)
        return getattr(result, "text", None) or (result if isinstance(result, str) else "")
