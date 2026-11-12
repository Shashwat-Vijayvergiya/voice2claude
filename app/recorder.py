"""Microphone capture with simple energy-based silence detection.

Audio stays in memory: ``stop()`` returns FLAC bytes (16 kHz mono, 16-bit).
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from typing import Callable

import numpy as np
import soundfile as sf

from app.models import Recording

log = logging.getLogger(__name__)

TARGET_RATE = 16_000
BLOCK_SECONDS = 0.05


class RecorderError(Exception):
    """A problem the user needs to see (no mic, device busy, ...)."""


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


class SilenceDetector:
    """Decides when a recording should stop on its own.

    Speech = a block louder than ``max(min_threshold, noise_floor * ratio)``.
    ``noise_floor`` is a low percentile of recent block levels, so one
    unusually quiet block can't drag it down (a plain minimum could, letting
    ordinary room noise pass as speech). Once speech has started,
    ``silence_seconds`` of quiet stops the recording. If no speech arrives
    within ``no_speech_timeout`` seconds, it stops too.

    Until speech is confirmed, earlier blocks are re-checked against the
    current threshold: if you start talking right away, the floor is briefly
    your own voice, and those blocks count as speech once pauses reveal the
    real floor. Trailing quiet is counted from the last loud block, so this
    never delays the auto-stop.
    """

    def __init__(
        self,
        silence_seconds: float = 2.0,
        no_speech_timeout: float = 8.0,
        max_seconds: float = 180.0,
        min_threshold: float = 0.01,
        ratio: float = 3.0,
        min_speech_seconds: float = 0.15,
        warmup_seconds: float = 0.15,
        floor_percentile: float = 10.0,
        floor_window_seconds: float = 30.0,
    ):
        self.silence_seconds = silence_seconds
        self.no_speech_timeout = no_speech_timeout
        self.max_seconds = max_seconds
        self.min_threshold = min_threshold
        self.ratio = ratio
        self.min_speech_seconds = min_speech_seconds
        self.warmup_seconds = warmup_seconds
        self.floor_percentile = floor_percentile
        self.floor_window_seconds = floor_window_seconds
        self.noise_floor: float | None = None
        self.elapsed = 0.0
        self.silent_for = 0.0
        self.speech_detected = False
        self._floor_levels: deque[float] | None = None
        self._pending: list[float] = []  # levels kept until speech is confirmed

    @property
    def threshold(self) -> float:
        floor = self.noise_floor or 0.0
        return max(self.min_threshold, floor * self.ratio)

    def feed(self, level: float, seconds: float) -> str | None:
        """Feed one block's RMS. Returns a stop reason, or None to keep going."""
        self.elapsed += seconds
        # Skip the stream's first moments: mics often deliver near-silent
        # blocks while starting up. Later zeros are kept on purpose (noise
        # suppression outputs true silence between words).
        if self.elapsed > self.warmup_seconds:
            if self._floor_levels is None:
                self._floor_levels = deque(maxlen=max(1, int(self.floor_window_seconds / seconds)))
            self._floor_levels.append(level)
            self.noise_floor = float(np.percentile(self._floor_levels, self.floor_percentile))

        threshold = self.threshold

        if self.speech_detected:
            self.silent_for = 0.0 if level > threshold else self.silent_for + seconds
        else:
            self._pending.append(level)
            loud = [lv > threshold for lv in self._pending]
            # A single click or bump shouldn't count as speech.
            if sum(loud) * seconds >= self.min_speech_seconds:
                self.speech_detected = True
                last_loud = max(i for i, x in enumerate(loud) if x)
                self.silent_for = (len(loud) - 1 - last_loud) * seconds
                self._pending = []

        if self.elapsed >= self.max_seconds:
            return "max_length"
        if self.speech_detected and self.silent_for >= self.silence_seconds:
            return "silence"
        if not self.speech_detected and self.elapsed >= self.no_speech_timeout:
            return "no_speech"
        return None


