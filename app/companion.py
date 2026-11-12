"""Open Voice to Claude whenever Claude desktop starts.

A small watcher (run as a systemd user service) checks every few seconds
whether a ``claude-desktop`` process exists. When Claude *starts*, it opens
Voice to Claude, or brings it forward if it's already open. It only reacts to
Claude starting, so closing Voice to Claude while Claude runs won't make it
pop back up.

Standard library only: it runs on the system Python, like ``voice2claude``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

CLAUDE_PROCESS = "claude-desktop"
POLL_SECONDS = 2.0
SERVICE = "voice-to-claude-companion.service"
REPO_DIR = Path(__file__).resolve().parent.parent
CLI = REPO_DIR / "voice2claude"


def claude_running(proc: Path = Path("/proc")) -> bool:
    """True if any process is named claude-desktop (reads /proc; no pgrep needed)."""
    uid = os.getuid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if (entry / "comm").read_text().strip() == CLAUDE_PROCESS:
                return True
        except OSError:  # process ended while we looked
            continue
    return False


def watch(
    open_app: Callable[[], object],
    is_running: Callable[[], bool] = claude_running,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = POLL_SECONDS,
    max_checks: int | None = None,
) -> None:
    """Call ``open_app`` each time Claude goes from not running to running."""
    was_running = False
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        try:
            now = is_running()
        except OSError:
            now = was_running
        if now and not was_running:
            print("Claude desktop started; opening Voice to Claude", flush=True)
            try:
                open_app()
            except Exception as exc:  # keep watching even if one launch fails
                print(f"Could not open Voice to Claude: {exc}", file=sys.stderr, flush=True)
        was_running = now
        sleep(interval)


# ---- systemd user service ---------------------------------------------------------


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SERVICE


def unit_text(cli: Path = CLI) -> str:
    return f"""[Unit]
Description=Open Voice to Claude when Claude desktop starts
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={cli} watch-claude
Restart=on-failure
RestartSec=5
# Voice to Claude is started from here; don't close it when this watcher stops or restarts.
KillMode=process

[Install]
WantedBy=graphical-session.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)


def install(run: Callable[..., subprocess.CompletedProcess] = _systemctl, path: Path | None = None) -> str:
    path = path or unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(), encoding="utf-8")
    for args in (("daemon-reload",), ("enable", "--now", SERVICE), ("restart", SERVICE)):
        result = run(*args)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}")
    return f"Installed {path}. Voice to Claude will now open whenever Claude desktop starts (from your next login on, and right now)."


def uninstall(run: Callable[..., subprocess.CompletedProcess] = _systemctl, path: Path | None = None) -> str:
    path = path or unit_path()
    run("disable", "--now", SERVICE)
    if path.exists():
        path.unlink()
    run("daemon-reload")
    return "Removed. Voice to Claude no longer opens with Claude desktop (an open window stays open)."


def status(run: Callable[..., subprocess.CompletedProcess] = _systemctl) -> str:
    enabled = run("is-enabled", SERVICE).stdout.strip()
    if enabled in ("", "not-found"):
        enabled = "not installed"
    active = run("is-active", SERVICE).stdout.strip() or "inactive"
    return f"companion: {enabled}, {active}; Claude desktop {'running' if claude_running() else 'not running'}"


# ---- GNOME keyboard shortcut ----------------------------------------------------------

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = MEDIA_KEYS + ".custom-keybinding"
CUSTOM_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SHORTCUT_NAME = "Voice to Claude"
DEFAULT_BINDING = "<Control><Alt>space"


def _gsettings(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=10)


def _parse_list(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    return [p.strip().strip("'\"") for p in text.strip("[]").split(",") if p.strip().strip("'\"")]


def _format_list(paths: list[str]) -> str:
    return "[" + ", ".join(f"'{p}'" for p in paths) + "]"


def _get(run, path: str, key: str) -> str:
    return run("get", f"{CUSTOM_SCHEMA}:{path}", key).stdout.strip().strip("'")


def find_shortcut(run=_gsettings) -> str | None:
    for path in _parse_list(run("get", MEDIA_KEYS, "custom-keybindings").stdout):
        if _get(run, path, "name") == SHORTCUT_NAME or "voice2claude" in _get(run, path, "command"):
            return path
    return None


def shortcut_install(binding: str = DEFAULT_BINDING, run=_gsettings) -> str:
    """Add (or update) a GNOME custom shortcut that runs `voice2claude toggle`, keeping all others."""
    paths = _parse_list(run("get", MEDIA_KEYS, "custom-keybindings").stdout)
    path = find_shortcut(run)
    if path is None:
        n = 0
        while f"{CUSTOM_BASE}custom{n}/" in paths:
            n += 1
        path = f"{CUSTOM_BASE}custom{n}/"
        paths.append(path)
    for args in (
        ("set", MEDIA_KEYS, "custom-keybindings", _format_list(paths)),
        ("set", f"{CUSTOM_SCHEMA}:{path}", "name", SHORTCUT_NAME),
        ("set", f"{CUSTOM_SCHEMA}:{path}", "command", f"{CLI} toggle"),
        ("set", f"{CUSTOM_SCHEMA}:{path}", "binding", binding),
    ):
        result = run(*args)
        if result.returncode != 0:
            raise RuntimeError(f"gsettings {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return f"Shortcut set: {binding.replace('<Control>', 'Ctrl+').replace('<Alt>', 'Alt+').replace('<Super>', 'Super+')} runs '{CLI} toggle'."


def shortcut_uninstall(run=_gsettings) -> str:
    path = find_shortcut(run)
    if path is None:
        return "No Voice to Claude shortcut found."
    paths = [p for p in _parse_list(run("get", MEDIA_KEYS, "custom-keybindings").stdout) if p != path]
    run("set", MEDIA_KEYS, "custom-keybindings", _format_list(paths))
    run("reset-recursively", f"{CUSTOM_SCHEMA}:{path}")
    return "Shortcut removed."


def shortcut_status(run=_gsettings) -> str:
    path = find_shortcut(run)
    if path is None:
        return "shortcut: not set"
    return f"shortcut: {_get(run, path, 'binding')} -> {_get(run, path, 'command')}"
