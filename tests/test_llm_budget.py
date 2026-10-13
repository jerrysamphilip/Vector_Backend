"""The OpenAI call cap: refuses past the daily limit and clamps output tokens."""
import pytest

from app.core.config import settings
from app.services import llm_budget
from app.services.llm_budget import LLMBudgetExceeded, charge


def test_daily_cap_refuses_extra_calls_and_clamps_tokens(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_MAX_CALLS_PER_DAY", 2)
    monkeypatch.setattr(settings, "OPENAI_MAX_OUTPUT_TOKENS", 300)
    monkeypatch.setattr(llm_budget, "datetime", type("D", (), {"now": staticmethod(
        lambda tz=None: __import__("datetime").datetime(2001, 1, 1))}))
    from app.core.database import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as db:
        db.execute(text("DELETE FROM platform_flags WHERE name LIKE 'llm_calls:2001%'"))
        db.commit()
    assert charge({"max_tokens": 5000})["max_tokens"] == 300
    assert charge({})["max_tokens"] == 300
    with pytest.raises(LLMBudgetExceeded):
        charge({})
