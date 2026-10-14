"""Hard cap on OpenAI spend: OPENAI_MAX_USD_PER_DAY (default $0.20 per UTC day).

Spend is kept in micro-dollars in platform_flags, shared by the API and the worker.
Before each call the worst-case cost (estimated prompt tokens + max output tokens) is reserved
atomically; if it would push the day's total over the cap the call is refused with
LLMBudgetExceeded and callers fall back exactly as they do when no key is configured.
After the call the reservation is replaced by the real cost from the response's usage.
Output tokens per call are also clamped (OPENAI_MAX_OUTPUT_TOKENS), and OPENAI_MAX_CALLS_PER_DAY
is a second ceiling. OPENAI_MAX_USD_PER_DAY=0 blocks all calls.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


class LLMBudgetExceeded(RuntimeError):
    pass


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _micro(usd: float) -> int:
    return int(round(usd * 1_000_000))


def _prompt_tokens(messages) -> int:
    chars = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        chars += len(c) if isinstance(c, str) else len(str(c or ""))
    return chars // 3 + 20            # deliberately pessimistic: ~3 chars per token


def _cost_micro(prompt_tokens: int, output_tokens: int) -> int:
    return _micro(prompt_tokens * settings.OPENAI_PRICE_INPUT_PER_M / 1_000_000
                  + output_tokens * settings.OPENAI_PRICE_OUTPUT_PER_M / 1_000_000)


def charge(kwargs: dict) -> Tuple[dict, int]:
    """Reserve the worst-case cost of this call. Returns (kwargs with clamped max_tokens, reserved micro-USD)."""
    kwargs = dict(kwargs)
    cap_tokens = int(settings.OPENAI_MAX_OUTPUT_TOKENS)
    for key in ("max_tokens", "max_completion_tokens"):
        if kwargs.get(key):
            kwargs[key] = min(int(kwargs[key]), cap_tokens)
            out = kwargs[key]
            break
    else:
        kwargs["max_tokens"] = out = cap_tokens
    est = _cost_micro(_prompt_tokens(kwargs.get("messages")), out)
    limit = _micro(settings.OPENAI_MAX_USD_PER_DAY)
    day = _day()
    db = SessionLocal()
    try:
        for name in (f"llm_usd_micro:{day}", f"llm_calls:{day}"):
            db.execute(text("INSERT IGNORE INTO platform_flags (name, value) VALUES (:n, '0')"), {"n": name})
        ok = db.execute(text("UPDATE platform_flags SET value = CAST(value AS UNSIGNED) + :e "
                             "WHERE name = :n AND CAST(value AS UNSIGNED) + :e <= :l"),
                        {"n": f"llm_usd_micro:{day}", "e": est, "l": limit}).rowcount  # type: ignore[attr-defined]
        if ok:
            ok = db.execute(text("UPDATE platform_flags SET value = CAST(value AS UNSIGNED) + 1 "
                                 "WHERE name = :n AND CAST(value AS UNSIGNED) < :l"),
                            {"n": f"llm_calls:{day}", "l": int(settings.OPENAI_MAX_CALLS_PER_DAY)}).rowcount  # type: ignore[attr-defined]
            if not ok:                 # call ceiling hit: give the dollar reservation back
                db.execute(text("UPDATE platform_flags SET value = GREATEST(CAST(value AS SIGNED) - :e, 0) WHERE name = :n"),
                           {"n": f"llm_usd_micro:{day}", "e": est})
        db.commit()
    finally:
        db.close()
    if not ok:
        logger.warning("[LLM] daily cap reached (USD %.2f / %s calls); call refused",
                       settings.OPENAI_MAX_USD_PER_DAY, settings.OPENAI_MAX_CALLS_PER_DAY)
        raise LLMBudgetExceeded("Daily OpenAI spend cap reached")
    return kwargs, est


def settle(reserved_micro: int, response=None) -> None:
    """Replace the reservation with the real cost (or release it if the call failed)."""
    actual = 0
    usage = getattr(response, "usage", None)
    if usage is not None:
        actual = _cost_micro(getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)
    delta = actual - reserved_micro
    if delta == 0:
        return
    db = SessionLocal()
    try:
        db.execute(text("UPDATE platform_flags SET value = GREATEST(CAST(value AS SIGNED) + :d, 0) WHERE name = :n"),
                   {"d": delta, "n": f"llm_usd_micro:{_day()}"})
        db.commit()
    finally:
        db.close()


def spent_today_usd() -> float:
    db = SessionLocal()
    try:
        v = db.execute(text("SELECT value FROM platform_flags WHERE name = :n"), {"n": f"llm_usd_micro:{_day()}"}).scalar()
    finally:
        db.close()
    return int(v or 0) / 1_000_000
