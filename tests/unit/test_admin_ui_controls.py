"""Unit tests (no database, no network) for the admin UI controls round:

- sending domains: hostname validation and the DNS record guidance (app/services/domain_tenancy.py);
- invitation resend cooldown (app/routers/user_router.py resend_wait_seconds);
- contact scoring guards: idempotent grade history and out-of-office replies
  (app/services/contact_scoring.py grade_history_needed / is_auto_reply).
"""
import pytest

from app.services.domain_tenancy import dns_record_guidance, normalize_domain_name


# ── AUC-U01..04 domain name validation ───────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("example.com", "example.com"),
    ("  Mail.Example.COM ", "mail.example.com"),
    ("example.com.", "example.com"),
    ("a-b.example.co.uk", "a-b.example.co.uk"),
    ("xn--bcher-kva.example", "xn--bcher-kva.example"),
    ("example.xn--p1ai", "example.xn--p1ai"),
])
def test_auc_u01_valid_domains_are_normalised(raw, expected):
    assert normalize_domain_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, 42, "localhost", "example", "http://example.com", "example.com/path", "user@example.com",
    "example.com:25", "*.example.com", "-bad.example.com", "bad-.example.com", "exa mple.com", "example..com",
    "192.168.1.10", "example.c", "example.123", "under_score.example.com", ".example.com",
])
def test_auc_u02_invalid_domains_are_rejected(raw):
    assert normalize_domain_name(raw) is None


def test_auc_u03_length_limits():
    label = "a" * 63
    assert normalize_domain_name(f"{label}.com") == f"{label}.com"
    assert normalize_domain_name(f"{'a' * 64}.com") is None
    at_limit = ".".join(["abcdefghi"] * 25) + ".com"  # exactly 253 characters
    assert len(at_limit) == 253 and normalize_domain_name(at_limit) == at_limit
    assert normalize_domain_name("a" + at_limit) is None


def test_auc_u04_guidance_with_tokens():
    records = dns_record_guidance("example.com", ["tok1", "tok2", "tok3"], "eu-west-1")
    dkim = [r for r in records if r["kind"] == "DKIM"]
    assert [r["host"] for r in dkim] == [f"tok{i}._domainkey.example.com" for i in (1, 2, 3)]
    assert all(r["type"] == "CNAME" and r["value"].endswith(".dkim.amazonses.com") for r in dkim)
    spf = next(r for r in records if r["kind"] == "SPF")
    assert spf == {**spf, "type": "TXT", "host": "mail.example.com", "value": "v=spf1 include:amazonses.com ~all"}
    dmarc = next(r for r in records if r["kind"] == "DMARC")
    assert dmarc["host"] == "_dmarc.example.com" and dmarc["value"].startswith("v=DMARC1; p=none")
    mx = next(r for r in records if r["kind"] == "MAIL_FROM")
    assert mx["type"] == "MX" and "feedback-smtp.eu-west-1.amazonses.com" in mx["value"] and mx["required"] is False


def test_auc_u05_guidance_without_tokens_explains_dkim():
    records = dns_record_guidance("example.com")
    dkim = [r for r in records if r["kind"] == "DKIM"]
    assert len(dkim) == 1 and "<token>" in dkim[0]["host"] and "SES" in dkim[0]["note"]
    assert {r["kind"] for r in records} == {"DKIM", "SPF", "MAIL_FROM", "DMARC"}
    assert "<region>" in next(r for r in records if r["kind"] == "MAIL_FROM")["value"]


def test_auc_u06_guidance_caps_token_count():
    assert len([r for r in dns_record_guidance("example.com", [f"t{i}" for i in range(50)]) if r["kind"] == "DKIM"]) == 10


# ── AUC-U10..12 invitation resend cooldown ───────────────────

def test_auc_u10_resend_allowed_when_never_sent_or_after_a_minute():
    from app.routers.user_router import RESEND_COOLDOWN_SECONDS, resend_wait_seconds
    assert RESEND_COOLDOWN_SECONDS == 60
    assert resend_wait_seconds(None) == 0
    assert resend_wait_seconds(60) == 0
    assert resend_wait_seconds(3600) == 0


def test_auc_u11_resend_blocked_within_a_minute():
    from app.routers.user_router import resend_wait_seconds
    assert resend_wait_seconds(0) == 60
    assert resend_wait_seconds(1) == 59
    assert resend_wait_seconds(59) == 1


def test_auc_u12_clock_skew_counts_as_just_sent():
    from app.routers.user_router import resend_wait_seconds
    assert resend_wait_seconds(-5) == 60
    assert resend_wait_seconds(10, cooldown=30) == 20


# ── AUC-U20..23 contact scoring guards ───────────────────────

def test_auc_u20_history_written_for_a_real_grade_change():
    from app.services.contact_scoring import grade_history_needed
    assert grade_history_needed("B", "A", None) is True
    assert grade_history_needed("B", "A", "B") is True       # latest row recorded the old grade
    assert grade_history_needed("A", "B", "A") is True       # changed back later: a new change


def test_auc_u21_history_skipped_when_latest_row_has_same_value():
    from app.services.contact_scoring import grade_history_needed
    assert grade_history_needed("B", "A", "A") is False      # an overlapping run already logged it


def test_auc_u22_history_skipped_without_change_or_first_grade():
    from app.services.contact_scoring import grade_history_needed
    assert grade_history_needed("A", "A", None) is False
    assert grade_history_needed(None, "A", None) is False    # first score is not a change
    assert grade_history_needed("", "B", None) is False


@pytest.mark.parametrize("subject, body, ooo", [
    ("Automatic reply: Quick question", "I am out of the office until Monday.", True),
    ("Re: Quick question", "I'm on vacation with limited access to my email.", True),
    ("Out of Office", "", True),
    ("Re: Quick question", "Sounds good, let's talk Tuesday.", False),
    (None, None, False),
    ("Re: pricing", "Can you send the proposal?", False),
])
def test_auc_u23_auto_replies_detected(subject, body, ooo):
    from app.services.contact_scoring import is_auto_reply
    assert is_auto_reply(subject, body) is ooo
