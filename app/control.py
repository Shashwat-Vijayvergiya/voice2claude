"""Local control socket so a desktop shortcut can drive the running app.

Standard library only: the ``voice2claude`` CLI imports this with the system
Python, so it must not pull in Qt, pydantic or anything from the venv.

Protocol: client sends one line (``toggle``, ``show`` or ``ping``), server
answers ``ok`` or ``error <reason>``.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

COMMANDS = ("toggle", "show", "ping")
REPO_DIR = Path(__file__).resolve().parent.parent


MAX_SOCKET_PATH = 100  # Unix socket paths are limited to ~108 bytes


def socket_path() -> Path:
    override = os.environ.get("VOICE2CLAUDE_SOCKET")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if override:
        path = Path(override)
    elif runtime and Path(runtime).is_dir():
        path = Path(runtime) / "voice-to-claude.sock"
    else:
        path = Path(f"/tmp/voice-to-claude-{os.getuid()}.sock")
    if len(os.fsencode(str(path))) > MAX_SOCKET_PATH:
        # Too long to bind; use a short, stable stand-in so client and app still agree.
        digest = hashlib.sha1(os.fsencode(str(path))).hexdigest()[:10]
        path = Path(f"/tmp/voice-to-claude-{os.getuid()}-{digest}.sock")
    return path


def lock_path(path: Path | None = None) -> Path:
    return (path or socket_path()).with_suffix(".lock")


def acquire_instance_lock(path: Path | None = None):
    """Hold an exclusive lock for the app's lifetime (released by the OS on exit or crash).

    Returns the open file to keep, or None if another instance holds it. This
    guards against duplicate apps even when the control socket can't be used.
    """
    lock = lock_path(path)
    handle = open(lock, "a+")
    try:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def instance_locked(path: Path | None = None) -> bool:
    handle = acquire_instance_lock(path)
    if handle is None:
        return True
    handle.close()
    return False


def send_command(command: str, path: Path | None = None, timeout: float = 2.0) -> str:
    """Send one command. Raises OSError if the app is not running."""
    path = path or socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(command.encode() + b"\n")
        return s.makefile("r", encoding="utf-8").readline().strip()


def is_running(path: Path | None = None) -> bool:
    try:
        return send_command("ping", path, timeout=1.0) == "ok"
    except OSError:
        return False


class _Handler(socketserver.StreamRequestHandler):
    timeout = 2.0

    def handle(self) -> None:
        try:
            line = self.rfile.readline(64).decode("utf-8", "replace").strip().lower()
        except (OSError, socket.timeout):
            return
        if line not in COMMANDS:
            self.wfile.write(b"error unknown command\n")
            return
        if line != "ping":
            try:
                self.server.on_command(line)  # type: ignore[attr-defined]
            except Exception:
                log.exception("Control command %r failed", line)
                self.wfile.write(b"error internal\n")
                return
        self.wfile.write(b"ok\n")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ControlServer:
    """Listens on the socket in a background thread and calls ``on_command(cmd)``.

    ``on_command`` runs on the server thread: the UI side must hand it over to
    the Qt thread (a Qt signal does that).
    """

    def __init__(self, on_command: Callable[[str], None], path: Path | None = None):
        self.path = path or socket_path()
        self.on_command = on_command
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.path.exists():
            if is_running(self.path):
                raise RuntimeError("Voice to Claude is already running")
            self.path.unlink()  # stale socket from a crash
        old_umask = os.umask(0o177)  # socket readable/writable by this user only
        try:
            self._server = _Server(str(self.path), _Handler)
        finally:
            os.umask(old_umask)
        self._server.on_command = self.on_command  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, name="control", daemon=True)
        self._thread.start()
        log.info("Control socket at %s", self.path)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _launch_app(extra_args: list[str]) -> None:
    venv_python = REPO_DIR / ".venv" / "bin" / "python"
    python = str(venv_python if venv_python.exists() else sys.executable)
    subprocess.Popen(
        [python, str(REPO_DIR / "main.py"), *extra_args],
        cwd=str(REPO_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


USAGE = """usage: voice2claude <command>

  toggle               start or stop recording (starts the app if it isn't running)
  show                 bring the window forward (starts the app if it isn't running)
  status               say whether the app is running

  shortcut install     set Ctrl+Alt+Space to start/stop recording (GNOME)
  shortcut uninstall   remove that shortcut
  shortcut status      show the shortcut
  companion install    open Voice to Claude whenever Claude desktop starts
  companion uninstall  stop doing that
  companion status     show whether it's set up
  watch-claude         (used by the companion service) watch for Claude desktop
"""


def main(argv: list[str]) -> int:
    if argv[:1] == ["companion"] and len(argv) == 2 and argv[1] in ("install", "uninstall", "status"):
        from app import companion

        try:
            print(getattr(companion, argv[1])())
        except (RuntimeError, OSError) as exc:
            sys.stderr.write(f"voice2claude: {exc}\n")
            return 1
        return 0
    if argv[:1] == ["shortcut"] and len(argv) == 2 and argv[1] in ("install", "uninstall", "status"):
        from app import companion

        try:
            print(getattr(companion, f"shortcut_{argv[1]}")())
        except (RuntimeError, OSError) as exc:
            sys.stderr.write(f"voice2claude: {exc}\n")
            return 1
        return 0
    if argv == ["watch-claude"]:
        from app import companion

        companion.watch(lambda: main(["show"]))
        return 0
    if len(argv) != 1 or argv[0] not in ("toggle", "show", "status"):
        sys.stderr.write(USAGE)
        return 2
    cmd = argv[0]
    if cmd == "status":
        running = is_running()
        print("running" if running else "not running")
        return 0 if running else 1
    try:
        reply = send_command(cmd)
    except OSError:
        if instance_locked():
            sys.stderr.write(
                "voice2claude: the app is running but its control socket isn't reachable; "
                "see ~/.local/share/voice-to-claude/logs/app.log\n"
            )
            return 1
        _launch_app(["--record"] if cmd == "toggle" else [])
        print("Voice to Claude was not running; starting it.")
        return 0
    if reply != "ok":
        sys.stderr.write(f"voice2claude: {reply}\n")
        return 1
    return 0
