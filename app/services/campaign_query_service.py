# app/services/campaign_query_service.py
"""
Read-side campaign queries used by the campaign API: tenant-scoped lookups, the analytics
dashboard, the template list, per-step emails and the schedule timeline (SQL in
app/repositories/campaigns_reads.py).

The ``*_for`` route methods scope the campaign to the caller's tenant first; the other methods take a
campaign the caller already scoped.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

from app.core.clock import iso_utc, utcnow
from app.core.errors import NotFoundError
from app.domain.reports import delivered
from app.models.campaign import Campaign
from app.models.email_message import EmailEvent
from app.models.email_sequence import EmailSequence
from app.repositories.campaigns import CampaignsUnitOfWork

CAMPAIGN_NOT_FOUND = "Campaign not found"
SEQUENCE_STEP_NOT_FOUND = "Sequence step not found"

# Lead-status buckets shown on the analytics donut, in display order
LEAD_STATUSES = ["ACTIVE", "REPLIED", "BOUNCED", "UNSUBSCRIBED", "COMPLETED"]
# Event types counted per day / per step on the analytics page
_ANALYTICS_EVENT_TYPES = [
    EmailEvent.EVENT_OPEN,
    EmailEvent.EVENT_REPLY,
    EmailEvent.EVENT_BOUNCE,
    EmailEvent.EVENT_SENDER_BOUNCE,
    EmailEvent.EVENT_POSITIVE_REPLY,
    EmailEvent.EVENT_REPLY_OOO,
]
# Daily bar-chart field for each (non-open) event type
_DAILY_FIELD_BY_EVENT = {
    EmailEvent.EVENT_REPLY: "replied",
    EmailEvent.EVENT_BOUNCE: "bounced",
    EmailEvent.EVENT_SENDER_BOUNCE: "sender_bounced",
    EmailEvent.EVENT_POSITIVE_REPLY: "positive_replied",
    EmailEvent.EVENT_REPLY_OOO: "ooo",
}
SCHEDULE_PROSPECTS_PER_BUCKET = 5
STEP_REPLIES_LIMIT = 15


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested)
# ──────────────────────────────────────────────────────────────────────────────

def pct(numerator: int, denominator: int) -> float:
    """numerator / denominator as a percentage rounded to 2 places; 0.0 when denominator is 0."""
    return round(numerator / denominator * 100, 2) if denominator > 0 else 0.0


def step_label(step_number: int) -> str:
    """'Initial Email' for step 1, 'Follow-up N' for the rest."""
    return "Initial Email" if step_number == 1 else f"Follow-up {step_number - 1}"


def build_lead_stats(total_prospects: int, status_map: Dict[str, int]) -> Dict[str, Any]:
    """Donut data: one slice per known status with a non-zero count, plus 'Other' for the rest."""
    data: List[Dict[str, Any]] = [
        {"name": s.title(), "value": status_map.get(s, 0)} for s in LEAD_STATUSES if status_map.get(s, 0) > 0
    ]
    known = sum(item["value"] for item in data)
    if known < total_prospects:
        data.append({"name": "Other", "value": total_prospects - known})
    return {"total": total_prospects, "data": data}


def build_schedule(messages: Iterable[Any], step_by_sequence: Dict[str, int]) -> Dict[str, Any]:
    """
    Group message rows into (day, step, SENT|QUEUED) buckets, counting each prospect once per
    bucket and listing the first few. Rows need: message_id, scheduled_at, sent_at, status,
    sequence_id, prospect_id, subject_variant, first_name, last_name, email.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    total_sent = 0
    total_scheduled = 0

    for msg in messages:
        date_val = msg.sent_at or msg.scheduled_at
        if not date_val:
            continue

        date_str = date_val.date().isoformat()
        status = "SENT" if msg.status == "SENT" else "QUEUED"
        step = step_by_sequence.get(msg.sequence_id, 1)
        key = f"{date_str}_{step}_{status}"

        bucket = buckets.setdefault(key, {
            "date": date_str,
            "status": status,
            "step_number": step,
            "prospect_count": 0,
            "prospects": [],
            "seen_prospect_ids": set(),
        })

        # De-duplicate same prospect in same day/step/status bucket.
        if msg.prospect_id in bucket["seen_prospect_ids"]:
            continue
        bucket["seen_prospect_ids"].add(msg.prospect_id)
        bucket["prospect_count"] += 1

        if len(bucket["prospects"]) < SCHEDULE_PROSPECTS_PER_BUCKET:
            bucket["prospects"].append({
                "name": f"{msg.first_name or ''} {msg.last_name or ''}".strip(),
                "email": msg.email,
                "message_id": msg.message_id,
                # Real per-message timestamps so the UI can confirm exactly whether/when THIS
                # prospect's email went out, not only the day-level bucket it is grouped under.
                "sent_at": iso_utc(msg.sent_at),
                "scheduled_at": iso_utc(msg.scheduled_at),
                "message_status": msg.status,
                "subject_variant": msg.subject_variant,  # A/B subject test (BR-SF-18)
            })

        if status == "SENT":
            total_sent += 1
        else:
            total_scheduled += 1

    schedule = [
        {k: v[k] for k in ("date", "status", "prospect_count", "step_number", "prospects")}
        for v in buckets.values()
    ]
    schedule.sort(key=lambda x: (x["date"], x["step_number"]))
    return {"schedule": schedule, "total_scheduled": total_scheduled, "total_sent": total_sent}


