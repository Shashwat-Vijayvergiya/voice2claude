"""Settings, paths, logging and API-key lookup.

Defaults ship in ``config/settings.json`` inside the repo. The user's own copy
lives in the data directory (``~/.local/share/voice-to-claude/settings.json``
by default) and only needs the keys they want to change.
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

APP_NAME = "voice-to-claude"
REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = REPO_DIR / "config" / "settings.json"
VOCABULARY_FILE = REPO_DIR / "config" / "vocabulary.json"

API_KEY_ENV = "GROQ_API_KEY"
KEYRING_SERVICE = APP_NAME
KEYRING_USER = "groq_api_key"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    whisper_model: str = "whisper-large-v3-turbo"
    llm_model: str = "openai/gpt-oss-120b"
    audio_device: int | str | None = None
    silence_seconds: float = Field(2.0, gt=0, le=30)
    no_speech_timeout_seconds: float = Field(8.0, gt=0, le=60)
    max_record_seconds: float = Field(180, gt=1, le=1800)
    language: str = "auto"
    compression_strength: Literal["balanced", "aggressive"] = "balanced"
    llm_reasoning_effort: Literal["low", "medium", "high"] = "low"
    skip_under_words: int = Field(30, ge=0, le=1000)
    compression_timeout_seconds: float = Field(20.0, gt=1, le=120)
    current_project: str = "default"
    theme: Literal["system", "light", "dark"] = "system"
    history_enabled: bool = True


class ConfigError(Exception):
    """Raised when a settings file exists but cannot be used."""


def data_dir() -> Path:
    """Where user data lives. ``VOICE2CLAUDE_HOME`` overrides (used by tests)."""
    override = os.environ.get("VOICE2CLAUDE_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def user_settings_file() -> Path:
    return data_dir() / "settings.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def load_settings() -> Settings:
    """Merge repo defaults with the user's overrides and validate."""
    merged: dict = {}
    if DEFAULTS_FILE.exists():
        merged.update(_read_json(DEFAULTS_FILE))
    user_file = user_settings_file()
    if user_file.exists():
        merged.update(_read_json(user_file))
    try:
        return Settings(**merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings: {exc}") from exc


def save_settings(settings: Settings) -> Path:
    """Write the full settings object to the user's settings file."""
    path = user_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.model_dump(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def update_user_settings(**changes) -> Settings:
    """Change a few keys in the user's file, leaving the rest to the repo defaults."""
    path = user_settings_file()
    current = _read_json(path) if path.exists() else {}
    current.update(changes)
    try:
        settings = Settings(**{**(_read_json(DEFAULTS_FILE) if DEFAULTS_FILE.exists() else {}), **current})
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return settings


def load_vocabulary() -> list[str]:
    if not VOCABULARY_FILE.exists():
        return []
    terms = _read_json(VOCABULARY_FILE).get("terms", [])
    return [str(t) for t in terms if str(t).strip()]


def get_api_key() -> str | None:
    """Groq key from the environment, then the system keyring. Never from settings.json."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as exc:  # no keyring backend, locked keyring, etc.
        log.debug("Keyring lookup failed: %s", exc)
        return None
    return key.strip() if key else None


MISSING_KEY_MESSAGE = (
    "No Groq API key found.\n\n"
    f"Set the {API_KEY_ENV} environment variable, or store it in the system keyring:\n"
    f"  python -c \"import keyring; keyring.set_password('{KEYRING_SERVICE}', "
    f"'{KEYRING_USER}', input('Key: '))\"\n\n"
    "Get a free key at https://console.groq.com/keys"
)


def setup_logging(level: int = logging.INFO) -> Path:
    """Log to stderr and to a rotating file in the data directory."""
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_v2c", False):
            root.removeHandler(h)
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    for h in (file_handler, stream_handler):
        h.setFormatter(fmt)
        h._v2c = True  # type: ignore[attr-defined]
        root.addHandler(h)
    # The openai/httpx clients log request details at INFO; keep them quiet.
    for name in ("httpx", "httpx2"):  # openai>=3 uses httpx2
        logging.getLogger(name).setLevel(logging.WARNING)
    return log_file
