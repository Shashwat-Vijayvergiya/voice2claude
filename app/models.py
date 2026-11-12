"""Small data objects passed between pipeline steps."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Mode(str, Enum):
    COMMAND = "COMMAND"
    QUESTION = "QUESTION"
    CODE = "CODE"
    DICTATION = "DICTATION"


class LLMOutput(BaseModel):
    """Exactly what the compression LLM must return."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode
    command: str
    ambiguities: list[str] = Field(default_factory=list)
    destructive: bool = False


class CompressionContext(BaseModel):
    """Background sent with each compression call (filled in Phase 4)."""

    project_notes: str = ""
    recent_commands: list[str] = Field(default_factory=list)


class CommandResult:
    """One finished command, for history and recent-command context."""

    def __init__(self, item_id: str, compressed: "Compressed", report, transcript: "Transcript | None",
                 recompressed: bool):
        self.item_id = item_id
        self.compressed = compressed
        self.report = report  # checker.CheckReport
        self.transcript = transcript
        self.recompressed = recompressed


class Compressed(BaseModel):
    original: str
    command: str
    mode: Mode | None = None
    ambiguities: list[str] = Field(default_factory=list)
    destructive: bool = False
    skipped: bool = False          # no LLM call was made
    skip_reason: str | None = None
    fallback_error: str | None = None  # LLM failed; command is the cleaned transcript
    latency_s: float = 0.0
    model: str | None = None


class Recording(BaseModel):
    """Encoded audio held in memory. Never written to disk."""

    model_config = ConfigDict(frozen=True)

    audio: bytes = Field(repr=False)
    sample_rate: int
    duration_s: float
    speech_detected: bool
    stop_reason: str = "manual"  # manual | silence | no_speech | max_length
    filename: str = "audio.flac"


class Transcript(BaseModel):
    text: str
    audio_seconds: float
    latency_s: float
    model: str
