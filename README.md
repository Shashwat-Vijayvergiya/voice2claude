# Voice to Claude

Speak an instruction, get a short structured prompt for Claude, review it, paste it.
Speech-to-text and compression both run on one free Groq API key.

> Status: v1 complete (all 5 phases). See `DEVELOPMENT_STATUS.md` for details and known limitations.

**How it works:** press your shortcut → speak → the app transcribes (Groq Whisper), rewrites it into a
structured prompt (Groq LLM), checks nothing important was lost → you review, edit if needed, press
**Copy** (Ctrl+Enter) → paste into Claude and press Enter yourself. The app never sends anything to
Claude and never presses Enter.

**Privacy:** audio and text go to Groq for processing; no audio is ever written to disk. Avoid dictating secrets.

## Install (Ubuntu)

Needs Python 3.12 or newer (tested on 3.12 and 3.14; Ubuntu 26.04's own `python3` works).

```bash
sudo apt install python3-venv libportaudio2 wl-clipboard xclip
cd voice-to-claude
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`libportaudio2` is for the microphone, `wl-clipboard`/`xclip` for Copy on Wayland/X11.

Provide your Groq key (get one at https://console.groq.com/keys), either as an environment variable:

```bash
export GROQ_API_KEY=...
```

or in the system keyring (recommended; the key isn't shown while you paste it):

```bash
.venv/bin/python -c "import keyring, getpass; keyring.set_password('voice-to-claude', 'groq_api_key', getpass.getpass('Groq key: ').strip())"
```

The key is never written to `settings.json`.

## Run

```bash
.venv/bin/python main.py
```

Press **Record** (or your shortcut) and speak. Recording stops when you press it again,
after 2 s of silence, or after 8 s if you say nothing. The transcript appears in the
**Original** pane, where you can edit it. If Groq fails, press **Retry** (once); the
audio is kept in memory only for that, never written to disk.

## Reviewing the command

After transcription, the transcript is rewritten into a short structured prompt
(TASK / REQUIREMENTS / CONSTRAINTS / VALIDATION) in the **Command** pane. Below it:

- **Green ✓**: every number, file, code name, quoted text and negative instruction ("don't…",
  "never…", "only…") from what you said is still in the command.
- **Red ✗**: items that went missing, plus a warning if most of your wording disappeared
  (the model may have answered your request instead of rewriting it).
- **Orange**: points the model found unclear. Answer them by editing the command.
- **Red box**: destructive wording (delete, drop, reset, force push, rm -rf…). Tick the box to
  confirm you've read it; Copy stays blocked until you do.

The check is a safety net, not proof. Always read the command before using it.

Edit the command freely and the check re-runs. Edit the transcript and press **Compress** to
rewrite it again. **Mode** forces COMMAND / QUESTION / CODE / DICTATION; *Auto* lets the model
decide. Under 30 words or DICTATION mode, no LLM call is made (only fillers are removed);
press **Compress** to compress a short command anyway.

## Copying

**Copy** (or **Ctrl+Enter**, also inside the text boxes) puts the command, including your edits,
on the clipboard: `wl-copy` on Wayland, `xclip` on X11, or Qt's clipboard as a last resort (that one
only lasts while the app is open; the status bar says so). Then paste into Claude and press Enter.

Copy is disabled while the command is empty, or while a destructive command isn't confirmed.
If you edited the command, the edited version is what follow-ups will build on.

To check the compression prompt against real Groq calls (uses your quota, about 16 requests):

```bash
.venv/bin/python tests/eval_compression.py
```

Add your own spoken commands to `tests/samples/commands.json` with the items that must survive.

## Projects, notes and follow-ups

Pick a project in the top bar (**Project → New project…** to add one). Each project has:

- **Notes** (**Project → Edit notes…**): a few lines about the goal, stack and rules. A
  **Global** tab holds notes for every project (your languages, style). Text inside `<!-- -->` is ignored.
- **Recent commands**: the last 5 commands, so follow-ups work. Say *"now add authentication to it"*
  and "it" is resolved from what you asked before. Short follow-ups (under 30 words) are still
  compressed when they refer back ("it", "that", "now", "also", "same"…); other short commands aren't.
  **Project → Reset recent commands** starts fresh.

Notes and recent commands are sent to Groq as *background* with each compression. They never
override what you say, and the app never reads your code.

## History and stats

Every command is appended to `~/.local/share/voice-to-claude/history.jsonl` (transcript, command,
mode, token estimates, timings, check result). The status bar shows the project's totals; the
Command header shows this command's `~before → ~after tokens`. Token counts are **estimates**
(tiktoken `o200k_base`; Claude's tokenizer isn't public). Its 3.6 MB encoding file is downloaded
once on first start; no text of yours is sent for counting.

- **History → Clear history…** permanently deletes the file.
- **History → Open data folder** shows where everything lives.
- Set `"history_enabled": false` in your settings file to stop saving history.

## Keyboard shortcut (works on Wayland and X11)

The app listens on a private local socket; the `voice2claude` command talks to it:

```bash
./voice2claude toggle   # start/stop recording (starts the app if needed)
./voice2claude show     # bring the window forward
./voice2claude status   # running / not running
```

Bind it to a key: **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → +**

- Name: `Voice to Claude`
- Command: the full path, e.g. `/home/<you>/Desktop/claude/voice-to-claude/voice2claude toggle`
- Shortcut: **Ctrl+Alt+Space** (suggested)

Avoid **Super+Space**: GNOME uses it to switch keyboard layouts/input sources.

On Wayland, apps can't force themselves to the front, so when a transcript is ready
the window may only flash in the dock. Click it to review.

## Theme

**View → Theme**: System (follows your desktop's dark mode, live), Light or Dark. Saved in your settings.

## Configuration

- Defaults: `config/settings.json` (in the repo).
- Your overrides: `~/.local/share/voice-to-claude/settings.json` (only the keys you change).
- Logs: `~/.local/share/voice-to-claude/logs/app.log`.
- Notes and recent commands: `~/.local/share/voice-to-claude/context/` (seeded from `context_data/global.md`).
- Set `VOICE2CLAUDE_HOME` to use a different data directory.

## Tests

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

Unit tests mock the Groq API and never use your quota.

## Troubleshooting

| Problem | Fix |
|---|---|
| "No Groq API key found" | Store the key (Install section), then restart the app. |
| "No speech detected" while you talk | Raise the mic input volume in Settings → Sound → Input. |
| "The microphone stopped sending audio" | The mic was unplugged or taken by another app; check it and press Record. |
| "Groq's free-tier limit was reached" | Wait a minute. Compression allows roughly 6 calls per minute on the free tier. |
| Copy fails | `sudo apt install wl-clipboard xclip`; you can always select the text and copy by hand. |
| Shortcut does nothing | `./voice2claude status`; check the full path in the custom shortcut. |
| Anything else | `~/.local/share/voice-to-claude/logs/app.log` (no transcripts are logged there). |
