import json
from types import SimpleNamespace

import httpx2 as httpx
import openai
import pytest

from app.compressor import Compressor
from app.models import CompressionContext, Mode

REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
LONG = ("so basically I want you to refactor the parse_config function in utils.py and do not change the "
        "public API because other modules depend on it and then run pytest to make sure all the tests still pass")


def reply(mode="COMMAND", command="TASK: Refactor parse_config in utils.py", ambiguities=(), destructive=False):
    return json.dumps({"mode": mode, "command": command, "ambiguities": list(ambiguities), "destructive": destructive})


def status_error(cls, code, text="boom"):
    return cls(text, response=httpx.Response(code, request=REQ), body=None)


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = SimpleNamespace(content=item)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def make(*script, **kw):
    client = FakeClient(*script)
    sleeps = []
    return Compressor(client, sleep=sleeps.append, **kw), client, sleeps


def test_success():
    c, client, _ = make(reply(ambiguities=["Which tests?"], destructive=True))
    out = c.compress(LONG)
    assert out.mode is Mode.COMMAND
    assert out.command.startswith("TASK:")
    assert out.ambiguities == ["Which tests?"]
    assert out.destructive and not out.skipped and out.fallback_error is None
    call = client.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["temperature"] == 0
    assert call["reasoning_effort"] == "low"
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert "<transcript>" in call["messages"][1]["content"]
    assert "Mode: detect it." in call["messages"][1]["content"]


def test_reasoning_effort_only_for_gpt_oss():
    c, client, _ = make(reply(), model="llama-3.3-70b-versatile")
    c.compress(LONG)
    assert "reasoning_effort" not in client.calls[0]


def test_short_transcript_skipped():
    c, client, _ = make()
    out = c.compress("um fix the typo in README.md")
    assert out.skipped and out.skip_reason == "short"
    assert out.command == "Fix the typo in README.md"
    assert client.calls == []


def test_forced_dictation_skipped():
    c, client, _ = make()
    out = c.compress(LONG, forced_mode=Mode.DICTATION)
    assert out.skipped and out.skip_reason == "dictation" and out.mode is Mode.DICTATION
    assert client.calls == []


def test_detected_dictation_uses_cleaned_transcript():
    c, _, _ = make(reply(mode="DICTATION", command="model rewrote this"))
    out = c.compress("um " + LONG)
    assert out.mode is Mode.DICTATION
    assert out.command.startswith("So basically")


def test_forced_mode_is_sent_and_kept():
    c, client, _ = make(reply(mode="COMMAND"))
    out = c.compress(LONG, forced_mode=Mode.QUESTION)
    assert out.mode is Mode.QUESTION
    assert "Mode: QUESTION (chosen by the user" in client.calls[0]["messages"][1]["content"]


def test_context_is_sent_as_background():
    c, client, _ = make(reply())
    c.compress(LONG, context=CompressionContext(project_notes="FastAPI app", recent_commands=["Add login"]))
    msg = client.calls[0]["messages"][1]["content"]
    assert "<project_notes>\nFastAPI app" in msg and "1. Add login" in msg
    assert msg.index("<project_notes>") < msg.index("<transcript>")


def test_aggressive_strength_in_prompt():
    c, client, _ = make(reply(), strength="aggressive")
    c.compress(LONG)
    assert "aggressive" in client.calls[0]["messages"][1]["content"]


def test_bad_json_retried_once_with_feedback():
    c, client, _ = make("not json at all", reply())
    out = c.compress(LONG)
    assert out.fallback_error is None
    assert len(client.calls) == 2
    retry_msgs = client.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "not json at all"}
    assert "invalid" in retry_msgs[-1]["content"]


def test_schema_violation_retried():
    c, client, _ = make(json.dumps({"mode": "SHOUT", "command": "x", "ambiguities": [], "destructive": False}), reply())
    assert c.compress(LONG).fallback_error is None
    assert len(client.calls) == 2


def test_empty_command_counts_as_invalid():
    c, _, _ = make(reply(command="  "), reply(command="  "))
    out = c.compress(LONG)
    assert out.fallback_error and "not valid" in out.fallback_error


def test_bad_json_twice_falls_back_to_transcript():
    c, client, _ = make("nope", "{still nope")
    out = c.compress(LONG)
    assert out.fallback_error
    assert out.command == LONG[0].upper() + LONG[1:]  # cleaned transcript
    assert len(client.calls) == 2


def test_rate_limit_retried_then_ok():
    c, client, sleeps = make(status_error(openai.RateLimitError, 429), reply())
    assert c.compress(LONG).fallback_error is None
    assert sleeps == [2.0]


def test_timeout_twice_falls_back():
    c, _, _ = make(openai.APITimeoutError(request=REQ), openai.APITimeoutError(request=REQ))
    out = c.compress(LONG)
    assert "too long" in out.fallback_error


def test_bad_key_no_retry():
    c, client, sleeps = make(status_error(openai.AuthenticationError, 401))
    out = c.compress(LONG)
    assert "API key" in out.fallback_error
    assert len(client.calls) == 1 and sleeps == []


def test_schema_unsupported_falls_back_to_json_object():
    c, client, _ = make(
        status_error(openai.BadRequestError, 400, "response_format json_schema not supported"), reply(), reply()
    )
    out = c.compress(LONG)
    assert out.fallback_error is None
    assert client.calls[1]["response_format"] == {"type": "json_object"}
    c.compress(LONG + " again")  # the switch is remembered
    assert len(client.calls) == 3
    assert client.calls[2]["response_format"] == {"type": "json_object"}


def test_other_bad_request_does_not_loop():
    c, client, _ = make(status_error(openai.BadRequestError, 400, "context too long"))
    out = c.compress(LONG)
    assert out.fallback_error and len(client.calls) == 1


def test_empty_transcript():
    c, client, _ = make()
    out = c.compress("   ")
    assert out.skipped and out.command == "" and client.calls == []


# ---- Phase 4: short follow-ups use context ------------------------------------

from app.compressor import is_follow_up  # noqa: E402

RECENT = CompressionContext(recent_commands=["TASK: Add a GET /api/health endpoint to the FastAPI app"])


def test_short_follow_up_is_compressed_with_context():
    c, client, _ = make(reply(command="TASK: Add authentication to the /api/health endpoint"))
    out = c.compress("now add authentication to it", context=RECENT)
    assert not out.skipped and len(client.calls) == 1
    assert "/api/health" in client.calls[0]["messages"][1]["content"]


def test_short_without_reference_still_skipped():
    c, client, _ = make()
    assert c.compress("fix the typo in README.md", context=RECENT).skipped
    assert client.calls == []


def test_short_reference_without_history_skipped():
    c, client, _ = make()
    assert c.compress("now add authentication to it").skipped
    assert client.calls == []


@pytest.mark.parametrize("text,expected", [
    ("now add authentication", True), ("also do that for the users table", True), ("same for login", True),
    ("fix the README typo", False), ("rename utils.py", False),
])
def test_is_follow_up(text, expected):
    assert is_follow_up(text, RECENT) is expected


def test_force_compresses_short_text():
    c, client, _ = make(reply(command="TASK: Fix the README title typo"))
    out = c.compress("fix the typo in the README title", force=True)
    assert not out.skipped and len(client.calls) == 1


def test_force_does_not_override_forced_dictation():
    c, client, _ = make()
    assert c.compress(LONG, forced_mode=Mode.DICTATION, force=True).skipped
    assert client.calls == []
