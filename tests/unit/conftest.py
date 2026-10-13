"""
Unit layer guard rails: pure logic only.

- The environment is fixed before any app module is imported (no .env/OpenAI key can leak in).
- Every unit test runs with outbound sockets and SQLAlchemy connections blocked: a test that
  touches the network or the database fails loudly with UnitTestIsolationError instead of
  quietly depending on a running stack.
- Every test under tests/unit is marked `unit` (also when tests/conftest.py is not loaded).

Run:  python -m pytest tests/unit -q        or   python3 scripts/test.py run unit
"""
import os
import socket
import sys

# ── Environment, before anything imports app.core.config ──
# (In a full `python -m pytest` run tests/conftest.py has already configured and imported the app;
#  then nothing here is changed, so the functional tests keep their real database settings.)
if "app.core.config" not in sys.modules:
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("RUN_BACKGROUND_JOBS", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "unit-only-jwt-secret-0123456789abcdef0123456789")
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", "unit-only-credentials-key")
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    # Point the (lazily connecting) engine at a closed port, so even a missed guard cannot reach a DB.
    os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
    os.environ.setdefault("MYSQL_PORT", "9")
# Tests must never call OpenAI: an empty key disables every LLM code path (env beats .env).
os.environ["OPENAI_API_KEY"] = ""

import pytest  # noqa: E402


class UnitTestIsolationError(RuntimeError):
    """A unit test tried to use the network or the database."""


_ALLOWED_FAMILIES = {getattr(socket, "AF_UNIX", None)}


def _blocked(*_a, **_k):
    raise UnitTestIsolationError("unit tests must not open network connections (move the test to tests/ "
                                 "as a functional test, or mock the call)")


@pytest.fixture(autouse=True)
def _no_network_no_db(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, *args, **kwargs):
        if self.family in _ALLOWED_FAMILIES:
            return real_connect(self, *args, **kwargs)
        _blocked()

    def guarded_connect_ex(self, *args, **kwargs):
        if self.family in _ALLOWED_FAMILIES:
            return real_connect_ex(self, *args, **kwargs)
        _blocked()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    try:
        from sqlalchemy.engine import Engine
    except ImportError:  # pragma: no cover - sqlalchemy is a hard dependency of the app
        Engine = None
    if Engine is not None:
        def no_db(*_a, **_k):
            raise UnitTestIsolationError("unit tests must not use the database (use a functional test in tests/)")
        monkeypatch.setattr(Engine, "connect", no_db)
        monkeypatch.setattr(Engine, "raw_connection", no_db)
    yield


def pytest_collection_modifyitems(config, items):
    here = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        if str(item.fspath).startswith(here + os.sep) and not item.get_closest_marker("unit"):
            item.add_marker(pytest.mark.unit)
