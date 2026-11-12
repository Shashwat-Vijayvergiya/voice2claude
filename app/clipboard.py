"""Copy text to the system clipboard.

Wayland: ``wl-copy``. X11: ``xclip``. If neither works, Qt's own clipboard
(which only lasts while this app is running). Text goes in via stdin, never
as a command-line argument, so it doesn't show up in ``ps``.

``wl-copy`` forks a helper that keeps serving the clipboard. Its output must
not be captured: the helper would hold the pipe open and the call would hang.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Callable

log = logging.getLogger(__name__)

TIMEOUT = 3.0

WL_COPY = ["wl-copy", "--type", "text/plain;charset=utf-8"]
XCLIP = ["xclip", "-selection", "clipboard", "-in"]


class ClipboardError(Exception):
    pass


def _run(cmd: list[str], text: str) -> int:
    return subprocess.run(
        cmd,
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=TIMEOUT,
        check=False,
    ).returncode


def _qt_copy(text: str) -> bool:
    try:
        from PySide6.QtGui import QGuiApplication

        clip = QGuiApplication.clipboard() if QGuiApplication.instance() else None
    except Exception:
        return False
    if clip is None:
        return False
    clip.setText(text)
    return True


def candidates(env: dict | None = None) -> list[tuple[str, list[str]]]:
    env = os.environ if env is None else env
    out = []
    if env.get("WAYLAND_DISPLAY"):
        out.append(("wl-copy", WL_COPY))
    if env.get("DISPLAY"):
        out.append(("xclip", XCLIP))
    return out


def copy(
    text: str,
    env: dict | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str], str], int] = _run,
    qt_copy: Callable[[str], bool] = _qt_copy,
) -> str:
    """Copy ``text``; return the method used. Raises ClipboardError if nothing worked."""
    if not text:
        raise ClipboardError("Nothing to copy.")
    problems = []
    tools = candidates(env)
    for name, cmd in tools:
        if which(cmd[0]) is None:
            problems.append(f"{name} not installed")
            continue
        try:
            rc = run(cmd, text)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{name} failed: {exc}")
            continue
        if rc == 0:
            return name
        problems.append(f"{name} exited with {rc}")
    if qt_copy(text):
        log.warning("Clipboard tools unavailable (%s); used Qt clipboard", "; ".join(problems) or "no display")
        return "qt"
    hint = "Install them with: sudo apt install wl-clipboard xclip" if tools else "No graphical session found."
    raise ClipboardError(f"Could not copy ({'; '.join(problems) or 'no clipboard available'}). {hint}")