def resample(samples: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """Linear-interpolation resample. Fine for speech going to Whisper."""
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    n_out = int(round(samples.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def encode_flac(samples: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.clip(samples, -1.0, 1.0), rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def _default_stream_factory(**kwargs):
    import sounddevice as sd

    return sd.InputStream(**kwargs)


def _default_rate_for(device) -> int:
    import sounddevice as sd

    return int(sd.query_devices(device, kind="input")["default_samplerate"])


class Recorder:
    """Start/stop microphone capture. Not reusable across threads; call from the UI thread."""

    def __init__(
        self,
        device: int | str | None = None,
        silence_seconds: float = 2.0,
        no_speech_timeout: float = 8.0,
        max_seconds: float = 180.0,
        on_auto_stop: Callable[[str], None] | None = None,
        stream_factory: Callable[..., object] = _default_stream_factory,
        default_rate_for: Callable[[object], int] = _default_rate_for,
    ):
        self.device = device
        self.silence_seconds = silence_seconds
        self.no_speech_timeout = no_speech_timeout
        self.max_seconds = max_seconds
        self.on_auto_stop = on_auto_stop
        self._stream_factory = stream_factory
        self._default_rate_for = default_rate_for
        self._lock = threading.Lock()
        self._stream = None
        self._blocks: list[np.ndarray] = []
        self._rate = TARGET_RATE
        self._detector: SilenceDetector | None = None
        self._stop_reason: str | None = None
        self._last_audio = 0.0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        self._blocks = []
        self._stop_reason = None
        self._detector = SilenceDetector(self.silence_seconds, self.no_speech_timeout, self.max_seconds)

        errors = []
        rates = [TARGET_RATE]
        try:
            native = self._default_rate_for(self.device)
            if native != TARGET_RATE:
                rates.append(native)
        except Exception as exc:
            raise RecorderError(_mic_message(exc)) from exc

        for rate in rates:
            try:
                stream = self._stream_factory(
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                    blocksize=int(rate * BLOCK_SECONDS),
                    callback=self._callback,
                )
                self._rate = rate
                self._last_audio = time.monotonic()
                self._stream = stream
                stream.start()
                log.info("Recording at %d Hz", rate)
                return
            except Exception as exc:
                self._stream = None
                errors.append(exc)
                log.warning("Could not open mic at %d Hz: %s", rate, exc)
        raise RecorderError(_mic_message(errors[-1]))

    def seconds_since_audio(self) -> float:
        """How long since the mic last delivered a block (watchdog for unplugged mics)."""
        return time.monotonic() - self._last_audio if self._stream is not None else 0.0

    def _callback(self, indata, frames, time_info, status) -> None:
        self._last_audio = time.monotonic()
        if status:
            log.debug("Audio status: %s", status)
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        with self._lock:
            self._blocks.append(block)
            detector = self._detector
            reason = detector.feed(rms(block), frames / self._rate) if detector else None
            fire = reason is not None and self._stop_reason is None
            if fire:
                self._stop_reason = reason
        if fire and self.on_auto_stop:
            self.on_auto_stop(reason)

    def stop(self) -> Recording:
        stream, self._stream = self._stream, None
        if stream is None:
            raise RecorderError("Not recording")
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            log.warning("Error closing audio stream: %s", exc)

        with self._lock:
            blocks, self._blocks = self._blocks, []
            detector = self._detector
            reason = self._stop_reason or "manual"
        samples = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        samples = resample(samples, self._rate, TARGET_RATE)
        return Recording(
            audio=encode_flac(samples) if samples.size else b"",
            sample_rate=TARGET_RATE,
            duration_s=samples.size / TARGET_RATE,
            speech_detected=bool(detector and detector.speech_detected),
            stop_reason=reason,
        )


def _mic_message(exc: Exception) -> str:
    text = str(exc)
    if "No input device" in text or "Error querying device" in text or "-1" in text:
        return "No microphone found. Plug one in or pick a device in settings, then try again."
    if "busy" in text.lower() or "Device unavailable" in text:
        return "The microphone is busy in another app. Close it and try again."
    return f"Could not open the microphone: {text}"
