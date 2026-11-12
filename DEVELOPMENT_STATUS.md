# Development status

| Phase | State |
|---|---|
| 1. Foundation | ✅ Done (2026-11-12) |
| 2. Voice to text | ✅ Done (2026-11-12); real spoken tests by the user confirmed accurate |
| 3. Compression layer | ✅ Built (2026-11-12); 11/12 real-API eval samples pass |
| 4. Context and history | ✅ Built (2026-11-12); 15/16 real-API eval samples pass, incl. follow-ups |
| 5. Clipboard and polish | ✅ Done (2026-11-12); fresh install verified on Python 3.14 |

Tests: 229 passing (`QT_QPA_PLATFORM=offscreen .venv/bin/pytest`). All Groq calls are mocked.
Real-API eval: `.venv/bin/python tests/eval_compression.py` (16 samples, uses quota).

## Phase 1: Foundation

Built:
- Project layout, `requirements.txt`, `.gitignore`, `pytest.ini`.
- Python 3.12.10 venv at `.venv` (from pyenv; system Python is 3.14).
- `app/config.py`: pydantic `Settings`, repo defaults merged with user overrides in the data dir, atomic save, vocabulary loader, Groq key lookup (env var, then keyring), rotating file + stderr logging.
- `app/ui.py`: PySide6 main window with status line, project label, mode picker, original/command panes, Copy button (disabled until Phase 5).
- `main.py`: starts logging, loads settings (settings error → dialog), shows a clear message when the key is missing.

## Phase 2: Voice to text

