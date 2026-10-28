"""Test runs never read the git-ignored .env (a developer's real OpenAI / AWS keys).

Every case points app.core.config at a temporary fake .env in tmp_path; the real .env is never touched.
"""
import sys

import pytest

from app.core import config

FAKE_OPENAI = "sk-fake-enviso-not-a-real-key"
FAKE_AWS = "AKIAFAKEENVISO0000000"


@pytest.fixture
def fake_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={FAKE_OPENAI}\nAWS_ACCESS_KEY_ID={FAKE_AWS}\n", encoding="utf-8")
    monkeypatch.setattr(config, "_ENV_FILE", str(env_file))
    # The conftest exports OPENAI_API_KEY="" (environment beats .env): drop it so only the file could set it.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    return env_file


def _build() -> config.Settings:
    """The production path: get_settings without its cache."""
    return config.get_settings.__wrapped__()


@pytest.mark.parametrize("flag", ["1", "true", "YES"])
def test_enviso_flag_set_ignores_dotenv(fake_env_file, monkeypatch, flag):
    monkeypatch.setenv(config.IGNORE_DOTENV_VAR, flag)
    s = _build()
    assert s.OPENAI_API_KEY == ""
    assert s.AWS_ACCESS_KEY_ID == ""
    assert config.settings_env_file() is None
    assert config._dotenv_keys() == set()


def test_enviso_without_flag_reads_dotenv(fake_env_file, monkeypatch):
    monkeypatch.setenv(config.IGNORE_DOTENV_VAR, "0")
    s = _build()
    assert s.OPENAI_API_KEY == FAKE_OPENAI
    assert s.AWS_ACCESS_KEY_ID == FAKE_AWS
    assert config.settings_env_file() == str(fake_env_file)
    assert {"OPENAI_API_KEY", "AWS_ACCESS_KEY_ID"} <= config._dotenv_keys()


def test_enviso_unset_flag_under_pytest_ignores_dotenv(fake_env_file, monkeypatch):
    monkeypatch.delenv(config.IGNORE_DOTENV_VAR, raising=False)
    assert "pytest" in sys.modules
    assert config.dotenv_disabled() is True
    assert _build().OPENAI_API_KEY == ""


def test_enviso_unset_flag_outside_pytest_reads_dotenv(fake_env_file, monkeypatch):
    """Production / dev behaviour is unchanged: no flag and no pytest means .env is read."""
    monkeypatch.delenv(config.IGNORE_DOTENV_VAR, raising=False)
    monkeypatch.delitem(sys.modules, "pytest")
    assert config.dotenv_disabled() is False
    assert config.settings_env_file() == str(fake_env_file)


def test_enviso_class_default_is_off_in_tests():
    """Settings() built directly (not via get_settings) does not read .env either in a test process."""
    assert config.Settings.model_config.get("env_file") is None
