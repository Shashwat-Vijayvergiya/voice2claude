"""Glue between the window, pipeline, context store and history.

Keeps track of the current project, records each finished command, and
handles the Project/History menu actions.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from app import clipboard, config
from app.context import DEFAULT_PROJECT, ContextError, ContextStore
from app.history import History, make_entry
from app.models import CommandResult

log = logging.getLogger(__name__)


class Session(QObject):
    def __init__(self, window, pipeline, context: ContextStore, history: History, project: str):
        super().__init__(window)
        self.window = window
        self.pipeline = pipeline
        self.context = context
        self.history = history
        self.project = project if context.exists(project) else DEFAULT_PROJECT

        pipeline.context_provider = self.build_context
        pipeline.command_finished.connect(self.record)
        window.project_selected.connect(self.switch_project)
        window.new_project_requested.connect(self.new_project)
        window.edit_notes_requested.connect(self.edit_notes)
        window.reset_recent_requested.connect(self.reset_recent)
        window.clear_history_requested.connect(self.clear_history)
        window.open_data_requested.connect(self.open_data_folder)
        window.copy_requested.connect(self.copy)
        window.theme_requested.connect(self.set_theme)
        self._last: CommandResult | None = None
        self.copy_fn = clipboard.copy  # replaced in tests
        self.apply_theme_fn = None  # set by main: (theme) -> None

        # Dialog hooks (replaced in tests).
        self.ask_text = lambda title, label: QInputDialog.getText(window, title, label)
        self.confirm = lambda title, text: (
            QMessageBox.question(window, title, text) == QMessageBox.StandardButton.Yes
        )

        self.refresh()

    # ---- context ------------------------------------------------------------

    def build_context(self, exclude_item: str | None = None):
        return self.context.build(self.project, exclude_item=exclude_item)

    def record(self, result: CommandResult) -> None:
        self._last = result
        try:
            self.history.append(make_entry(result, self.project))
        except OSError as exc:
            log.warning("Could not write history: %s", exc)
        c = result.compressed
        if not c.fallback_error and c.command.strip():
            try:
                self.context.add_command(self.project, result.item_id, c.original, c.command)
            except OSError as exc:
                log.warning("Could not save recent command: %s", exc)
        self.refresh()

    # ---- copy -----------------------------------------------------------------

    def copy(self, text: str) -> None:
        """Clipboard only: the user pastes into Claude and presses Enter themselves."""
        try:
            method = self.copy_fn(text)
        except clipboard.ClipboardError as exc:
            log.warning("Copy failed: %s", exc)
            QMessageBox.warning(self.window, "Copy failed", f"{exc}\n\nYou can still select the text and copy it by hand.")
            return
        note = " (kept only while this app is open)" if method == "qt" else ""
        self.window.statusBar().showMessage(f"Copied{note}. Paste it into Claude and press Enter.", 6000)

        last = self._last
        if last is None:
            return
        edited = text.strip() != last.compressed.command.strip()
        if edited:
            # Follow-ups should build on what you actually sent, not the first draft.
            try:
                self.context.add_command(self.project, last.item_id, last.compressed.original, text)
            except OSError as exc:
                log.warning("Could not update recent command: %s", exc)
        try:
            self.history.append({
                "event": "copied", "project": self.project, "item_id": last.item_id,
                "edited": edited, "command": text if edited else None, "method": method,
            })
        except OSError as exc:
            log.warning("Could not write history: %s", exc)
        self.refresh()

    # ---- theme ----------------------------------------------------------------

    def set_theme(self, theme: str) -> None:
        if self.apply_theme_fn:
            self.apply_theme_fn(theme)
        try:
            config.update_user_settings(theme=theme)
        except (config.ConfigError, OSError) as exc:
            log.warning("Could not save theme: %s", exc)

    # ---- projects -----------------------------------------------------------

    def switch_project(self, name: str) -> None:
        if not name or name == self.project:
            return
        self.project = self.context.create_project(name)
        try:
            config.update_user_settings(current_project=self.project)
        except (config.ConfigError, OSError) as exc:
            log.warning("Could not save current project: %s", exc)
        log.info("Project: %s", self.project)
        self.refresh()

    def new_project(self) -> None:
        name, ok = self.ask_text("New project", "Project name:")
        if not ok or not name.strip():
            return
        try:
            slug = self.context.create_project(name)
        except ContextError as exc:
            QMessageBox.warning(self.window, "New project", str(exc))
            return
        self.switch_project(slug)
        self.refresh()

    def edit_notes(self) -> None:
        from app.ui import NotesDialog

        dialog = NotesDialog(
            self.window, self.context.read_global(), self.project, self.context.read_project(self.project)
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.context.write_global(dialog.global_edit.toPlainText())
            self.context.write_project(self.project, dialog.project_edit.toPlainText())
            self.refresh()

    def reset_recent(self) -> None:
        self.context.reset_recent(self.project)
        self.refresh()
        self.window.statusBar().showMessage(f"Recent commands for “{self.project}” cleared.", 4000)

    # ---- history ------------------------------------------------------------

    def clear_history(self) -> None:
        if not self.confirm(
            "Clear history",
            "Permanently delete the saved history of all commands (transcripts, commands, stats)? "
            "This can't be undone.",
        ):
            return
        self.history.clear()
        self.refresh()
        self.window.statusBar().showMessage("History cleared.", 4000)

    def open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.context.root.parent)))

    # ---- display ------------------------------------------------------------

    def refresh(self) -> None:
        self.window.set_projects(self.context.list_projects(), self.project)
        ctx = self.context.build(self.project)
        self.window.set_context_info(len(ctx.recent_commands), bool(ctx.project_notes))
        if not self.history.enabled:
            self.window.set_stats("History off")
            return
        s = self.history.stats(self.project)
        if s.compressed:
            self.window.set_stats(
                f"{self.project}: {s.commands} command(s), "
                f"~{s.tokens_before} → ~{s.tokens_after} tokens ({-s.saving_percent:+.0f}%)"
            )
        else:
            self.window.set_stats(f"{self.project}: {s.commands} command(s)")
