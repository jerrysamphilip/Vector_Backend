"""Unit tests (no database, no network) for contact scoring on fit and engagement (BR-CM-43 / CM-083).

Covers app/services/scoring_engine.py: the default model, model validation, every fit operator,
company size bands, whole-word keyword matching, recency decay, caps, look-back window,
weights, clamping and grades.
"""
from datetime import datetime, timedelta

import pytest

from app.services import scoring_engine as se

NOW = datetime(2026, 10, 1, 12, 0, 0)


def _rule(field, operator, value="", points=10, **kw):
    return se.validate_rule({"field": field, "operator": operator, "value": value, "points": points, **kw})


def _model(**changes):
    m = se.default_model()
    m.update(changes)
    return se.validate_model(m)


# ── CS-U01..05 default model ─────────────────────────────────

def test_cs_u01_default_model_is_valid_and_round_trips():
    assert se.validate_model(se.default_model()) == se.default_model()


def test_cs_u02_default_model_is_a_copy():
    m = se.default_model()
    m["fit_rules"].clear()
    assert se.default_model()["fit_rules"], "mutating a copy must not change the defaults"


def test_cs_u03_default_weights_add_up_and_thresholds_decrease():
    m = se.default_model()
    assert m["fit_weight"] + m["engagement_weight"] == 100
    t = m["grade_thresholds"]
    assert t["A"] > t["B"] > t["C"] > 0


def test_cs_u04_default_engagement_covers_every_signal():
    assert set(se.default_model()["engagement"]) == set(se.ENGAGEMENT_SIGNALS)


def test_cs_u05_empty_contact_scores_zero_grade_d():
    r = se.compute_score(se.default_model(), {}, {}, NOW)
    assert (r["score"], r["fit"], r["engagement"], r["grade"]) == (0, 0, 0, "D")


# ── CS-U06..15 validation ────────────────────────────────────

def test_cs_u06_partial_model_falls_back_to_defaults():
    m = se.validate_model({"fit_weight": 70, "engagement_weight": 30})
    assert m["fit_weight"] == 70 and m["fit_rules"] == se.default_model()["fit_rules"]


@pytest.mark.parametrize("bad", [
    {"fit_weight": 60, "engagement_weight": 60},
    {"grade_thresholds": {"A": 50, "B": 60, "C": 10}},
    {"grade_thresholds": {"A": 80, "B": 50, "C": 0}},
    {"half_life_days": -1},
    {"half_life_days": 366},
    {"lookback_days": 0},
    {"engagement": {"unknown_signal": {"points": 1, "max": 1}}},
    {"engagement": {"meeting": {"points": 101, "max": 10}}},
    {"fit_rules": "nope"},
    {"fit_rules": [{"field": "designation", "operator": "contains_any", "value": "ceo", "points": 101}]},
    {"fit_rules": [{"field": "salary", "operator": "equals", "value": "x", "points": 1}]},
    {"fit_rules": [{"field": "designation", "operator": "gte", "value": "1", "points": 1}]},
    {"fit_rules": [{"field": "employees", "operator": "contains_any", "value": "1", "points": 1}]},
    {"fit_rules": [{"field": "employees", "operator": "gte", "value": "lots", "points": 1}]},
    {"fit_rules": [{"field": "employees", "operator": "between", "value": "50", "points": 1}]},
    {"fit_rules": [{"field": "industry", "operator": "in", "value": " , ", "points": 1}]},
    {"fit_rules": [{"field": "industry", "operator": "in", "value": "x" * 501, "points": 1}]},
    {"fit_weight": True, "engagement_weight": 0},
    {"fit_weight": 50.5, "engagement_weight": 49.5},
    "not a dict",
])
def test_cs_u07_invalid_models_are_rejected(bad):
    with pytest.raises(se.ScoringModelError):
        se.validate_model(bad)


def test_cs_u08_too_many_rules_rejected():
    rules = [{"field": "industry", "operator": "equals", "value": "x", "points": 1}] * (se.MAX_RULES + 1)
    with pytest.raises(se.ScoringModelError):
        se.validate_model({"fit_rules": rules})


def test_cs_u09_rule_normalisation_numbers_and_ranges():
    assert _rule("annual_revenue", "gte", "10,000,000")["value"] == "10000000"
    assert _rule("employees", "between", "1,000 to 50")["value"] == "50-1000"
    assert _rule("employees", "between", "50..199")["value"] == "50-199"


