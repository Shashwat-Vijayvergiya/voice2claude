"""Start Voice to Claude."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from app import config, control

# Qt's Wayland text-input plugin logs harmless focus warnings on every click.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland.textinput=false;qt.qpa.services=false")


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice to Claude")
    parser.add_argument("--record", action="store_true", help="start recording right away")
    args = parser.parse_args()

    # Single instance: if the app is already running, pass the request on and exit.
    if control.is_running():
        control.send_command("toggle" if args.record else "show")
        return 0
    # Second guard that doesn't depend on the socket working.
    instance_lock = control.acquire_instance_lock()
    if instance_lock is None:
        sys.stderr.write("Voice to Claude is already running (its control socket isn't reachable).\n")
        return 1

    log_file = config.setup_logging()
    log = logging.getLogger("voice2claude")
    log.info("Starting; logs at %s", log_file)

    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    # Ctrl+C / SIGTERM: quit through Qt so the mic is released and the socket removed.
    # Python only runs signal handlers when it gets control, so wake it twice a second.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    wakeup = QTimer()
    wakeup.start(500)
    wakeup.timeout.connect(lambda: None)
    app.setApplicationName("Voice to Claude")
    app.setDesktopFileName("voice-to-claude")

    try:
        settings = config.load_settings()
    except config.ConfigError as exc:
        log.error("%s", exc)
        QMessageBox.critical(None, "Voice to Claude: settings error", str(exc))
        return 1

    from app.compressor import Compressor
    from app.context import ContextStore
    from app.history import History, init_tokenizer
    from app.pipeline import Pipeline
    from app.recorder import Recorder
    from app.transcriber import Transcriber, make_groq_client
    from app.session import Session
    from app.ui import MainWindow

    api_key = config.get_api_key()
    missing = None
    transcriber = None
    compressor = None
    if api_key:
        compressor = Compressor(
            make_groq_client(api_key, config.GROQ_BASE_URL, timeout=settings.compression_timeout_seconds),
            model=settings.llm_model,
            strength=settings.compression_strength,
            reasoning_effort=settings.llm_reasoning_effort,
            skip_under_words=settings.skip_under_words,
        )
        transcriber = Transcriber(
            make_groq_client(api_key, config.GROQ_BASE_URL),
            model=settings.whisper_model,
            language=settings.language,
            vocabulary=config.load_vocabulary(),
        )
    else:
        log.warning("Groq API key not found")
        missing = config.MISSING_KEY_MESSAGE

    recorder = Recorder(
        device=settings.audio_device,
        silence_seconds=settings.silence_seconds,
        no_speech_timeout=settings.no_speech_timeout_seconds,
        max_seconds=settings.max_record_seconds,
    )
    pipeline = Pipeline(recorder, transcriber, compressor, missing_key_message=missing)
    window = MainWindow(project=settings.current_project, api_key_missing_message=missing)

    window.record_button.clicked.connect(pipeline.toggle)
    window.retry_button.clicked.connect(pipeline.retry)
    pipeline.status_changed.connect(window.set_status)
    pipeline.retry_available.connect(window.set_retry_available)
    pipeline.transcript_ready.connect(window.show_transcript)
    pipeline.compressed_ready.connect(window.show_compressed)
    pipeline.compressed_ready.connect(lambda *_: window.bring_forward())
    window.compress_requested.connect(pipeline.compress_text)
    window.mode_changed.connect(pipeline.set_forced_mode)

    data = config.data_dir()
    init_tokenizer(data / "tiktoken-cache")  # background; rough estimate until loaded
    context = ContextStore(data / "context")
    context.ensure()
    history = History(data / "history.jsonl", enabled=settings.history_enabled)
    session = Session(window, pipeline, context, history, settings.current_project)

    from app.theme import apply_theme, follow_system

    current_theme = {"value": settings.theme}

    def set_theme(theme: str) -> None:
        current_theme["value"] = theme
        apply_theme(app, theme)

    apply_theme(app, settings.theme)
    follow_system(app, lambda: current_theme["value"])
    session.apply_theme_fn = set_theme
    window.set_theme_checked(settings.theme)

    class CommandBridge(QObject):
        received = Signal(str)  # emitted from the socket thread, delivered on the Qt thread

    bridge = CommandBridge()

    def on_command(cmd: str) -> None:
        if cmd == "toggle":
            pipeline.toggle()
        elif cmd == "show":
            window.bring_forward()

    bridge.received.connect(on_command)
    server = control.ControlServer(bridge.received.emit)
    try:
        server.start()
    except (RuntimeError, OSError) as exc:
        log.warning("Control socket unavailable, shortcut won't work: %s", exc)

    app.aboutToQuit.connect(pipeline.shutdown)
    app.aboutToQuit.connect(server.stop)

    from app.ui import Status

    def on_unhandled(exc_type, exc, tb):
        # Log it and show it instead of only printing to a terminal nobody sees.
        logging.getLogger("voice2claude").error("Unhandled error", exc_info=(exc_type, exc, tb))
        try:
            window.set_status(Status.ERROR, f"Unexpected error: {exc}. Details in {log_file}")
        except Exception:
            pass

    sys.excepthook = on_unhandled
    threading.excepthook = lambda a: on_unhandled(a.exc_type, a.exc_value, a.exc_traceback)

    window.show()
    if args.record:
        QTimer.singleShot(0, pipeline.toggle)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
