import threading

import pytest

from app.models import Recording, Transcript
from app.pipeline import Pipeline
from app.recorder import RecorderError
from app.transcriber import TranscriptionError
from app.ui import Status


class FakeRecorder:
    def __init__(self, recording=None, start_error=None):
        self.recording = recording or Recording(
            audio=b"fLaC", sample_rate=16000, duration_s=2.0, speech_detected=True
        )
        self.start_error = start_error
        self.is_recording = False
        self.on_auto_stop = None
        self.starts = 0

    def start(self):
        if self.start_error:
            raise self.start_error
        self.starts += 1
        self.is_recording = True

    def stop(self):
        self.is_recording = False
        return self.recording


class FakeTranscriber:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.release = threading.Event()
        self.release.set()

    def transcribe(self, recording):
        self.release.wait(5)
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Transcript(text=item, audio_seconds=recording.duration_s, latency_s=0.3, model="m")


def make(qtbot, recorder=None, transcriber=None, **kw):
    recorder = recorder or FakeRecorder()
    p = Pipeline(recorder, transcriber, **kw)
    p.statuses = []
    p.transcripts = []
    p.retry_flags = []
    p.status_changed.connect(lambda s, m: p.statuses.append((s, m)))
    p.transcript_ready.connect(p.transcripts.append)
    p.retry_available.connect(p.retry_flags.append)
    return p, recorder


def wait_status(qtbot, p, status):
    qtbot.waitUntil(lambda: p.status is status, timeout=3000)


def test_toggle_record_then_transcribe(qtbot):
    p, rec = make(qtbot, transcriber=FakeTranscriber("add tests for utils.py"))
    p.toggle()
    assert p.status is Status.LISTENING and rec.is_recording
    p.toggle()
    wait_status(qtbot, p, Status.READY)
    assert [s for s, _ in p.statuses] == [Status.LISTENING, Status.TRANSCRIBING, Status.READY]
    assert p.transcripts[0].text == "add tests for utils.py"
    assert p._pending is None  # audio dropped after success


def test_auto_stop_from_audio_thread(qtbot):
    p, rec = make(qtbot, transcriber=FakeTranscriber("hello there"))
    p.toggle()
    t = threading.Thread(target=rec.on_auto_stop, args=("silence",))
    t.start()
    t.join()
    wait_status(qtbot, p, Status.READY)
    assert p.transcripts[0].text == "hello there"


def test_toggle_ignored_while_transcribing(qtbot):
    tr = FakeTranscriber("x")
    tr.release.clear()
    p, rec = make(qtbot, transcriber=tr)
    p.toggle()
    p.toggle()
    assert p.status is Status.TRANSCRIBING
    p.toggle()  # ignored
    assert rec.starts == 1
    tr.release.set()
    wait_status(qtbot, p, Status.READY)


def test_no_speech_skips_api(qtbot):
    silent = Recording(audio=b"fLaC", sample_rate=16000, duration_s=8, speech_detected=False)
    tr = FakeTranscriber()
    p, _ = make(qtbot, recorder=FakeRecorder(recording=silent), transcriber=tr)
    p.toggle()
    p.toggle()
    assert p.status is Status.IDLE
    assert "No speech" in p.statuses[-1][1]
    assert tr.calls == 0


def test_missing_key_blocks_recording(qtbot):
    p, rec = make(qtbot, transcriber=None, missing_key_message="No key")
    p.toggle()
    assert p.status is Status.ERROR and p.statuses[-1][1] == "No key"
    assert rec.starts == 0


def test_mic_error_shown(qtbot):
    p, _ = make(qtbot, recorder=FakeRecorder(start_error=RecorderError("No microphone found.")),
                transcriber=FakeTranscriber())
    p.toggle()
    assert p.status is Status.ERROR and "microphone" in p.statuses[-1][1]


def test_failure_keeps_audio_and_allows_one_retry(qtbot):
    tr = FakeTranscriber(TranscriptionError("Groq took too long"), "recovered")
    p, _ = make(qtbot, transcriber=tr)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.ERROR)
    assert p.retry_flags[-1] is True
    assert p._pending is not None
    p.retry()
    wait_status(qtbot, p, Status.READY)
    assert p.transcripts[-1].text == "recovered"
    assert tr.calls == 2


def test_only_one_retry(qtbot):
    tr = FakeTranscriber(TranscriptionError("a"), TranscriptionError("b"))
    p, _ = make(qtbot, transcriber=tr)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.ERROR)
    p.retry()
    qtbot.waitUntil(lambda: tr.calls == 2 and p.status is Status.ERROR, timeout=3000)
    assert p.retry_flags[-1] is False
    assert p._pending is None
    p.retry()  # no effect
    assert tr.calls == 2


def test_non_retryable_error_drops_audio(qtbot):
    tr = FakeTranscriber(TranscriptionError("bad key", retryable=False))
    p, _ = make(qtbot, transcriber=tr)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.ERROR)
    assert p.retry_flags[-1] is False and p._pending is None


