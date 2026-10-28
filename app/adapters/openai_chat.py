"""
OpenAI chat completions behind a Protocol (ADR 0009 adapter layer).

Only the transport: no prompts, no budget. Callers reserve and settle spend through
app/services/llm_budget.py around ``complete`` (the $0.20/day cap) and fall back to rules when
``chat_client()`` is None (no key configured). Unit tests hand services a fake ``ChatClient``
(constructor argument, or ``app.adapters.providers.override``).

Two shared clients: ``chat_client()`` for short classification calls (20 s timeout, no SDK retries)
and ``generation_chat_client()`` for email generation (the SDK's default timeout and retries, as
app/services/ai_email_service.py always used). ``ChatCompletionsView`` gives a ``ChatClient`` the
SDK's ``.chat.completions.create(...)`` shape for code written against the SDK.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from openai import OpenAI

from app.core.config import settings

LLM_TIMEOUT_SECONDS = 20.0


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        """One chat completion; returns the provider response (``.choices[0].message.content``,
        ``.usage``). Raises on transport / provider errors."""
        ...


class OpenAIChatClient:
    """``timeout`` / ``max_retries`` None leave the SDK defaults."""

    def __init__(self, api_key: str, model: str, *, timeout: Optional[float] = LLM_TIMEOUT_SECONDS,
                 max_retries: Optional[int] = 0) -> None:
        options: Dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        if max_retries is not None:
            options["max_retries"] = max_retries
        self._client = OpenAI(api_key=api_key, **options)
        self._model = model

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        """``model`` and ``temperature`` default to the configured model and 0; kwargs override them."""
        create: Any = self._client.chat.completions.create  # messages are plain dicts; the SDK's overloads are TypedDicts
        params: Dict[str, Any] = {"model": self._model, "temperature": 0}
        params.update(kwargs)
        return create(messages=messages, **params)


class _CompletionsView:
    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def create(self, *, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        return self._client.complete(messages, **kwargs)


class _ChatView:
    def __init__(self, client: ChatClient) -> None:
        self.completions = _CompletionsView(client)


class ChatCompletionsView:
    """A ChatClient seen through the SDK's ``client.chat.completions.create(model=..., messages=...)``."""

    def __init__(self, client: ChatClient) -> None:
        self.client = client
        self.chat = _ChatView(client)


_client: Optional[OpenAIChatClient] = None
_generation_client: Optional[OpenAIChatClient] = None


def chat_client() -> Optional[ChatClient]:
    """The shared client, or None when OPENAI_API_KEY is empty (tests, keyless installs)."""
    global _client
    if not settings.OPENAI_API_KEY:
        return None
    if _client is None:
        _client = OpenAIChatClient(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)
    return _client


def generation_chat_client() -> Optional[ChatClient]:
    """The shared generation client (SDK default timeout / retries), or None without a key."""
    global _generation_client
    if not settings.OPENAI_API_KEY:
        return None
    if _generation_client is None:
        _generation_client = OpenAIChatClient(settings.OPENAI_API_KEY, settings.OPENAI_MODEL,
                                              timeout=None, max_retries=None)
    return _generation_client
