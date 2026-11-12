"""Main window: status, record/retry, editable transcript, compressed command preview."""

from __future__ import annotations

import html
import time
from enum import Enum

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.checker import CheckReport, check
from app.history import estimate_tokens, saving_percent
from app.models import Compressed, Mode


class Status(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    ERROR = "ERROR"


_STATUS_COLORS = {
    Status.IDLE: "#888888",
    Status.LISTENING: "#d33",
    Status.TRANSCRIBING: "#c80",
    Status.PROCESSING: "#c80",
    Status.READY: "#2a2",
    Status.ERROR: "#d33",
}

_BUSY = (Status.TRANSCRIBING, Status.PROCESSING)
_RED, _ORANGE, _GREEN = "#d33", "#c70", "#2a2"

_KIND_NAMES = {"number": "number", "path": "file", "identifier": "name", "quote": "quote", "negation": "negative"}


class MainWindow(QMainWindow):
    compress_requested = Signal(str)  # edited transcript text
    mode_changed = Signal(object)  # Mode | None
    project_selected = Signal(str)
    new_project_requested = Signal()
    edit_notes_requested = Signal()
    reset_recent_requested = Signal()
    clear_history_requested = Signal()
    open_data_requested = Signal()
    insert_requested = Signal(str)  # paste into the window you came from (Claude); text as shown/edited
    copy_requested = Signal(str)  # clipboard only (Edit menu, Ctrl+Shift+C)
    theme_requested = Signal(str)  # system | light | dark

    def __init__(self, project: str = "default", api_key_missing_message: str | None = None):
        super().__init__()
        self.setWindowTitle("Voice to Claude")
        self.resize(1000, 600)

        # ---- top bar
        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.project_label = QLabel("Project:")
        self.project_picker = QComboBox()
        self.project_picker.setMinimumWidth(140)
        self.project_picker.setToolTip("Notes and recent commands are kept per project")
        self.set_projects([project], project)
        self.project_picker.currentTextChanged.connect(self._on_project_picked)
        self.context_label = QLabel()
        self.context_label.setObjectName("context_info")
        self.context_label.setStyleSheet("color: #888")
        self.mode_picker = QComboBox()
        self.mode_picker.addItem("Auto", None)
        for mode in Mode:
            self.mode_picker.addItem(mode.value, mode)
        self.mode_picker.currentIndexChanged.connect(lambda _i: self.mode_changed.emit(self.forced_mode()))
        self.record_button = QPushButton("Record")
        self.record_button.setToolTip("Start/stop recording (same as the voice2claude toggle shortcut)")
        self.compress_button = QPushButton("Compress")
        self.compress_button.setToolTip("Compress the text in the Original pane again (e.g. after editing it)")
        self.compress_button.clicked.connect(self._request_compress)

        top = QHBoxLayout()
        top.addWidget(self.status_label)
        top.addWidget(self.record_button)
        top.addWidget(self.compress_button)
        top.addStretch(1)
        top.addWidget(self.project_label)
        top.addWidget(self.project_picker)
        top.addWidget(self.context_label)
        top.addWidget(QLabel("Mode:"))
        top.addWidget(self.mode_picker)

        # ---- panes
        self.original_edit = QPlainTextEdit()
        self.original_edit.setPlaceholderText("Transcript appears here. You can edit it, then press Compress.")
        self.command_edit = QPlainTextEdit()
        self.command_edit.setPlaceholderText("Compressed command appears here. You can edit it.")
        self.command_edit.textChanged.connect(self._schedule_recheck)
        self.command_edit.textChanged.connect(self._update_copy_state)
        self.command_info = QLabel()
        self.command_info.setObjectName("command_info")

        command_box = QWidget()
        cl = QVBoxLayout(command_box)
        cl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Command</b>"))
        head.addStretch(1)
        head.addWidget(self.command_info)
        cl.addLayout(head)
        cl.addWidget(self.command_edit)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(_titled("Original", self.original_edit))
        splitter.addWidget(command_box)

        # ---- check results
        self.check_label = QLabel()
        self.check_label.setObjectName("check")
        self.check_label.setWordWrap(True)
        self.check_label.setTextFormat(Qt.TextFormat.RichText)
        self.ambiguity_label = QLabel()
        self.ambiguity_label.setObjectName("ambiguities")
        self.ambiguity_label.setWordWrap(True)
        self.ambiguity_label.setTextFormat(Qt.TextFormat.RichText)

        self.destructive_frame = QFrame()
        self.destructive_frame.setObjectName("destructive")
        self.destructive_frame.setStyleSheet(
            "#destructive { border: 2px solid #d33; border-radius: 4px; background: rgba(221,51,51,0.10); }"
        )
        self.destructive_label = QLabel()
        self.destructive_label.setWordWrap(True)
        self.destructive_confirm = QCheckBox("I've checked this destructive instruction")
        self.destructive_confirm.toggled.connect(self._update_copy_state)
        dl = QVBoxLayout(self.destructive_frame)
        dl.addWidget(self.destructive_label)
        dl.addWidget(self.destructive_confirm)
        self.destructive_frame.setVisible(False)

        # ---- bottom bar
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.retry_button = QPushButton("Retry")
        self.retry_button.setVisible(False)
        self.insert_button = QPushButton("Insert")
        self.insert_button.setEnabled(False)
        self.insert_button.setDefault(True)
        self.insert_button.setToolTip(
            "Insert the command into the window you were using, e.g. Claude's prompt box (Ctrl+Enter). "
            "It is never sent: review it there and press Enter yourself."
        )
        self.insert_button.clicked.connect(self.request_insert)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.request_insert)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.request_insert)
        # Text boxes swallow Ctrl+Enter as a line break before the shortcut sees it.
        self._copy_keys = _CtrlEnterFilter(self.request_insert, self)
        self.command_edit.installEventFilter(self._copy_keys)
        self.original_edit.installEventFilter(self._copy_keys)
        bottom = QHBoxLayout()
        bottom.addWidget(self.message_label, 1)
        bottom.addWidget(self.retry_button)
        bottom.addWidget(self.insert_button)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(splitter, 1)
        root.addWidget(self.check_label)
        root.addWidget(self.ambiguity_label)
        root.addWidget(self.destructive_frame)
        root.addLayout(bottom)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self._build_menus()
        self._update_copy_state()  # Insert and Copy-only start disabled (nothing to send yet)
        self.stats_label = QLabel()
        self.statusBar().addPermanentWidget(self.stats_label)

        self._compressed: Compressed | None = None
        self._checked_original = ""
        self.report: CheckReport | None = None
        self._destructive_words: list[str] = []
        self._recheck_timer = QTimer(self)
        self._recheck_timer.setSingleShot(True)
        self._recheck_timer.setInterval(300)
        self._recheck_timer.timeout.connect(self.recheck)
        self._setting_command = False

        self._listen_started = 0.0
        self._listen_message = ""
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._tick)

        if api_key_missing_message:
            self.set_status(Status.ERROR, api_key_missing_message)
        else:
            self.set_status(Status.IDLE, "Press Record or your shortcut and speak.")

    # ---- status -----------------------------------------------------------

    def set_status(self, status: Status, message: str = "") -> None:
        self.status = status
        color = _STATUS_COLORS[status]
        self.status_label.setText(f"<b style='color:{color}'>● {status.value}</b>")
        self.message_label.setText(message)

        self.record_button.setText("Stop" if status is Status.LISTENING else "Record")
        self.record_button.setEnabled(status not in _BUSY)
        self.compress_button.setEnabled(status not in _BUSY and status is not Status.LISTENING)

        if status is Status.LISTENING:
            self._listen_started = time.monotonic()
            self._listen_message = message
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()

    def set_retry_available(self, available: bool) -> None:
        self.retry_button.setVisible(available)

    def forced_mode(self) -> Mode | None:
        # Qt hands enum item data back as a plain str ("CODE"), so convert.
        data = self.mode_picker.currentData()
        return Mode(data) if data else None

    # ---- results ----------------------------------------------------------

    def show_transcript(self, transcript) -> None:
        self.original_edit.setPlainText(transcript.text)
        self.clear_command()

    def clear_command(self) -> None:
        self._compressed = None
        self._setting_command = True
        self.command_edit.clear()
        self._setting_command = False
        self.command_info.clear()
        self.check_label.clear()
        self.ambiguity_label.clear()
        self.report = None
        self._set_destructive([], llm_flag=False)

    def show_compressed(self, compressed: Compressed, report: CheckReport) -> None:
        self._compressed = compressed
        self._checked_original = compressed.original
        self._setting_command = True
        self.command_edit.setPlainText(compressed.command)
        self._setting_command = False
        self.command_info.setText(_info_text(compressed))

        if compressed.ambiguities:
            items = "".join(f"<li>{html.escape(a)}</li>" for a in compressed.ambiguities)
            self.ambiguity_label.setText(
                f"<span style='color:{_ORANGE}'><b>Unclear, check before sending:</b></span><ul style='margin:0'>{items}</ul>"
            )
        else:
            self.ambiguity_label.clear()
        self._show_report(report)

    def recheck(self) -> None:
        """Re-run the preservation check after the user edits the command."""
        if self._compressed is None:
            return
        self._show_report(check(self._checked_original, self.command_edit.toPlainText(), self._compressed.mode))

    def copy_allowed(self) -> bool:
        if not self.command_edit.toPlainText().strip():
            return False
        if self._destructive_words or (self._compressed and self._compressed.destructive):
            return self.destructive_confirm.isChecked()
        return True

    # ---- projects, context, stats ---------------------------------------------

    def set_projects(self, names: list[str], current: str) -> None:
        self.project_picker.blockSignals(True)
        self.project_picker.clear()
        self.project_picker.addItems(names)
        self.project_picker.setCurrentText(current)
        self.project_picker.blockSignals(False)

    def current_project(self) -> str:
        return self.project_picker.currentText()

    def set_context_info(self, recent: int, has_notes: bool) -> None:
        notes = "notes" if has_notes else "no notes"
        self.context_label.setText(f"({notes}, {recent} recent)")
        self.context_label.setToolTip(
            "Sent as background with each compression: your global and project notes, "
            f"plus the last {recent} command(s) in this project so follow-ups like \"now add tests\" make sense."
        )

    def set_stats(self, text: str) -> None:
        self.stats_label.setText(text)

    def _on_project_picked(self, name: str) -> None:
        if name:
            self.project_selected.emit(name)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        edit = bar.addMenu("&Edit")
        self.copy_action = edit.addAction("&Copy command only", self.request_copy)
        self.copy_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.copy_action.setToolTip("Put the command on the clipboard without inserting it anywhere")
        project = bar.addMenu("&Project")
        project.addAction("&New project…", self.new_project_requested.emit)
        project.addAction("&Edit notes…", self.edit_notes_requested.emit)
        project.addSeparator()
        project.addAction("&Reset recent commands", self.reset_recent_requested.emit)
        data = bar.addMenu("&History")
        data.addAction("&Clear history…", self.clear_history_requested.emit)
        data.addAction("&Open data folder", self.open_data_requested.emit)
        view = bar.addMenu("&View")
        themes = view.addMenu("&Theme")
        self._theme_group = QActionGroup(self)
        self.theme_actions = {}
        for key, label in (("system", "&System"), ("light", "&Light"), ("dark", "&Dark")):
            act = themes.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked=False, k=key: self.theme_requested.emit(k))
            self._theme_group.addAction(act)
            self.theme_actions[key] = act

    def set_theme_checked(self, theme: str) -> None:
        if theme in self.theme_actions:
            self.theme_actions[theme].setChecked(True)

    def request_insert(self) -> None:
        self._through_gate(self.insert_requested)

    def request_copy(self) -> None:
        self._through_gate(self.copy_requested)

    def _through_gate(self, signal) -> None:
        """Insert/copy only through the gate: non-empty, and destructive commands confirmed."""
        if self.copy_allowed():
            signal.emit(self.command_edit.toPlainText().strip())
        elif self._destructive_words or (self._compressed and self._compressed.destructive):
            self.statusBar().showMessage("Tick the destructive-action box first.", 4000)

    # ---- internals --------------------------------------------------------

    def _show_report(self, report: CheckReport) -> None:
        self.report = report
        if not report.items:
            self.check_label.setText("<span style='color:#888'>No numbers, names, files or negatives to check.</span>")
        elif report.ok:
            self.check_label.setText(
                f"<span style='color:{_GREEN}'>✓ All {len(report.items)} key items kept "
                f"(numbers, names, files, negatives).</span>"
            )
        else:
            missing = ", ".join(
                f"<b>{html.escape(i.label)}</b> <span style='color:#888'>({_KIND_NAMES[i.kind]})</span>"
                for i in report.missing
            )
            self.check_label.setText(
                f"<span style='color:{_RED}'>✗ Missing from the command ({len(report.missing)} of "
                f"{len(report.items)}):</span> <span style='color:{_RED}'>{missing}</span>"
            )
        if report.low_coverage:
            self.check_label.setText(
                self.check_label.text() + f"<br><span style='color:{_RED}'>✗ Only {report.coverage:.0%} of your "
                "wording made it into the command. The model may have answered or replaced your request "
                "instead of rewriting it. Compare both panes carefully.</span>"
            )
        self._set_destructive(report.destructive_words, llm_flag=bool(self._compressed and self._compressed.destructive))

    def _set_destructive(self, words: list[str], llm_flag: bool) -> None:
        if words != self._destructive_words:
            self.destructive_confirm.setChecked(False)  # new risky wording needs a fresh look
        self._destructive_words = words
        show = bool(words) or llm_flag
        if show:
            detail = ", ".join(f"“{html.escape(w)}”" for w in words) if words else "flagged by the model"
            self.destructive_label.setText(
                f"<b style='color:{_RED}'>⚠ Destructive action</b>: {detail}. "
                "Read the command carefully before using it."
            )
        self.destructive_frame.setVisible(show)
        self._update_copy_state()

    def _update_copy_state(self, *_args) -> None:
        allowed = self.copy_allowed()
        self.insert_button.setEnabled(allowed)
        self.copy_action.setEnabled(allowed)

    def _schedule_recheck(self) -> None:
        if not self._setting_command:
            self._recheck_timer.start()

    def _request_compress(self) -> None:
        text = self.original_edit.toPlainText().strip()
        if text:
            self.compress_requested.emit(text)

    def bring_forward(self) -> None:
        # Wayland only lets apps request focus; the compositor may just flash the dock icon.
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _tick(self) -> None:
        secs = int(time.monotonic() - self._listen_started)
        self.message_label.setText(f"{self._listen_message}  ({secs} s)")


