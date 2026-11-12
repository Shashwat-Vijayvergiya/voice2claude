import os
import stat
import threading

import pytest

from app import control


@pytest.fixture
def sock(tmp_path, monkeypatch):
    path = tmp_path / "v2c.sock"
    monkeypatch.setenv("VOICE2CLAUDE_SOCKET", str(path))
    return path


@pytest.fixture
def server(sock):
    got = []
    event = threading.Event()

    def on_command(cmd):
        got.append(cmd)
        event.set()

    srv = control.ControlServer(on_command)
    srv.start()
    srv.got, srv.event = got, event
    yield srv
    srv.stop()


def test_socket_path_override(sock):
    assert control.socket_path() == sock


def test_toggle_reaches_callback(server):
    assert control.send_command("toggle") == "ok"
    assert server.event.wait(2)
    assert server.got == ["toggle"]


def test_ping_does_not_call_callback(server):
    assert control.is_running()
    assert server.got == []


def test_unknown_command_rejected(server):
    assert control.send_command("rm -rf /").startswith("error")
    assert server.got == []


def test_socket_is_private(server, sock):
    assert stat.S_IMODE(os.stat(sock).st_mode) & 0o077 == 0


def test_second_server_refused(server):
    with pytest.raises(RuntimeError, match="already running"):
        control.ControlServer(lambda c: None).start()


def test_stale_socket_is_replaced(sock):
    sock.write_text("")  # leftover file, nothing listening
    srv = control.ControlServer(lambda c: None)
    srv.start()
    try:
        assert control.is_running()
    finally:
        srv.stop()
    assert not sock.exists()


def test_not_running(sock):
    assert not control.is_running()
    with pytest.raises(OSError):
        control.send_command("toggle")


def test_cli_status_and_usage(sock, server, capsys):
    assert control.main(["status"]) == 0
    assert control.main(["nope"]) == 2


def test_cli_toggle_sends(server):
    assert control.main(["toggle"]) == 0
    assert server.event.wait(2)
    assert server.got == ["toggle"]


def test_cli_launches_app_when_not_running(sock, monkeypatch):
    launched = []
    monkeypatch.setattr(control, "_launch_app", launched.append)
    assert control.main(["toggle"]) == 0
    assert launched == [["--record"]]
    assert control.main(["show"]) == 0
    assert launched[-1] == []


def test_control_module_is_stdlib_only():
    """The CLI runs on the system Python, which has none of the venv packages."""
    import ast
    import sys

    tree = ast.parse(open(control.__file__).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    names.discard("__future__")
    names.discard("app")  # only app.companion, itself checked as stdlib-only in test_companion
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


# ---- Phase 5: long paths and the instance lock ---------------------------------

def test_long_socket_path_gets_short_stand_in(tmp_path, monkeypatch):
    long_path = tmp_path / ("x" * 120) / "v2c.sock"
    monkeypatch.setenv("VOICE2CLAUDE_SOCKET", str(long_path))
    p = control.socket_path()
    assert len(str(p)) <= control.MAX_SOCKET_PATH and str(p).startswith("/tmp/voice-to-claude-")
    assert control.socket_path() == p  # stable, so client and app agree
    srv = control.ControlServer(lambda c: None)
    srv.start()  # binds fine
    try:
        assert control.is_running()
    finally:
        srv.stop()
        control.lock_path(p).unlink(missing_ok=True)


def test_instance_lock_is_exclusive(sock):
    first = control.acquire_instance_lock()
    assert first is not None
    assert control.acquire_instance_lock() is None
    assert control.instance_locked()
    first.close()  # the OS releases it on exit or crash too
    assert not control.instance_locked()


def test_cli_does_not_launch_second_app_when_locked(sock, monkeypatch):
    launched = []
    monkeypatch.setattr(control, "_launch_app", launched.append)
    held = control.acquire_instance_lock()
    try:
        assert control.main(["toggle"]) == 1  # running but unreachable: report, don't launch
        assert launched == []
    finally:
        held.close()
    assert control.main(["toggle"]) == 0 and launched == [["--record"]]
