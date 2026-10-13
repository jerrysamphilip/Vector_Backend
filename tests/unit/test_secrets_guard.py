"""Start-up secret checks (app/core/secrets_guard.py, BR-DF-09): deployed environments refuse the public
defaults; local runs fall back with a warning."""
import base64

import pytest

from app.core import secrets_guard as sg

LONG = "x" * 40


@pytest.fixture
def local(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")


@pytest.mark.parametrize("env,k8s,expected", [
    ("test", None, False), ("development", None, False), ("", None, False),
    ("production", None, True), ("prod", None, True), (" Staging ", None, True), ("stage", None, True),
    ("development", "10.0.0.1", True)])
def test_is_deployed(monkeypatch, env, k8s, expected):
    monkeypatch.setenv("ENVIRONMENT", env)
    if k8s:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", k8s)
    else:
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert sg.is_deployed() is expected


def test_a_long_custom_jwt_secret_is_used_as_is(deployed):
    assert sg.resolve_jwt_secret(LONG) == LONG


def test_local_runs_get_a_random_secret_instead_of_the_default(local):
    a, b = sg.resolve_jwt_secret(sg.DEFAULT_JWT_SECRET), sg.resolve_jwt_secret("")
    assert a != sg.DEFAULT_JWT_SECRET and len(a) >= 32 and a != b


@pytest.mark.parametrize("value", [sg.DEFAULT_JWT_SECRET, "", None, "short-secret"])
def test_deployed_refuses_weak_jwt_secrets(deployed, value):
    with pytest.raises(RuntimeError):
        sg.resolve_jwt_secret(value)


def test_fernet_key_is_deterministic_and_valid(local):
    key = sg.credentials_fernet_key("my-key")
    assert key == sg.credentials_fernet_key("my-key") != sg.credentials_fernet_key("other-key")
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_local_without_key_uses_the_development_key(local):
    assert sg.credentials_fernet_key("") == sg.credentials_fernet_key(sg._DEV_CREDENTIALS_KEY)


def test_deployed_requires_a_real_credentials_key(deployed):
    with pytest.raises(RuntimeError):
        sg.credentials_fernet_key("")
    with pytest.raises(RuntimeError):
        sg.credentials_fernet_key(sg._DEV_CREDENTIALS_KEY)
    assert sg.credentials_fernet_key(LONG)