def _info_text(c: Compressed) -> str:
    mode = c.mode.value if c.mode else "—"
    if c.fallback_error:
        return f"<span style='color:{_RED}'>compression failed, showing transcript</span>"
    before, after = estimate_tokens(c.original), estimate_tokens(c.command)
    tokens = f"~{before} → ~{after} tokens ({-saving_percent(before, after):+.0f}%)"
    if c.skipped:
        reason = {"short": "short, not compressed", "dictation": "dictation, fillers removed", "empty": "empty"}
        return f"{mode} · {reason.get(c.skip_reason, c.skip_reason)} · {tokens}"
    return f"{mode} · {tokens} · {c.latency_s:.1f} s"


class _CtrlEnterFilter(QObject):
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._callback()
            return True
        return False


class NotesDialog(QDialog):
    """Edit global.md and the current project's project.md."""

    def __init__(self, parent: QWidget | None, global_text: str, project: str, project_text: str):
        super().__init__(parent)
        self.setWindowTitle("Notes")
        self.resize(640, 460)
        self.global_edit = QPlainTextEdit(global_text)
        self.project_edit = QPlainTextEdit(project_text)
        tabs = QTabWidget()
        tabs.addTab(self.project_edit, f"Project: {project}")
        tabs.addTab(self.global_edit, "Global")
        hint = QLabel(
            "Short background sent with each compression (about 2,000 characters max). "
            "It never overrides what you say. Text inside <!-- --> is ignored."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(hint)
        layout.addWidget(buttons)


def _titled(title: str, widget: QWidget) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QLabel(f"<b>{title}</b>"))
    layout.addWidget(widget)
    return box
