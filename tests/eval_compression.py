"""Real-API check of the compression layer. NOT run by pytest (uses Groq quota).

    .venv/bin/python tests/eval_compression.py            # all samples
    .venv/bin/python tests/eval_compression.py hinglish   # samples whose id contains "hinglish"

Rerun every time app/prompts.py changes. Preservation is scored separately from
compression: a shorter command that loses a must-keep item is a failure.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.checker import check  # noqa: E402
from app.compressor import Compressor, word_count  # noqa: E402
from app.models import CompressionContext  # noqa: E402
from app.transcriber import make_groq_client  # noqa: E402

SAMPLES = Path(__file__).parent / "samples" / "commands.json"
PAUSE_SECONDS = 4.0  # stay under the free tier's tokens-per-minute limit


def main(argv: list[str]) -> int:
    settings = config.load_settings()
    key = config.get_api_key()
    if not key:
        print(config.MISSING_KEY_MESSAGE)
        return 2
    compressor = Compressor(
        make_groq_client(key, config.GROQ_BASE_URL, timeout=settings.compression_timeout_seconds),
        model=settings.llm_model,
        strength=settings.compression_strength,
        reasoning_effort=settings.llm_reasoning_effort,
        skip_under_words=settings.skip_under_words,
    )
    samples = json.loads(SAMPLES.read_text())["samples"]
    if argv:
        samples = [s for s in samples if any(a in s["id"] for a in argv)]

    failures = 0
    total_before = total_after = 0
    for n, s in enumerate(samples):
        if n and not s.get("expect_skipped"):
            time.sleep(PAUSE_SECONDS)
        ctx = CompressionContext(**s.get("context", {}))
        out = compressor.compress(s["transcript"], context=ctx)
        text = out.command
        problems = []
        lost = [p for p in s.get("must_keep", []) if not re.search(p, text, re.I | re.M)]
        if lost:
            problems.append(f"lost {lost}")
        invented = [p for p in s.get("must_not", []) if re.search(p, text, re.I | re.M)]
        if invented:
            problems.append(f"should not contain {invented}")
        if out.fallback_error:
            problems.append(f"fallback: {out.fallback_error}")
        if s.get("mode") and out.mode and out.mode.value not in s["mode"].split("|"):
            problems.append(f"mode {out.mode.value} (expected {s['mode']})")
        destructive = out.destructive or bool(check(s["transcript"], text, out.mode).destructive_words)
        if destructive != s.get("destructive", False):
            problems.append(f"destructive={destructive}")
        if s.get("expect_ambiguity") and not out.ambiguities:
            problems.append("expected an ambiguity")
        if out.fallback_error and "your own wording" in out.fallback_error:
            problems.append("rewrite rejected (replaced your words)")
        if s.get("expect_skipped") and not out.skipped:
            problems.append("expected skip")
        report = check(s["transcript"], text, out.mode)
        if report.low_coverage and not s.get("expect_skipped"):
            problems.append(f"low wording coverage {report.coverage}")

        before, after = word_count(s["transcript"]), word_count(text)
        total_before += before
        total_after += after
        status = "PASS" if not problems else "FAIL"
        failures += bool(problems)
        mode = out.mode.value if out.mode else "-"
        cov = f"  coverage={report.coverage}" if report.coverage is not None else ""
        print(f"\n[{status}] {s['id']}  mode={mode}  {before}→{after} words  {out.latency_s:.1f}s{cov}"
              f"{'  (skipped)' if out.skipped else ''}")
        print("  " + text.replace("\n", "\n  "))
        if out.ambiguities:
            print(f"  ambiguities: {out.ambiguities}")
        if report.missing:
            print(f"  checker flags: {[i.label for i in report.missing]}")
        for p in problems:
            print(f"  ✗ {p}")

    print(f"\n{len(samples) - failures}/{len(samples)} passed; words {total_before} → {total_after}"
          f" ({100 * (1 - total_after / max(1, total_before)):.0f}% shorter)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
