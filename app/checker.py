"""Deterministic preservation check (no LLM).

Pulls the things that must survive compression out of the transcript
(numbers, file names/paths, code identifiers, quoted text, negative
instructions) and reports which ones are missing from the output. Also flags
destructive wording. It is a safety net, not proof: the user still reviews.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- normalisation -------------------------------------------------------

_CONTRACTIONS = {
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "cannot", "won't": "will not", "shouldn't": "should not",
    "mustn't": "must not", "isn't": "is not", "aren't": "are not",
    "wasn't": "was not", "weren't": "were not", "haven't": "have not",
    "hasn't": "has not", "wouldn't": "would not", "couldn't": "could not",
}
_CONTRACTION_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _CONTRACTIONS) + r")\b", re.I)


def _expand(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    return _CONTRACTION_RE.sub(lambda m: _CONTRACTIONS[m.group(1).lower()], text)


_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/']*[A-Za-z0-9_]|[A-Za-z0-9_]|[,;:!?]|\.(?=\s|$)")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(_expand(text))]


# ---- numbers -------------------------------------------------------------

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_WORD_VALUE = {w: i for i, w in enumerate(_UNITS)} | {w: i * 10 for i, w in enumerate(_TENS) if w}
# "one" is skipped as a must-keep item: "this one", "one of them" are rarely numbers.
_NUMBER_WORD_RE = re.compile(
    r"\b(?:(" + "|".join(t for t in _TENS if t) + r")(?:[\s-](" + "|".join(_UNITS[1:10]) + r"))?"
    r"|(" + "|".join(_UNITS[2:]) + r"))\b",
    re.I,
)
_DIGITS_RE = re.compile(r"(?<![\w.])\d+(?:[.,:]\d+)*%?(?![\w])")


def _word_numbers(text: str, with_end: bool = False) -> list[tuple]:
    """[(spoken form, digits)] for 'two'..'ninety-nine' (plus end offset if asked)."""
    out = []
    for m in _NUMBER_WORD_RE.finditer(text):
        if m.group(3):
            value = _WORD_VALUE[m.group(3).lower()]
        else:
            value = _WORD_VALUE[m.group(1).lower()] + (_WORD_VALUE[m.group(2).lower()] if m.group(2) else 0)
        out.append((m.group(0), str(value), m.end()) if with_end else (m.group(0), str(value)))
    return out


# "five retries, no wait, three": the first number was replaced, so it may go.
_CORRECTION_RE = re.compile(
    r"^[\s\w]{0,25}?[,.;]?\s*(?:no,?\s+wait|wait,?\s+no|no,?\s+actually|actually,?\s+(?:make|change)|i\s+mean|sorry|scratch\s+that)\b",
    re.I,
)


def _corrected(text: str, end: int) -> bool:
    return _CORRECTION_RE.match(text[end:end + 60]) is not None


def _numbers_in(text: str) -> set[str]:
    found = {m.group(0).rstrip("%") for m in _DIGITS_RE.finditer(text)}
    found |= {digits for _, digits in _word_numbers(text)}
    return found


# ---- files, identifiers, quotes -------------------------------------------

_EXTS = ("py|pyi|js|jsx|ts|tsx|json|jsonl|md|txt|yaml|yml|toml|ini|cfg|env|html|css|scss|sh|"
         "bash|go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|php|sql|csv|lock|xml|vue|svelte|swift|dart|ipynb")
_PATH_RE = re.compile(
    r"(?<![\w/.~-])(?:~?/?[\w.-]+/)*[\w-]*\.(?:" + _EXTS + r")(?![\w])"   # utils.py, src/app.ts, .env
    r"|(?<![\w/])(?:~|\.{1,2})?/[\w.-]+(?:/[\w.-]+)+/?",                  # /etc/hosts, ./src/lib
    re.I,
)
_SPOKEN_FILE_RE = re.compile(r"\b([\w-]+) dot (" + _EXTS + r")\b", re.I)
_IDENT_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"   # snake_case, SCREAMING_CASE
    r"|[a-z]+[0-9]*[A-Z][A-Za-z0-9]*"                # camelCase
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*"              # PascalCase, PySide6
    r"|__\w+__)\b"                                   # dunders
)
_QUOTE_RE = re.compile(r"\"([^\"]{2,})\"|“([^”]{2,})”|(?<![\w])'([^']{2,})'(?![\w])")

# ---- negations -----------------------------------------------------------

_NEGATIONS = {"not", "never", "without", "no", "cannot", "avoid", "except", "only", "nor",
              "none", "nothing", "neither", "exclude", "excluding", "instead"}
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "it", "this", "that",
         "any", "anything", "do", "does", "did", "be", "is", "are", "was", "will", "should",
         "must", "we", "you", "i", "my", "your", "our", "with", "at", "by", "from", "as", "so",
         "just", "please", "also", "ever", "really", "actually", "at", "all", "them", "these",
         "those", "there", "here", "have", "has", "use", "make", "want", "else"}
# "not sure", "not certain": hedging, not an instruction
_HEDGES = {"sure", "certain", "really", "100%"}
_PUNCT = {",", ";", ":", "!", "?", "."}
_INTERJECTION_FOLLOWERS = {"wait", "actually", "sorry", "no", "okay", "ok", "yeah", "hold", "scratch", "never"}
_NEG_WINDOW = 6

# ---- destructive wording ---------------------------------------------------

_DESTRUCTIVE_RE = re.compile(
    r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bgit\s+reset\s+--hard|\bgit\s+clean\b"
    r"|\bforce[\s-]push|\bpush\s+(?:--force|-f)\b|--force\b"
    r"|\b(?:delete|deleting|deletes|drop|dropping|drops|remove|removing|removes|reset|resetting"
    r"|truncate|truncating|wipe|wiping|purge|purging|erase|erasing|destroy|destroying"
    r"|overwrite|overwriting|uninstall|uninstalling)\b",
    re.I,
)


@dataclass
class Item:
    kind: str          # number | path | identifier | quote | negation
    label: str         # what the user sees
    found: bool = False


# Coverage: share of the speaker's content words that appear in the output.
# Normal rewrites keep most of them; a reply that replaced the request (answered
# it, described it, wrote code for it) keeps very few. Calibrated on
# tests/samples (good rewrites ≥ 0.5, replacements ≤ 0.2).
LOW_COVERAGE = 0.35
_MIN_COVERAGE_WORDS = 8
_CHATTER = {"basically", "like", "actually", "okay", "alright", "really", "just", "thing", "things", "stuff",
            "kind", "sort", "wondering", "guess", "maybe", "think", "know", "mean", "gonna", "wanna", "yeah",
            "right", "well", "need", "can", "could", "would", "let", "lets", "something", "somehow", "whatever",
            "achha", "mujhe", "chahiye", "kare", "karna", "hai", "aur", "mein", "jo", "ko", "uske", "andar",
            "because", "also", "then", "after", "first", "again", "now", "hmm"}


@dataclass
class CheckReport:
    items: list[Item] = field(default_factory=list)
    destructive_words: list[str] = field(default_factory=list)
    coverage: float | None = None  # None when the transcript is too short to judge

    @property
    def low_coverage(self) -> bool:
        return self.coverage is not None and self.coverage < LOW_COVERAGE

    @property
    def missing(self) -> list[Item]:
        return [i for i in self.items if not i.found]

    @property
    def ok(self) -> bool:
        return not self.missing


def _stem(word: str) -> str:
    return word[:5] if len(word) > 5 else word


def _negation_items(transcript: str) -> list[tuple[str, list[str]]]:
    """[(label, content words)] — each negation plus the words it governs."""
    toks = _tokens(transcript)
    items = []
    for i, tok in enumerate(toks):
        if tok not in _NEGATIONS:
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok == "no" and (nxt in _PUNCT or nxt in _INTERJECTION_FOLLOWERS or nxt == ""):
            continue  # "no, wait…" / "no actually…" self-correction, not an instruction
        if tok == "not" and nxt in _HEDGES:
            continue  # "I'm not sure"
        content: list[str] = []
        end = i
        for j in range(i + 1, min(len(toks), i + 1 + _NEG_WINDOW)):
            if toks[j] in _PUNCT:
                break
            end = j
            if toks[j] not in _STOP and toks[j] not in _NEGATIONS and len(toks[j]) > 2:
                content.append(toks[j])
                if len(content) == 2:
                    break
        if content:
            label = " ".join(toks[i:end + 1])
            items.append((label, content))
    return items


_SEGMENT_RE = re.compile(r"[\n;!?]|\.(?=\s|$)")


def _segments(text: str) -> list[list[str]]:
    """Output split into lines/sentences, so a negation on one line can't vouch for another."""
    return [toks for part in _SEGMENT_RE.split(text) if (toks := _tokens(part))]


