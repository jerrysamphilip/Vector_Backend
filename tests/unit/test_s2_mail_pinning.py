"""
S2 / F007: mailbox connections resolve the host once, check every address (app/utils/host_safety.py)
and connect to the checked IP, while SNI, STARTTLS and the certificate check use the host name. A
second DNS answer (rebinding) can't redirect the connection.

No network: socket.getaddrinfo / socket.create_connection are replaced and the TLS context only
records what it would wrap.
"""
import io
import socket
import ssl

import pytest

from app.adapters import providers
from app.adapters.mail import TlsMailConnector, pinned_addresses
from app.core import tls
from app.core.config import settings

PUBLIC, PRIVATE = "93.184.216.34", "10.0.0.7"


class _Stream(io.RawIOBase):
    """A read stream the fake server can append to after the client sends a command."""

    def __init__(self, data=b""):
        self._buf, self._pos = bytearray(data), 0

    def readable(self):
        return True

    def append(self, data):
        self._buf += data

    def readinto(self, b):
        chunk = self._buf[self._pos:self._pos + len(b)]
        b[:len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


class FakeSocket:
    def __init__(self, address, script=b""):
        self.address, self.sent = address, []
        self._stream = _Stream(script)
        self._file = io.BufferedReader(self._stream)

    def makefile(self, mode="rb"):
        return self._file

    def sendall(self, data):
        self.sent.append(data)
        # Python 3.12's imaplib always asks for CAPABILITY after the greeting (3.13 reads it from the
        # greeting): answer it like a server so the connection completes on both versions
        tag, _, command = data.partition(b" ")
        if command.strip() == b"CAPABILITY":
            self._stream.append(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag + b" OK CAPABILITY completed\r\n")

    def close(self):
        pass

    def shutdown(self, how):
        pass


class RecordingContext:
    """The real verifying context's settings, recording every wrap instead of doing TLS."""

    def __init__(self, scripts):
        real = ssl.create_default_context()
        self.check_hostname, self.verify_mode = real.check_hostname, real.verify_mode
        self.wrapped = []
        self._scripts = list(scripts)

    def wrap_socket(self, sock, server_hostname=None):
        self.wrapped.append((sock.address, server_hostname))
        return FakeSocket(sock.address, self._scripts.pop(0))


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "MAIL_TLS_VERIFY", True)


@pytest.fixture
def net(monkeypatch):
    """Scripted DNS (first answer public, any later one private) and recorded TCP connections."""
    state = {"lookups": 0, "connections": [], "scripts": []}

    def getaddrinfo(host, port, *args, **kwargs):
        state["lookups"] += 1
        ip = PUBLIC if state["lookups"] == 1 else PRIVATE
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def create_connection(address, timeout=None, source_address=None):
        state["connections"].append(address)
        return FakeSocket(address[0], state["scripts"].pop(0))

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return state


def _context(monkeypatch, *scripts):
    ctx = RecordingContext(scripts)
    monkeypatch.setattr(tls, "tls_context", lambda: ctx)
    return ctx


IMAP_GREETING = b"* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] ready\r\n"


def test_imap_ssl_connects_to_the_checked_ip_with_sni_for_the_name(deployed, net, monkeypatch):
    net["scripts"] = [b""]
    ctx = _context(monkeypatch, IMAP_GREETING)
    imap = TlsMailConnector().open_imap("imap.example.com", 993, timeout=5)
    assert net["lookups"] == 1                                   # resolved once
    assert net["connections"] == [(PUBLIC, 993)]                 # to the checked address
    assert ctx.wrapped == [(PUBLIC, "imap.example.com")]         # certificate checked for the name
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert imap.host == "imap.example.com"


