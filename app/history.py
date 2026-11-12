"""Command history (one JSONL file) and token estimates.

Token counts use tiktoken's o200k_base encoding as an *estimate*: Claude's own
tokenizer isn't public. The encoding file (3.6 MB) is downloaded once from
OpenAI's public blob store and cached in the data dir; until it's available
(or if it can't be fetched) a characters ÷ 4 estimate is used. No user text
is ever sent anywhere for counting.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---- token estimates ------------------------------------------------------------

_encoder = None
_encoder_lock = threading.Lock()
_encoder_failed = False


def init_tokenizer(cache_dir: Path, background: bool = True) -> None:
    """Load tiktoken's encoding (downloading it once if needed)."""
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache_dir))

    def load():
        global _encoder, _encoder_failed
        try:
            import tiktoken

            enc = tiktoken.get_encoding("o200k_base")
            with _encoder_lock:
                _encoder = enc
            log.info("Token estimates: tiktoken o200k_base")
        except Exception as exc:
            _encoder_failed = True
            log.warning("tiktoken unavailable, using a rough estimate: %s", exc)

    if background:
        threading.Thread(target=load, name="tiktoken", daemon=True).start()
    else:
        load()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    with _encoder_lock:
        enc = _encoder
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return max(1, round(len(text) / 4))


def is_exact_tokenizer() -> bool:
    return _encoder is not None


def saving_percent(before: int, after: int) -> float:
    """Positive = shorter. Can be negative: structure sometimes adds tokens."""
    return round(100 * (before - after) / before, 1) if before else 0.0


# ---- history -----------------------------------------------------------------


@dataclass
class Stats:
    commands: int = 0
    compressed: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    @property
    def saving_percent(self) -> float:
        return saving_percent(self.tokens_before, self.tokens_after)


class History:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled

    def append(self, entry: dict) -> None:
        if not self.enabled:
            return
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping corrupt history line %d", n)
        return out[-limit:] if limit else out

    def stats(self, project: str | None = None) -> Stats:
        s = Stats()
        for e in self.entries():
            if e.get("event"):  # e.g. "copied": not a command of its own
                continue
            if project and e.get("project") != project:
                continue
            s.commands += 1
            if not e.get("skipped") and not e.get("fallback_error"):
                s.compressed += 1
                s.tokens_before += e.get("tokens_before", 0)
                s.tokens_after += e.get("tokens_after", 0)
        return s

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def make_entry(result, project: str) -> dict:
    """History line for a finished command (``result`` is a models.CommandResult)."""
    c, r = result.compressed, result.report
    before, after = estimate_tokens(c.original), estimate_tokens(c.command)
    entry = {
        "project": project,
        "item_id": result.item_id,
        "recompressed": result.recompressed,
        "mode": c.mode.value if c.mode else None,
        "transcript": c.original,
        "command": c.command,
        "skipped": c.skip_reason if c.skipped else None,
        "fallback_error": c.fallback_error,
        "tokens_before": before,
        "tokens_after": after,
        "saving_percent": saving_percent(before, after),
        "token_estimate": "tiktoken-o200k" if is_exact_tokenizer() else "chars/4",
        "compress_seconds": c.latency_s,
        "model": c.model,
        "ambiguities": c.ambiguities,
        "check": {
            "items": len(r.items),
            "missing": [i.label for i in r.missing],
            "coverage": r.coverage,
            "destructive": r.destructive_words or (["model flag"] if c.destructive else []),
        },
    }
    if result.transcript is not None:
        entry["audio_seconds"] = result.transcript.audio_seconds
        entry["transcribe_seconds"] = result.transcript.latency_s
        entry["whisper_model"] = result.transcript.model
    return entry