def test_cs_u10_rule_label_and_id_are_generated():
    r = _rule("country", "is_set", "ignored")
    assert r["value"] == "" and r["label"] and len(r["id"]) == 12


def test_cs_u11_rule_list_value_is_joined():
    assert _rule("industry", "in", ["Software", " SaaS "])["value"] == "Software, SaaS"


def test_cs_u12_duplicate_rule_ids_are_made_unique():
    rule = {"id": "same", "field": "industry", "operator": "equals", "value": "x", "points": 1}
    m = se.validate_model({"fit_rules": [rule, rule]})
    assert len({r["id"] for r in m["fit_rules"]}) == 2


def test_cs_u13_bad_rule_id_is_replaced():
    r = _rule("industry", "equals", "x", id="bad id with spaces!")
    assert r["id"] != "bad id with spaces!"


def test_cs_u14_load_model_falls_back_on_broken_documents():
    assert se.load_model(None) == se.default_model()
    assert se.load_model({"fit_weight": 99, "engagement_weight": 99}) == se.default_model()


def test_cs_u15_long_label_is_truncated():
    assert len(_rule("industry", "equals", "x", label="L" * 300)["label"]) == se.MAX_LABEL


# ── CS-U16..30 fit operators ─────────────────────────────────

def test_cs_u16_contains_any_is_whole_word_and_case_insensitive():
    r = _rule("designation", "contains_any", "CTO, vp")
    assert se.rule_matches(r, {"designation": "Co-founder & CTO"})
    assert se.rule_matches(r, {"designation": "VP Marketing"})
    assert not se.rule_matches(r, {"designation": "Director of Sales"})  # 'cto' inside 'director'
    assert not se.rule_matches(r, {"designation": "MVP builder"})


def test_cs_u17_contains_any_multi_word_keyword():
    r = _rule("designation", "contains_any", "vice president")
    assert se.rule_matches(r, {"designation": "Senior Vice President, Ops"})


@pytest.mark.parametrize("op, value, actual, expected", [
    ("equals", "software", "Software", True),
    ("equals", "software", "Hardware", False),
    ("not_equals", "software", "Hardware", True),
    ("not_equals", "software", None, True),
    ("in", "UK, United Kingdom", "united kingdom", True),
    ("in", "UK, United Kingdom", "France", False),
    ("in", "UK", None, False),
    ("not_in", "UK", "France", True),
    ("not_in", "UK", None, True),
    ("is_set", "", "x", True),
    ("is_set", "", "  ", False),
    ("is_not_set", "", None, True),
])
def test_cs_u18_text_operators(op, value, actual, expected):
    assert se.rule_matches(_rule("industry", op, value), {"industry": actual}) is expected


@pytest.mark.parametrize("op, value, employees, expected", [
    ("gte", "200", 200, True), ("gte", "200", 199, False),
    ("lte", "50", 50, True), ("lte", "50", 51, False),
    ("between", "50-199", 50, True), ("between", "50-199", 199, True), ("between", "50-199", 200, False),
    ("equals", "10", 10, True), ("not_equals", "10", 11, True),
    ("is_set", "", 5, True), ("is_not_set", "", None, True),
    ("gte", "1", None, False),
])
def test_cs_u19_number_operators(op, value, employees, expected):
    assert se.rule_matches(_rule("employees", op, value), {"employees": employees}) is expected


@pytest.mark.parametrize("band, expected", [
    ("51-200", 51), ("10000+", 10000), ("1,001-5,000", 1001), ("500", 500), ("", None), (None, None), ("unknown", None),
])
def test_cs_u20_employee_band_lower_bound(band, expected):
    assert se.employees_from_band(band) == expected


def test_cs_u21_employees_derived_from_band_when_missing():
    r = _rule("employees", "gte", "200")
    assert se.rule_matches(r, {"emp_band": "201-500"})
    assert not se.rule_matches(r, {"emp_band": "11-50"})


def test_cs_u22_revenue_numeric_strings():
    r = _rule("annual_revenue", "gte", "10000000")
    assert se.rule_matches(r, {"annual_revenue": "25,000,000"})
    assert not se.rule_matches(r, {"annual_revenue": "abc"})


