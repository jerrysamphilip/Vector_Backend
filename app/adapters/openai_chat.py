"""
OpenAI chat completions behind a Protocol (ADR 0009 adapter layer).

Only the transport: no prompts, no budget. Callers reserve and settle spend through
app/services/llm_budget.py around ``complete`` (the $0.20/day cap) and fall back to rules when
``chat_client()`` is None (no key configured). Unit tests hand services a fake ``ChatClient``.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from openai import OpenAI

from app.core.config import settings

LLM_TIMEOUT_SECONDS = 20.0


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        """One chat completion; returns the provider response (``.choices[0].message.content``,
        ``.usage``). Raises on transport / provider errors."""
        ...


class OpenAIChatClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)
        self._model = model

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        create: Any = self._client.chat.completions.create  # messages are plain dicts; the SDK's overloads are TypedDicts
        return create(model=self._model, messages=messages, temperature=0, **kwargs)


_client: Optional[OpenAIChatClient] = None


def chat_client() -> Optional[ChatClient]:
    """The shared client, or None when OPENAI_API_KEY is empty (tests, keyless installs)."""
    global _client
    if not settings.OPENAI_API_KEY:
        return None
    if _client is None:
        _client = OpenAIChatClient(settings.OPENAI_API_KEY, settings.OPENAI_MODEL)
    return _client