# ──────────────────────────────────────────────────────────────────────────────
# Queries
# ──────────────────────────────────────────────────────────────────────────────

class CampaignQueryService:
    """Tenant-scoped lookups and read models for one campaign."""

    def __init__(self, db: Any = None, *, uow: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db

    @classmethod
    def for_session(cls, db: Any) -> "CampaignQueryService":
        return cls(db)

    # ── Scoping ──────────────────────────────────────────────────────────────

    def get_campaign_for_tenant(self, campaign_id: str, tenant_id: str) -> Campaign:
        """The campaign if it belongs to the tenant; NotFoundError for missing and cross-tenant alike."""
        campaign = self.uow.campaigns.get(tenant_id, campaign_id)
        if not campaign:
            raise NotFoundError(CAMPAIGN_NOT_FOUND)
        return campaign

    def require_sequence_step_for_tenant(self, sequence_id: str, tenant_id: str) -> EmailSequence:
        """The sequence step if its campaign belongs to the tenant; NotFoundError otherwise."""
        step = self.uow.content.step_for_tenant(tenant_id, sequence_id)
        if not step:
            raise NotFoundError(SEQUENCE_STEP_NOT_FOUND)
        return step

    # ── Routes (scoped to the caller's tenant) ───────────────────────────────

    def analytics_for(self, user: Any, campaign_id: str, days: int) -> Dict[str, Any]:
        self.get_campaign_for_tenant(campaign_id, user.tenant_id)
        return self.get_analytics(campaign_id, days)

    def templates_for(self, user: Any, campaign_id: str) -> List[Dict[str, Any]]:
        self.get_campaign_for_tenant(campaign_id, user.tenant_id)
        return self.list_templates(campaign_id)

    def step_emails_for(self, user: Any, campaign_id: str, step: int) -> Dict[str, Any]:
        self.get_campaign_for_tenant(campaign_id, user.tenant_id)
        return self.get_step_emails(campaign_id, step)

    def schedule_for(self, user: Any, campaign_id: str) -> Dict[str, Any]:
        self.get_campaign_for_tenant(campaign_id, user.tenant_id)
        return self.get_schedule(campaign_id)

    # ── Small counts ─────────────────────────────────────────────────────────

    def _prospects_with_status(self, campaign_id: str, statuses: List[str]) -> int:
        return self.uow.reads.contacts_with_status(campaign_id, statuses)

    def _step_numbers(self, campaign_id: str) -> Dict[str, int]:
        return self.uow.content.step_numbers(campaign_id)

    # ── Analytics ────────────────────────────────────────────────────────────

    def get_analytics(self, campaign_id: str, days: int, today: Optional[date] = None) -> Dict[str, Any]:
        """Daily bar chart, per-step metrics, lead status breakdown and lifetime totals."""
        end_date = today or utcnow().date()
        start_date = end_date - timedelta(days=days - 1)
        metrics = self._lifetime_metrics(campaign_id)
        return {
            "campaign_id": campaign_id,
            "bar_data": self._daily_metrics(campaign_id, start_date, end_date),
            "sequences": self._sequence_metrics(campaign_id),
            "lead_stats": self._lead_stats(campaign_id),
            "total_messages_sent": metrics["sent_count"],
            "metrics": metrics,
        }

    def _daily_metrics(self, campaign_id: str, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        reads = self.uow.reads
        sent_map = reads.sent_per_day(campaign_id, start_date)
        # Opens per day = unique prospects who opened that day. Every bar counts that day's events only: the
        # lifetime enrolment statuses (OPENED / REPLIED) are not added to the last bar (UAT 3-5); manual marks
        # record an event (campaign_tracking_service), so they show on the day they were made.
        open_map = reads.openers_per_day(campaign_id, start_date)
        by_field: Dict[str, Dict[Any, int]] = {f: {} for f in _DAILY_FIELD_BY_EVENT.values()}
        for day, event_type, count in reads.events_per_day(campaign_id, start_date, _ANALYTICS_EVENT_TYPES):
            name = _DAILY_FIELD_BY_EVENT.get(event_type)
            if name:
                by_field[name][day] = count

        bar_data = []
        current_date = start_date
        while current_date <= end_date:
            bar_data.append({
                "date": current_date.isoformat(),
                "sent": sent_map.get(current_date, 0),
                "opened": open_map.get(current_date, 0),
                "replied": by_field["replied"].get(current_date, 0),
                "bounced": by_field["bounced"].get(current_date, 0),
                "sender_bounced": by_field["sender_bounced"].get(current_date, 0),
                "positive_replied": by_field["positive_replied"].get(current_date, 0),
                "ooo": by_field["ooo"].get(current_date, 0),
            })
            current_date += timedelta(days=1)
        return bar_data

    def _sequence_metrics(self, campaign_id: str) -> List[Dict[str, Any]]:
        reads = self.uow.reads
        sequences = self.uow.content.steps(campaign_id)
        sequence_ids = [s.sequence_id for s in sequences]
        seq_sent_map = reads.sent_per_step(campaign_id, sequence_ids)
        seq_openers_map = reads.unique_contacts_per_step(campaign_id, sequence_ids, EmailEvent.EVENT_OPEN)
        seq_clickers_map = reads.unique_contacts_per_step(campaign_id, sequence_ids, EmailEvent.EVENT_CLICK)
        event_map: Dict[str, Dict[int, int]] = {}
        for seq_id, e_type, count in reads.event_messages_per_step(campaign_id, sequence_ids, _ANALYTICS_EVENT_TYPES):
            event_map.setdefault(seq_id, {})[e_type] = count

        # Orphaned sent messages (manual/other) are ignored so no "Email 0 - Manual/Other" step shows.
        result = []
        for seq in sequences:
            s_id = seq.sequence_id
            sent_count = seq_sent_map.get(s_id, 0)
            e_counts = event_map.get(s_id, {})
            opened_count = seq_openers_map.get(s_id, 0)
            clicked_count = seq_clickers_map.get(s_id, 0)
            replied_count = e_counts.get(EmailEvent.EVENT_REPLY, 0)
            bounced_count = e_counts.get(EmailEvent.EVENT_BOUNCE, 0)
            if seq.step_number == 1:
                replied_count = max(replied_count, self._prospects_with_status(campaign_id, ["REPLIED"]))
            result.append({
                "step": seq.step_number,
                "type": step_label(seq.step_number),
                "sent": sent_count,
                "opened_count": opened_count,
                "replied_count": replied_count,
                "bounced_count": bounced_count,
                "open_rate": pct(opened_count, delivered(sent_count, bounced_count)),
                "click_rate": pct(clicked_count, opened_count),
                "reply_rate": pct(replied_count, delivered(sent_count, bounced_count)),
                "bounce_rate": pct(bounced_count, sent_count),
                "positive_replied_count": e_counts.get(EmailEvent.EVENT_POSITIVE_REPLY, 0),
                "ooo_count": e_counts.get(EmailEvent.EVENT_REPLY_OOO, 0),
                "sender_bounced_count": e_counts.get(EmailEvent.EVENT_SENDER_BOUNCE, 0),
            })
        result.sort(key=lambda x: (x["step"] if x["step"] > 0 else 99))
        return result

    def _lead_stats(self, campaign_id: str) -> Dict[str, Any]:
        reads = self.uow.reads
        return build_lead_stats(reads.enrolment_count(campaign_id), reads.status_counts(campaign_id, LEAD_STATUSES))

    def _lifetime_metrics(self, campaign_id: str) -> Dict[str, Any]:
        reads = self.uow.reads
        total_sent = reads.sent_total(campaign_id)
        total_opened = max(reads.unique_contacts(campaign_id, EmailEvent.EVENT_OPEN),
                           self._prospects_with_status(campaign_id, ["OPENED", "REPLIED"]))
        total_replied = max(reads.messages_with_event(campaign_id, EmailEvent.EVENT_REPLY),
                            self._prospects_with_status(campaign_id, ["REPLIED"]))
        total_bounced = reads.messages_with_event(campaign_id, EmailEvent.EVENT_BOUNCE)
        # Clicked = unique prospects with a click event (shared definition).
        total_clicked = reads.unique_contacts(campaign_id, EmailEvent.EVENT_CLICK)
        return {
            "sent_count": total_sent,
            "opened_count": total_opened,
            "clicked_count": total_clicked,
            "replied_count": total_replied,
            "positive_replied_count": reads.messages_with_event(campaign_id, EmailEvent.EVENT_POSITIVE_REPLY),
            "ooo_count": reads.messages_with_event(campaign_id, EmailEvent.EVENT_REPLY_OOO),
            "bounced_count": total_bounced,
            "sender_bounced_count": reads.messages_with_event(campaign_id, EmailEvent.EVENT_SENDER_BOUNCE),
            "unsubscribed_count": reads.messages_with_event(campaign_id, EmailEvent.EVENT_UNSUBSCRIBE),
            "open_rate": pct(total_opened, delivered(total_sent, total_bounced)),
            "click_rate": pct(total_clicked, total_opened),
            "reply_rate": pct(total_replied, delivered(total_sent, total_bounced)),
            "bounce_rate": pct(total_bounced, total_sent),
        }

    # ── Templates / steps / schedule ─────────────────────────────────────────

    def list_templates(self, campaign_id: str) -> List[Dict[str, Any]]:
        """The campaign's templates with their step number, ordered by step then persona."""
        step_by_sequence = self._step_numbers(campaign_id)
        templates = [
            {
                "template_id": tmpl.template_id,
                "sequence_id": tmpl.sequence_id,
                "step_number": step_by_sequence.get(tmpl.sequence_id or ""),
                "designation": tmpl.designation,
                "subject": tmpl.subject,
                "body": tmpl.body,
                "tone": tmpl.tone,
                "ai_model": tmpl.ai_model,
                "is_ai_generated": tmpl.is_ai_generated or False,
                "is_approved": tmpl.approved_by is not None,
                "approved_at": tmpl.approved_at,
                "created_at": tmpl.created_at,
            }
            for tmpl in self.uow.content.templates(campaign_id)
        ]
        templates.sort(key=lambda x: (x["step_number"] or 0, x["designation"] or ""))
        return templates

    def get_step_emails(self, campaign_id: str, step: int) -> Dict[str, Any]:
        """A step's template plus the latest inbound replies to messages sent from that step."""
        seq = self.uow.content.step(campaign_id, step)
        if not seq:
            raise NotFoundError("Sequence not found")
        template = next((t for t in self.uow.content.templates(campaign_id) if t.sequence_id == seq.sequence_id),
                        None)
        replies = self.uow.reads.recent_step_replies(campaign_id, seq.sequence_id, STEP_REPLIES_LIMIT)
        return {
            "step": step,
            "template": {
                "subject": template.subject if template else "No subject",
                "body": template.body if template else "No body content"
            },
            "recent_replies": [{
                "from_email": r.from_email,
                "subject": r.subject or "No Subject",
                "body_text": r.body_text or "",
                "received_at": iso_utc(r.sent_at)
            } for r in replies],
        }

    def get_schedule(self, campaign_id: str) -> Dict[str, Any]:
        """Schedule timeline: messages bucketed by day, step and sent/queued."""
        return build_schedule(self.uow.reads.schedule_rows(campaign_id), self._step_numbers(campaign_id))
