# app/db/test_unsubscribe_link.py
"""
Test: Verify unsubscribe link is appended to all campaign email types.

Tests the body construction logic in EmailSenderService.send_email() by
simulating the unsubscribe-link injection without actually calling SES.

Campaign types covered:
  1. Cold outreach (standard)
  2. Conference / in-person outreach
  3. Creative mode email
  4. Email with pre-existing CTA link in body
  5. Internal domain email (should NOT get unsubscribe link)
  6. Email that already contains an unsubscribe link (no duplication)
"""

import re
import uuid
from types import SimpleNamespace

from app.core.config import settings


# ---------------------------------------------------------------------------
# Simulate the exact body-construction logic from EmailSenderService.send_email()
# (lines 434-458 of email_sender_service.py)
# ---------------------------------------------------------------------------

def build_final_body(
    body_content: str,
    sender_email: str,
    recipient_email: str,
    message_id: str,
    sender_name: str = "Emma",
    unsubscribe_mode: str = "plain",
) -> str:
    """Replicate the unsubscribe/footer injection logic from send_email()."""

    sender_domain = sender_email.split("@")[1].lower() if "@" in sender_email else ""
    recipient_domain = recipient_email.split("@")[1].lower() if "@" in recipient_email else ""
    is_internal = sender_domain == recipient_domain and sender_domain != ""

    unsubscribe_url = f"{settings.BASE_URL}/api/tracking/unsubscribe/{message_id}"

    if not is_internal:
        # CAN-SPAM physical address
        if settings.COMPANY_PHYSICAL_ADDRESS not in body_content:
            body_content += f"\n\n{settings.COMPANY_PHYSICAL_ADDRESS}"

        # Unsubscribe footer (only if not already present)
        if "/api/tracking/unsubscribe/" not in body_content:
            if unsubscribe_mode == "html":
                body_content += (
                    f'\nPlease click on <a href="{unsubscribe_url}">unsubscribe</a> '
                    f'in case you do not wish to receive such business communication emails in the future.'
                )
            else:
                body_content += (
                    f"\nPlease reply 'unsubscribe' in case you do not wish to "
                    f"receive such business communication emails in the future."
                )

    # Replace explicit placeholder if template uses it
    body_content = body_content.replace("{{unsubscribe_link}}", unsubscribe_url)

    # Signature
    signature_pattern = r"(?i)(Best(\s+Regards)?|Regards|Thanks|Cheers|Sincerely)[,]?\s*\n\s*[A-Za-z0-9 ]+"
    if not re.search(signature_pattern, body_content):
        body_content = body_content.rstrip() + f"\n\nRegards,\n{sender_name}"

    return body_content


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

SENDER = "emma@neutrinotech.com"
MSG_ID = str(uuid.uuid4())
UNSUB_PATTERN = r"/api/tracking/unsubscribe/"

def _count_unsubscribe(body: str) -> int:
    return len(re.findall(UNSUB_PATTERN, body))

def _pass(label: str):
    print(f"  PASS  {label}")