Built:
- `app/models.py`: `Recording` (FLAC bytes in memory, duration, speech flag, stop reason) and `Transcript`.
- `app/recorder.py`: 16 kHz mono capture (falls back to the device's native rate and resamples), FLAC encoding in memory, clear errors for missing/busy mic. `SilenceDetector`:
  - noise floor = 10th percentile of the last 30 s of block levels (skipping 0.15 s of startup); threshold = max(0.01, 3 × floor);
  - speech needs ≥ 0.15 s of loud blocks (clicks ignored); earlier blocks are re-checked when the floor drops, so speaking immediately still counts;
  - stops after `silence_seconds` (2 s) of quiet following speech, after `no_speech_timeout_seconds` (8 s) with no speech, or at `max_record_seconds` (180 s).
- `app/transcriber.py`: Groq Whisper via the `openai` client (`max_retries=0`; own retry logic). Vocabulary prompt (≤ 600 chars), language `auto`/`en`/ISO code, one retry on 429 (honours `retry-after`, capped 10 s), timeout, connection and 5xx errors; no retry for bad key or 4xx. 25 MB upload guard.
- `app/control.py` + `voice2claude` CLI: Unix socket at `$XDG_RUNTIME_DIR/voice-to-claude.sock` (mode 0600), commands `toggle`/`show`/`ping`. Stdlib only, so the CLI runs on the system Python and starts fast. `voice2claude toggle` starts the app (recording) if it isn't running. Stale sockets are replaced; a second app launch hands off and exits.
- `app/pipeline.py`: state machine IDLE → LISTENING → TRANSCRIBING → READY/ERROR on the Qt thread; audio-thread auto-stop and network calls come back through Qt signals. No-speech recordings never reach Groq. On failure the audio stays in memory for exactly one Retry, then it's dropped; it's also dropped after success. Punctuation-only transcripts (Whisper's answer to noise) count as empty.
- `app/ui.py`: Record/Stop button, elapsed seconds while listening, Retry button, transcript shown in the editable Original pane, best-effort window raise when a transcript is ready (Wayland may only flash the dock icon).
- `main.py`: wires everything up; `--record` flag; SIGINT/SIGTERM quit cleanly (mic released, socket removed).
- Settings added: `no_speech_timeout_seconds`, `max_record_seconds`.

Checked on this machine (Ubuntu 26.04, Wayland):
- Mic opens natively at 16 kHz.
- 8 s of real room noise (median ~0.006–0.008 RMS): no false speech; stopped with `no_speech`. Floor 0.0055, threshold 0.0165.
- One real Groq upload of that audio: accepted, 0.66 s latency, returned `"."` (now treated as empty).
- Live app driven by `voice2claude status/toggle`; second launch hand-off; clean SIGTERM shutdown.

Not yet checked: **a real spoken command** (speaker→mic loopback isn't possible here because audio output is a headset). The user needs to do this, and it's the remaining item before Phase 2 is fully done.

Findings fixed during Phase 2:
- Speaking right after pressing the shortcut made the noise floor equal the voice, so a short command was thrown away as "no speech". Fixed by re-checking earlier blocks.
- The mic's first block reads ~0.00001; a plain-minimum floor collapsed to 0 and let room-noise peaks (~0.012) near the threshold. Fixed with warmup + percentile floor.
- `openai` 3.x uses `httpx2`, not `httpx` (tests import `httpx2`).

## Phase 3: Compression layer

Built:
- `app/prompts.py`: system prompt (12 rules, 4 modes, one worked example), strength text, strict JSON schema, user-message builder (mode hint, strength, `<project_notes>`, `<recent_commands>`, `<transcript>`).
- `app/compressor.py`: one Groq call (`openai/gpt-oss-120b`, `reasoning_effort=low`, temperature 0, strict `json_schema`, falls back to `json_object` if a model rejects schemas). pydantic validation; one retry on bad JSON (with the error fed back) or transient API error. Skip rules: empty, < 30 words, forced DICTATION. Auto-detected DICTATION uses the filler-cleaned transcript. Never raises: failures return the cleaned transcript with `fallback_error`.
- `app/api_errors.py`: shared Groq error classification (transcriber refactored onto it).
- `app/checker.py` (no LLM):
  - must-keep items: numbers (digits and number words are interchangeable; "one" ignored; numbers the speaker corrected, like "five, no wait, three", are allowed to drop), file names/paths (incl. spoken "utils dot py"), snake/camel/Pascal/SCREAMING identifiers, quoted text, negations (each negation + the words it governs must reappear with a negation on the same output line; "no wait" and "not sure" ignored; skipped in CODE mode);
  - wording coverage: share of the speaker's content words in the output, warned below 0.35 (good rewrites scored 0.5–1.0; a reply that replaced the request scored 0.05–0.22);
  - destructive wording; `clean_fillers()` for skip/dictation paths.
- `app/ui.py`: Command pane with mode, words before→after and latency; green/red check line, low-coverage warning, orange ambiguities, red destructive box with confirmation checkbox. `copy_allowed()` enforces the confirmation (Copy itself is Phase 5). Re-check 300 ms after edits; new destructive wording un-ticks the box. Compress button re-runs on an edited transcript. Mode picker.
- `app/pipeline.py`: TRANSCRIBING → PROCESSING → READY/ERROR; compression and the check run on a worker thread; `compress_text()` for re-compress; forced mode and a context provider hook for Phase 4.
- Settings added: `llm_reasoning_effort`, `skip_under_words`, `compression_timeout_seconds`.
- `tests/samples/commands.json` (12 typed samples) + `tests/eval_compression.py`.

Eval history (real Groq, 12 samples):
1. First prompt: 8/12. Lost `KeyError`, "no code, just explain", "only dev"; turned "don't change the public API" into "keep… unchanged"; invented VALIDATION steps; wrote the shell command instead of rewriting the request. 3% *longer* than speech.
2. Stricter rules: 9/12, but 31% longer (padding, repetition, invented details).
3. Added one worked example: 8/12, 26% shorter, constraints kept; exposed a reply that described the dictation instead of returning it, which the item check couldn't see → added the coverage check.
4. Final: **11/12, 9% shorter**, no lost constraints, no invented requirements.

Known limitations:
- "Make the function faster" doesn't produce a "which function?" ambiguity (the remaining eval failure). In a real Claude session that's often clear from context.
- Checker false alarm when a negation is paraphrased into a positive restriction ("not any extra tools" → "find and wc only"). It's a red marker to glance at, not a block.
- Savings are modest (plan expected this); the gain is structure and preserved constraints.
- The prompt is ~1,000 tokens, so the free tier's tokens-per-minute limit allows roughly 6 compressions a minute; the automatic retry covers short bursts.

Bugs found and fixed during Phase 3:
- Qt returns the mode picker's enum data as a plain string, which would have broken forced modes (forced DICTATION would still have called the LLM).
- Operator-precedence bug in the json_schema fallback could have recursed forever.
- Negation check accepted a negation from a *different* line; now same line/sentence only.

## Phase 4: Context and history

Built:
- `app/context.py`: `ContextStore` under `<data>/context/`: `global.md` (seeded from the repo's `context_data/global.md`), `projects/<slug>/project.md`, `projects/<slug>/recent.json` (last 5). `build(project, exclude_item)` returns notes (global + project, `<!-- -->` stripped, template-only files ignored, capped at 2,000 chars) and recent commands (each capped at 500 chars, oldest first). Re-compressing an item replaces its entry and leaves it out of its own context. Project names are slugified (no path traversal). Corrupt `recent.json` is ignored and rewritten.
- `app/history.py`: `History` JSONL log (append, read, per-project stats, clear; corrupt lines skipped; off when `history_enabled` is false). `make_entry()` records transcript, command, mode, skip/fallback, token estimates and saving %, compress/transcribe/audio seconds, models, ambiguities, check summary. Token estimates use tiktoken `o200k_base`, downloaded once to `<data>/tiktoken-cache` in the background; chars ÷ 4 until then or if offline.
- `app/session.py`: glue between window, pipeline, context and history; project switching (saved via `config.update_user_settings`, which writes only changed keys so later default changes still apply); New project, Edit notes, Reset recent, Clear history (with confirmation), Open data folder.
- `app/compressor.py`: short commands are compressed anyway when there are recent commands and they refer back (`it`, `that`, `now`, `also`, `same`…). Without this, the plan's own example ("now add authentication") would have skipped the LLM and never seen its context.
- `app/pipeline.py`: an id per recording/typed text; `command_finished` signal with a `CommandResult` (compressed, report, transcript, recompressed flag); context provider gets `exclude_item`; a failing context load doesn't block the command.
- `app/ui.py`: project picker with "(notes, N recent)" indicator; Project and History menus; `NotesDialog` (Project + Global tabs); Command header shows `~before → ~after tokens (±%)`; status bar shows project totals.
- Compress button now forces compression of short commands (found in the user's first Phase 4 test: a 22-word command stayed uncompressed even after pressing Compress). Automatic flow after recording keeps the skip rule.
- `app/prompts.py`: new rule to keep reasons that limit what Claude can do ("because pytest isn't installed there").

Deviation from the plan, on purpose: the plan says only the project note and recent commands are sent. `global.md` is sent too (both are short, user-written, and comment-stripped), because otherwise it would have no effect at all. Easy to revert in `ContextStore.build()`.

Eval (real Groq, 16 samples): **15/16 pass**.
- New: "now add authentication to it" resolves to the `/api/health` endpoint from the previous command and overrides its old "No authentication"; "also do the same for the login form" carries the validation rules over; notes saying "pytest" don't override "use unittest, not pytest"; a notes stack (FastAPI, PostgreSQL) doesn't leak into an unrelated README task.
- Found and fixed: the model dropped the reason "pytest isn't installed there" (Claude might otherwise just install it) → prompt rule.
- Remaining failure unchanged: no "which function?" ambiguity.
- Overall only 2% shorter: follow-ups grow when "it" is spelled out, which is their purpose.

## Environment notes

- `libportaudio2`, `wl-clipboard` and `xclip` installed (2026-11-12).
- Default input: Raptor Lake digital mic at 43 % volume. Output: GHW-123P headset (laptop speaker muted).
- Groq key stored in the system keyring (2026-11-12); verified against the API. `whisper-large-v3-turbo`, `whisper-large-v3` and `openai/gpt-oss-120b` all available.
- GNOME already uses Super+Space for switching input sources; the README suggests Ctrl+Alt+Space instead. Two custom shortcuts (custom0, custom1) already exist.

## Phase 5: Clipboard and polish

Built:
- `app/clipboard.py`: `wl-copy` (Wayland) → `xclip` (X11) → Qt clipboard fallback (warned as temporary). Text via stdin, never argv. Output is discarded on purpose: `wl-copy` forks a helper that holds pipes open, so capturing output hangs (seen in testing).
- Copy button + Ctrl+Enter (also caught inside the text boxes, which otherwise swallow it as a newline), enabled only when `copy_allowed()` (non-empty; destructive commands confirmed). Copy never sends or presses anything else.
- Copied text replaces the item's recent-command entry when edited, so follow-ups build on what was actually sent; history gets a `{"event": "copied", "edited": …}` line (excluded from stats).
- `app/theme.py`: Fusion style with explicit light and dark palettes; System follows the desktop (Qt colour scheme, then `gsettings`), live. View → Theme, saved via `update_user_settings`.
- Error recovery: unhandled exceptions (Qt slots and threads) go to the log and show as ERROR in the window; a 1 s watchdog stops recording with a clear message if the mic stops delivering audio for 3 s (unplugged headset), instead of listening forever.
- Single instance hardened: `flock` instance lock next to the socket; long socket paths (> 100 bytes) get a short stable stand-in under `/tmp`; `voice2claude` won't launch a second app if one holds the lock.
- README: install with `python3` (3.12+), `python3-venv` added, hidden-input key command, Copy/Theme/Troubleshooting sections.

Verified:
- Real `wl-copy` → `wl-paste` round trip in the test suite (skipped without Wayland).
- Rendered the real window in light and dark on Wayland and looked at both.
- Fresh install from the README into an empty copy with system Python 3.14.4: 24 s pip install, 229 tests pass, app starts, shortcut works.

Bugs found and fixed in Phase 5:
- Light theme stayed dark: Fusion's "standard" palette follows the desktop's dark mode → explicit light palette.
- Ctrl+Enter in a text box inserted a newline instead of copying.
- **Fresh-install test exposed:** a socket path over the Unix limit left the app without its control socket, and then every `voice2claude toggle` launched *another* app (three recorded at once). Fixed with the path stand-in and the instance lock; re-tested the same scenario.

## v1 status

All five phases are built. Known limitations (see each phase): no "which function?" ambiguity for vague references; negation paraphrased as a positive restriction can show a red false alarm; modest token savings (structure is the main gain); free-tier rate limits (~6 compressions/min).

Ideas for later (from the plan): pipe into `claude "<command>"` after approval; offline Faster-Whisper/Ollama fallback; strike-through diff view; learn vocabulary from your edits (history already records edited copies); command templates.
