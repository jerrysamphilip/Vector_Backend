"""S2: adapter providers and the process-wide override used by non-request code (C-9)."""
import pytest

from app.adapters import openai_chat, providers
from app.adapters.m365 import HttpxOAuthHttp
from app.adapters.mail import TlsMailConnector
from app.adapters.ses import SesEmailTransport
from app.adapters.storage import LocalFileStore
from app.core.config import settings


def test_real_implementations_by_default(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "ATTACHMENTS_DIR", "/tmp/s2-attachments")
    assert isinstance(providers.mail_connector(), TlsMailConnector)
    assert isinstance(providers.oauth_http(), HttpxOAuthHttp)
    store = providers.attachment_store()
    assert isinstance(store, LocalFileStore) and str(store.root) == "/tmp/s2-attachments"
    if "IMPORT_STORAGE_DIR" in type(settings).model_fields:  # added by the import chunk
        monkeypatch.setattr(settings, "IMPORT_STORAGE_DIR", "")
    assert str(providers.import_store().root) == "/tmp/imports"  # next to the attachments
    # empty key: no LLM client, callers use their rule-based fallback
    assert providers.chat_client() is None
    assert providers.generation_chat_client() is None


def test_email_transport_wraps_the_shared_client(monkeypatch):
    shared = object()
    monkeypatch.setattr(providers, "ses_transport", lambda: SesEmailTransport(shared))
    assert providers.email_transport().client is shared


def test_override_swaps_and_restores():
    fake, inner = object(), object()
    real = providers.mail_connector()
    with providers.override(mail_connector=fake, chat_client=None):
        assert providers.mail_connector() is fake
        assert providers.chat_client() is None
        with providers.override(mail_connector=inner):
            assert providers.mail_connector() is inner
        assert providers.mail_connector() is fake
    assert providers.mail_connector() is real


def test_override_restores_after_an_error():
    fake = object()
    with pytest.raises(RuntimeError):
        with providers.override(oauth_http=fake):
            raise RuntimeError("boom")
    assert providers.oauth_http() is not fake


def test_override_rejects_unknown_names():
    with pytest.raises(TypeError, match="unknown provider"):
        with providers.override(smtp=object()):
            pass


def test_override_none_beats_a_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(openai_chat, "_client", None)
    with providers.override(chat_client=None, generation_chat_client=None):
        assert providers.chat_client() is None
        assert providers.generation_chat_client() is None
    assert openai_chat._client is None  # nothing was built inside the block


def test_getters_are_fastapi_dependencies():
    """Routes take providers with Depends(...): tests replace them with app.dependency_overrides."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/probe")
    def probe(store=Depends(providers.attachment_store)):
        return {"kind": type(store).__name__}

    class Fake:
        pass

    app.dependency_overrides[providers.attachment_store] = lambda: Fake()
    assert TestClient(app).get("/probe").json() == {"kind": "Fake"}
