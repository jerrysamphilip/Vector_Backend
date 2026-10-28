"""A3: pure sales domain rules and query objects (app/domain/sales.py)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.domain import sales as d
from app.domain.paging import SortDirection


def stage(prob=10, won=False, lost=False):
    return SimpleNamespace(probability=prob, is_won=won, is_lost=lost)


def test_stage_status_and_category():
    assert d.stage_status(stage()) == "OPEN"
    assert d.stage_status(stage(won=True)) == "WON"
    assert d.stage_status(stage(lost=True)) == "LOST"
    assert d.StageStatus.of(True, False) is d.StageStatus.WON
    assert d.category_for(stage(100, won=True)) == "CLOSED"
    assert d.category_for(stage(0, lost=True)) == "OMITTED"
    assert d.category_for(stage(70)) == "COMMIT"
    assert d.category_for(stage(40)) == "BEST_CASE"
    assert d.category_for(stage(39)) == "PIPELINE"
    assert set(d.FORECAST_CATEGORIES) == {c.value for c in d.ForecastCategory}


def test_lead_stage_transitions():
    full = {k: True for k in d.SQL_CRITERIA}
    assert d.lead_stage_error("NEW", "BOGUS", None, None).startswith("stage must be one of")
    assert d.lead_stage_error("NEW", "NEW", None, None) is None
    assert "already converted" in d.lead_stage_error("CONVERTED", "NEW", None, None)
    assert "Use Convert" in d.lead_stage_error("NEW", "CONVERTED", None, None)
    assert d.lead_stage_error("NEW", "SQL", {"budget": True}, None).startswith("To mark as SQL, confirm:")
    assert d.lead_stage_error("NEW", "SQL", full, None) is None
    assert "reason" in d.lead_stage_error("NEW", "DISQUALIFIED", None, "  ")
    assert d.lead_stage_error("NEW", "DISQUALIFIED", None, "No budget") is None
    assert d.missing_criteria(full) == []


def test_lifecycle_only_moves_forward():
    order = ["LEAD", "MQL", "SQL", "OPPORTUNITY", "CUSTOMER"]
    assert d.lifecycle_moves_forward(order, "LEAD", "SQL")
    assert not d.lifecycle_moves_forward(order, "SQL", "LEAD")
    assert not d.lifecycle_moves_forward(order, "SQL", "SQL")
    assert not d.lifecycle_moves_forward(order, "SQL", None)
    assert d.lifecycle_moves_forward(order, None, "LEAD")


def test_stale_reason_and_idle_days():
    today = date(2026, 10, 27)
    assert d.stale_reason(3, 14, None, today) is None
    assert d.stale_reason(20, 14, None, today) == "no activity for 20 days"
    assert d.stale_reason(1, 14, date(2026, 10, 1), today) == "close date 2026-10-01 has passed"
    assert d.stale_reason(20, 14, date(2026, 10, 1), today) == \
        "no activity for 20 days and close date 2026-10-01 has passed"
    now = datetime(2026, 10, 27, 12)
    assert d.idle_days(now - timedelta(days=5), now - timedelta(days=2), now) == 2
    assert d.idle_days(None, None, now) == 0
    assert d.idle_days(now + timedelta(days=1), None, now) == 0   # clock skew never goes negative


def test_parse_amount():
    assert d.parse_amount(None) is None and d.parse_amount("") is None
    assert d.parse_amount("1,234.567") == Decimal("1234.57")
    with pytest.raises(ValueError, match="number"):
        d.parse_amount("abc")
    with pytest.raises(ValueError, match="negative"):
        d.parse_amount(-1)


def test_stage_write_and_hierarchy_rules():
    assert d.stage_write_error({"probability": 101}) == "Probability must be between 0 and 100"
    assert d.stage_write_error({"is_won": True, "is_lost": True}) == "A stage cannot be both won and lost"
    assert d.stage_write_error({"probability": None, "name": "x"}) is None
    assert d.hierarchy_error(2, None, False, False) == "Levels 2 to 4 need a manager"
    assert d.hierarchy_error(1, None, False, False) is None
    assert "higher level" in d.hierarchy_error(2, 2, True, False)
    assert "higher level" in d.hierarchy_error(2, None, True, False)
    assert "own manager" in d.hierarchy_error(3, 1, True, True)
    assert d.hierarchy_error(3, 1, True, False) is None


def test_owner_scope():
    assert d.EVERYONE.unrestricted and d.EVERYONE.allows("x") and d.EVERYONE.allows(None)
    s = d.OwnerScope(frozenset({"a"}), include_unowned=True)
    assert s.allows("a") and s.allows(None) and not s.allows("b")
    assert not d.OwnerScope(frozenset({"a"})).allows(None)


def test_lead_filter_from_params():
    f = d.LeadFilter.from_params("me-id", stage="NEW,SQL", owner="me", q="  ann ", source="", open_only=True)
    assert f.stages == ("NEW", "SQL") and f.effective_stages == ("NEW", "SQL")
    assert f.owner_id == "me-id" and f.search == "ann" and f.source is None
    g = d.LeadFilter.from_params("u", open_only=True, q="   ")
    assert g.effective_stages == d.OPEN_LEAD_STAGES and g.search is None
    assert g.with_stage("SQL").effective_stages == ("SQL",)
    assert d.LeadFilter.from_params("u", owner="other").owner_id == "other"


def test_opportunity_filter_and_list_query():
    f = d.OpportunityFilter.from_params("u", owner="me", status="OPEN,WON", q="acme")
    assert f.owner_id == "u" and f.statuses == ("OPEN", "WON") and f.search == "acme"
    g = f.replace(stage_ids=("s1",))
    assert g.stage_ids == ("s1",) and g.statuses == f.statuses
    q = d.ListQuery.build("amount", "desc", 2, 25, d.OPPORTUNITY_SORT_FIELDS)
    assert q.sort.direction is SortDirection.DESC and q.paging.offset == 25
    with pytest.raises(ValueError):
        d.ListQuery.build("password", "asc", 1, 10, d.LEAD_SORT_FIELDS)
    assert d.ApprovalQuery(status="ALL").statuses == ("PENDING", "APPROVED", "REJECTED")
    assert d.ApprovalQuery().statuses == ("PENDING",)


def test_naive_utc_and_entity_ids():
    aware = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    assert d.naive_utc(aware) == datetime(2026, 1, 1, 10)
    assert d.naive_utc(None) is None
    u = "8a7fe580-5736-42fc-b78f-95175c72df0b"
    assert d.parse_entity_id(u) == UUID(u)
    assert d.parse_entity_id(u.upper()) == UUID(u)
    assert d.parse_entity_id("8a7fe580573642fcb78f95175c72df0b") is None   # hyphenless never matched a row
    assert d.parse_entity_id("does-not-exist") is None


def test_stage_change_message():
    kind, title, body = d.stage_change_message("Big", "Won", "WON", "Ann", None, "Fit")
    assert (kind, title) == ("DEAL_WON", "Deal won: Big") and body.endswith("Reason: Fit")
    assert d.stage_change_recipients("o", "m", "o") == ["m"]
