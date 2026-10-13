"""Hard cap on OpenAI usage.

Every chat call goes through charge(): a per-day call counter kept in platform_flags (shared by
the API and the worker) and a ceiling on output tokens per call. When the day's allowance is used
up the call is refused with LLMBudgetExceeded, and callers fall back exactly as they do when no key
is configured. OPENAI_MAX_CALLS_PER_DAY=0 blocks all calls.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


class LLMBudgetExceeded(RuntimeError):
    pass


def charge(kwargs: dict) -> dict:
    """Reserve one call for today and clamp max_tokens; returns the (possibly adjusted) kwargs."""
    limit = int(settings.OPENAI_MAX_CALLS_PER_DAY)
    cap = int(settings.OPENAI_MAX_OUTPUT_TOKENS)
    name = "llm_calls:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        # Atomic: only increments while below the limit, so concurrent processes cannot overshoot
        db.execute(text("INSERT IGNORE INTO platform_flags (name, value) VALUES (:n, '0')"), {"n": name})
        n = db.execute(text("UPDATE platform_flags SET value = CAST(value AS UNSIGNED) + 1 "
                            "WHERE name = :n AND CAST(value AS UNSIGNED) < :l"), {"n": name, "l": limit}).rowcount
        db.commit()
    finally:
        db.close()
    if not n:
        logger.warning("[LLM] daily call cap of %s reached; call refused", limit)
        raise LLMBudgetExceeded(f"Daily OpenAI call cap of {limit} reached")
    kwargs = dict(kwargs)
    for key in ("max_tokens", "max_completion_tokens"):
        if key in kwargs and kwargs[key]:
            kwargs[key] = min(int(kwargs[key]), cap)
            break
    else:
        kwargs["max_tokens"] = cap
    return kwargs
