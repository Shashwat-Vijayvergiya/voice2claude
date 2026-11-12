"""Rewrite a transcript into a structured prompt with one Groq LLM call.

Never raises: if the call fails, the cleaned transcript comes back with
``fallback_error`` set, so the user always has something to review.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

import openai
from pydantic import ValidationError

from app import prompts
from app.api_errors import classify
from app.checker import clean_fillers
from app.models import Compressed, CompressionContext, LLMOutput, Mode

log = logging.getLogger(__name__)

MAX_COMPLETION_TOKENS = 2048


def word_count(text: str) -> int:
    return len(text.split())


# Words that point back at an earlier command: "now add auth to it", "also do that for X".
_FOLLOW_UP_RE = re.compile(
    r"\b(it|its|that|this|those|these|them|they|there|now|also|same|again|instead|too|then|previous|last|above)\b",
    re.I,
)


def is_follow_up(transcript: str, context: CompressionContext) -> bool:
    """Short commands skip the LLM, unless they refer back to a recent command."""
    return bool(context.recent_commands) and _FOLLOW_UP_RE.search(transcript) is not None


class Compressor:
    def __init__(
        self,
        client,
        model: str = "openai/gpt-oss-120b",
        strength: str = "balanced",
        reasoning_effort: str | None = "low",
        skip_under_words: int = 30,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.model = model
        self.strength = strength
        self.reasoning_effort = reasoning_effort
        self.skip_under_words = skip_under_words
        self._sleep = sleep
        self._use_schema = True  # strict JSON schema; dropped if the model rejects it

    def compress(
        self,
        transcript: str,
        forced_mode: Mode | None = None,
        context: CompressionContext | None = None,
        force: bool = False,
    ) -> Compressed:
        """``force`` (the Compress button) bypasses the short-command skip."""
        transcript = transcript.strip()
        cleaned = clean_fillers(transcript)
        base = dict(original=transcript, mode=forced_mode, model=self.model)

        if not transcript:
            return Compressed(command="", skipped=True, skip_reason="empty", **base)
        if forced_mode is Mode.DICTATION:
            return Compressed(command=cleaned, skipped=True, skip_reason="dictation", **base)
        context = context or CompressionContext()
        short = word_count(transcript) < self.skip_under_words
        if short and not force and not is_follow_up(transcript, context):
            return Compressed(command=cleaned, skipped=True, skip_reason="short", **base)

        started = time.monotonic()
        messages = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.build_user_message(
                transcript,
                forced_mode.value if forced_mode else None,
                self.strength,
                context.project_notes,
                context.recent_commands,
            )},
        ]
        try:
            out = self._call_with_retry(messages)
        except _Failed as exc:
            log.warning("Compression failed, showing transcript instead: %s", exc)
            return Compressed(
                command=cleaned,
                fallback_error=str(exc),
                latency_s=round(time.monotonic() - started, 2),
                **base,
            )

        mode = forced_mode or out.mode
        command = cleaned if mode is Mode.DICTATION else out.command.strip()
        return Compressed(
            original=transcript,
            command=command,
            mode=mode,
            ambiguities=[a.strip() for a in out.ambiguities if a.strip()],
            destructive=out.destructive,
            latency_s=round(time.monotonic() - started, 2),
            model=self.model,
        )

    # ---- internals --------------------------------------------------------

    def _call_with_retry(self, messages: list[dict]) -> LLMOutput:
        last_error = "unknown error"
        for attempt in (1, 2):
            try:
                raw = self._call(messages)
            except openai.OpenAIError as exc:
                failure = classify(exc)
                last_error = failure.message
                if attempt == 1 and failure.transient:
                    log.warning("Compression call failed (%s); retrying in %.1fs", failure.message, failure.wait)
                    self._sleep(failure.wait)
                    continue
                raise _Failed(failure.message) from exc
            try:
                out = LLMOutput.model_validate(json.loads(raw))
                if not out.command.strip():
                    raise ValueError("command is empty")
                return out
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = f"the model's reply was not valid ({_first_line(exc)})"
                log.warning("Invalid compression JSON on attempt %d: %s", attempt, _first_line(exc))
                if attempt == 1:
                    messages = messages + [
                        {"role": "assistant", "content": raw[:2000]},
                        {"role": "user", "content": (
                            f"That reply was invalid: {_first_line(exc)}. "
                            "Reply again with only the JSON object described in the instructions."
                        )},
                    ]
        raise _Failed(last_error)

    def _call(self, messages: list[dict]) -> str:
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=0,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )
        if self.reasoning_effort and "gpt-oss" in self.model:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self._use_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "compressed_prompt", "strict": True, "schema": prompts.RESPONSE_SCHEMA},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self.client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            text = str(exc).lower()
            if self._use_schema and ("json_schema" in text or "response_format" in text):
                log.info("Model %s rejected json_schema; falling back to json_object", self.model)
                self._use_schema = False
                return self._call(messages)
            raise
        return response.choices[0].message.content or ""


class _Failed(Exception):
    pass


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
