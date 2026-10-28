# app/utils/reply_classifier.py
"""
Classification helpers for inbound prospect replies (IMAP sync pipeline):
  - out-of-office / auto-response detection
  - AI-based positive/negative reply intent classification

Both are best-effort: on any failure they return a "don't know" result rather
than raising, since a missed classification should never break IMAP sync.
"""

import logging
import re
from typing import Any, Optional, Protocol, Tuple

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

_openai_client = None
if settings.OPENAI_API_KEY:
    _openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

# RFC 3834 header set by virtually every mail server for genuine auto-responses
# (Exchange/O365 "Automatic replies", Gmail vacation responder, etc).
_AUTO_SUBMITTED_OOO_VALUES = {"auto-replied", "auto-generated"}

_OOO_TEXT_PATTERN = re.compile(
    r"(out[\s-]of[\s-](the[\s-])?office|automatic reply|auto-?reply|"
    r"away from (the )?office|on (annual )?leave|on vacation|"
    r"currently (unavailable|out)|vacation (auto-?)?respon|"
    r"away from my (desk|email)|limited access to (my )?email|"
    r"returning (to the office )?on)",
    re.IGNORECASE,
)


def is_out_of_office(subject: str, body: str, auto_submitted_header: Optional[str] = None) -> bool:
    """
    Detect an out-of-office / vacation auto-response.

    Prefers the `Auto-Submitted` header when the sending server sets it;
    falls back to subject/body keyword matching for servers that don't.
    """
    header_value = (auto_submitted_header or "").strip().lower()
    if header_value in _AUTO_SUBMITTED_OOO_VALUES:
        return True

    haystack = f"{subject or ''}\n{(body or '')[:500]}"
    return bool(_OOO_TEXT_PATTERN.search(haystack))


class LLMBudget(Protocol):
    """The spend-cap hooks (app.services.llm_budget satisfies it): ``charge`` reserves the call's
    worst-case cost and raises when the daily cap is reached; ``settle`` books the real cost."""

    def charge(self, kwargs: dict) -> Tuple[dict, int]: ...

    def settle(self, reserved_micro: int, response: Any = None) -> None: ...


def classify_reply_intent(subject: str, body: str, budget: Optional[LLMBudget] = None) -> Optional[str]:
    """
    Classify a prospect's reply as 'positive', 'neutral', or 'negative'.

    Returns None when classification is unavailable (no API key configured,
    no LLM budget given, empty body, daily cap reached) or the call fails —
    callers must treat None as "unknown", not as a negative/neutral result.
    The caller passes the LLM budget (app.services.llm_budget); without it no
    call is made, so every LLM call stays under the daily spend cap.
    """
    if not _openai_client or budget is None or not (body or "").strip():
        return None

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Classify a prospect's email reply to a cold B2B outreach email. "
                    "Respond with exactly one word, lowercase, nothing else:\n"
                    "positive - shows buying interest, agrees to a call/meeting, asks "
                    "for more info/pricing, or otherwise wants to move forward\n"
                    "negative - declines, says not interested, asks to stop emailing, "
                    "or is hostile\n"
                    "neutral - anything else (auto-generated text, unclear, unrelated "
                    "question, deferral without commitment)"
                ),
            },
            {"role": "user", "content": f"Subject: {subject or ''}\n\nReply:\n{(body or '')[:1500]}"},
        ]
        try:
            kwargs, reserved = budget.charge({"messages": messages, "max_tokens": 5})
        except Exception as exc:  # daily cap reached (LLMBudgetExceeded) or the ledger is unavailable
            logger.info(f"[ReplyClassifier] Intent classification skipped: {exc}")
            return None
        try:
            response = _openai_client.chat.completions.create(
                model=settings.OPENAI_MODEL, temperature=0, **kwargs)
        except Exception:
            budget.settle(reserved, None)
            raise
        budget.settle(reserved, response)
        label = (response.choices[0].message.content or "").strip().lower()
        return label if label in ("positive", "neutral", "negative") else None
    except Exception as e:
        logger.warning(f"[ReplyClassifier] Intent classification failed: {e}")
        return None
