# Voice to Claude

Speak an instruction, get a short structured prompt for Claude, review it, paste it.
Speech-to-text and compression both run on one free Groq API key.

> Status: v1 complete (all 5 phases). See `DEVELOPMENT_STATUS.md` for details and known limitations.

**How it works:** press your shortcut → speak → the app transcribes (Groq Whisper), rewrites it into a
structured prompt (Groq LLM), checks nothing important was lost → you review, edit if needed, press
**Insert** (Ctrl+Enter) → it lands in Claude's prompt box → you press Enter yourself. The app never
presses Enter.

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

## Inserting into Claude

**Insert** (or **Ctrl+Enter**, also inside the text boxes) puts the command, including your edits,
straight into the window you were using, normally Claude's prompt box:

1. the command goes on the clipboard (`wl-copy` / `xclip`),
2. Voice to Claude minimises, so GNOME returns focus to the window you came from,
3. Ctrl+V is sent through GNOME's Remote Desktop portal.

It never presses Enter: review the text in Claude and send it yourself.

**First time only:** GNOME asks *"Allow remote interaction?"*. Allow it; the answer is remembered
(token in `~/.local/share/voice-to-claude/portal-restore-token`; delete that file to be asked again).
While inserting, GNOME may briefly show a remote-control icon in the top bar.

If the window behind Voice to Claude isn't Claude, the paste goes there instead. Use
**Edit → Copy command only** (Ctrl+Shift+C) when you want to paste somewhere yourself. If inserting
fails for any reason, the window comes back and the command is already on the clipboard: click
Claude's prompt box and press Ctrl+V.

Insert and Copy are disabled while the command is empty, or while a destructive command isn't
confirmed. If you edited the command, the edited version is what follow-ups will build on.

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

## Open with Claude desktop

Make Voice to Claude open every time Claude desktop starts:

```bash
./voice2claude companion install     # set up (takes effect now and at every login)
./voice2claude companion status      # check it
./voice2claude companion uninstall   # undo
```

This installs a small systemd user service (`~/.config/systemd/user/voice-to-claude-companion.service`)
that checks every 2 s whether `claude-desktop` is running. When Claude *starts*, it opens Voice to
Claude (or brings it forward). It works however Claude is launched and survives Claude updates. Closing
Voice to Claude while Claude is open won't make it reopen; it comes back the next time Claude starts.
Logs: `journalctl --user -u voice-to-claude-companion`.

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
| Insert pastes in the wrong window | Click in Claude's prompt box before pressing the shortcut, or use Edit → Copy command only. |
| Insert says permission was declined | Delete `~/.local/share/voice-to-claude/portal-restore-token` and Insert again to get GNOME's dialog back. |
| Shortcut does nothing | `./voice2claude status`; check the full path in the custom shortcut. |
| Anything else | `~/.local/share/voice-to-claude/logs/app.log` (no transcripts are logged there). |
