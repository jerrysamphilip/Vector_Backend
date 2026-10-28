"""A4 pure rules: IMAP sync steps (app/domain/inbox_sync.py), mailbox edits (inbox_mailbox.py) and
deliverability (deliverability.py). No I/O, no fakes."""
import email.message
from datetime import date, datetime, timedelta

import pytest

from app.domain import deliverability as dl
from app.domain import inbox_mailbox as mb
from app.domain import inbox_sync as sync
from app.domain.inbox import LabelSource

NOW = datetime(2026, 10, 27, 12, 0)
LIMITS = dl.RiskLimits(min_sends=100, bounce_rate=0.05, complaint_rate=0.003, min_bounces=10, min_complaints=3)


def _raw(**headers):
    m = email.message.EmailMessage()
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content("Thanks!\n\nOn Mon someone wrote:\n> old text")
    return m.as_bytes()


# ── IMAP sync ─────────────────────────────────────────────────────────────────
def test_parse_message_fields_and_utc_date():
    raw = _raw(From="Pat <PAT@Client.io>", To="me@sender.io", Subject="=?utf-8?q?Re=3A_H=C3=A9llo?=",
               Date="Mon, 12 Oct 2026 12:00:00 +0200", Message_ID=" <m1@client.io> ", In_Reply_To="<o@x>")
    parsed = sync.parse_message(raw, NOW, lambda body: body.split("\n")[0])
    assert (parsed.sender_email, parsed.receiver_email, parsed.subject) == ("pat@client.io", "me@sender.io", "Re: Héllo")
    assert parsed.sent_at == datetime(2026, 10, 12, 10, 0) and parsed.provider_message_id == "<m1@client.io>"
    assert parsed.body == "Thanks!" and parsed.contact_email("INBOUND") == "pat@client.io"
    assert parsed.contact_email("OUTBOUND") == "me@sender.io" and parsed.in_reply_to == "<o@x>"


def test_parse_message_without_addresses_or_id():
    assert sync.parse_message(_raw(To="me@sender.io"), NOW, str) is None
    parsed = sync.parse_message(_raw(From="a@b.io", To="c@d.io", Subject="Hi", Date="garbage"), NOW, str)
    assert parsed.sent_at == NOW
    again = sync.parse_message(_raw(From="a@b.io", To="c@d.io", Subject="Hi", Date="garbage"), NOW, str)
    assert parsed.provider_message_id == again.provider_message_id  # stable: stored once (B11)
    assert parsed.provider_message_id.startswith("<no-message-id-")
    assert sync.synthetic_message_id("A@b.io ", "d", "s", "c") == sync.synthetic_message_id("a@b.io", "d", "s", "c")


def test_search_window_and_new_uids():
    assert sync.search_criteria(41, 7, NOW) == ("(UID 42:*)", "UID > 41")
    assert sync.search_criteria(0, 7, NOW) == ('(SINCE "20-Oct-2026")', "SINCE 20-Oct-2026")
    assert sync.new_uids([b"5 3 9 3 x"], 4) == [5, 9]
    assert sync.new_uids([None], 0) == [] and sync.new_uids([], 0) == []


def test_cursor_and_position_rules():
    stored = sync.SyncPosition(7, 30)
    assert sync.start_uid(stored, 7) == 30 and sync.start_uid(stored, 8) == 0 and sync.start_uid(None, 7) == 0
    assert sync.parse_uidvalidity([b"12"]) == 12 and sync.parse_uidvalidity([b"x"]) is None
    assert sync.FolderCursor("INBOX", None, 7, 0).position_changed
    assert sync.FolderCursor("INBOX", sync.SyncPosition(6, 1), 7, 0).position_changed
    assert not sync.FolderCursor("INBOX", stored, 7, 30).position_changed
    assert not sync.FolderCursor("INBOX", False, 7, 0).position_changed
    assert not sync.FolderCursor("INBOX", False, 7, 0).can_store


def test_sync_schedule():
    assert sync.sync_due(None, NOW) and sync.sync_due(NOW - timedelta(minutes=5), NOW)
    assert not sync.sync_due(NOW - timedelta(minutes=3), NOW)
    assert sync.days_back_for(None, NOW) == 7
    assert sync.days_back_for(NOW - timedelta(hours=2), NOW) == 2
    assert sync.days_back_for(NOW - timedelta(days=30), NOW) == 7


def test_mailbox_quoting_fetch_and_labels():
    assert sync.quote_mailbox("INBOX") == "INBOX"
    assert sync.quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
    assert sync.fetched_bytes("OK", [(b"1", b"raw")]) == b"raw"
    assert sync.fetched_bytes("NO", [(b"1", b"raw")]) is None and sync.fetched_bytes("OK", [b")"]) is None
    assert sync.ooo_label("auto-replied").source is LabelSource.HEADER
    assert sync.ooo_label(None).confidence == 0.8
    assert sync.is_unsubscribe_reply("Unsubscribe!\n--\nPat") and not sync.is_unsubscribe_reply("don't stop")
    summary = sync.header_summary(_raw(From="a@b.io", To="c@d.io", Subject="x" * 100))
    assert summary["from"] == "a@b.io" and len(summary["subject"]) == 80


