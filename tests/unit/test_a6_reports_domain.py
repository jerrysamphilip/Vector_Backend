"""A6 domain layer (app/domain/reports.py): report scope, periods, sorting whitelist, series, rates and
the custom report catalogue / definition / filter validation. Pure: no database, no fakes."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.domain import reports as d


# ── Scope ──

def test_agents_always_see_themselves():
    assert d.report_owner_scope("AGENT", "me", None, "someone") == "me"


def test_workspace_viewers_get_the_requested_user_or_everyone():
    assert d.report_owner_scope("ADMIN", "me", None, "u2") == "u2"
    assert d.report_owner_scope("ADMIN", "me", None, None) is None


def test_managers_get_their_team_or_one_member_and_403_outside():
    assert d.report_owner_scope("MANAGER", "me", {"me", "b", "a"}, None) == ["a", "b", "me"]
    assert d.report_owner_scope("MANAGER", "me", {"me", "a"}, "a") == "a"
    with pytest.raises(d.ReportInputError) as exc:
        d.report_owner_scope("MANAGER", "me", {"me"}, "x")
    assert exc.value.status == 403 and exc.value.message == "That user is outside your team"


def test_sees_owner():
    assert d.sees_owner(None, "x") and d.sees_owner({"x"}, "x") and not d.sees_owner({"y"}, "x")


# ── Periods ──

def test_date_window_defaults_to_30_days_ending_today():
    w = d.DateWindow.or_default(None, None, date(2025, 5, 31))
    assert (w.start, w.end, w.end_exclusive) == (date(2025, 5, 2), date(2025, 5, 31), date(2025, 6, 1))
    w = d.DateWindow.or_default(None, date(2025, 1, 30), date(2025, 5, 31))
    assert w.start == date(2025, 1, 1)


def test_sales_period_defaults_and_rejects_reversed_range():
    p = d.SalesPeriod.or_default(None, date(2025, 3, 31), date(2030, 1, 1))
    assert p.first == date(2025, 1, 1) and p.end == datetime(2025, 4, 1)
    assert p.as_dict() == {"from": date(2025, 1, 1), "to": date(2025, 3, 31)}
    with pytest.raises(d.ReportInputError, match="date_from is after date_to"):
        d.SalesPeriod.or_default(date(2025, 2, 1), date(2025, 1, 1), date(2025, 1, 1))


def test_quarter_bounds_and_add_months():
    assert d.add_months(date(2025, 11, 15), 3) == date(2026, 2, 1)
    assert d.quarter_bounds(date(2025, 4, 1), date(2026, 4, 1), 2) == (date(2025, 7, 1), date(2025, 10, 1))
    assert d.quarter_bounds(date(2025, 4, 1), date(2026, 4, 1), None) == (date(2025, 4, 1), date(2026, 4, 1))
    p = d.SalesPeriod.of_bounds(date(2025, 7, 1), date(2025, 10, 1))
    assert p.last == date(2025, 9, 30) and p.start == datetime(2025, 7, 1)


def test_rates():
    assert d.rate(1, 3) == 33.33 and d.rate(5, 0) == 0.0
    assert d.pct(1, 3) == 33.3 and d.pct(1, 0) is None and d.pct(0, None) is None


# ── Comparison sort whitelist ──

@pytest.mark.parametrize("sort_by,expected", [("open_rate", "open_rate"), ("campaign_name", "campaign_name"),
                                              ("bogus", "created_at"), (None, "created_at"),
                                              ("campaign_name; DROP TABLE x", "created_at")])
def test_comparison_sort_only_whitelisted_fields(sort_by, expected):
    assert d.ComparisonSort.parse(sort_by, "asc").field == expected


def test_comparison_sort_direction():
    assert d.ComparisonSort.parse("sent_count", "asc").descending is False
    assert d.ComparisonSort.parse("sent_count", "ASC").descending is True
    assert d.ComparisonSort.parse("reply_rate", None).is_rate


# ── Series and medians ──

def test_daily_series_fills_missing_days():
    w = d.DateWindow(date(2025, 5, 1), date(2025, 5, 3))
    pts = d.build_timeseries(w, {date(2025, 5, 2): 4}, {}, {date(2025, 5, 3): 1}, {}, "daily")
    assert [(p.date, p.sent, p.replied) for p in pts] == [("2025-05-01", 0, 0), ("2025-05-02", 4, 0),
                                                          ("2025-05-03", 0, 1)]


def test_weekly_series_sums_7_day_chunks_only_beyond_a_week():
    w = d.DateWindow(date(2025, 5, 1), date(2025, 5, 10))
    sent = {date(2025, 5, i): 1 for i in range(1, 11)}
    pts = d.build_timeseries(w, sent, {}, {}, {}, "weekly")
    assert [(p.date, p.sent) for p in pts] == [("2025-05-01", 7), ("2025-05-08", 3)]
    short = d.DateWindow(date(2025, 5, 1), date(2025, 5, 7))
    assert len(d.build_timeseries(short, sent, {}, {}, {}, "weekly")) == 7


def test_median_hours_upper_middle_of_positive_gaps():
    t = datetime(2025, 5, 1)
    pairs = [(t.replace(hour=5), t), (t.replace(hour=1), t), (t, t), (None, t), (t.replace(hour=3), t)]
    assert d.median_hours(pairs) == 3.0
    assert d.median_hours([]) == 0.0


# ── Custom report catalogue ──

def test_catalogue_and_builders():
    meta = d.catalogue(True)
    assert meta["can_build"] is True and set(meta["objects"]) == {"leads", "deals", "contacts", "activities", "tasks"}
    assert meta["objects"]["deals"]["measures"]["sum_amount"] == "Total amount"
    assert d.can_build_reports("MANAGER", None) and d.can_build_reports("AGENT", 2)
    assert not d.can_build_reports("AGENT", None) and not d.can_build_reports("AGENT", 3)


@pytest.mark.parametrize("spec,message", [
    (d.DefinitionSpec("nope", "x"), "object must be one of"),
    (d.DefinitionSpec("leads", "nope"), "Can group leads by"),
    (d.DefinitionSpec("leads", "stage", group_by_2="nope"), "Can group leads by"),
    (d.DefinitionSpec("leads", "stage", measure="sum_amount"), "measure must be one of"),
])
def test_definition_whitelist(spec, message):
    with pytest.raises(d.ReportInputError, match=message) as exc:
        d.check_definition(spec)
    assert exc.value.status == 400


def test_date_field_and_bounds():
    obj = d.check_definition(d.DefinitionSpec("deals", "stage", measure="avg_amount"))
    d.check_date_field(obj, "close_date")
    with pytest.raises(d.ReportInputError, match="date_field must be one of"):
        d.check_date_field(obj, "occurred_at")
    assert d.date_bounds(date(2025, 5, 1), date(2025, 5, 31)) == (datetime(2025, 5, 1), datetime(2025, 6, 1))
    assert d.date_bounds(None, None) == (None, None)


FIELDS = {"name": "Name", "seats": "Seats", "day": "Day", "kind": "Kind", "owner": "Owner"}
KINDS = {"seats": d.FieldKind("numeric"), "day": d.FieldKind("date"), "kind": d.FieldKind("enum", ("A", "B"))}


def _err(f):
    with pytest.raises(d.ReportInputError) as exc:
        d.check_filter(FIELDS, KINDS, f, "me")
    return exc.value


@pytest.mark.parametrize("value", [{"$gt": 1}, None, [{"a": 1}]])
def test_dict_or_missing_values_are_422(value):
    e = _err({"field": "name", "operator": "is", "value": value})
    assert e.status == 422 and "Name" in e.message


def test_typed_values_are_coerced_or_422():
    assert d.check_filter(FIELDS, KINDS, {"field": "seats", "value": " 12 "}, "me").values == (Decimal("12"),)
    assert "not a number" in _err({"field": "seats", "operator": "is", "value": "lots"}).message
    assert _err({"field": "seats", "operator": "is", "value": True}).status == 422
    assert "not a date" in _err({"field": "day", "operator": "is", "value": "tomorrow"}).message
    assert d.check_filter(FIELDS, KINDS, {"field": "day", "operator": "is_not", "value": "2026-10-18"},
                          "me").values == (datetime(2026, 10, 18),)
    assert "must be one of" in _err({"field": "kind", "value": "C"}).message


def test_lists_only_with_is_or_is_not_and_me_is_the_viewer():
    assert "list of values" in _err({"field": "name", "operator": "contains", "value": ["a"]}).message
    f = d.check_filter(FIELDS, KINDS, {"field": "name", "operator": "is", "value": ["a", 1]}, "me")
    assert f.many and f.values == ("a", "1")
    assert d.check_filter(FIELDS, KINDS, {"field": "name", "operator": "is_empty", "value": {"x": 1}}, "me").values == ()
    assert d.check_filter(FIELDS, KINDS, {"field": "owner", "value": "me"}, "u9").values == ("u9",)


def test_unknown_or_unhashable_field_or_operator_is_400():
    assert _err({"field": {"x": 1}, "operator": "is", "value": "a"}).status == 400
    assert _err({"field": "name", "operator": ["is"], "value": "a"}).status == 400
    assert _err({"field": "column_name", "value": "a"}).message == "Bad filter on column_name"


# ── Result shaping ──

def test_flat_rows_sorted_by_value_or_month():
    rows = d.shape_flat([("a", 1), (None, 5), ("b", 3)], {"a": "Alpha"}, "stage")
    assert [(r["label"], r["value"]) for r in rows] == [("(none)", 5.0), ("b", 3.0), ("Alpha", 1.0)]
    months = d.shape_flat([("2025-06", 1), (None, 2), ("2025-05", 4)], {}, "created_month")
    assert [r["key"] for r in months] == ["2025-05", "2025-06", None]
    assert d.report_total(rows, False) == 9.0 and d.report_total(rows, True) is None


def test_matrix_rows_columns_and_totals():
    rows, cols = d.shape_matrix([("a", "x", 1), ("a", None, 2), ("b", "x", 5)], {"a": "A"}, {"x": "X"}, False)
    assert cols == [{"key": "x", "label": "X"}, {"key": "(none)", "label": "(none)"}]
    assert [(r["key"], r["value"], r["values"]) for r in rows] == [("b", 5.0, {"x": 5.0}),
                                                                   ("a", 3.0, {"x": 1.0, "(none)": 2.0})]
    averaged, _ = d.shape_matrix([("a", "x", 1.234)], {}, {}, True)
    assert averaged[0]["value"] is None and averaged[0]["values"] == {"x": 1.23}