def test_cs_u23_tags_list_field():
    r = _rule("tags", "in", "vip, partner")
    assert se.rule_matches(r, {"tags": ["Prospect", "VIP"]})
    assert not se.rule_matches(r, {"tags": []})


def test_cs_u24_has_phone_derived():
    r = _rule("has_phone", "equals", "true")
    assert se.rule_matches(r, {"phone": "+1 555 0100"})
    assert se.rule_matches(r, {"has_phone": True})
    assert not se.rule_matches(r, {"phone": None, "mobile_phone": ""})


def test_cs_u25_negative_points_reduce_fit_but_never_below_zero():
    m = _model(fit_rules=[{"field": "email_type", "operator": "equals", "value": "PERSONAL", "points": -50}])
    fit, rows = se.fit_breakdown(m, {"email_type": "PERSONAL"})
    assert fit == 0 and rows[0]["matched"] and rows[0]["contributed"] == -50


def test_cs_u26_fit_is_capped_at_100():
    rules = [{"field": "industry", "operator": "is_set", "points": 60}, {"field": "country", "operator": "is_set", "points": 60}]
    fit, _ = se.fit_breakdown(_model(fit_rules=rules), {"industry": "x", "country": "y"})
    assert fit == 100


def test_cs_u27_breakdown_lists_every_rule_with_matched_flag():
    m = se.default_model()
    _, rows = se.fit_breakdown(m, {"designation": "CEO"})
    assert len(rows) == len(m["fit_rules"])
    assert [r["id"] for r in rows if r["matched"]] == ["exec"]


def test_cs_u28_default_rules_executive_business_email():
    c = {"designation": "Chief Executive Officer", "email_type": "BUSINESS"}
    fit, rows = se.fit_breakdown(se.default_model(), c)
    assert fit == 40
    assert {r["id"] for r in rows if r["matched"]} == {"exec", "business_email"}


def test_cs_u29_default_rules_unqualified_personal_email():
    fit, _ = se.fit_breakdown(se.default_model(), {"email_type": "PERSONAL", "lead_status": "UNQUALIFIED"})
    assert fit == 0


def test_cs_u30_default_rules_size_bands_do_not_overlap():
    m = se.default_model()
    _, big = se.fit_breakdown(m, {"emp_band": "201-500"})
    _, mid = se.fit_breakdown(m, {"emp_band": "51-200"})
    assert {r["id"] for r in big if r["matched"]} == {"size_large"}
    assert {r["id"] for r in mid if r["matched"]} == {"size_mid"}


# ── CS-U31..40 engagement ────────────────────────────────────

def test_cs_u31_decay_weight_halves_each_half_life():
    assert se.decay_weight(0, 30) == 1.0
    assert se.decay_weight(30, 30) == pytest.approx(0.5)
    assert se.decay_weight(60, 30) == pytest.approx(0.25)
    assert se.decay_weight(1000, 0) == 1.0  # no decay
    assert se.decay_weight(-5, 30) == 1.0   # future dates count as now


def test_cs_u32_points_times_count_without_decay():
    m = _model(half_life_days=0)
    eng, rows = se.engagement_breakdown(m, {"email_opened": [NOW, NOW]}, NOW)
    opened = next(r for r in rows if r["signal"] == "email_opened")
    assert opened["count"] == 2 and opened["contributed"] == 6.0 and eng == 6


def test_cs_u33_signal_cap_applies():
    m = _model(half_life_days=0)
    _, rows = se.engagement_breakdown(m, {"email_opened": [(NOW, 50)]}, NOW)
    opened = next(r for r in rows if r["signal"] == "email_opened")
    assert opened["contributed"] == m["engagement"]["email_opened"]["max"]


def test_cs_u34_old_activity_counts_less():
    m = se.default_model()
    fresh, _ = se.engagement_breakdown(m, {"meeting": [NOW]}, NOW)
    old, _ = se.engagement_breakdown(m, {"meeting": [NOW - timedelta(days=30)]}, NOW)
    assert fresh == 15 and old == 8  # 15 x 0.5 = 7.5 -> 8 (half up)


def test_cs_u35_activity_outside_lookback_ignored():
    m = se.default_model()
    eng, rows = se.engagement_breakdown(m, {"call": [NOW - timedelta(days=m["lookback_days"] + 1)]}, NOW)
    assert eng == 0 and next(r for r in rows if r["signal"] == "call")["count"] == 0


