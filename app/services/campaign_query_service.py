# app/services/campaign_query_service.py
"""
Read-side campaign queries used by the campaign API: tenant-scoped lookups, the analytics
dashboard, the template list, per-step emails and the schedule timeline.

Everything here is tenant-agnostic except the ``*_for_tenant`` lookups; callers scope the
campaign first (``get_campaign_for_tenant``) and then ask for its data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.services.campaign_analytics_service import (
    sent_message_filters,
    unique_clickers_expr,
    unique_openers_expr,
)

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
                "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
                "scheduled_at": msg.scheduled_at.isoformat() if msg.scheduled_at else None,
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

    def __init__(self, db: Session) -> None:
        self.db = db

    # ── Scoping ──────────────────────────────────────────────────────────────

    def get_campaign_for_tenant(self, campaign_id: str, tenant_id: str) -> Campaign:
        """The campaign if it belongs to the tenant; NotFoundError for missing and cross-tenant alike."""
        campaign = self.db.query(Campaign).filter(Campaign.campaign_id == campaign_id).first()
        if not campaign or campaign.tenant_id != tenant_id:
            raise NotFoundError(CAMPAIGN_NOT_FOUND)
        return campaign

    def require_sequence_step_for_tenant(self, sequence_id: str, tenant_id: str) -> EmailSequence:
        """The sequence step if its campaign belongs to the tenant; NotFoundError otherwise."""
        step = (
            self.db.query(EmailSequence)
            .join(Campaign, Campaign.campaign_id == EmailSequence.campaign_id)
            .filter(EmailSequence.sequence_id == sequence_id, Campaign.tenant_id == tenant_id)
            .first()
        )
        if not step:
            raise NotFoundError(SEQUENCE_STEP_NOT_FOUND)
        return step

    # ── Small counts ─────────────────────────────────────────────────────────

    def _prospects_with_status(self, campaign_id: str, statuses: List[str]) -> int:
        return self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(statuses),
        ).scalar() or 0

    def _step_numbers(self, campaign_id: str) -> Dict[str, int]:
        return {s.sequence_id: s.step_number for s in self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).all()}

    # ── Analytics ────────────────────────────────────────────────────────────

    def get_analytics(self, campaign_id: str, days: int, today: Optional[date] = None) -> Dict[str, Any]:
        """Daily bar chart, per-step metrics, lead status breakdown and lifetime totals."""
        end_date = today or datetime.utcnow().date()
        start_date = end_date - timedelta(days=days - 1)
        bar_data = self._daily_metrics(campaign_id, start_date, end_date)
        sequences = self._sequence_metrics(campaign_id)
        lead_stats = self._lead_stats(campaign_id)
        metrics = self._lifetime_metrics(campaign_id)
        return {
            "campaign_id": campaign_id,
            "bar_data": bar_data,
            "sequences": sequences,
            "lead_stats": lead_stats,
            "total_messages_sent": metrics["sent_count"],
            "metrics": metrics,
        }

    def _daily_metrics(self, campaign_id: str, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        db = self.db
        sent_map = {row.date: row.count for row in db.query(
            func.date(EmailMessage.sent_at).label('date'),
            func.count(EmailMessage.message_id).label('count')
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.sent_at >= start_date,
            *sent_message_filters(),
        ).group_by(func.date(EmailMessage.sent_at)).all()}

        event_results = db.query(
            func.date(EmailEvent.event_time).label('date'),
            EmailEvent.event_type.label('type'),
            func.count(EmailEvent.event_id).label('count')
        ).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_time >= start_date,
            EmailEvent.event_type.in_(_ANALYTICS_EVENT_TYPES)
        ).group_by(func.date(EmailEvent.event_time), EmailEvent.event_type).all()

        # Opens per day = unique prospects who opened that day
        open_map = {row.date: row.count for row in db.query(
            func.date(EmailEvent.event_time).label('date'),
            unique_openers_expr().label('count')
        ).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailEvent.event_time >= start_date,
            EmailEvent.event_type == EmailEvent.EVENT_OPEN,
        ).group_by(func.date(EmailEvent.event_time)).all()}

        by_field: Dict[str, Dict[Any, int]] = {f: {} for f in _DAILY_FIELD_BY_EVENT.values()}
        for row in event_results:
            field = _DAILY_FIELD_BY_EVENT.get(row.type)
            if field:
                by_field[field][row.date] = row.count

        bar_data = []
        current_date = start_date
        while current_date <= end_date:
            d_opened = open_map.get(current_date, 0)
            d_replied = by_field["replied"].get(current_date, 0)
            if current_date == end_date:
                # Today also reflects prospect statuses set without an event (e.g. manual marks)
                d_opened = max(d_opened, self._prospects_with_status(campaign_id, ["OPENED", "REPLIED"]))
                d_replied = max(d_replied, self._prospects_with_status(campaign_id, ["REPLIED"]))
            bar_data.append({
                "date": current_date.isoformat(),
                "sent": sent_map.get(current_date, 0),
                "opened": d_opened,
                "replied": d_replied,
                "bounced": by_field["bounced"].get(current_date, 0),
                "sender_bounced": by_field["sender_bounced"].get(current_date, 0),
                "positive_replied": by_field["positive_replied"].get(current_date, 0),
                "ooo": by_field["ooo"].get(current_date, 0),
            })
            current_date += timedelta(days=1)
        return bar_data

    def _sequence_metrics(self, campaign_id: str) -> List[Dict[str, Any]]:
        db = self.db
        sequences = db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id
        ).order_by(EmailSequence.step_number).all()
        sequence_ids = [s.sequence_id for s in sequences]

        seq_sent_map = {row[0]: row[1] for row in db.query(
            EmailMessage.sequence_id,
            func.count(EmailMessage.message_id)
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            *sent_message_filters(),
            EmailMessage.sequence_id.in_(sequence_ids)
        ).group_by(EmailMessage.sequence_id).all()}

        def unique_per_step(expr: Any, event_type: str) -> Dict[str, int]:
            return dict(db.query(EmailMessage.sequence_id, expr).join(
                EmailEvent, EmailEvent.message_id == EmailMessage.message_id
            ).filter(
                EmailMessage.campaign_id == campaign_id,
                EmailMessage.sequence_id.in_(sequence_ids),
                EmailEvent.event_type == event_type
            ).group_by(EmailMessage.sequence_id).all())

        seq_openers_map = unique_per_step(unique_openers_expr(), EmailEvent.EVENT_OPEN)
        seq_clickers_map = unique_per_step(unique_clickers_expr(), EmailEvent.EVENT_CLICK)

        event_counts = db.query(
            EmailMessage.sequence_id,
            EmailEvent.event_type,
            func.count(func.distinct(EmailEvent.message_id))
        ).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.sequence_id.in_(sequence_ids),
            EmailEvent.event_type.in_(_ANALYTICS_EVENT_TYPES)
        ).group_by(EmailMessage.sequence_id, EmailEvent.event_type).all()

        event_map: Dict[str, Dict[str, int]] = {}
        for seq_id, e_type, count in event_counts:
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
                "open_rate": pct(opened_count, sent_count),
                "click_rate": pct(clicked_count, opened_count),
                "reply_rate": pct(replied_count, sent_count),
                "bounce_rate": pct(bounced_count, sent_count),
                "positive_replied_count": e_counts.get(EmailEvent.EVENT_POSITIVE_REPLY, 0),
                "ooo_count": e_counts.get(EmailEvent.EVENT_REPLY_OOO, 0),
                "sender_bounced_count": e_counts.get(EmailEvent.EVENT_SENDER_BOUNCE, 0),
            })

        result.sort(key=lambda x: (x["step"] if x["step"] > 0 else 99))
        return result

    def _lead_stats(self, campaign_id: str) -> Dict[str, Any]:
        total_prospects = self.db.query(func.count(CampaignProspect.id)).filter(
            CampaignProspect.campaign_id == campaign_id
        ).scalar() or 0
        status_map = {row[0]: row[1] for row in self.db.query(
            CampaignProspect.status,
            func.count(CampaignProspect.id)
        ).filter(
            CampaignProspect.campaign_id == campaign_id,
            CampaignProspect.status.in_(LEAD_STATUSES)
        ).group_by(CampaignProspect.status).all()}
        return build_lead_stats(total_prospects, status_map)

    def _lifetime_metrics(self, campaign_id: str) -> Dict[str, Any]:
        db = self.db
        total_sent = db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.campaign_id == campaign_id,
            *sent_message_filters(),
        ).scalar() or 0

        all_msg_ids = db.query(EmailMessage.message_id).filter(EmailMessage.campaign_id == campaign_id)

        def unique_prospects(expr: Any, event_type: str) -> int:
            return db.query(expr).join(
                EmailEvent, EmailEvent.message_id == EmailMessage.message_id
            ).filter(
                EmailMessage.campaign_id == campaign_id,
                EmailEvent.event_type == event_type
            ).scalar() or 0

        def messages_with(event_type: str) -> int:
            return db.query(func.count(func.distinct(EmailEvent.message_id))).filter(
                EmailEvent.message_id.in_(all_msg_ids),
                EmailEvent.event_type == event_type
            ).scalar() or 0

        total_opened = max(unique_prospects(unique_openers_expr(), EmailEvent.EVENT_OPEN),
                           self._prospects_with_status(campaign_id, ["OPENED", "REPLIED"]))
        total_replied = max(messages_with(EmailEvent.EVENT_REPLY),
                            self._prospects_with_status(campaign_id, ["REPLIED"]))
        total_bounced = messages_with(EmailEvent.EVENT_BOUNCE)
        # Clicked = unique prospects with a click event (shared definition).
        total_clicked = unique_prospects(unique_clickers_expr(), EmailEvent.EVENT_CLICK)

        return {
            "sent_count": total_sent,
            "opened_count": total_opened,
            "clicked_count": total_clicked,
            "replied_count": total_replied,
            "positive_replied_count": messages_with(EmailEvent.EVENT_POSITIVE_REPLY),
            "ooo_count": messages_with(EmailEvent.EVENT_REPLY_OOO),
            "bounced_count": total_bounced,
            "sender_bounced_count": messages_with(EmailEvent.EVENT_SENDER_BOUNCE),
            "unsubscribed_count": messages_with(EmailEvent.EVENT_UNSUBSCRIBE),
            "open_rate": pct(total_opened, total_sent),
            "click_rate": pct(total_clicked, total_opened),
            "reply_rate": pct(total_replied, total_sent),
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
                "step_number": step_by_sequence.get(tmpl.sequence_id),
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
            for tmpl in self.db.query(EmailTemplate).filter(EmailTemplate.campaign_id == campaign_id).all()
        ]
        templates.sort(key=lambda x: (x["step_number"] or 0, x["designation"] or ""))
        return templates

    def get_step_emails(self, campaign_id: str, step: int) -> Dict[str, Any]:
        """A step's template plus the latest inbound replies to messages sent from that step."""
        db = self.db
        seq = db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == step
        ).first()
        if not seq:
            raise NotFoundError("Sequence not found")

        template = db.query(EmailTemplate).filter(
            EmailTemplate.campaign_id == campaign_id,
            EmailTemplate.sequence_id == seq.sequence_id
        ).first()

        # Inbound messages in the same conversation as an outbound message of this step
        sent_conversations = db.query(EmailMessage.conversation_id).filter(
            EmailMessage.sequence_id == seq.sequence_id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.conversation_id.isnot(None)
        )
        replies = db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.direction == "INBOUND",
            EmailMessage.conversation_id.in_(sent_conversations)
        ).order_by(EmailMessage.sent_at.desc()).limit(STEP_REPLIES_LIMIT).all()

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
                "received_at": r.sent_at.isoformat() if r.sent_at else None
            } for r in replies],
        }

    def get_schedule(self, campaign_id: str) -> Dict[str, Any]:
        """Schedule timeline: messages bucketed by day, step and sent/queued."""
        messages = self.db.query(
            EmailMessage.message_id,
            EmailMessage.scheduled_at,
            EmailMessage.sent_at,
            EmailMessage.status,
            EmailMessage.sequence_id,
            EmailMessage.prospect_id,
            EmailMessage.subject_variant,
            Prospect.first_name,
            Prospect.last_name,
            Prospect.email
        ).join(
            Prospect, Prospect.prospect_id == EmailMessage.prospect_id
        ).filter(
            EmailMessage.campaign_id == campaign_id
        ).all()
        return build_schedule(messages, self._step_numbers(campaign_id))
