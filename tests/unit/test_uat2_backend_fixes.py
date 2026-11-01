"""UAT round 2 backend fixes (no database): the create-campaign unsubscribe mode (B-01) and plain-text
email snippets on the contact, deal and lead-suggestion timelines (A1-U4b). The follow-up reply rate (C-08),
import preview company counts (D-02) and deal-on-contact lifecycle (E-02) are covered next to their
services in test_a6_reports_services.py, test_a7_imports_services.py and test_a3_sales_services.py."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.campaign_schema import CampaignCreate, CampaignUpdate
from app.services import contact_query_service as cq
from app.services import sales_deals
from app.services.campaign_crud_service import CampaignCRUDService
from app.utils.email_utils import plain_text_snippet

HTML_BODY = ("<p>Hi Bea,</p><p>Step 3: <strong>bold claim</strong> and <em>italic&nbsp;note</em> &amp; more</p>"
             "<br/><ul><li>one</li><li>two</li></ul>")
PLAIN = "Hi Bea, Step 3: bold claim and italic note & more one two"


# ── B-01: unsubscribe_mode on create ────────────────────────────────

def test_create_schema_keeps_the_unsubscribe_mode():
    assert CampaignCreate(campaign_name="X").unsubscribe_mode == "plain"
    assert CampaignCreate(campaign_name="X", unsubscribe_mode="html").unsubscribe_mode == "html"
    assert CampaignCreate(campaign_name="X", unsubscribe_mode=" HTML ").unsubscribe_mode == "html"


@pytest.mark.parametrize("schema", [CampaignCreate, CampaignUpdate])
@pytest.mark.parametrize("bad", ["link", "", "x" * 20])
def test_unsubscribe_mode_must_be_plain_or_html(schema, bad):
    with pytest.raises(ValidationError, match="unsubscribe_mode must be 'plain' or 'html'"):
        schema(campaign_name="X", unsubscribe_mode=bad)


def test_update_schema_still_accepts_both_modes_and_leaving_it_out():
    assert CampaignUpdate(unsubscribe_mode="html").changes() == {"unsubscribe_mode": "html"}
    assert "unsubscribe_mode" not in CampaignUpdate(campaign_name="Y").changes()


class _Repo:
    def __init__(self):
        self.added = []

    def add(self, tenant_id, campaign):
        self.added.append((tenant_id, campaign))

    def flush(self):
        pass


class _Audit:
    def __init__(self):
        self.logged = []

    def log_action(self, **kw):
        self.logged.append(kw)


@pytest.mark.parametrize("mode", ["html", "plain"])
def test_create_persists_the_chosen_unsubscribe_mode(mode):
    uow = SimpleNamespace(campaigns=_Repo(), session=None)
    svc = CampaignCRUDService(uow=uow, audit=_Audit())
    campaign = svc.create("t1", "u1", CampaignCreate(campaign_name="Wizard", unsubscribe_mode=mode))
    assert campaign.unsubscribe_mode == mode and uow.campaigns.added == [("t1", campaign)]


# ── A1-U4b: plain-text timeline snippets ────────────────────────────

def test_plain_text_snippet_strips_tags_decodes_entities_and_collapses_whitespace():
    assert plain_text_snippet(HTML_BODY, 400) == PLAIN
    assert plain_text_snippet("<style>p{color:red}</style>Hello\n\n  there", 400) == "Hello there"
    assert plain_text_snippet("plain  text\nbody", 400) == "plain text body"
    assert plain_text_snippet("a < b and c > d", 400) == "a < b and c > d"
    assert plain_text_snippet(HTML_BODY, 10) == PLAIN[:10]
    assert plain_text_snippet(None, 400) == plain_text_snippet("", 400) == ""


def _message(**kw):
    base = dict(message_id="m1", direction="OUTBOUND", from_email="me@x.io", to_email="bea@x.io",
                reply_label_source=None, subject="Step 3", subject_variant=None, body_text=HTML_BODY, status="SENT",
                final_status=None, failure_reason=None, campaign_id="c1", conversation_id=None,
                sent_at=datetime(2026, 10, 1, 9), scheduled_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_contact_timeline_snippet_is_plain_text():
    class Repo:
        def emails_of(self, prospect_id, limit):
            return [(_message(), "Campaign")]

        def email_events_of(self, prospect_id, kinds, limit):
            return []

    items = cq._email_items(Repo(), SimpleNamespace(prospect_id="p1"), 10)
    assert items[0]["email"]["snippet"] == PLAIN


def test_deal_timeline_snippet_is_plain_text():
    item = sales_deals._email_item(_message(direction="INBOUND"))
    assert item["kind"] == "EMAIL_RECEIVED" and item["email"]["snippet"] == PLAIN
