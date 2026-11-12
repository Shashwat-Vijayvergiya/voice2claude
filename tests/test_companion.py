import ast
import subprocess
import sys
from types import SimpleNamespace

from app import companion


def fake_proc(tmp_path, procs):
    for pid, name in procs.items():
        d = tmp_path / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(name + "\n")
    (tmp_path / "self").mkdir(parents=True)  # non-numeric entries are skipped
    return tmp_path


def test_claude_running_detection(tmp_path):
    assert companion.claude_running(fake_proc(tmp_path / "a", {1: "bash", 2: "claude-desktop"}))
    assert not companion.claude_running(fake_proc(tmp_path / "b", {1: "bash", 3: "claude"}))


def test_opens_only_when_claude_starts():
    # Claude: off, on, on, on, off, on  -> opens on each start, not on every check
    states = iter([False, True, True, True, False, True])
    opened = []
    companion.watch(lambda: opened.append(1), is_running=lambda: next(states), sleep=lambda s: None, max_checks=6)
    assert len(opened) == 2


def test_already_running_at_login_opens_once():
    opened = []
    companion.watch(lambda: opened.append(1), is_running=lambda: True, sleep=lambda s: None, max_checks=5)
    assert opened == [1]


def test_failed_open_keeps_watching(capsys):
    states = iter([True, False, True])
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    companion.watch(flaky, is_running=lambda: next(states), sleep=lambda s: None, max_checks=3)
    assert len(calls) == 2
    assert "boom" in capsys.readouterr().err


def test_unit_text():
    text = companion.unit_text()
    assert f"ExecStart={companion.CLI} watch-claude" in text
    assert "KillMode=process" in text  # stopping the watcher must not close the app
    assert "WantedBy=graphical-session.target" in text and "Restart=on-failure" in text


def ok(*args):
    ok.calls.append(args)
    return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")


def test_install_and_uninstall(tmp_path):
    ok.calls = []
    unit = tmp_path / "systemd" / "user" / companion.SERVICE
    msg = companion.install(run=ok, path=unit)
    assert unit.read_text() == companion.unit_text() and "Installed" in msg
    assert ("enable", "--now", companion.SERVICE) in ok.calls
    companion.uninstall(run=ok, path=unit)
    assert not unit.exists() and ("disable", "--now", companion.SERVICE) in ok.calls


def test_install_reports_systemctl_failure(tmp_path):
    def bad(*args):
        return SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus")

    try:
        companion.install(run=bad, path=tmp_path / "u.service")
    except RuntimeError as exc:
        assert "Failed to connect to bus" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_companion_is_stdlib_only():
    tree = ast.parse(open(companion.__file__).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    names.discard("__future__")
    assert names <= set(sys.stdlib_module_names)


def test_cli_usage_lists_companion(capsys):
    from app import control

    assert control.main(["nope"]) == 2
    assert "companion install" in capsys.readouterr().err


def test_cli_runs_on_system_python():
    out = subprocess.run([str(companion.CLI), "companion", "status"], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0 and out.stdout.startswith("companion:")
