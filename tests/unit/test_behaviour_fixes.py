"""Unit tests (no database, no network) for the behaviour-fix round.

Covers the pure helpers behind: deal won / lost / stage notifications (E2E-J1-87b), built-in
starter templates when AI generation is unavailable (D1), the Send Now batch cursor, and the
S10 response schemas accepting null where the column is nullable.
"""
import re

import pytest
from fastapi import HTTPException

from app.services import sales as sales_svc
from app.services.sequence_generator import AI_FALLBACK_NOTICE, builtin_sequence_emails

# Tokens the sender resolves at send time (execution_service substitutions)
KNOWN_TOKENS = {"first_name", "company_name", "designation", "our_company", "signature_block", "calendar_link"}


# ── BF-U01..U04 deal stage notifications ─────────────────────

def test_bf_u01_recipients_are_owner_and_manager_not_actor():
    assert sales_svc.stage_change_recipients("owner", "mgr", "admin") == ["owner", "mgr"]


def test_bf_u02_recipients_skip_actor_missing_and_duplicates():
    assert sales_svc.stage_change_recipients("owner", "mgr", "owner") == ["mgr"]
    assert sales_svc.stage_change_recipients("owner", None, "x") == ["owner"]
    assert sales_svc.stage_change_recipients("owner", "owner", "x") == ["owner"]
    assert sales_svc.stage_change_recipients(None, None, "x") == []
    assert sales_svc.stage_change_recipients("mgr", "mgr", "mgr") == []


@pytest.mark.parametrize("status, kind, title_start", [
    ("WON", "DEAL_WON", "Deal won: Big deal"),
    ("LOST", "DEAL_LOST", "Deal lost: Big deal"),
    ("OPEN", "DEAL_STAGE", "Deal moved to Proposal: Big deal"),
])
def test_bf_u03_message_kind_and_title(status, kind, title_start):
    k, title, body = sales_svc.stage_change_message("Big deal", "Proposal", status, "Ann Lee", "Qualified", "Budget")
    assert k == kind
    assert title.startswith(title_start)
    assert "Ann Lee" in body


def test_bf_u04_message_body_details():
    _, _, body = sales_svc.stage_change_message("D", "Proposal", "OPEN", "Ann", "Qualified")
    assert body == "Ann moved the deal from Qualified to Proposal."
    _, _, body = sales_svc.stage_change_message("D", "Won", "WON", None, None, "Great fit")
    assert body.startswith("Someone marked the deal won.") and "Reason: Great fit" in body
    # An open-stage move never carries a close reason
    _, _, body = sales_svc.stage_change_message("D", "Proposal", "OPEN", "Ann", None, "ignored")
    assert "Reason" not in body


# ── BF-U05..U10 built-in starter templates (D1) ───────────────

def test_bf_u05_one_template_per_step_with_intro_follow_up_final():
    emails = builtin_sequence_emails(3)
    assert [e["step_number"] for e in emails] == [1, 2, 3]
    assert [e["kind"] for e in emails] == ["INTRO", "FOLLOW_UP", "FINAL_FOLLOW_UP"]
    assert len({e["subject"] for e in emails}) == 3
    assert len({e["body"] for e in emails}) == 3


@pytest.mark.parametrize("n", [1, 2, 4, 6])
def test_bf_u06_every_step_count_is_covered(n):
    emails = builtin_sequence_emails(n)
    assert len(emails) == n
    assert emails[0]["kind"] == "INTRO"
    if n > 1:
        assert emails[-1]["kind"] == "FINAL_FOLLOW_UP"
        assert all(e["kind"] == "FOLLOW_UP" for e in emails[1:-1])
    for e in emails:
        assert e["subject"].strip() and e["body"].strip()


def test_bf_u07_cta_token_only_with_cta_link():
    without = builtin_sequence_emails(3, cta_enabled=False)
    assert all("{{calendar_link}}" not in e["body"] and e["cta"] == "" for e in without)
    with_cta = builtin_sequence_emails(3, cta_enabled=True)
    assert all("{{calendar_link}}" in e["body"] and "{{calendar_link}}" in e["cta"] for e in with_cta)


def test_bf_u08_only_known_personalization_tokens():
    for flag in (False, True):
        for e in builtin_sequence_emails(5, cta_enabled=flag, include_first_name_in_subject=flag):
            tokens = set(re.findall(r"{{\s*(\w+)\s*}}", e["subject"] + e["body"]))
            assert tokens <= KNOWN_TOKENS, tokens
            assert "{{first_name}}" in e["body"] and "{{signature_block}}" in e["body"]


def test_bf_u09_first_name_in_subject_follows_preference():
    assert not any("{{first_name}}" in e["subject"] for e in builtin_sequence_emails(3))
    assert all("{{first_name}}" in e["subject"] for e in builtin_sequence_emails(3, include_first_name_in_subject=True))


def test_bf_u10_deterministic_and_notice_text():
    assert builtin_sequence_emails(4, True, True) == builtin_sequence_emails(4, True, True)
    assert builtin_sequence_emails(0) == builtin_sequence_emails(1)
    assert AI_FALLBACK_NOTICE == "AI unavailable - starter templates were used, please review"


# ── BF-U11..U12 Send Now batch cursor ────────────────────────

def test_bf_u11_send_now_cursor_round_trip():
    from app.routers.campaign_router import SEND_NOW_BATCH, format_send_now_cursor, parse_send_now_cursor
    assert SEND_NOW_BATCH == 25
    c = format_send_now_cursor(2, "abc-123")
    assert c == "2:abc-123"
    assert parse_send_now_cursor(c) == (2, "abc-123")
    assert parse_send_now_cursor(None) is None
    assert parse_send_now_cursor("") is None


@pytest.mark.parametrize("bad", ["x:1", "1", "1:", ":abc", "-1:abc", "1:" + "a" * 65])
def test_bf_u12_send_now_cursor_rejects_garbage(bad):
    from app.routers.campaign_router import parse_send_now_cursor
    with pytest.raises(HTTPException) as exc:
        parse_send_now_cursor(bad)
    assert exc.value.status_code == 400


# ── BF-U13 S10 nullable response fields ──────────────────────

def test_bf_u13_response_schemas_accept_null_for_nullable_columns():
    from datetime import datetime

    from app.routers.company_profile_router import CompanyProfileResponse
    from app.routers.prospect_list_router import ProspectInListResponse, ProspectListResponse
    from app.routers.user_router import UserOut
    from app.schemas.inbox_schema import SendingInboxResponse

    assert CompanyProfileResponse(profile_id="p", profile_name="n", company_name="c", company_description=None,
                                  default_sender_name=None, default_cta_link=None, is_default=None).is_default is None
    assert SendingInboxResponse(inbox_id="i", email_address="a@b.c", status=None, daily_limit=None).status is None
    assert ProspectListResponse(list_id="l", list_name="n", source_type="CSV", prospect_count=0,
                                uploaded_at=None).uploaded_at is None
    assert ProspectInListResponse(prospect_id="p", email="a@b.c", consent_status=None).consent_status is None
    assert UserOut(user_id="u", tenant_id="t", first_name="a", last_name="b", email="a@b.c", role="AGENT",
                   status=None, auth_provider="local", email_verified=False, avatar_url=None,
                   created_at=datetime(2026, 1, 1), last_login_at=None, permissions=[]).status is None
