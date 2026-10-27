"""F024: reset / invitation / magic sign-in links never copy a request Origin verbatim (app/utils/safe_origin.py)."""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.routers import auth_router
from app.services import invitation_email
from app.utils import safe_origin


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")


@pytest.fixture
def configured_urls(monkeypatch):
    monkeypatch.setattr(settings, "RESET_PASSWORD_URL", "")
    monkeypatch.setattr(settings, "MAGIC_LOGIN_URL", "")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    monkeypatch.setattr(settings, "BASE_URL", "https://api.example.com")
    monkeypatch.delenv("APP_PROFILE", raising=False)


@pytest.mark.parametrize("raw, expected", [
    ("http://localhost:5173/reset", "http://localhost:5173"),
    ("http://LOCALHOST:5173", "http://localhost:5173"),
    ("https://127.0.0.1", "https://127.0.0.1"),
    ("https://sales.neutrino-ai.com/app/team", "https://sales.neutrino-ai.com"),
])
def test_sec_origin_legit(local_env, raw, expected):
    assert safe_origin.safe_origin(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://evil.example\\@localhost",          # backslash: browsers open evil.example
    "http://evil.example\\@localhost:5173/x",
    "http://evil.example@localhost:5173",        # user info
    "http://user:pw@localhost",
    "http://localhost :5173",                    # white space
    "http://localhost\t:5173",
    "http://local\nhost:5173",
    "http://localhost\x00.evil.example",         # control character
    "http://localhost:99999",                    # bad port
    "http://localhost:abc",
    "http://localhost:0",
    "javascript:alert(1)//localhost",
    "javascript://localhost/%0aalert(1)",
    "ftp://localhost",
    "//localhost:5173",
    "http://[::1",                               # malformed
    "https://evil.example.com",
    "https://sales.neutrino-ai.com.evil.example",
    "",
    None,
])
def test_sec_origin_attacks_refused(local_env, raw):
    assert safe_origin.safe_origin(raw) == ""
    assert invitation_email.normalized_origin(raw) == ""


def test_sec_localhost_refused_when_deployed(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert safe_origin.safe_origin("http://localhost:5173") == ""
    assert safe_origin.safe_origin("https://sales.neutrino-ai.com") == "https://sales.neutrino-ai.com"
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    assert safe_origin.safe_origin("http://localhost:5173") == ""
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert safe_origin.safe_origin("http://localhost:5173") == ""


def _request(**headers):
    return SimpleNamespace(headers=headers)


def test_sec_reset_link_malicious_origin_falls_back(local_env, configured_urls):
    link = auth_router._build_reset_link_for_origin("tok", _request(origin="http://evil.example\\@localhost"))
    assert link == "https://app.example.com/reset-password?token=tok"
    # a bad Origin does not stop a good Referer
    link = auth_router._build_reset_link_for_origin(
        "tok", _request(origin="http://evil.example@localhost", referer="http://localhost:5173/login"))
    assert link == "http://localhost:5173/reset-password?token=tok"


def test_sec_reset_and_magic_link_localhost_only_locally(monkeypatch, configured_urls):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    req = _request(origin="http://localhost:5173")
    assert auth_router._build_reset_link_for_origin("t", req) == "http://localhost:5173/reset-password?token=t"
    assert invitation_email.build_magic_login_link("t", req) == "http://localhost:5173/magic-login?token=t"
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert auth_router._build_reset_link_for_origin("t", req) == "https://app.example.com/reset-password?token=t"
    assert invitation_email.build_magic_login_link("t", req) == "https://app.example.com/magic-login?token=t"


def test_sec_magic_link_malicious_referer(local_env, configured_urls):
    link = invitation_email.build_magic_login_link("t", _request(referer="http://evil.example\\@127.0.0.1:5173/"))
    assert link == "https://app.example.com/magic-login?token=t"


def test_sec_parse_origin_parts():
    assert safe_origin.parse_origin("http://localhost:5173/x?y") == ("http", "localhost", 5173)
    assert safe_origin.parse_origin("HTTPS://Sales.Neutrino-AI.com") == ("https", "sales.neutrino-ai.com", None)
    assert safe_origin.parse_origin("http://" + "a" * 3000) is None
