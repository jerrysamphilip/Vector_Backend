"""F004 sync auth dependencies; credential / config hygiene (undecryptable values, ini precedence and secrets)."""
import inspect
import logging

from app.core import auth, config, encrypted_type


def test_sec_auth_dependencies_are_sync():
    assert not inspect.iscoroutinefunction(auth.get_current_user)
    assert not inspect.iscoroutinefunction(auth.require_role("ADMIN"))
    assert not inspect.iscoroutinefunction(auth.require_permission("manage_team"))


def test_sec_undecryptable_value_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(encrypted_type, "_reported", set())
    bad = encrypted_type.PREFIX + "not-a-fernet-token"
    other = encrypted_type.PREFIX + "another-bad-token"
    with caplog.at_level(logging.ERROR, logger="app.core.encrypted_type"):
        for _ in range(5):
            assert encrypted_type.decrypt_value(bad) is None
        assert encrypted_type.decrypt_value(other) is None
    records = [r for r in caplog.records if "could not be decrypted" in r.getMessage()]
    assert len(records) == 2
    assert "not-a-fernet-token" not in caplog.text  # the stored value is never logged


def test_sec_undecryptable_set_is_bounded(monkeypatch):
    monkeypatch.setattr(encrypted_type, "_reported", set())
    monkeypatch.setattr(encrypted_type, "_MAX_REPORTED", 3)
    for i in range(10):
        encrypted_type.decrypt_value(f"{encrypted_type.PREFIX}bad-{i}")
    assert len(encrypted_type._reported) <= 3


def test_sec_encryption_round_trip_still_works():
    stored = encrypted_type.encrypt_value("s3cret")
    assert stored.startswith(encrypted_type.PREFIX) and encrypted_type.decrypt_value(stored) == "s3cret"


def test_sec_ini_precedence_env_then_dotenv_then_ini():
    ini = {"MYSQL_HOST": "ini-host", "MYSQL_PORT": 3307, "LOG_LEVEL": "DEBUG", "BASE_URL": "https://ini",
           "POSTGRES_HOST": "other-service"}
    overrides, unknown, secrets = config.select_ini_overrides(
        ini, known_fields=config.Settings.model_fields.keys(),
        environ_keys={"MYSQL_HOST"}, dotenv_keys={"LOG_LEVEL"})
    assert overrides == {"MYSQL_PORT": 3307, "BASE_URL": "https://ini"}
    assert unknown == ["POSTGRES_HOST"]
    assert secrets == []


def test_sec_ini_secrets_ignored():
    ini = {"JWT_SECRET_KEY": "x" * 40, "CREDENTIALS_ENCRYPTION_KEY": "k", "MYSQL_PASSWORD": "p",
           "OPENAI_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "a", "AWS_ACCESS_KEY_ID": "id", "METRICS_TOKEN": "m",
           "SES_WEBHOOK_TOKEN": "t", "GOOGLE_CLIENT_SECRET": "g", "MS365_CLIENT_SECRET": "ms", "SENTRY_DSN": "d",
           "POSTGRES_PASSWORD": "pg", "REFRESH_TOKEN_EXPIRE_DAYS": 3}
    overrides, unknown, secrets = config.select_ini_overrides(
        ini, known_fields=config.Settings.model_fields.keys(), environ_keys=(), dotenv_keys=())
    assert overrides == {"REFRESH_TOKEN_EXPIRE_DAYS": 3}
    assert set(secrets) == set(ini) - {"REFRESH_TOKEN_EXPIRE_DAYS"}
    assert unknown == []


def test_sec_is_secret_key():
    for key in ("JWT_SECRET_KEY", "mysql_password", "OPENAI_API_KEY", "METRICS_TOKEN", "SENTRY_DSN",
                "SOME_PRIVATE_KEY"):
        assert config.is_secret_key(key)
    for key in ("MYSQL_HOST", "ACCESS_TOKEN_EXPIRE_MINUTES", "OPENAI_MAX_OUTPUT_TOKENS", "FRONTEND_URL"):
        assert not config.is_secret_key(key)


def test_sec_get_settings_warns_about_ini_secrets(monkeypatch, caplog):
    monkeypatch.setattr(config, "load_settings_from_ini", lambda: {"JWT_SECRET_KEY": "from-ini" * 8,
                                                                   "APP_NAME": "From ini"})
    monkeypatch.setattr(config, "_dotenv_keys", lambda: set())
    monkeypatch.delenv("APP_NAME", raising=False)
    config.get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="app.core.config"):
            built = config.get_settings()
    finally:
        config.get_settings.cache_clear()
    assert built.APP_NAME == "From ini"
    assert built.JWT_SECRET_KEY != "from-ini" * 8
    assert "JWT_SECRET_KEY" in caplog.text and "from-ini" not in caplog.text


def test_sec_is_local_environment(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    for env, local in (("development", True), ("test", True), ("local", True), ("production", False),
                       ("staging", False), ("uat", False), ("", False)):
        monkeypatch.setattr(config.settings, "ENVIRONMENT", env)
        assert config.settings.is_local_environment is local, env
    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert config.settings.is_local_environment is False