# ── Mailbox edits ─────────────────────────────────────────────────────────────
def test_clean_updates_keeps_saved_secrets_and_checks_ports():
    out = mb.clean_updates({"smtp_password": "", "imap_username": None, "imap_host": "  ", "smtp_host": " h ",
                            "smtp_port": 25})
    assert out == {"imap_host": None, "smtp_host": "h", "smtp_port": 25}
    with pytest.raises(mb.InvalidPort, match="imap_port must be between 1 and 65535"):
        mb.clean_updates({"imap_port": 70000})
    assert mb.changes_receiving({"imap_host": None}) and not mb.changes_receiving({"smtp_host": "h"})


def test_warmup_toggle():
    on = mb.warmup_toggle(True, None, NOW)
    assert on == {"warmup_status": "ACTIVE", "warmup_start_date": NOW, "warmup_day": 0,
                  "warmup_issue_code": None, "warmup_issue_message": None}
    assert "warmup_start_date" not in mb.warmup_toggle(True, NOW, NOW)
    assert mb.warmup_toggle(False, NOW, NOW)["warmup_issue_code"] == "WARMUP_DISABLED"
    assert mb.domain_of_address("a@b.io") == "b.io" and mb.domain_of_address("nope") is None


# ── Deliverability ────────────────────────────────────────────────────────────
def test_risk_reason_counts_then_rates():
    assert dl.risk_reason(dl.rates(50, 10, 0), LIMITS) == "10 hard bounces in the first 50 sends"
    assert dl.risk_reason(dl.rates(50, 9, 2), LIMITS) is None
    assert "hard-bounce rate 6.0%" in dl.risk_reason(dl.rates(200, 12, 0), LIMITS)
    assert "spam-complaint rate" in dl.risk_reason(dl.rates(1000, 0, 3), LIMITS)
    assert dl.risk_reason(dl.rates(0, 0, 0), LIMITS) is None


def test_health_and_legacy_scores():
    assert dl.health_score(dl.rates(100, 0, 0), LIMITS) == 100
    assert dl.health_score(dl.rates(100, 5, 0), LIMITS) == 60
    assert dl.health_score(dl.rates(100, 50, 50), LIMITS) == 0
    assert dl.legacy_score(None, "COMPLAINT") == 80 and dl.legacy_score(3, "BOUNCE") == 0
    assert dl.legacy_score(100, "OPEN") == 100 and dl.legacy_score(50, "OTHER") == 50


def test_local_statistics_fill_days_and_summary():
    start = datetime(2026, 10, 1, 9, 0)
    out = dl.local_statistics(start, [(date(2026, 10, 2), 10), (None, 4)], [(date(2026, 10, 2), 1)],
                              [(date(2026, 10, 3), 2)])
    assert len(out["data_points"]) == 15 and out["data_points"][0]["Timestamp"] == datetime(2026, 10, 1)
    assert out["summary"] == {"total_attempts": 10, "total_bounces": 1, "total_complaints": 2, "total_rejects": 0,
                              "bounce_rate": 10.0, "complaint_rate": 20.0}
    ses = dl.ses_statistics([{"Timestamp": 2, "DeliveryAttempts": 0, "Bounces": 0, "Complaints": 0, "Rejects": 1},
                             {"Timestamp": 1, "DeliveryAttempts": 4, "Bounces": 1, "Complaints": 0, "Rejects": 0}])
    assert [p["Timestamp"] for p in ses["data_points"]] == [1, 2] and ses["summary"]["bounce_rate"] == 25.0


def test_dns_verdicts_and_records():
    assert dl.spf_status(True, False) == "PASS" and dl.spf_status(False, False) == "WARNING"
    assert dl.dmarc_status(["v=spf1", "v=DMARC1; p=none"]) == "WARNING"
    assert dl.dmarc_status(["v=DMARC1; p=reject"]) == "PASS" and dl.dmarc_status([]) == "FAIL"
    assert dl.dkim_status("Success") == "PASS" and dl.dkim_status("NotStarted") == "UNKNOWN"
    assert len(dl.scan_warnings("a.com", "WARNING", "WARNING")) == 2 and dl.scan_warnings("a.com", "PASS", "PASS") == []
    records = dl.verification_records("a.com", ["t1"])
    assert records[0]["host"] == "t1._domainkey.a.com" and len(records) == 3
    assert dl.snapshot_values(None, None, 0.1) == {"reputation_score": 100, "bounce_rate_24h": 0.0,
                                                   "complaint_rate_24h": 0.1, "open_rate_24h": 0.0}
    assert dl.domain_of("x@Mail.A.com") == "mail.a.com" and dl.normalize_domain_name("http://a.com") is None
