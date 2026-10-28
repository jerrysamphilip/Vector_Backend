"""S-26 (IMAP derived from SMTP, app/domain/inbox_mailbox.py) and S-25 (DNS records vs live DNS,
app/domain/deliverability.py): pure rules."""
from types import SimpleNamespace

import pytest

from app.domain import deliverability as rules
from app.domain.inbox_mailbox import EffectiveImap, derived_imap_host, effective_imap


# ── S-26 ──

@pytest.mark.parametrize("smtp,imap", [
    ("smtp.gmail.com", "imap.gmail.com"), ("SMTP.Office365.com.", "outlook.office365.com"),
    ("smtp-mail.outlook.com", "outlook.office365.com"), ("smtp.zoho.in", "imap.zoho.in"),
    ("smtppro.zoho.eu", "imappro.zoho.eu"), ("smtp.acme.io", "imap.acme.io"),
    ("email-smtp.us-east-1.amazonaws.com", None), ("smtp.sendgrid.net", None), ("mail.acme.io", None),
    ("smtp.local", "imap.local"), ("smtp", None), ("", None), (None, None)])
def test_tbe_imap_host_from_smtp(smtp, imap):
    assert derived_imap_host(smtp) == imap


def _box(**kw):
    base = dict(email_address="me@acme.io", imap_host=None, imap_port=None, imap_username=None, imap_password=None,
                smtp_host="smtp.zoho.com", smtp_username="me@acme.io", smtp_password="app-pw")
    base.update(kw)
    return SimpleNamespace(**base)


def test_tbe_no_imap_settings_derive_everything_from_smtp():
    imap = effective_imap(_box(imap_port=1143))  # a saved port without a saved host is not used
    assert imap == EffectiveImap("imap.zoho.com", 993, "me@acme.io", "app-pw", True)
    assert imap.configured() is True


def test_tbe_saved_imap_settings_win_and_gaps_reuse_smtp():
    saved = effective_imap(_box(imap_host=" imap.mail.acme.io ", imap_port=143, imap_username="inbox",
                                imap_password="imap-pw"))
    assert saved == EffectiveImap("imap.mail.acme.io", 143, "inbox", "imap-pw", False)
    partial = effective_imap(_box(imap_host="imap.acme.io", smtp_username="smtp-user"))
    assert (partial.effective_imap_username, partial.effective_imap_password) == ("smtp-user", "app-pw")
    assert partial.effective_imap_port == 993
    # the IMAP username stays when only the password is borrowed
    assert effective_imap(_box(imap_username="box")).effective_imap_username == "box"


def test_tbe_send_only_relays_and_missing_secrets_are_not_configured():
    ses = effective_imap(_box(smtp_host="email-smtp.eu-west-1.amazonaws.com", smtp_username="AKIA"))
    assert ses.effective_imap_host is None and ses.effective_imap_password is None
    assert ses.effective_imap_username == "me@acme.io" and not ses.configured()
    no_secret = effective_imap(_box(smtp_password=None))
    assert no_secret.effective_imap_host == "imap.zoho.com" and not no_secret.configured()
    assert no_secret.configured(oauth=True)  # Microsoft 365 signs in with a token
    nothing = effective_imap(SimpleNamespace())
    assert nothing == EffectiveImap(None, 993, None, None, False)


# ── S-25 ──

def _record(kind, rtype, host, value):
    return {"kind": kind, "type": rtype, "host": host, "value": value, "required": True, "note": ""}


SPF = _record("SPF", "TXT", "mail.acme.io", "v=spf1 include:amazonses.com ~all")


def test_tbe_spf_add_ok_and_merge_into_the_existing_record():
    assert rules.compare_record(SPF, [])["status"] == "ADD"
    assert rules.compare_record(SPF, ["google-site-verification=x"])["status"] == "ADD"
    ok = rules.compare_record(SPF, ["v=spf1 include:_spf.google.com include:amazonses.com -all"])
    assert ok["status"] == "OK" and ok["current_value"].startswith("v=spf1 include:_spf")
    assert ok["suggested_value"] == SPF["value"]
    update = rules.compare_record(SPF, ["v=spf1 include:_spf.google.com ~all", "other"])
    assert update["status"] == "UPDATE" and update["current_value"] == "v=spf1 include:_spf.google.com ~all"
    assert update["suggested_value"] == "v=spf1 include:_spf.google.com include:amazonses.com ~all"
    assert update["current_values"] == ["v=spf1 include:_spf.google.com ~all", "other"]
    two = rules.compare_record(SPF, ["v=spf1 include:amazonses.com ~all", "v=spf1 mx ~all"])
    assert two["status"] == "UPDATE"  # two SPF records are an SPF error


@pytest.mark.parametrize("existing,merged", [
    ("v=spf1 mx", "v=spf1 mx include:amazonses.com"),
    ("v=spf1 a redirect=_spf.acme.io", "v=spf1 a include:amazonses.com redirect=_spf.acme.io"),
    ("v=spf1 ip4:1.2.3.4 -all", "v=spf1 ip4:1.2.3.4 include:amazonses.com -all"),
    ("v=spf1 include:amazonses.com ~all", "v=spf1 include:amazonses.com ~all")])
def test_tbe_spf_merge(existing, merged):
    assert rules.merged_spf(existing) == merged


def test_tbe_dkim_mx_and_dmarc_compare():
    dkim = _record("DKIM", "CNAME", "tok._domainkey.acme.io", "tok.dkim.amazonses.com")
    assert rules.compare_record(dkim, ["TOK.dkim.amazonses.com."])["status"] == "OK"
    wrong = rules.compare_record(dkim, ["other.example.com."])
    assert wrong["status"] == "UPDATE" and wrong["current_value"] == "other.example.com."
    assert rules.compare_record(dkim, None)["status"] == "UNKNOWN"
    placeholder = _record("DKIM", "CNAME", "<token>._domainkey.acme.io", "<token>.dkim.amazonses.com")
    assert rules.compare_record(placeholder, [])["status"] == "UNKNOWN"
    mx = _record("MAIL_FROM", "MX", "mail.acme.io", "10 feedback-smtp.us-east-1.amazonses.com")
    assert rules.compare_record(mx, ["20 feedback-smtp.us-east-1.amazonses.com."])["status"] == "OK"
    assert rules.compare_record(mx, ["10 mx.acme.io."])["status"] == "UPDATE"
    no_region = _record("MAIL_FROM", "MX", "mail.acme.io", "10 feedback-smtp.<region>.amazonses.com")
    assert rules.compare_record(no_region, [])["status"] == "ADD"
    unknown = rules.compare_record(no_region, ["10 mx.acme.io."])
    assert unknown["status"] == "UNKNOWN" and unknown["current_value"] == "10 mx.acme.io."
    dmarc = _record("DMARC", "TXT", "_dmarc.acme.io", "v=DMARC1; p=none")
    assert rules.compare_record(dmarc, [])["status"] == "ADD"
    assert rules.compare_record(dmarc, ["v=DMARC1; p=reject"])["current_value"] == "v=DMARC1; p=reject"
    assert rules.compare_record(dmarc, ["v=DMARC1; p=reject", "v=DMARC1; p=none"])["status"] == "UPDATE"


def test_tbe_record_summary_counts_every_status():
    records = [{"status": "OK"}, {"status": "ADD"}, {"status": "ADD"}, {"status": "UNKNOWN"}]
    assert rules.record_summary(records) == {"OK": 1, "ADD": 2, "UPDATE": 0, "UNKNOWN": 1}
