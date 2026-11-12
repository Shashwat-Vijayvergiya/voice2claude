import json

import pytest

from app import config
from app.checker import check
from app.context import ContextStore
from app.history import History
from app.models import CommandResult, Compressed, Mode
from app.pipeline import Pipeline
from app.session import Session
from app.ui import MainWindow
from tests.test_pipeline import FakeRecorder


@pytest.fixture
def env(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE2CLAUDE_HOME", str(tmp_path))
    store = ContextStore(tmp_path / "context")
    store.ensure()
    hist = History(tmp_path / "history.jsonl")
    window = MainWindow()
    qtbot.addWidget(window)
    pipeline = Pipeline(FakeRecorder(), None)
    session = Session(window, pipeline, store, hist, "default")
    session.confirm = lambda *a: True
    return session, window, pipeline, store, hist, tmp_path


def finished(pipeline, item_id="i1", original="add a health endpoint to the API", command="TASK: Add health endpoint",
             recompressed=False, fallback=None):
    c = Compressed(original=original, command=command, mode=Mode.COMMAND, fallback_error=fallback)
    pipeline.command_finished.emit(CommandResult(item_id, c, check(original, command), None, recompressed))


def test_finished_command_goes_to_history_and_context(env):
    session, window, pipeline, store, hist, _ = env
    finished(pipeline)
    assert len(hist.entries()) == 1
    assert pipeline.context_provider().recent_commands == ["TASK: Add health endpoint"]
    assert "1 recent" in window.context_label.text()
    assert "1 command" in window.stats_label.text()


def test_fallback_logged_but_not_remembered(env):
    session, window, pipeline, store, hist, _ = env
    finished(pipeline, fallback="timeout", command="raw text")
    assert len(hist.entries()) == 1
    assert store.recent("default") == []


def test_recompress_replaces_and_is_excluded(env):
    session, window, pipeline, store, hist, _ = env
    finished(pipeline, "a", command="cmd a")
    finished(pipeline, "b", command="cmd b v1")
    finished(pipeline, "b", command="cmd b v2", recompressed=True)
    assert pipeline.context_provider().recent_commands == ["cmd a", "cmd b v2"]
    assert pipeline.context_provider(exclude_item="b").recent_commands == ["cmd a"]
    assert len(hist.entries()) == 3  # history is a log: every attempt is kept


def test_switch_project_isolates_context_and_persists(env):
    session, window, pipeline, store, hist, home = env
    finished(pipeline, command="default cmd")
    session.ask_text = lambda *a: ("Voice App", True)
    session.new_project()
    assert session.project == "voice-app" and window.current_project() == "voice-app"
    assert pipeline.context_provider().recent_commands == []
    finished(pipeline, "x", command="voice cmd")
    window.project_picker.setCurrentText("default")
    assert session.project == "default"
    assert pipeline.context_provider().recent_commands == ["default cmd"]
    saved = json.loads((home / "settings.json").read_text())
    assert saved == {"current_project": "default"}  # only the changed key
    assert config.load_settings().whisper_model == "whisper-large-v3-turbo"


def test_cancelled_new_project(env):
    session, *_ = env
    session.ask_text = lambda *a: ("", False)
    session.new_project()
    assert session.project == "default"


def test_unknown_saved_project_falls_back(qtbot, tmp_path):
    store = ContextStore(tmp_path / "c")
    store.ensure()
    w = MainWindow()
    qtbot.addWidget(w)
    s = Session(w, Pipeline(FakeRecorder(), None), store, History(tmp_path / "h.jsonl"), "deleted-project")
    assert s.project == "default"


def test_reset_recent(env):
    session, window, pipeline, store, *_ = env
    finished(pipeline)
    window.reset_recent_requested.emit()
    assert store.recent("default") == [] and "0 recent" in window.context_label.text()


def test_clear_history_needs_confirmation(env):
    session, window, pipeline, store, hist, _ = env
    finished(pipeline)
    session.confirm = lambda *a: False
    window.clear_history_requested.emit()
    assert len(hist.entries()) == 1
    session.confirm = lambda *a: True
    window.clear_history_requested.emit()
    assert hist.entries() == []


def test_history_off_shown(qtbot, tmp_path):
    store = ContextStore(tmp_path / "c")
    store.ensure()
    w = MainWindow()
    qtbot.addWidget(w)
    Session(w, Pipeline(FakeRecorder(), None), store, History(tmp_path / "h.jsonl", enabled=False), "default")
    assert w.stats_label.text() == "History off"


def test_notes_flag(env):
    session, window, pipeline, store, *_ = env
    assert "no notes" in window.context_label.text()
    store.write_project("default", "# default\nStack: FastAPI")
    session.refresh()
    assert "(notes," in window.context_label.text()


# ---- Phase 5: copy ---------------------------------------------------------

from app.clipboard import ClipboardError  # noqa: E402


def test_copy_unedited(env, monkeypatch):
    session, window, pipeline, store, hist, _ = env
    copied = []
    session.copy_fn = lambda t: copied.append(t) or "wl-copy"
    finished(pipeline, command="TASK: Add health endpoint")
    window.copy_requested.emit("TASK: Add health endpoint")
    assert copied == ["TASK: Add health endpoint"]
    assert "Copied" in window.statusBar().currentMessage()
    event = hist.entries()[-1]
    assert event["event"] == "copied" and event["edited"] is False and event["command"] is None
    assert hist.stats().commands == 1  # the copy event isn't counted as a command


def test_copy_edited_updates_recent(env):
    session, window, pipeline, store, hist, _ = env
    session.copy_fn = lambda t: "wl-copy"
    finished(pipeline, "i1", command="TASK: Add health endpoint")
    window.copy_requested.emit("TASK: Add health endpoint at /healthz")
    assert pipeline.context_provider().recent_commands == ["TASK: Add health endpoint at /healthz"]
    assert hist.entries()[-1]["edited"] is True


def test_copy_failure_shows_message(env, monkeypatch):
    session, window, pipeline, store, hist, _ = env
    shown = []
    monkeypatch.setattr("app.session.QMessageBox.warning", lambda *a: shown.append(a[2]))

    def boom(t):
        raise ClipboardError("Could not copy. Install them with: sudo apt install wl-clipboard xclip")

    session.copy_fn = boom
    finished(pipeline)
    window.copy_requested.emit("x")
    assert shown and "apt install" in shown[0]
    assert not any(e.get("event") for e in hist.entries())


def test_qt_fallback_warns_it_is_temporary(env):
    session, window, pipeline, *_ = env
    session.copy_fn = lambda t: "qt"
    window.copy_requested.emit("x")
    assert "only while this app is open" in window.statusBar().currentMessage()


def test_theme_saved_and_applied(env, tmp_path):
    session, window, *_ , home = env
    applied = []
    session.apply_theme_fn = applied.append
    window.theme_actions["dark"].trigger()
    assert applied == ["dark"]
    assert json.loads((home / "settings.json").read_text())["theme"] == "dark"



# ---- Insert into the previous window -----------------------------------------------

from app.inserter import InsertError  # noqa: E402


class FakePaster:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def paste(self, before_keys):
        self.calls += 1
        before_keys()
        if self.error:
            raise InsertError(self.error)


def test_insert_copies_first_minimizes_then_pastes(env, qtbot):
    session, window, pipeline, store, hist, _ = env
    order = []
    session.copy_fn = lambda t: order.append(("copy", t)) or "wl-copy"
    session.INSERT_FOCUS_DELAY = 0
    session.paster = FakePaster()
    window.show()
    finished(pipeline, command="TASK: Add health endpoint")
    window.insert_requested.emit("TASK: Add health endpoint")
    qtbot.waitUntil(lambda: not session._inserting, timeout=3000)
    assert order == [("copy", "TASK: Add health endpoint")] and session.paster.calls == 1
    assert "Inserted" in window.statusBar().currentMessage()
    assert hist.entries()[-1]["event"] == "inserted"


def test_insert_failure_restores_window_and_says_ctrl_v(env, qtbot):
    session, window, pipeline, *_ = env
    session.copy_fn = lambda t: "wl-copy"
    session.INSERT_FOCUS_DELAY = 0
    session.paster = FakePaster(error="Permission to type into other windows was declined.")
    window.insert_requested.emit("x")
    qtbot.waitUntil(lambda: not session._inserting, timeout=3000)
    assert "declined" in window.message_label.text() and "Ctrl+V" in window.message_label.text()
    assert not window.isMinimized()


def test_insert_without_real_clipboard_does_not_paste(env):
    session, window, *_ = env
    session.copy_fn = lambda t: "qt"  # Qt clipboard may not survive the focus change
    session.paster = FakePaster()
    window.insert_requested.emit("x")
    assert session.paster.calls == 0 and "Ctrl+V" in window.message_label.text()


def test_insert_ignored_while_one_is_running(env):
    session, window, *_ = env
    session.copy_fn = lambda t: "wl-copy"
    session.paster = FakePaster()
    session._inserting = True
    window.insert_requested.emit("x")
    assert session.paster.calls == 0
