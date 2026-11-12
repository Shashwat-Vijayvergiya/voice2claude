import io

import numpy as np
import pytest
import soundfile as sf

from app.recorder import (
    TARGET_RATE,
    Recorder,
    RecorderError,
    SilenceDetector,
    encode_flac,
    resample,
    rms,
)

BLOCK = 0.05


def feed_many(det, level, seconds):
    reason = None
    for _ in range(int(round(seconds / BLOCK))):
        reason = det.feed(level, BLOCK)
        if reason:
            return reason
    return reason


# ---- SilenceDetector -----------------------------------------------------

def test_stops_after_silence_following_speech():
    det = SilenceDetector(silence_seconds=2.0, no_speech_timeout=8.0)
    assert feed_many(det, 0.001, 0.5) is None  # room noise
    assert feed_many(det, 0.2, 1.0) is None  # speech
    assert det.speech_detected
    assert feed_many(det, 0.001, 1.9) is None
    assert feed_many(det, 0.001, 0.2) == "silence"


def test_short_pause_does_not_stop():
    det = SilenceDetector(silence_seconds=2.0)
    feed_many(det, 0.2, 1.0)
    assert feed_many(det, 0.001, 1.5) is None
    assert feed_many(det, 0.2, 0.5) is None  # talking again resets the silence timer
    assert det.silent_for == 0.0


def test_no_speech_timeout():
    det = SilenceDetector(no_speech_timeout=3.0)
    assert feed_many(det, 0.001, 3.1) == "no_speech"
    assert not det.speech_detected


def test_single_click_is_not_speech():
    det = SilenceDetector()
    feed_many(det, 0.001, 0.5)
    det.feed(0.5, BLOCK)  # one loud block
    assert not det.speech_detected


def test_speech_from_first_block_is_still_detected():
    # Talking before any quiet moment: floor starts at voice level.
    det = SilenceDetector(silence_seconds=2.0)
    assert feed_many(det, 0.2, 1.5) is None
    assert not det.speech_detected  # can't tell yet
    assert feed_many(det, 0.001, 0.5) is None
    assert det.speech_detected  # pauses reveal the floor; earlier speech counts
    # Quiet is counted from the last loud block, so the stop isn't delayed.
    assert feed_many(det, 0.001, 1.45) is None
    assert feed_many(det, 0.001, 0.1) == "silence"


def test_single_quiet_dip_does_not_collapse_floor():
    # Real readings from this machine: room median ~0.008, peaks ~0.012, one dip to 0.0001.
    det = SilenceDetector()
    feed_many(det, 0.008, 1.0)
    det.feed(0.0001, BLOCK)
    assert det.noise_floor > 0.005
    assert feed_many(det, 0.012, 1.0) is None  # noise peaks stay below threshold
    assert not det.speech_detected


def test_startup_zeros_do_not_fake_the_floor():
    # Real mic on this machine: first block ~0.00001, room noise ~0.0055.
    det = SilenceDetector()
    det.feed(0.00001, BLOCK)
    feed_many(det, 0.0055, 1.0)
    assert det.noise_floor == pytest.approx(0.0055)
    assert det.threshold == pytest.approx(0.0165)
    assert feed_many(det, 0.0073, 1.0) is None  # room noise peaks are not speech
    assert not det.speech_detected


def test_max_length():
    det = SilenceDetector(max_seconds=5.0, silence_seconds=10)
    assert feed_many(det, 0.2, 6.0) == "max_length"


def test_threshold_adapts_to_noisy_room():
    det = SilenceDetector()
    feed_many(det, 0.02, 1.0)  # steady fan noise above the 0.01 floor
    assert not det.speech_detected
    assert det.threshold == pytest.approx(0.06)


# ---- audio helpers -------------------------------------------------------

def test_rms():
    assert rms(np.zeros(10, dtype=np.float32)) == 0.0
    assert rms(np.ones(10, dtype=np.float32) * 0.5) == pytest.approx(0.5)
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0


def test_resample_length_and_passthrough():
    x = np.sin(np.linspace(0, 100, 44100)).astype(np.float32)
    assert resample(x, 44100).size == 16000
    assert resample(x, 16000) is x or np.array_equal(resample(x, 16000), x)


def test_flac_roundtrip():
    x = (0.3 * np.sin(np.linspace(0, 2000, TARGET_RATE))).astype(np.float32)
    data, rate = sf.read(io.BytesIO(encode_flac(x)), dtype="float32")
    assert rate == TARGET_RATE
    assert data.size == x.size
    assert np.max(np.abs(data - x)) < 1e-3


# ---- Recorder with a fake stream ----------------------------------------

class FakeStream:
    def __init__(self, fail_rates=(), **kwargs):
        self.kwargs = kwargs
        if kwargs["samplerate"] in fail_rates:
            raise RuntimeError("Invalid sample rate")
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


def make_recorder(native=16000, fail_rates=(), **kw):
    streams = []

    def factory(**kwargs):
        s = FakeStream(fail_rates=fail_rates, **kwargs)
        streams.append(s)
        return s

    rec = Recorder(stream_factory=factory, default_rate_for=lambda d: native, **kw)
    return rec, streams


def push(rec, level, seconds, rate=16000):
    n = int(rate * BLOCK)
    for _ in range(int(round(seconds / BLOCK))):
        block = np.full((n, 1), level, dtype=np.float32)
        rec._callback(block, n, None, None)


def test_recorder_returns_flac_with_speech():
    rec, streams = make_recorder()
    rec.start()
    assert rec.is_recording and streams[0].started
    push(rec, 0.0005, 0.3)
    push(rec, 0.2, 1.0)
    out = rec.stop()
    assert not rec.is_recording and streams[0].closed
    assert out.speech_detected
    assert out.duration_s == pytest.approx(1.3, abs=0.01)
    assert out.audio[:4] == b"fLaC"
    assert out.stop_reason == "manual"


def test_recorder_auto_stop_fires_once():
    reasons = []
    rec, _ = make_recorder(silence_seconds=0.5, on_auto_stop=reasons.append)
    rec.start()
    push(rec, 0.2, 0.5)
    push(rec, 0.0, 2.0)
    assert reasons == ["silence"]
    assert rec.stop().stop_reason == "silence"


def test_recorder_falls_back_to_native_rate_and_resamples():
    rec, streams = make_recorder(native=48000, fail_rates=(16000,))
    rec.start()
    assert streams[-1].kwargs["samplerate"] == 48000
    push(rec, 0.2, 1.0, rate=48000)
    out = rec.stop()
    assert out.sample_rate == 16000
    assert out.duration_s == pytest.approx(1.0, abs=0.01)


def test_recorder_no_mic():
    rec = Recorder(default_rate_for=lambda d: (_ for _ in ()).throw(ValueError("No input device matching")))
    with pytest.raises(RecorderError, match="No microphone"):
        rec.start()


def test_recorder_all_rates_fail():
    rec, _ = make_recorder(native=48000, fail_rates=(16000, 48000))
    with pytest.raises(RecorderError):
        rec.start()
    assert not rec.is_recording


def test_stop_without_start():
    rec, _ = make_recorder()
    with pytest.raises(RecorderError):
        rec.stop()


def test_seconds_since_audio():
    rec, _ = make_recorder()
    assert rec.seconds_since_audio() == 0.0  # not recording
    rec.start()
    push(rec, 0.1, 0.1)
    assert rec.seconds_since_audio() < 0.5
    rec.stop()
