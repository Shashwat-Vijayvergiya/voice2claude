import os
import shutil
import subprocess

import pytest

from app import clipboard
from app.clipboard import ClipboardError, copy

WAYLAND = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
X11 = {"DISPLAY": ":0"}


class Runner:
    def __init__(self, codes=None, raises=None):
        self.codes, self.raises, self.calls = codes or {}, raises or {}, []

    def __call__(self, cmd, text):
        self.calls.append((cmd[0], text))
        if cmd[0] in self.raises:
            raise self.raises[cmd[0]]
        return self.codes.get(cmd[0], 0)


def have_all(name):
    return f"/usr/bin/{name}"


def test_wayland_uses_wl_copy_with_text_on_stdin():
    run = Runner()
    assert copy("TASK: ✓ x", env=WAYLAND, which=have_all, run=run, qt_copy=lambda t: False) == "wl-copy"
    assert run.calls == [("wl-copy", "TASK: ✓ x")]


def test_x11_uses_xclip():
    run = Runner()
    assert copy("x", env=X11, which=have_all, run=run, qt_copy=lambda t: False) == "xclip"


def test_wl_copy_failure_falls_back_to_xclip():
    run = Runner(codes={"wl-copy": 1})
    assert copy("x", env=WAYLAND, which=have_all, run=run, qt_copy=lambda t: False) == "xclip"


def test_timeout_falls_back():
    run = Runner(raises={"wl-copy": subprocess.TimeoutExpired("wl-copy", 3)})
    assert copy("x", env=WAYLAND, which=have_all, run=run, qt_copy=lambda t: False) == "xclip"


def test_missing_tools_use_qt():
    got = []
    assert copy("x", env=WAYLAND, which=lambda n: None, run=Runner(), qt_copy=lambda t: got.append(t) or True) == "qt"
    assert got == ["x"]


def test_nothing_works_raises_with_install_hint():
    with pytest.raises(ClipboardError, match="apt install wl-clipboard"):
        copy("x", env=WAYLAND, which=lambda n: None, run=Runner(), qt_copy=lambda t: False)


def test_no_display():
    with pytest.raises(ClipboardError, match="No graphical session"):
        copy("x", env={}, which=have_all, run=Runner(), qt_copy=lambda t: False)


def test_empty_text_rejected():
    with pytest.raises(ClipboardError):
        copy("", env=WAYLAND, which=have_all, run=Runner())


def test_text_never_in_command_line():
    assert all("TASK" not in part for part in clipboard.WL_COPY + clipboard.XCLIP)


@pytest.mark.skipif(not (os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste")), reason="needs a Wayland session")
def test_real_wl_copy_roundtrip_does_not_hang():
    """Real wl-copy forks a helper; capturing its output would hang. Must return fast."""
    text = "voice-to-claude test ✓ " + str(os.getpid())
    assert copy(text) == "wl-copy"
    pasted = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=3).stdout
    assert pasted == text