def test_smtp_ssl_and_starttls_are_pinned(deployed, net, monkeypatch):
    net["scripts"] = [b""]
    ctx = _context(monkeypatch, b"220 smtp.example.com ready\r\n")
    TlsMailConnector().open_smtp("smtp.example.com", 465, use_ssl=True, timeout=5)
    assert net["connections"] == [(PUBLIC, 465)] and ctx.wrapped == [(PUBLIC, "smtp.example.com")]

    net.update(lookups=0, connections=[],
               scripts=[b"220 hi\r\n250-smtp.example.com\r\n250 STARTTLS\r\n220 go ahead\r\n"])
    ctx = _context(monkeypatch, b"250-smtp.example.com\r\n250 AUTH PLAIN\r\n")
    smtp = TlsMailConnector().open_smtp("smtp.example.com", 587, use_ssl=False, timeout=5)
    assert net["lookups"] == 1 and net["connections"] == [(PUBLIC, 587)]
    assert ctx.wrapped == [(PUBLIC, "smtp.example.com")]          # STARTTLS: SNI is the name too
    assert smtp.has_extn("auth")


def test_a_rebinding_answer_is_never_used(deployed, net, monkeypatch):
    """The name is looked up once; the tls layer gets the checked IP and does no second lookup."""
    net["scripts"] = [b""]
    _context(monkeypatch, IMAP_GREETING)
    TlsMailConnector().open_imap("rebind.example.com", 993)
    assert net["lookups"] == 1 and net["connections"] == [(PUBLIC, 993)]


def test_falls_back_to_the_next_checked_address_only(deployed, monkeypatch):
    tried = []

    def create_connection(address, timeout=None, source_address=None):
        tried.append(address)
        if address[0] == "2606:4700::1111":
            raise OSError("network unreachable")
        return FakeSocket(address[0])

    monkeypatch.setattr(socket, "create_connection", create_connection)
    ctx = _context(monkeypatch, IMAP_GREETING)
    TlsMailConnector(resolver=lambda host, port: ["2606:4700::1111", PUBLIC]).open_imap("imap.example.com", 993)
    assert tried == [("2606:4700::1111", 993), (PUBLIC, 993)] and ctx.wrapped == [(PUBLIC, "imap.example.com")]

    tried.clear()
    with pytest.raises(OSError, match="unreachable"):
        TlsMailConnector(resolver=lambda host, port: ["2606:4700::1111"]).open_imap("imap.example.com", 993)
    assert tried == [("2606:4700::1111", 993)]


@pytest.mark.parametrize("answer, reason", [
    (["93.184.216.34", "10.0.0.7"], "private"),
    (["169.254.169.254"], "metadata"),
    (["127.0.0.1"], "private"),
])
def test_unsafe_hosts_are_refused_before_connecting(deployed, net, answer, reason):
    connector = TlsMailConnector(resolver=lambda host, port: answer)
    with pytest.raises(tls.UnsafeMailHostError, match=reason) as info:
        connector.open_smtp("mail.example.com", 587, use_ssl=False, timeout=5)
    assert net["connections"] == []
    assert isinstance(info.value, OSError)  # callers' `except OSError` paths still catch it
    assert tls.tls_error_message(info.value).startswith("Mail server not allowed:")


def test_ip_literals_are_checked_too(deployed, net):
    with pytest.raises(tls.UnsafeMailHostError, match="metadata"):
        TlsMailConnector().open_imap("169.254.169.254", 993)
    assert net["lookups"] == 0 and net["connections"] == []


def test_local_setups_are_not_pinned(monkeypatch, net):
    """mailpit / test servers: no lookup here, the libraries connect to the name as before."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert pinned_addresses("mailpit", 1025) is None
    assert net["lookups"] == 0


def test_warmup_imap_uses_the_provided_connector():
    from app.services import warmup_imap

    class Box:
        def login(self, user, password):
            self.login_as = (user, password)

    opened = []

    class Connector:
        def open_imap(self, host, port, *, timeout=None, starttls=None):
            opened.append((host, port, timeout, starttls))
            return Box()

    with providers.override(mail_connector=Connector()):
        box = warmup_imap._connect("imap.example.com", 143, "u", "p")
    assert opened == [("imap.example.com", 143, 30, True)] and box.login_as == ("u", "p")
