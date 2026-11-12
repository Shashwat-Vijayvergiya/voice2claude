import json

import pytest

from app import history as h
from app.checker import check
from app.history import History, Stats, estimate_tokens, make_entry, saving_percent
from app.models import CommandResult, Compressed, Mode, Transcript


@pytest.fixture
def hist(tmp_path):
    return History(tmp_path / "history.jsonl")


def result(original="refactor parse_config in utils.py and do not change the API", command="TASK: Refactor parse_config",
           skipped=False, fallback=None, recompressed=False):
    c = Compressed(original=original, command=command, mode=Mode.COMMAND, skipped=skipped,
                   skip_reason="short" if skipped else None, fallback_error=fallback, latency_s=1.1, model="m")
    t = Transcript(text=original, audio_seconds=4.2, latency_s=0.5, model="whisper")
    return CommandResult("id1", c, check(original, command), t, recompressed)


def test_estimate_fallback_without_tiktoken(monkeypatch):
    monkeypatch.setattr(h, "_encoder", None)
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd" * 10) == 10


def test_estimate_with_tiktoken(monkeypatch):
    # Offline only: use the cache the app fills on first run; never download in tests.
    from pathlib import Path

    cache = Path.home() / ".local" / "share" / "voice-to-claude" / "tiktoken-cache"
    if not cache.is_dir() or not any(cache.iterdir()):
        pytest.skip("tiktoken encoding not cached yet (the app downloads it on first run)")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))
    h.init_tokenizer(cache, background=False)
    assert estimate_tokens("TASK: Refactor parse_config in utils.py. Do not change the public API.") == 17


def test_saving_percent():
    assert saving_percent(100, 70) == 30.0
    assert saving_percent(100, 110) == -10.0
    assert saving_percent(0, 5) == 0.0


def test_entry_contents():
    e = make_entry(result(), "proj")
    assert e["project"] == "proj" and e["mode"] == "COMMAND"
    assert e["transcript"].startswith("refactor") and e["command"].startswith("TASK")
    assert e["tokens_before"] > e["tokens_after"] > 0
    assert e["audio_seconds"] == 4.2 and e["transcribe_seconds"] == 0.5 and e["compress_seconds"] == 1.1
    assert "utils.py" in e["check"]["missing"]
    assert e["skipped"] is None and e["fallback_error"] is None


def test_append_and_survive_restart(hist, tmp_path):
    hist.append(make_entry(result(), "proj"))
    hist.append(make_entry(result(recompressed=True), "proj"))
    again = History(tmp_path / "history.jsonl")
    entries = again.entries()
    assert len(entries) == 2 and "ts" in entries[0]
    assert entries[1]["recompressed"] is True


def test_disabled_writes_nothing(tmp_path):
    off = History(tmp_path / "history.jsonl", enabled=False)
    off.append(make_entry(result(), "p"))
    assert not (tmp_path / "history.jsonl").exists()


def test_stats_per_project_and_only_compressed_tokens(hist):
    hist.append({**make_entry(result(), "a"), "tokens_before": 100, "tokens_after": 70})
    hist.append({**make_entry(result(skipped=True), "a"), "tokens_before": 10, "tokens_after": 9})
    hist.append({**make_entry(result(fallback="timeout"), "a"), "tokens_before": 50, "tokens_after": 50})
    hist.append({**make_entry(result(), "b"), "tokens_before": 100, "tokens_after": 100})
    s = hist.stats("a")
    assert (s.commands, s.compressed, s.tokens_before, s.tokens_after) == (3, 1, 100, 70)
    assert s.saving_percent == 30.0
    assert hist.stats().commands == 4


def test_corrupt_line_skipped(hist):
    hist.append({"x": 1})
    with hist.path.open("a") as f:
        f.write("{broken\n")
    hist.append({"x": 2})
    assert [e["x"] for e in hist.entries()] == [1, 2]


def test_clear(hist):
    hist.append({"x": 1})
    hist.clear()
    assert hist.entries() == []
    hist.clear()  # fine when already gone


def test_unicode_kept(hist):
    hist.append({"transcript": "mujhe ek function chahiye — “quoted”"})
    assert "“quoted”" in hist.path.read_text(encoding="utf-8")
    assert json.loads(hist.path.read_text())["transcript"].startswith("mujhe")


def test_stats_empty():
    assert Stats().saving_percent == 0.0
