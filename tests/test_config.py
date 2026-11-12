import json

import pytest

from app import config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE2CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return tmp_path


def test_defaults_load():
    s = config.load_settings()
    assert s.whisper_model == "whisper-large-v3-turbo"
    assert s.llm_model == "openai/gpt-oss-120b"
    assert s.compression_strength == "balanced"


def test_user_overrides_merge(isolated_home):
    (isolated_home / "settings.json").write_text(json.dumps({"llm_model": "x/y", "silence_seconds": 1.5}))
    s = config.load_settings()
    assert s.llm_model == "x/y"
    assert s.silence_seconds == 1.5
    assert s.whisper_model == "whisper-large-v3-turbo"


def test_save_roundtrip():
    s = config.load_settings()
    s.current_project = "demo"
    path = config.save_settings(s)
    assert path.exists()
    assert config.load_settings().current_project == "demo"


def test_bad_json_raises(isolated_home):
    (isolated_home / "settings.json").write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_invalid_value_raises(isolated_home):
    (isolated_home / "settings.json").write_text(json.dumps({"compression_strength": "adaptive"}))
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_api_key_never_in_settings():
    assert "api_key" not in " ".join(config.Settings.model_fields)


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "  test-key ")
    assert config.get_api_key() == "test-key"


def test_api_key_from_keyring(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, user: "kr-key")
    assert config.get_api_key() == "kr-key"


def test_api_key_missing(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, user: None)
    assert config.get_api_key() is None


def test_keyring_failure_is_handled(monkeypatch):
    import keyring

    def boom(*a):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    assert config.get_api_key() is None


def test_vocabulary_loads():
    terms = config.load_vocabulary()
    assert "pytest" in terms


def test_logging_writes_file():
    import logging

    log_file = config.setup_logging()
    logging.getLogger("t").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello" in log_file.read_text()
