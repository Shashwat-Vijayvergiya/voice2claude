"""Light/dark theme. Fusion style with explicit palettes, so it looks the same
on GNOME, KDE and X11. "system" follows the desktop's dark-mode preference."""

from __future__ import annotations

import logging
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

log = logging.getLogger(__name__)

THEMES = ("system", "light", "dark")


def system_prefers_dark(app: QApplication) -> bool:
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except AttributeError:
        pass
    try:  # GNOME: Qt can report Unknown, so ask the desktop directly
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1,
        ).stdout
        return "dark" in out
    except (OSError, subprocess.TimeoutExpired):
        return False


def dark_palette() -> QPalette:
    p = QPalette()
    base, window, text, mid = QColor("#1e1f22"), QColor("#2b2d31"), QColor("#e6e6e6"), QColor("#3a3c42")
    for role, color in {
        QPalette.ColorRole.Window: window,
        QPalette.ColorRole.WindowText: text,
        QPalette.ColorRole.Base: base,
        QPalette.ColorRole.AlternateBase: window,
        QPalette.ColorRole.ToolTipBase: window,
        QPalette.ColorRole.ToolTipText: text,
        QPalette.ColorRole.Text: text,
        QPalette.ColorRole.Button: mid,
        QPalette.ColorRole.ButtonText: text,
        QPalette.ColorRole.BrightText: QColor("#ff6b6b"),
        QPalette.ColorRole.Highlight: QColor("#4c7bd9"),
        QPalette.ColorRole.HighlightedText: QColor("#ffffff"),
        QPalette.ColorRole.Link: QColor("#7aa7ff"),
        QPalette.ColorRole.PlaceholderText: QColor("#8a8d93"),
    }.items():
        p.setColor(role, color)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6f7278"))
    return p


def light_palette() -> QPalette:
    """Explicit, because Fusion's "standard" palette follows the desktop's dark mode."""
    p = QPalette()
    base, window, text, mid = QColor("#ffffff"), QColor("#f3f3f3"), QColor("#1e1e1e"), QColor("#e4e4e4")
    for role, color in {
        QPalette.ColorRole.Window: window,
        QPalette.ColorRole.WindowText: text,
        QPalette.ColorRole.Base: base,
        QPalette.ColorRole.AlternateBase: window,
        QPalette.ColorRole.ToolTipBase: QColor("#ffffe1"),
        QPalette.ColorRole.ToolTipText: text,
        QPalette.ColorRole.Text: text,
        QPalette.ColorRole.Button: mid,
        QPalette.ColorRole.ButtonText: text,
        QPalette.ColorRole.BrightText: QColor("#c62828"),
        QPalette.ColorRole.Highlight: QColor("#3b6fd6"),
        QPalette.ColorRole.HighlightedText: QColor("#ffffff"),
        QPalette.ColorRole.Link: QColor("#1f5fbf"),
        QPalette.ColorRole.PlaceholderText: QColor("#8a8a8a"),
    }.items():
        p.setColor(role, color)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#a0a0a0"))
    return p


def _set_scheme(app: QApplication, theme: str) -> None:
    """Tell Qt too, so dialogs and the title bar match (Qt 6.8+)."""
    hints = app.styleHints()
    try:
        if theme == "system":
            hints.unsetColorScheme()
        else:
            hints.setColorScheme(Qt.ColorScheme.Dark if theme == "dark" else Qt.ColorScheme.Light)
    except AttributeError:
        pass


def apply_theme(app: QApplication, theme: str) -> bool:
    """Apply a theme; returns True if the result is dark."""
    if theme not in THEMES:
        theme = "system"
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    _set_scheme(app, theme)
    dark = theme == "dark" or (theme == "system" and system_prefers_dark(app))
    app.setPalette(dark_palette() if dark else light_palette())
    log.info("Theme: %s (%s)", theme, "dark" if dark else "light")
    return dark


def follow_system(app: QApplication, get_theme) -> None:
    """Re-apply when the desktop switches dark mode, if the user chose "system"."""
    try:
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: apply_theme(app, "system") if get_theme() == "system" else None
        )
    except AttributeError:
        pass
