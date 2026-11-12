from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from app import theme


def test_dark_and_light(qtbot):
    app = QApplication.instance()
    assert theme.apply_theme(app, "dark") is True
    assert app.palette().color(QPalette.ColorRole.Window).lightness() < 80
    assert theme.apply_theme(app, "light") is False
    assert app.palette().color(QPalette.ColorRole.Window).lightness() > 180
    assert app.style().name().lower() == "fusion"


def test_system_follows_desktop(qtbot, monkeypatch):
    app = QApplication.instance()
    monkeypatch.setattr(theme, "system_prefers_dark", lambda a: True)
    assert theme.apply_theme(app, "system") is True
    monkeypatch.setattr(theme, "system_prefers_dark", lambda a: False)
    assert theme.apply_theme(app, "bogus") is False  # unknown value falls back to system
