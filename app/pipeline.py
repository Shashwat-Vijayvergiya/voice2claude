"""Connects the steps: record → transcribe → compress → check.

Lives on the Qt thread. Audio callbacks and network calls happen on other
threads and come back through Qt signals, which Qt queues onto this thread.
"""

from __future__ import annotations

import logging
import threading
import uuid

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from app.checker import check
from app.models import CommandResult, Compressed, CompressionContext, Mode, Recording, Transcript
from app.recorder import RecorderError
from app.transcriber import TranscriptionError
from app.ui import Status

log = logging.getLogger(__name__)

_STOP_MESSAGES = {
    "no_speech": "No speech detected. Press the shortcut and try again.",
    "max_length": "Reached the maximum recording length.",
}


class Pipeline(QObject):
    status_changed = Signal(object, str)  # Status, message
    transcript_ready = Signal(object)  # Transcript
    compressed_ready = Signal(object, object)  # Compressed, CheckReport
    command_finished = Signal(object)  # CommandResult, for history and recent context
    retry_available = Signal(bool)

    _auto_stop = Signal(str)
    _transcribed = Signal(object)
    _failed = Signal(object)
    _compressed = Signal(object, object)

    def __init__(
        self,
        recorder,
        transcriber,
        compressor=None,
        missing_key_message: str | None = None,
        context_provider=None,
    ):
        super().__init__()
        self.recorder = recorder
        self.transcriber = transcriber
        self.compressor = compressor
        # (exclude_item: str | None) -> CompressionContext
        self.context_provider = context_provider
        self._item_id: str | None = None  # one id per recording/typed text; re-compressing keeps it
        self._transcript: Transcript | None = None
        self._recompressing = False
        self.forced_mode: Mode | None = None
        self.missing_key_message = missing_key_message
        self.status = Status.IDLE
        self._pending: Recording | None = None
        self._retries_left = 0
        self._auto_stop.connect(self._on_auto_stop)
        self._transcribed.connect(self._on_transcribed)
        self._failed.connect(self._on_failed)
        self._compressed.connect(self._on_compressed)
        # Recorder calls this from the audio thread.
        recorder.on_auto_stop = self._auto_stop.emit
        # Silence/length timers only advance when audio arrives; if the mic dies
        # (headset unplugged), this notices and stops instead of listening forever.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_mic)

    # ---- public API -------------------------------------------------------

    @Slot()
    def toggle(self) -> None:
        if self.status is Status.LISTENING:
            self.stop()
        elif self.status in (Status.TRANSCRIBING, Status.PROCESSING):
            log.info("Busy; ignoring toggle")
        else:
            self.start()

    def start(self) -> None:
        if self.transcriber is None:
            self._set(Status.ERROR, self.missing_key_message or "Speech-to-text is not configured.")
            return
        self._drop_pending()
        try:
            self.recorder.start()
        except RecorderError as exc:
            self._set(Status.ERROR, str(exc))
            return
        self._set(Status.LISTENING, "Listening… press the shortcut again or pause to stop.")
        self._watchdog.start()

    def stop(self) -> None:
        if self.status is not Status.LISTENING:
            return
        self._watchdog.stop()
        try:
            recording = self.recorder.stop()
        except RecorderError as exc:
            self._set(Status.ERROR, str(exc))
            return
        if not recording.speech_detected or not recording.audio:
            self._set(Status.IDLE, _STOP_MESSAGES["no_speech"])
            return
        self._pending = recording
        self._retries_left = 1
        self._transcribe(recording)

    @Slot()
    def retry(self) -> None:
        if self._pending is None or self._retries_left <= 0 or self.status is not Status.ERROR:
            return
        self._retries_left -= 1
        self.retry_available.emit(False)
        self._transcribe(self._pending)

    @Slot(str)
    def compress_text(self, text: str) -> None:
        """Compress (or re-compress) text, e.g. an edited transcript."""
        if self.status in (Status.LISTENING, Status.TRANSCRIBING, Status.PROCESSING):
            return
        if self.compressor is None:
            self._set(Status.ERROR, self.missing_key_message or "Compression is not configured.")
            return
        recompressing = self._item_id is not None
        if not recompressing:
            self._item_id = uuid.uuid4().hex[:12]  # text typed in without a recording
        # An explicit request: compress even if it's short.
        self._compress(text, recompressing=recompressing, force=True)

    @Slot(object)
    def set_forced_mode(self, mode: Mode | None) -> None:
        self.forced_mode = mode

    def shutdown(self) -> None:
        if self.recorder.is_recording:
            try:
                self.recorder.stop()
            except RecorderError:
                pass
        self._drop_pending()

    # ---- internals --------------------------------------------------------

    def _transcribe(self, recording: Recording) -> None:
        self._set(Status.TRANSCRIBING, f"Transcribing {recording.duration_s:.1f} s of audio…")

        def work():
            try:
                self._transcribed.emit(self.transcriber.transcribe(recording))
            except TranscriptionError as exc:
                self._failed.emit(exc)
            except Exception as exc:  # never let a worker thread die silently
                log.exception("Unexpected transcription failure")
                self._failed.emit(TranscriptionError(f"Unexpected error: {exc}"))

        threading.Thread(target=work, name="transcribe", daemon=True).start()

    MIC_SILENT_SECONDS = 3.0

    def _check_mic(self) -> None:
        if self.status is not Status.LISTENING:
            self._watchdog.stop()
            return
        since = getattr(self.recorder, "seconds_since_audio", lambda: 0.0)()
        if since < self.MIC_SILENT_SECONDS:
            return
        log.warning("No audio from the mic for %.1fs; stopping", since)
        self._watchdog.stop()
        try:
            self.recorder.stop()
        except RecorderError:
            pass
        self._set(Status.ERROR, "The microphone stopped sending audio (unplugged?). Check it and press Record again.")

    @Slot(str)
    def _on_auto_stop(self, reason: str) -> None:
        if self.status is not Status.LISTENING:
            return
        log.info("Auto-stop: %s", reason)
        self.stop()
        if reason == "max_length" and self.status is Status.TRANSCRIBING:
            log.info(_STOP_MESSAGES["max_length"])

    @Slot(object)
    def _on_transcribed(self, transcript: Transcript) -> None:
        self._drop_pending()  # audio is no longer needed
        # Whisper answers near-silence with things like "." or "...".
        if not any(ch.isalnum() for ch in transcript.text):
            self._set(Status.IDLE, "Nothing was recognised. Try speaking closer to the mic.")
            return
        self._item_id = uuid.uuid4().hex[:12]
        self._transcript = transcript
        self.transcript_ready.emit(transcript)
        if self.compressor is None:
            self._set(
                Status.READY,
                f"Transcribed {transcript.audio_seconds:.1f} s of audio in {transcript.latency_s:.1f} s. "
                "Edit the text if needed.",
            )
            return
        self._compress(transcript.text, recompressing=False, force=False)

    def _compress(self, text: str, recompressing: bool, force: bool) -> None:
        self._set(Status.PROCESSING, "Compressing…")
        self._recompressing = recompressing
        mode = self.forced_mode
        try:
            context = (
                self.context_provider(exclude_item=self._item_id) if self.context_provider else CompressionContext()
            )
        except Exception:  # bad notes file etc. must not block the command
            log.exception("Could not load context; compressing without it")
            context = CompressionContext()

        def work():
            try:
                result = self.compressor.compress(text, forced_mode=mode, context=context, force=force)
            except Exception as exc:  # compress() shouldn't raise; be safe anyway
                log.exception("Unexpected compression failure")
                result = Compressed(original=text, command=text, fallback_error=f"Unexpected error: {exc}")
            self._compressed.emit(result, check(result.original, result.command, result.mode))

        threading.Thread(target=work, name="compress", daemon=True).start()

    @Slot(object, object)
    def _on_compressed(self, result: Compressed, report) -> None:
        self.compressed_ready.emit(result, report)
        self.command_finished.emit(
            CommandResult(self._item_id or "", result, report, self._transcript, self._recompressing)
        )
        if result.fallback_error:
            self._set(Status.ERROR, f"Compression failed ({result.fallback_error}). Showing the transcript instead; press Compress to try again.")
        elif result.skipped:
            why = {"short": "Short command, so it wasn't compressed.",
                   "dictation": "Dictation: fillers removed, nothing else changed.",
                   "empty": "Nothing to compress."}
            self._set(Status.READY, why.get(result.skip_reason, "Not compressed."))
        else:
            parts = [f"Compressed in {result.latency_s:.1f} s."]
            if report.low_coverage:
                parts.append("Most of your wording is missing: compare carefully.")
            if report.missing:
                parts.append(f"{len(report.missing)} item(s) missing: review in red below.")
            if result.ambiguities:
                parts.append("Some points are unclear.")
            self._set(Status.READY, " ".join(parts))

    @Slot(object)
    def _on_failed(self, exc: TranscriptionError) -> None:
        can_retry = exc.retryable and self._pending is not None and self._retries_left > 0
        if not can_retry:
            self._drop_pending()
        self._set(Status.ERROR, str(exc))
        self.retry_available.emit(can_retry)

    def _drop_pending(self) -> None:
        self._pending = None
        self._retries_left = 0
        self.retry_available.emit(False)

    def _set(self, status: Status, message: str) -> None:
        log.log(logging.WARNING if status is Status.ERROR else logging.INFO, "%s: %s", status.value, message)
        self.status = status
        self.status_changed.emit(status, message)