def _same_word(a: str, b: str) -> bool:
    """Loose match: 'dev'~'development', 'modify'~'modifying', 'tests'~'test'."""
    a, b = _stem(a), _stem(b)
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and long_.startswith(short)


def _negation_kept(output_segments: list[list[str]], content: list[str]) -> bool:
    for toks in output_segments:
        neg_positions = [k for k, t in enumerate(toks) if t in _NEGATIONS]
        if not neg_positions:
            continue
        for k, t in enumerate(toks):
            if any(_same_word(t, c) for c in content) and any(abs(k - n) <= _NEG_WINDOW for n in neg_positions):
                return True
    return False


def _contains(haystack: str, needle: str) -> bool:
    return re.search(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", haystack, re.I) is not None


def check(transcript: str, output: str, mode: str | None = None) -> CheckReport:
    """``mode`` is the command's mode value; in CODE mode the output is literal
    code, which can't carry negative instructions, so those aren't checked."""
    report = CheckReport()
    seen: set[tuple[str, str]] = set()

    def add(kind: str, label: str, found: bool) -> None:
        key = (kind, label.lower())
        if key not in seen:
            seen.add(key)
            report.items.append(Item(kind, label, found))

    # Numbers: "3" and "three" are interchangeable.
    out_numbers = _numbers_in(output)
    for m in _DIGITS_RE.finditer(transcript):
        if not _corrected(transcript, m.end()):
            add("number", m.group(0), m.group(0).rstrip("%") in out_numbers)
    for spoken, digits, end in _word_numbers(transcript, with_end=True):
        if not _corrected(transcript, end):
            add("number", spoken, digits in out_numbers)

    # Files and paths ("utils dot py" may come back as "utils.py").
    for m in _PATH_RE.finditer(transcript):
        add("path", m.group(0), _contains(output, m.group(0)))
    for m in _SPOKEN_FILE_RE.finditer(transcript):
        written = f"{m.group(1)}.{m.group(2)}"
        add("path", written, _contains(output, written) or _contains(output, m.group(0)))

    path_spans = [m.span() for m in _PATH_RE.finditer(transcript)]
    for m in _IDENT_RE.finditer(transcript):
        if any(a <= m.start() < b for a, b in path_spans):
            continue  # part of a file name, already checked
        add("identifier", m.group(0), _contains(output, m.group(0)))

    for m in _QUOTE_RE.finditer(transcript):
        quoted = next(g for g in m.groups() if g)
        add("quote", f'"{quoted}"', quoted.lower() in output.lower())

    if str(getattr(mode, "value", mode)) != "CODE":
        out_segments = _segments(output)
        for label, content in _negation_items(transcript):
            add("negation", label, _negation_kept(out_segments, content))

    report.destructive_words = destructive_words(output)
    report.coverage = coverage(transcript, output)
    return report


def _content_words(text: str) -> set[str]:
    return {t for t in _tokens(text)
            if len(t) > 2 and t not in _STOP and t not in _CHATTER and t not in _NEGATIONS and t not in _PUNCT}


def coverage(transcript: str, output: str) -> float | None:
    words = _content_words(transcript)
    if len(words) < _MIN_COVERAGE_WORDS:
        return None
    out = _tokens(output)
    kept = sum(1 for w in words if any(_same_word(w, o) for o in out))
    return round(kept / len(words), 2)


def destructive_words(text: str) -> list[str]:
    found: list[str] = []
    for m in _DESTRUCTIVE_RE.finditer(text):
        word = m.group(0).lower()
        if word not in found:
            found.append(word)
    return found


_FILLER_RE = re.compile(r"(?:,\s*)?(?<![\w'])(?:um+|uh+|uhm+|erm+|er|ah+|hmm+|mm+)(?![\w']),?", re.I)
_REPEAT_RE = re.compile(r"\b(\w+)(\s+\1\b)+", re.I)


def clean_fillers(text: str) -> str:
    """Remove pure disfluencies (um, uh…) and stutters (the the). Nothing else."""
    text = _FILLER_RE.sub("", text)
    text = _REPEAT_RE.sub(r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text