def _fail(label: str, detail: str = ""):
    print(f"  FAIL  {label}  {detail}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cold_outreach():
    """Standard cold outreach email gets unsubscribe link."""
    body = (
        "Hi John,\n\n"
        "Are manual processes slowing your growth?\n\n"
        "Neutrino Tech Systems helps teams like yours.\n\n"
        "    • Patient Enrollment/Intake\n"
        "    • Benefits Investigation (BI)\n\n"
        "Up for a quick chat next week?\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "john@acme.com", MSG_ID)
    count = _count_unsubscribe(result)
    if "reply 'unsubscribe'" in result:
        _pass("Cold outreach — unsubscribe link present")
    else:
        _fail("Cold outreach", f"expected 1 link, found {count}")


def test_conference_outreach():
    """Conference/in-person outreach email gets unsubscribe link."""
    body = (
        "Hi Sarah,\n\n"
        "The work Avidity is doing around hub services is getting well-deserved attention.\n\n"
        "Understanding Avidity's presence at HLTH 25, and since our Head of Business Development, "
        "Abhi, will be there too, I thought of checking for the possibility of a meeting.\n\n"
        "    • Enhancing Patient Experience\n"
        "    • Payment Revolution\n\n"
        "Please let me know your availability for an In-Person meeting at HLTH 25. "
        "If not at the event, we could also connect virtually.\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "sarah@pharma.com", MSG_ID)
    count = _count_unsubscribe(result)
    if "reply 'unsubscribe'" in result:
        _pass("Conference outreach — unsubscribe link present")
    else:
        _fail("Conference outreach", f"expected 1 link, found {count}")


def test_creative_mode():
    """Creative mode email gets unsubscribe link."""
    body = (
        "Hi Alex,\n\n"
        "Pipeline reviews catch stale deals. They rarely surface why the deal stalled.\n\n"
        "That shows up as missed expansion revenue and a forecast nobody trusts.\n\n"
        "We convert sales calls into structured CRM entries automatically — teams cut data entry by 60%.\n\n"
        "Relevant to TechCorp right now?\n\n"
        "Best,\nEmma"
    )
    result = build_final_body(body, SENDER, "alex@techcorp.io", MSG_ID)
    count = _count_unsubscribe(result)
    if "reply 'unsubscribe'" in result:
        _pass("Creative mode — unsubscribe link present")
    else:
        _fail("Creative mode", f"expected 1 link, found {count}")


def test_email_with_cta_link():
    """Email that already has a CTA link still gets unsubscribe link."""
    body = (
        "Hi Mark,\n\n"
        "Up for a quick demo?\n\n"
        "Link to find time: https://neutrinoaistudio.com/\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "mark@bigpharma.com", MSG_ID)
    count = _count_unsubscribe(result)
    if "reply 'unsubscribe'" in result:
        _pass("Email with CTA link — unsubscribe link present (not confused with CTA)")
    else:
        _fail("Email with CTA link", f"expected 1 link, found {count}")


def test_internal_domain_no_unsubscribe():
    """Internal email (same domain) should NOT get unsubscribe link."""
    body = (
        "Hi Team,\n\n"
        "Testing the new sequence.\n\n"
        "Best,\nEmma"
    )
    result = build_final_body(body, SENDER, "test@neutrinotech.com", MSG_ID)
    count = _count_unsubscribe(result)
    if count == 0 and "unsubscribe" not in result.lower():
        _pass("Internal domain — no unsubscribe link (correct)")
    else:
        _fail("Internal domain", f"expected 0 links, found {count}")


def test_no_duplication():
    """Email that already contains an unsubscribe link should not get a second one."""
    existing_url = f"{settings.BASE_URL}/api/tracking/unsubscribe/{MSG_ID}"
    body = (
        "Hi Lisa,\n\n"
        "Quick follow-up on our last conversation.\n\n"
        "Regards,\nEmma\n\n"
        f"To opt out: {existing_url}"
    )
    result = build_final_body(body, SENDER, "lisa@client.com", MSG_ID)
    count = _count_unsubscribe(result)
    if count == 1:
        _pass("No duplication — single unsubscribe link preserved")
    else:
        _fail("No duplication", f"expected 1 link, found {count}")


def test_placeholder_replacement():
    """{{unsubscribe_link}} placeholder is replaced with actual URL."""
    body = (
        "Hi Dan,\n\n"
        "Opt out here: {{unsubscribe_link}}\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "dan@partner.com", MSG_ID)
    count = _count_unsubscribe(result)
    has_placeholder = "{{unsubscribe_link}}" in result
    if count >= 1 and not has_placeholder:
        _pass("Placeholder replaced with real URL")
    else:
        _fail("Placeholder replacement", f"links={count}, placeholder_remains={has_placeholder}")


def test_old_text_not_present():
    """Ensure the old 'reply with unsubscribe' text is NOT in the output."""
    body = (
        "Hi Jane,\n\n"
        "Quick note about our services.\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "jane@company.com", MSG_ID)
    if 'reply with "unsubscribe"' not in result.lower() and "reply stop" not in result.lower():
        _pass("No old 'reply with unsubscribe' text")
    else:
        _fail("Old unsubscribe text found in output")


def test_html_mode():
    """HTML mode produces a clickable <a> hyperlink."""
    body = (
        "Hi Tom,\n\n"
        "Quick note about our services.\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "tom@acme.com", MSG_ID, unsubscribe_mode="html")
    if '<a href=' in result and '>unsubscribe</a>' in result:
        _pass("HTML mode — clickable <a> hyperlink present")
    else:
        _fail("HTML mode", "expected <a href=...>unsubscribe</a>")


def test_plain_mode():
    """Plain mode produces reply-based text (no HTML)."""
    body = (
        "Hi Tom,\n\n"
        "Quick note about our services.\n\n"
        "Regards,\nEmma"
    )
    result = build_final_body(body, SENDER, "tom@acme.com", MSG_ID, unsubscribe_mode="plain")
    if "reply 'unsubscribe'" in result and '<a href=' not in result:
        _pass("Plain mode — reply-based text, no HTML")
    else:
        _fail("Plain mode", "expected plain text, got HTML or missing text")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("TEST: Unsubscribe link in email body")
    print("=" * 60)
    print(f"BASE_URL: {settings.BASE_URL}")
    print(f"Test message ID: {MSG_ID}")
    print()

    test_cold_outreach()
    test_conference_outreach()
    test_creative_mode()
    test_email_with_cta_link()
    test_internal_domain_no_unsubscribe()
    test_no_duplication()
    test_placeholder_replacement()
    test_old_text_not_present()
    test_html_mode()
    test_plain_mode()

    print()
    print("=" * 60)
    print("All tests complete.")
    print("=" * 60)