def test_cs_u36_event_shapes_accepted():
    m = _model(half_life_days=0)
    act = {"call": [NOW, (NOW, 2), {"at": NOW, "count": 1}, {"at": NOW.date(), "count": 1}, ("bad", 1), None]}
    _, rows = se.engagement_breakdown(m, act, NOW)
    assert next(r for r in rows if r["signal"] == "call")["count"] == 5


def test_cs_u37_engagement_is_capped_at_100():
    m = _model(half_life_days=0)
    act = {k: [(NOW, 100)] for k in se.ENGAGEMENT_SIGNALS}
    eng, _ = se.engagement_breakdown(m, act, NOW)
    assert eng == 100


def test_cs_u38_zero_weight_signal_contributes_nothing():
    eng_cfg = dict(se.default_model()["engagement"], meeting={"points": 0, "max": 30})
    eng, _ = se.engagement_breakdown(_model(engagement=eng_cfg), {"meeting": [NOW] * 5}, NOW)
    assert eng == 0


def test_cs_u39_timezone_aware_datetimes_are_handled():
    from datetime import timezone
    eng, _ = se.engagement_breakdown(_model(half_life_days=0), {"call": [NOW.replace(tzinfo=timezone.utc)]}, NOW)
    assert eng == 8


def test_cs_u40_breakdown_lists_every_signal():
    _, rows = se.engagement_breakdown(se.default_model(), {}, NOW)
    assert [r["signal"] for r in rows] == list(se.ENGAGEMENT_SIGNALS)


# ── CS-U41..48 total and grades ──────────────────────────────

def test_cs_u41_total_is_weighted_average():
    m = _model(half_life_days=0, fit_rules=[{"field": "industry", "operator": "is_set", "points": 80}])
    r = se.compute_score(m, {"industry": "x"}, {"meeting": [NOW, NOW]}, NOW)  # fit 80, engagement 30
    assert (r["fit"], r["engagement"], r["score"]) == (80, 30, 55)


def test_cs_u42_fit_only_weighting():
    m = _model(fit_weight=100, engagement_weight=0, fit_rules=[{"field": "industry", "operator": "is_set", "points": 42}])
    assert se.compute_score(m, {"industry": "x"}, {"meeting": [NOW]}, NOW)["score"] == 42


def test_cs_u43_total_within_0_100():
    m = _model(half_life_days=0)
    act = {k: [(NOW, 100)] for k in se.ENGAGEMENT_SIGNALS}
    c = {"designation": "CEO and Director", "emp_band": "1000+", "annual_revenue": 1e9, "industry": "Software",
         "country": "UK", "email_type": "BUSINESS", "lifecycle_stage": "SQL"}
    r = se.compute_score(m, c, act, NOW)
    assert r["score"] == 100 and r["grade"] == "A"


@pytest.mark.parametrize("score, grade", [(100, "A"), (75, "A"), (74, "B"), (50, "B"), (49, "C"), (25, "C"), (24, "D"), (0, "D"), (None, None)])
def test_cs_u44_grade_thresholds(score, grade):
    assert se.grade_for(score, {"A": 75, "B": 50, "C": 25}) == grade


def test_cs_u45_custom_thresholds_change_grade():
    m = _model(grade_thresholds={"A": 30, "B": 20, "C": 10}, fit_rules=[{"field": "industry", "operator": "is_set", "points": 70}])
    assert se.compute_score(m, {"industry": "x"}, {}, NOW)["grade"] == "A"  # score 35


def test_cs_u46_result_carries_breakdown_and_weights():
    r = se.compute_score(se.default_model(), {"designation": "CEO"}, {}, NOW)
    assert {"fit_rules", "engagement_signals", "fit_weight", "engagement_weight"} <= set(r)


def test_cs_u47_rounding_is_half_up():
    m = _model(fit_rules=[{"field": "industry", "operator": "is_set", "points": 1}])
    assert se.compute_score(m, {"industry": "x"}, {}, NOW)["score"] == 1  # 0.5 -> 1, not banker's 0


def test_cs_u48_deterministic():
    c = {"designation": "VP Sales", "emp_band": "201-500"}
    a = {"email_clicked": [NOW - timedelta(days=3)], "email_replied": [(NOW.date(), 1)]}
    assert se.compute_score(se.default_model(), c, a, NOW) == se.compute_score(se.default_model(), c, a, NOW)