def test_new_recording_discards_old_audio(qtbot):
    tr = FakeTranscriber(TranscriptionError("timeout"), "second")
    p, _ = make(qtbot, transcriber=tr)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.ERROR)
    p.toggle()  # start fresh instead of retrying
    assert p._pending is None and p.status is Status.LISTENING


@pytest.mark.parametrize("text", ["", ".", " ... "])
def test_empty_transcript(qtbot, text):
    # "." is what Groq Whisper really returned for 8 s of room noise.
    p, _ = make(qtbot, transcriber=FakeTranscriber(text))
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.IDLE)
    assert p.transcripts == []


# ---- Phase 3: compression step -------------------------------------------

from app.models import Compressed, CompressionContext, Mode  # noqa: E402


class FakeCompressor:
    def __init__(self, command="TASK: Add tests for utils.py", **extra):
        self.command = command
        self.extra = extra
        self.calls = []

    def compress(self, text, forced_mode=None, context=None, force=False):
        self.calls.append((text, forced_mode, context))
        self.forced = force
        return Compressed(original=text, command=self.command, mode=forced_mode or Mode.COMMAND, **self.extra)


def make3(qtbot, transcript_text="add tests for utils.py and don't touch main.py", compressor=None, **kw):
    p, rec = make(qtbot, transcriber=FakeTranscriber(transcript_text), **kw)
    p.compressor = compressor or FakeCompressor()
    p.results = []
    p.compressed_ready.connect(lambda c, r: p.results.append((c, r)))
    return p, rec


def test_transcript_goes_through_compression(qtbot):
    p, _ = make3(qtbot)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.READY)
    assert [s for s, _ in p.statuses] == [Status.LISTENING, Status.TRANSCRIBING, Status.PROCESSING, Status.READY]
    compressed, report = p.results[0]
    assert compressed.command == "TASK: Add tests for utils.py"
    # The check runs against the transcript: main.py and the negation were dropped.
    assert {i.label for i in report.missing} >= {"main.py"}
    assert "missing" in p.statuses[-1][1]


def test_forced_mode_and_context_passed(qtbot):
    comp = FakeCompressor()
    p, _ = make3(qtbot, compressor=comp)
    p.context_provider = lambda exclude_item=None: CompressionContext(project_notes="notes")
    p.set_forced_mode(Mode.QUESTION)
    p.compress_text("why is it slow")
    qtbot.waitUntil(lambda: p.status is Status.READY, timeout=3000)
    text, mode, ctx = comp.calls[0]
    assert mode is Mode.QUESTION and ctx.project_notes == "notes"


def test_compression_fallback_is_error_with_transcript(qtbot):
    comp = FakeCompressor(command="raw transcript", fallback_error="Groq took too long to respond.")
    p, _ = make3(qtbot, compressor=comp)
    p.compress_text("raw transcript")
    wait_status(qtbot, p, Status.ERROR)
    assert "press Compress" in p.statuses[-1][1]
    assert p.results[0][0].command == "raw transcript"


def test_skipped_message(qtbot):
    comp = FakeCompressor(command="Fix typo", skipped=True, skip_reason="short")
    p, _ = make3(qtbot, compressor=comp)
    p.compress_text("fix typo")
    wait_status(qtbot, p, Status.READY)
    assert "Short command" in p.statuses[-1][1]


def test_compress_ignored_while_busy(qtbot):
    comp = FakeCompressor()
    p, _ = make3(qtbot, compressor=comp)
    p.toggle()  # LISTENING
    p.compress_text("anything")
    assert comp.calls == []


def test_compressor_exception_never_kills_pipeline(qtbot):
    class Boom:
        def compress(self, *a, **k):
            raise RuntimeError("bug")

    p, _ = make3(qtbot, compressor=Boom())
    p.compress_text("some text")
    wait_status(qtbot, p, Status.ERROR)
    assert p.results[0][0].command == "some text"


def test_compress_button_forces_and_recording_does_not(qtbot):
    comp = FakeCompressor()
    p, _ = make3(qtbot, compressor=comp)
    p.toggle()
    p.toggle()
    wait_status(qtbot, p, Status.READY)
    assert comp.forced is False  # automatic flow keeps the short-command skip
    p.compress_text("fix the typo")
    wait_status(qtbot, p, Status.READY)
    assert comp.forced is True  # the button overrides it


def test_dead_mic_watchdog(qtbot):
    rec = FakeRecorder()
    rec.seconds_since_audio = lambda: 5.0  # mic went quiet at the device level
    p, _ = make(qtbot, recorder=rec, transcriber=FakeTranscriber("x"))
    p.toggle()
    assert p.status is Status.LISTENING
    p._check_mic()
    assert p.status is Status.ERROR and "stopped sending audio" in p.statuses[-1][1]
    assert not rec.is_recording


def test_watchdog_quiet_while_audio_flows(qtbot):
    rec = FakeRecorder()
    rec.seconds_since_audio = lambda: 0.1
    p, _ = make(qtbot, recorder=rec, transcriber=FakeTranscriber("x"))
    p.toggle()
    p._check_mic()
    assert p.status is Status.LISTENING
