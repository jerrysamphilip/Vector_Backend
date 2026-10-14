"""Financial-year maths and small sales rules (app/services/sales.py, BRD 5.9: FY runs April-March)."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.core.errors import BadRequestError

from app.core.config import settings
from app.models.sales import SQL_CRITERIA
from app.services import sales


@pytest.fixture
def fy_start(monkeypatch):
    def set_month(month):
        monkeypatch.setattr(settings, "FISCAL_YEAR_START_MONTH", month)
    return set_month


def test_april_fiscal_year(fy_start):
    fy_start(4)
    assert sales.fiscal_label(2026) == "FY 2026-27"
    assert sales.fiscal_year_bounds(2026) == (date(2026, 4, 1), date(2027, 4, 1))
    assert sales.fiscal_year_of(date(2026, 3, 31)) == 2025
    assert sales.fiscal_year_of(date(2026, 4, 1)) == 2026


@pytest.mark.parametrize("month,quarter", [(4, 1), (6, 1), (7, 2), (9, 2), (10, 3), (12, 3), (1, 4), (3, 4)])
def test_april_fiscal_quarters(fy_start, month, quarter):
    fy_start(4)
    assert sales.fiscal_quarter_of(date(2026, month, 15)) == quarter


def test_calendar_fiscal_year(fy_start):
    fy_start(1)
    assert sales.fiscal_label(2026) == "FY 2026"
    assert sales.fiscal_year_bounds(2026) == (date(2026, 1, 1), date(2027, 1, 1))
    assert sales.fiscal_year_of(date(2026, 12, 31)) == 2026
    assert sales.fiscal_quarter_of(date(2026, 3, 31)) == 1
    assert sales.fiscal_quarter_of(date(2026, 10, 1)) == 4


def test_july_fiscal_year(fy_start):
    fy_start(7)
    assert sales.fiscal_label(2026) == "FY 2026-27"
    assert sales.fiscal_year_bounds(2026) == (date(2026, 7, 1), date(2027, 7, 1))
    assert sales.fiscal_year_of(date(2026, 6, 30)) == 2025
    assert sales.fiscal_quarter_of(date(2026, 7, 1)) == 1
    assert sales.fiscal_quarter_of(date(2026, 10, 1)) == 2
    assert sales.fiscal_quarter_of(date(2027, 6, 30)) == 4


def test_fiscal_label_century_rollover(fy_start):
    fy_start(4)
    assert sales.fiscal_label(2099) == "FY 2099-00"


def test_bounds_are_half_open_and_contiguous(fy_start):
    fy_start(4)
    assert sales.fiscal_year_bounds(2025)[1] == sales.fiscal_year_bounds(2026)[0]


@pytest.mark.parametrize("value,expected", [
    ("1,250,000", Decimal("1250000.00")), (" 99.5 ", Decimal("99.50")), (12, Decimal("12.00")),
    ("12.346", Decimal("12.35")), (None, None), ("", None)])
def test_money_parsing(value, expected):
    assert sales.money(value) == expected


@pytest.mark.parametrize("value", ["-1", "abc", "1.2.3"])
def test_money_rejects_negative_and_non_numbers(value):
    with pytest.raises(BadRequestError) as exc:
        sales.money(value)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("won,lost,prob,category", [
    (True, False, 100, "CLOSED"), (False, True, 0, "OMITTED"), (False, False, 70, "COMMIT"),
    (False, False, 69, "BEST_CASE"), (False, False, 40, "BEST_CASE"), (False, False, 39, "PIPELINE")])
def test_default_forecast_category(won, lost, prob, category):
    assert sales.category_for(SimpleNamespace(is_won=won, is_lost=lost, probability=prob)) == category


def test_stage_status():
    assert sales.stage_status(SimpleNamespace(is_won=True, is_lost=False)) == "WON"
    assert sales.stage_status(SimpleNamespace(is_won=False, is_lost=True)) == "LOST"
    assert sales.stage_status(SimpleNamespace(is_won=False, is_lost=False)) == "OPEN"


def test_sql_criteria_missing():
    assert sales.criteria_met(None) == list(SQL_CRITERIA.values())
    assert sales.criteria_met({k: True for k in SQL_CRITERIA}) == []
    first = next(iter(SQL_CRITERIA))
    assert SQL_CRITERIA[first] not in sales.criteria_met({first: True})


def test_as_float():
    assert sales.as_float(None) == 0.0
    assert sales.as_float(Decimal("2.50")) == 2.5
