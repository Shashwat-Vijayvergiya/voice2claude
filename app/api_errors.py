"""Turn Groq/OpenAI client errors into one user-facing shape."""

from __future__ import annotations

from dataclasses import dataclass

import openai

MAX_RETRY_WAIT = 10.0


@dataclass
class ApiFailure:
    message: str
    transient: bool   # worth one automatic retry right now
    wait: float = 0.0  # seconds before that retry
    retryable: bool = True  # worth offering the user a Retry button


def classify(exc: Exception) -> ApiFailure:
    if isinstance(exc, openai.RateLimitError):
        return ApiFailure("Groq's free-tier limit was reached. Wait a minute and try again.", True, _retry_after(exc))
    if isinstance(exc, openai.APITimeoutError):  # subclass of APIConnectionError: check first
        return ApiFailure("Groq took too long to respond.", True, 1.0)
    if isinstance(exc, openai.APIConnectionError):
        return ApiFailure("Could not reach Groq. Check your internet connection.", True, 1.0)
    if isinstance(exc, openai.AuthenticationError):
        return ApiFailure(
            "Groq rejected the API key. Create a new key and store it again (see README).",
            transient=False, retryable=False,
        )
    if isinstance(exc, openai.InternalServerError):
        return ApiFailure(f"Groq had a server error ({exc.status_code}).", True, 2.0)
    if isinstance(exc, openai.APIStatusError):
        return ApiFailure(f"Groq returned an error ({exc.status_code}): {_short(exc)}", False, retryable=False)
    return ApiFailure(f"Unexpected error talking to Groq: {_short(exc)}", False)


def _retry_after(exc: openai.APIStatusError) -> float:
    try:
        wait = float(exc.response.headers.get("retry-after", "2"))
    except (TypeError, ValueError, AttributeError):
        wait = 2.0
    return min(max(0.5, wait), MAX_RETRY_WAIT)


def _short(exc: Exception, limit: int = 200) -> str:
    text = str(getattr(exc, "message", "") or exc)
    return text if len(text) <= limit else text[:limit] + "…"
