"""F007 remainder: SMTP / IMAP hosts are checked for SSRF like webhook URLs (app/utils/host_safety.py)."""
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.routers import inbox_router
from app.utils import host_safety, webhooks


def _resolver(*addresses):
    return lambda host, port: list(addresses)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "::1", "169.254.169.254",
                                  "169.254.10.10", "0.0.0.0", "100.100.100.200", "fd00:ec2::254", "[::1]"])
def test_sec_literal_internal_addresses_refused(host):
    with pytest.raises(host_safety.UnsafeHostError):
        host_safety.check_host(host, resolver=None)


def test_sec_names_checked_on_every_resolved_address():
    assert host_safety.check_host("smtp.example.com", 587, resolver=_resolver("93.184.216.34")) == ["93.184.216.34"]
    with pytest.raises(host_safety.UnsafeHostError, match="private"):
        host_safety.check_host("smtp.example.com", resolver=_resolver("93.184.216.34", "10.0.0.1"))
    with pytest.raises(host_safety.UnsafeHostError, match="metadata"):
        host_safety.check_host("evil.example.com", resolver=_resolver("169.254.169.254"))
    with pytest.raises(host_safety.UnsafeHostError, match="resolve"):
        host_safety.check_host("nothing.example.com", resolver=_resolver())
    with pytest.raises(host_safety.UnsafeHostError, match="not allowed"):
        host_safety.check_host("metadata.google.internal", resolver=None)


@pytest.mark.parametrize("host", ["", "   ", "smtp.example.com/x", "user@smtp.example.com", "smtp example.com",
                                  "smtp\\example.com", "smtp.exa\nmple.com", "a" * 300])
def test_sec_host_syntax(host):
    with pytest.raises(host_safety.UnsafeHostError):
        host_safety.check_host(host, resolver=None)


def test_sec_port_range():
    with pytest.raises(host_safety.UnsafeHostError, match="port"):
        host_safety.check_host("93.184.216.34", 70000)


def test_sec_allow_private_still_blocks_metadata():
    assert host_safety.check_host("127.0.0.1", allow_private=True) == ["127.0.0.1"]
    assert host_safety.check_host("mailpit", allow_private=True, resolver=_resolver("172.18.0.3")) == ["172.18.0.3"]
    with pytest.raises(host_safety.UnsafeHostError, match="metadata"):
        host_safety.check_host("169.254.169.254", allow_private=True)
    with pytest.raises(host_safety.UnsafeHostError):
        host_safety.check_host("169.254.1.1", allow_private=True)


def test_sec_resolve_wraps_dns_errors(monkeypatch):
    def gai(*a, **k):
        raise host_safety.socket.gaierror("no such host")
    monkeypatch.setattr(host_safety.socket, "getaddrinfo", gai)
    with pytest.raises(host_safety.UnsafeHostError, match="Could not resolve"):
        host_safety.resolve("nope.example.com", 25)


def test_sec_webhooks_share_the_rule():
    assert webhooks.ip_block_reason is host_safety.ip_block_reason
    assert webhooks.BLOCKED_HOSTNAMES is host_safety.BLOCKED_HOSTNAMES


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")


@pytest.mark.parametrize("host, addresses", [
    ("127.0.0.1", ()), ("169.254.169.254", ()), ("10.1.2.3", ()),
    ("localhost", ("127.0.0.1",)), ("rebind.example.com", ("8.8.8.8", "192.168.0.10")),
])
def test_sec_inbox_mail_host_refused_when_deployed(deployed, monkeypatch, host, addresses):
    monkeypatch.setattr(host_safety, "resolve", _resolver(*addresses))
    with pytest.raises(HTTPException) as exc:
        inbox_router._check_mail_host(host)
    assert exc.value.status_code == 400
    assert "Mail server not allowed" in exc.value.detail


def test_sec_inbox_public_mail_host_allowed_when_deployed(deployed, monkeypatch):
    monkeypatch.setattr(host_safety, "resolve", _resolver("93.184.216.34"))
    inbox_router._check_mail_host("smtp.example.com")
    inbox_router._check_mail_host(None)


def test_sec_inbox_local_dev_keeps_mailpit(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    def no_dns(host, port):
        raise AssertionError("no DNS lookups on a local setup")
    monkeypatch.setattr(host_safety, "resolve", no_dns)
    for host in ("mailpit", "localhost", "127.0.0.1", "host.docker.internal", "smtp.example.com"):
        inbox_router._check_mail_host(host)
    with pytest.raises(HTTPException) as exc:
        inbox_router._check_mail_host("169.254.169.254")
    assert exc.value.status_code == 400
