from app.config import MISSING_KEY_MESSAGE
from app.ui import MainWindow, Status


def test_window_starts_idle(qtbot):
    w = MainWindow(project="demo")
    qtbot.addWidget(w)
    assert w.status is Status.IDLE
    assert w.current_project() == "demo"
    assert not w.copy_button.isEnabled()


def test_missing_key_shows_error(qtbot):
    w = MainWindow(api_key_missing_message=MISSING_KEY_MESSAGE)
    qtbot.addWidget(w)
    assert w.status is Status.ERROR
    assert "GROQ_API_KEY" in w.message_label.text()


def test_set_status(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    w.set_status(Status.LISTENING, "Speak now")
    assert "LISTENING" in w.status_label.text()
    assert w.message_label.text() == "Speak now"


# ---- Phase 3: preview ------------------------------------------------------

from app.checker import check  # noqa: E402
from app.models import Compressed, Mode  # noqa: E402

SRC = "refactor parse_config in utils.py and don't change the public API, use 3 retries"


def show(w, command, destructive=False, ambiguities=()):
    c = Compressed(original=SRC, command=command, mode=Mode.COMMAND, destructive=destructive,
                   ambiguities=list(ambiguities), latency_s=1.2)
    w.show_compressed(c, check(SRC, command))


def test_all_kept_is_green(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "TASK: Refactor parse_config in utils.py\nCONSTRAINTS:\n- Do not change the public API\n- 3 retries")
    assert "All" in w.check_label.text() and "kept" in w.check_label.text()
    assert "COMMAND" in w.command_info.text() and "1.2 s" in w.command_info.text()
    assert w.copy_allowed()


def test_missing_items_shown_in_red(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "TASK: Refactor parse_config")
    text = w.check_label.text()
    assert "Missing" in text and "#d33" in text
    assert "utils.py" in text and "3" in text


def test_edit_rechecks(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "TASK: Refactor parse_config")
    w.command_edit.setPlainText("Refactor parse_config in utils.py, do not change the public API, 3 retries")
    qtbot.waitUntil(lambda: w.report is not None and w.report.ok, timeout=2000)


def test_destructive_requires_confirmation(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "Delete utils.py", destructive=True)
    assert not w.destructive_frame.isHidden()
    assert "delete" in w.destructive_label.text()
    assert not w.copy_allowed()
    w.destructive_confirm.setChecked(True)
    assert w.copy_allowed()


def test_model_flag_alone_shows_warning(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "Clean up the build folder", destructive=True)
    assert not w.destructive_frame.isHidden() and not w.copy_allowed()


def test_new_destructive_wording_resets_confirmation(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "Delete utils.py", destructive=True)
    w.destructive_confirm.setChecked(True)
    w.command_edit.setPlainText("Delete utils.py and force push")
    w.recheck()
    assert not w.destructive_confirm.isChecked() and not w.copy_allowed()


def test_ambiguities_listed(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "TASK: x", ambiguities=["Which module is 'it'?"])
    assert "Which module" in w.ambiguity_label.text()


def test_mode_picker_and_compress_button(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    modes, texts = [], []
    w.mode_changed.connect(modes.append)
    w.compress_requested.connect(texts.append)
    w.mode_picker.setCurrentIndex(w.mode_picker.findText("CODE"))
    assert modes[-1] is Mode.CODE and w.forced_mode() is Mode.CODE
    w.original_edit.setPlainText("edited transcript")
    w.compress_button.click()
    assert texts == ["edited transcript"]


def test_new_transcript_clears_command(qtbot):
    from app.models import Transcript

    w = MainWindow()
    qtbot.addWidget(w)
    show(w, "Delete utils.py", destructive=True)
    w.show_transcript(Transcript(text="new", audio_seconds=1, latency_s=0.1, model="m"))
    assert w.command_edit.toPlainText() == "" and w.destructive_frame.isHidden()
    assert not w.copy_allowed()


def test_low_coverage_warning_shown(qtbot):
    from tests.test_checker import D

    w = MainWindow()
    qtbot.addWidget(w)
    c = Compressed(original=D, command="The appropriate mode is DICTATION.", mode=Mode.QUESTION)
    w.show_compressed(c, check(D, c.command))
    assert "made it into the command" in w.check_label.text()


# ---- Phase 5: copy gate -----------------------------------------------------

def test_copy_button_follows_gate(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    assert not w.copy_button.isEnabled()  # empty
    show(w, "TASK: Refactor parse_config in utils.py")
    assert w.copy_button.isEnabled()
    show(w, "Delete utils.py", destructive=True)
    assert not w.copy_button.isEnabled()
    w.destructive_confirm.setChecked(True)
    assert w.copy_button.isEnabled()


def test_copy_emits_edited_text_and_blocks_unconfirmed(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    got = []
    w.copy_requested.connect(got.append)
    show(w, "Delete utils.py", destructive=True)
    w.request_copy()  # blocked: not confirmed
    assert got == [] and "Tick" in w.statusBar().currentMessage()
    w.destructive_confirm.setChecked(True)
    w.command_edit.setPlainText("Delete utils.py only  \n")
    w.recheck()
    w.destructive_confirm.setChecked(True)
    w.request_copy()
    assert got == ["Delete utils.py only"]


def test_ctrl_enter_copies(qtbot):
    from PySide6.QtCore import Qt

    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    got = []
    w.copy_requested.connect(got.append)
    show(w, "TASK: Refactor parse_config in utils.py")
    qtbot.keyClick(w.command_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert got == ["TASK: Refactor parse_config in utils.py"]


def test_theme_menu(qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    got = []
    w.theme_requested.connect(got.append)
    w.set_theme_checked("light")
    assert w.theme_actions["light"].isChecked()
    w.theme_actions["dark"].trigger()
    assert got == ["dark"] and w.theme_actions["dark"].isChecked()
