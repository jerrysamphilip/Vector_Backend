"""Unit tests for the CRM KPI maths (BRD v2.0 section 8): percentages, forecast accuracy (BRD and the
older variance measure), targets, week windows and weekly adoption, quarter selection for forecast
accuracy, the KPI record, the reply-to-lead note and the once-per-day activity cache."""
from datetime import date

import pytest

from app.core.config import settings
from app.services import crm_kpis as kp
from app.services import user_activity as ua


# ── Percentages ──────────────────────────────────────────────

@pytest.mark.parametrize("part,whole,expected", [
    (1, 3, 33.3), (2, 3, 66.7), (0, 5, 0.0), (5, 5, 100.0), (7, 4, 175.0), (1, 1000, 0.1), (1, 2001, 0.0),
])
def test_pct(part, whole, expected):
    assert kp.pct(part, whole) == expected


@pytest.mark.parametrize("whole", [0, None])
def test_pct_without_denominator_is_none(whole):
    assert kp.pct(3, whole) is None


# ── Forecast accuracy ────────────────────────────────────────

def test_forecast_accuracy_is_actual_over_committed():
    assert kp.forecast_accuracy_pct(1500, 2000) == 75.0
    assert kp.forecast_accuracy_pct(2000, 2000) == 100.0
    assert kp.forecast_accuracy_pct(0, 2000) == 0.0


def test_forecast_accuracy_can_exceed_100():
    assert kp.forecast_accuracy_pct(3000, 2000) == 150.0


@pytest.mark.parametrize("committed", [0, 0.0, None, -10])
def test_forecast_accuracy_none_without_commitment(committed):
    assert kp.forecast_accuracy_pct(500, committed) is None


def test_variance_accuracy_keeps_old_formula():
    assert kp.variance_accuracy_pct(1500, 2000) == 75.0
    assert kp.variance_accuracy_pct(2500, 2000) == 75.0       # over-delivery is penalised too
    assert kp.variance_accuracy_pct(5000, 2000) == -50.0
    assert kp.variance_accuracy_pct(100, 0) is None


# ── Targets ──────────────────────────────────────────────────

LT1 = {"operator": "<", "value": 1.0, "label": "< 1%"}


@pytest.mark.parametrize("value,expected", [(0.0, True), (0.9, True), (1.0, False), (12.5, False), (None, None)])
def test_meets_duplicate_target(value, expected):
    assert kp.meets_target(value, LT1) is expected


def test_meets_target_other_operators_and_no_target():
    assert kp.meets_target(50, {"operator": ">=", "value": 50}) is True
    assert kp.meets_target(49.9, {"operator": ">", "value": 50}) is False
    assert kp.meets_target(50, {"operator": "<=", "value": 50}) is True
    assert kp.meets_target(50, None) is None


# ── Weeks and adoption ───────────────────────────────────────

def test_week_start_is_monday():
    assert kp.week_start(date(2026, 10, 13)) == date(2026, 10, 12)   # Tuesday -> Monday
    assert kp.week_start(date(2026, 10, 12)) == date(2026, 10, 12)
    assert kp.week_start(date(2026, 10, 18)) == date(2026, 10, 12)   # Sunday belongs to the same week
    assert kp.week_start(date(2027, 1, 1)) == date(2026, 12, 28)     # across a year end


def test_week_windows_nine_weeks_oldest_first():
    w = kp.week_windows(date(2026, 10, 13))
    assert len(w) == kp.ADOPTION_WEEKS == 9
    assert w[-1] == (date(2026, 10, 12), date(2026, 10, 19))
    assert w[0] == (date(2026, 8, 17), date(2026, 8, 24))
    assert all(b == c for (_, b), (c, _) in zip(w, w[1:]))           # contiguous


def test_adoption_weeks_counts_distinct_licensed_users():
    windows = kp.week_windows(date(2026, 10, 13), 3)
    licensed = {"a", "b", "c", "d"}
    activity = [
        ("a", date(2026, 10, 12)), ("a", date(2026, 10, 13)),       # twice in a week counts once
        ("b", date(2026, 10, 13)),
        ("x", date(2026, 10, 13)),                                   # not licensed (deactivated): ignored
        ("c", date(2026, 10, 4)),                                    # Sunday of the previous week
        ("d", date(2026, 9, 28)), ("c", date(2026, 9, 30)),
        ("a", date(2026, 9, 1)),                                     # outside the windows
    ]
    weeks = kp.adoption_weeks(activity, licensed, windows, today=date(2026, 10, 13))
    assert [(w["week_start"], w["active_users"], w["value"], w["partial"]) for w in weeks] == [
        (date(2026, 9, 28), 2, 50.0, False),
        (date(2026, 10, 5), 0, 0.0, False),
        (date(2026, 10, 12), 2, 50.0, True),
    ]
    assert weeks[1]["week_end"] == date(2026, 10, 11) and all(w["licensed_users"] == 4 for w in weeks)


