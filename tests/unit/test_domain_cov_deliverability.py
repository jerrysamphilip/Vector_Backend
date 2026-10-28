"""Coverage of sender-domain parsing and the auto-pause risk rule (app/domain/deliverability.py)."""
from app.domain.deliverability import RiskLimits, domain_of, rates, risk_reason

LIMITS = RiskLimits(min_sends=100, bounce_rate=0.05, complaint_rate=0.001, min_bounces=5, min_complaints=2)


def test_domain_of_lowercases_and_rejects_non_addresses():
    assert domain_of("Ada@Example.COM ") == "example.com"
    assert domain_of(None) is None
    assert domain_of("") is None
    assert domain_of("no-at-sign") is None
    assert domain_of("ada@") is None


def test_enough_sends_under_both_rate_limits_is_safe():
    assert risk_reason(rates(1000, 10, 0), LIMITS) is None


def test_enough_sends_bounce_and_complaint_rates():
    assert "hard-bounce rate 5.0%" in risk_reason(rates(100, 5, 0), LIMITS)
    assert "spam-complaint rate" in risk_reason(rates(1000, 0, 1), LIMITS)


def test_few_sends_count_bounces_then_complaints():
    assert risk_reason(rates(10, 5, 9), LIMITS) == "5 hard bounces in the first 10 sends"
    assert risk_reason(rates(10, 4, 2), LIMITS) == "2 spam complaints in the first 10 sends"
    assert risk_reason(rates(10, 4, 1), LIMITS) is None
