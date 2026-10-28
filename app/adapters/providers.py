"""
Providers for the external-service adapters (ADR 0009, chunk S2; C-9: no module patching in tests).

Each getter returns the real implementation unless it is overridden:

    email_transport()          EmailTransport   Amazon SES (app/adapters/ses.py)
    mail_connector()           MailConnector    mailbox SMTP / IMAP over verified TLS (app/adapters/mail.py)
    chat_client()              ChatClient|None  OpenAI, short classification calls (None without a key)
    generation_chat_client()   ChatClient|None  OpenAI, email generation (None without a key)
    attachment_store()         FileStore        template attachments (settings.ATTACHMENTS_DIR)
    import_store()             FileStore        uploaded import files (IMPORT_STORAGE_DIR, else uploads/imports)
    oauth_http()               OAuthHttp        Microsoft 365 / Google OAuth and API calls (app/adapters/m365.py)

Routes take them as FastAPI dependencies (``Depends(providers.attachment_store)``), so tests use
``app.dependency_overrides[providers.attachment_store] = lambda: fake``. Code outside a request (the
worker, services called from jobs) calls the getter, and tests swap the implementation with

    with providers.override(email_transport=FakeTransport(), chat_client=None):
        ...

which is process-wide for the duration of the block (restored on exit, also on errors). The getters
take no arguments so they can be FastAPI dependencies.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from app.adapters import openai_chat
from app.adapters.m365 import HttpxOAuthHttp, OAuthHttp
from app.adapters.mail import MailConnector, TlsMailConnector
from app.adapters.openai_chat import ChatClient
from app.adapters.ses import EmailTransport, ses_transport
from app.adapters.storage import FileStore, LocalFileStore
from app.core.config import settings

_MISSING: Any = object()
_overrides: Dict[str, Any] = {}

_mail_connector = TlsMailConnector()
_oauth_http = HttpxOAuthHttp()


def _resolve(name: str, real: Callable[[], Any]) -> Any:
    value = _overrides.get(name, _MISSING)
    return real() if value is _MISSING else value


def email_transport() -> EmailTransport:
    transport: EmailTransport = _resolve("email_transport", ses_transport)
    return transport


def mail_connector() -> MailConnector:
    connector: MailConnector = _resolve("mail_connector", lambda: _mail_connector)
    return connector


def chat_client() -> Optional[ChatClient]:
    client: Optional[ChatClient] = _resolve("chat_client", openai_chat.chat_client)
    return client


def generation_chat_client() -> Optional[ChatClient]:
    client: Optional[ChatClient] = _resolve("generation_chat_client", openai_chat.generation_chat_client)
    return client


def attachment_store() -> FileStore:
    store: FileStore = _resolve("attachment_store", lambda: LocalFileStore(settings.ATTACHMENTS_DIR))
    return store


def _local_import_store() -> LocalFileStore:
    root = getattr(settings, "IMPORT_STORAGE_DIR", None) or str(Path(settings.ATTACHMENTS_DIR).parent / "imports")
    return LocalFileStore(root)


def import_store() -> FileStore:
    store: FileStore = _resolve("import_store", _local_import_store)
    return store


def oauth_http() -> OAuthHttp:
    http: OAuthHttp = _resolve("oauth_http", lambda: _oauth_http)
    return http


PROVIDERS = frozenset({"email_transport", "mail_connector", "chat_client", "generation_chat_client",
                       "attachment_store", "import_store", "oauth_http"})


@contextmanager
def override(**implementations: Any) -> Iterator[None]:
    """Swap provider implementations for the duration of the block (None is a valid value for the
    chat clients: "no key configured"). Unknown names raise TypeError."""
    unknown = set(implementations) - PROVIDERS
    if unknown:
        raise TypeError(f"unknown provider(s): {', '.join(sorted(unknown))}")
    saved = {name: _overrides.get(name, _MISSING) for name in implementations}
    _overrides.update(implementations)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is _MISSING:
                _overrides.pop(name, None)
            else:
                _overrides[name] = value