def test_adoption_weeks_without_licensed_users():
    weeks = kp.adoption_weeks([("a", date(2026, 10, 13))], set(), kp.week_windows(date(2026, 10, 13), 1),
                              today=date(2026, 10, 13))
    assert weeks[0]["value"] is None and weeks[0]["active_users"] == 0


# ── Quarter selection ────────────────────────────────────────

@pytest.fixture
def april_fy(monkeypatch):
    monkeypatch.setattr(settings, "FISCAL_YEAR_START_MONTH", 4)


def test_forecast_quarters_explicit_quarter(april_fy):
    assert kp.forecast_quarters(2026, 3, date(2026, 10, 13)) == [date(2026, 10, 1)]
    assert kp.forecast_quarters(2026, 4, date(2026, 10, 13)) == [date(2027, 1, 1)]


def test_forecast_quarters_whole_year(april_fy):
    assert kp.forecast_quarters(2026, None, date(2026, 10, 13)) == [
        date(2026, 4, 1), date(2026, 7, 1), date(2026, 10, 1), date(2027, 1, 1)]


def test_forecast_quarters_date_range_uses_last_finished_quarter(april_fy):
    assert kp.forecast_quarters(None, None, date(2026, 10, 13)) == [date(2026, 7, 1)]   # Q2 ended 30 Sep
    assert kp.forecast_quarters(None, None, date(2026, 9, 30)) == [date(2026, 7, 1)]    # ends that very day
    assert kp.forecast_quarters(None, None, date(2026, 9, 29)) == [date(2026, 4, 1)]


def test_forecast_quarters_calendar_year(monkeypatch):
    monkeypatch.setattr(settings, "FISCAL_YEAR_START_MONTH", 1)
    assert kp.forecast_quarters(None, None, date(2026, 2, 10)) == [date(2025, 10, 1)]
    assert kp.forecast_quarters(2026, 1, date(2026, 2, 10)) == [date(2026, 1, 1)]


def test_add_months_wraps_years():
    assert kp.add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
    assert kp.add_months(date(2026, 1, 1), -3) == date(2025, 10, 1)


# ── KPI record and notes ─────────────────────────────────────

def test_kpi_record_shape():
    k = kp.kpi("duplicate_contact_rate", 0.5, 1, 200, period={"as_of": date(2026, 10, 13)}, scope="workspace",
               target=LT1, numerator_label="Duplicate contacts", denominator_label="Live contacts")
    assert k["key"] == "duplicate_contact_rate" and k["label"] == "Duplicate contact rate"
    assert k["formula"] == "duplicate contacts ÷ total contacts × 100"
    assert (k["value"], k["numerator"], k["denominator"], k["unit"]) == (0.5, 1, 200, "%")
    assert k["meets_target"] is True and k["available"] is True and k["note"] is None


def test_every_kpi_has_a_definition():
    assert set(kp.KPI_ORDER) == set(kp.DEFINITIONS)
    for d in kp.DEFINITIONS.values():
        assert d["label"] and "× 100" in d["formula"] and len(d["definition"]) > 40


def test_reply_to_lead_note():
    assert kp.reply_to_lead_note(3, False) is None
    assert "not configured" in kp.reply_to_lead_note(0, False)
    assert kp.reply_to_lead_note(0, True) == "No positive replies in the period."


# ── Activity cache: one write per user per day per process ───

def test_should_record_once_per_user_per_day():
    ua.forget()
    d1, d2 = date(2026, 10, 13), date(2026, 10, 14)
    assert ua.should_record("u1", d1) is True
    assert ua.should_record("u1", d1) is False
    assert ua.should_record("u2", d1) is True
    assert ua.should_record("u1", d2) is True          # a new day writes again
    ua.forget("u1")
    assert ua.should_record("u1", d2) is True
    ua.forget()


def test_record_skips_users_without_workspace_and_never_raises():
    ua.forget()

    class Boom:
        def begin(self):
            raise RuntimeError("database down")

    ua.record(Boom(), None, "platform-admin")          # no tenant: nothing to do
    ua.record(Boom(), "t1", "u9", date(2026, 10, 13))  # failure is swallowed
    assert ua.should_record("u9", date(2026, 10, 13)) is False   # and not retried the same day
    ua.forget()
