# app/services/subject_variant_service.py
"""
A/B testing of email subject lines (BR-SF-18 / RP-104) - database side.

- CRUD of a step's 2-4 subject variants (A..D, optional weight).
- Assignment: every pre-scheduled email of the step gets a variant
  (email_messages.subject_variant) and that variant's subject; existing
  assignments are kept when the schedule is re-planned. When the variant set or
  weights change, only NOT-YET-SENT emails are re-balanced / re-rendered.
- Stats per variant with the campaign-wide definitions from
  campaign_analytics_service (sent / unique opened / unique clicked / replied).
- Winner by open rate once every variant has MIN_SENDS_PER_VARIANT sends, and
  promotion of the winner: the step's remaining unsent emails switch to it.

The pure rules live in app/services/ab_testing.py.
"""

from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Query, Session

from app.core.clock import utcnow
from app.models.campaign import Campaign
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.models.subject_variant import EmailSubjectVariant
from app.services import ab_testing
from app.services.audit_service import AuditService
from app.services.campaign_analytics_service import sent_message_filters, unique_prospects_expr

# Emails that have not left yet and may still change subject (same set as
# CampaignEmailService.propagate_template_edit). SENDING (claimed) is excluded.
PENDING_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")


class SubjectVariantError(Exception):
    """A refused request; the router turns it into an HTTP error."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def check_subject(subject: str) -> Tuple[str, List[str]]:
    """Run a variant subject through the existing subject/compliance checks.

    Disallowed phrases (compliance_service) are refused with 400; spam-trigger and
    subject-line lint findings are returned as warnings, as for templates.
    """
    from app.services.compliance_service import (
        _check_subject_line,
        calculate_spam_score,
        check_disallowed_phrases,
    )

    clean = (subject or "").strip()
    if not clean:
        raise SubjectVariantError(422, "Subject must not be empty")
    if len(clean) > ab_testing.MAX_SUBJECT_LENGTH:
        raise SubjectVariantError(422, f"Subject must be at most {ab_testing.MAX_SUBJECT_LENGTH} characters")
    if any(ch in clean for ch in ("\r", "\n", "\x00")):
        raise SubjectVariantError(422, "Subject must be a single line")
    has_disallowed, found = check_disallowed_phrases(clean, "")
    if has_disallowed:
        raise SubjectVariantError(400, f"Subject contains disallowed phrases: {', '.join(found[:5])}")
    warnings = [w.message for w in _check_subject_line(clean)]
    _, triggers = calculate_spam_score(clean, "")
    if triggers:
        warnings.append(f"Spam trigger words: {', '.join(triggers[:5])}")
    return clean, warnings


class SubjectVariantService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    # ── lookups ─────────────────────────────────────────────────

    def get_step(self, campaign_id: str, step_number: int) -> EmailSequence:
        seq = self.db.query(EmailSequence).filter(
            EmailSequence.campaign_id == campaign_id,
            EmailSequence.step_number == step_number,
        ).first()
        if not seq:
            raise SubjectVariantError(404, "Sequence step not found")
        return seq

    def list_variants(self, sequence_id: str) -> List[EmailSubjectVariant]:
        rows = self.db.query(EmailSubjectVariant).filter(EmailSubjectVariant.sequence_id == sequence_id).all()
        return sorted(rows, key=lambda v: ab_testing.VARIANT_LABELS.index(v.label))

    def _get_variant(self, sequence_id: str, label: str) -> EmailSubjectVariant:
        norm = ab_testing.normalize_label(label)
        variant = None
        if norm:
            variant = self.db.query(EmailSubjectVariant).filter(
                EmailSubjectVariant.sequence_id == sequence_id,
                EmailSubjectVariant.label == norm,
            ).first()
        if not variant:
            raise SubjectVariantError(404, "Subject variant not found")
        return variant

    @staticmethod
    def promoted(variants: Iterable[EmailSubjectVariant]) -> Optional[EmailSubjectVariant]:
        return next((v for v in variants if v.is_winner), None)

    def _step_messages(self, sequence_id: str) -> Query:
        return self.db.query(EmailMessage).filter(
            EmailMessage.sequence_id == sequence_id,
            func.coalesce(EmailMessage.direction, "OUTBOUND") != "INBOUND",
        )

    def _sent_counts(self, sequence_id: str) -> Dict[str, int]:
        rows = self.db.query(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).filter(
            EmailMessage.sequence_id == sequence_id,
            EmailMessage.subject_variant.isnot(None),
            *sent_message_filters(),
        ).group_by(EmailMessage.subject_variant).all()
        return {label: count for label, count in rows}

    def _ensure_not_promoted(self, variants: List[EmailSubjectVariant]) -> None:
        winner = self.promoted(variants)
        if winner:
            raise SubjectVariantError(
                409, f"Variant {winner.label} was already promoted; the A/B test for this step is closed")

    # ── rendering / re-balancing unsent emails ─────────────────

    def _renderer(self, campaign: Campaign) -> Callable[..., str]:
        from app.services.campaign_email_service import CampaignEmailService
        from app.services.sender_identity import campaign_sender_name

        email_svc = CampaignEmailService(self.db)
        sender_name = campaign_sender_name(self.db, campaign)

        def render(subject: str, prospect: Prospect) -> str:
            return email_svc.substitute_placeholders(
                subject or "", prospect, sender_name, campaign.cta_link, campaign.sender_title)
        return render

    def resync_step(self, campaign: Campaign, seq: EmailSequence, rerender: Optional[Set[str]] = None) -> int:
        """Bring the step's unsent emails in line with its current variants.

        Sent emails are never touched. With >= 2 variants the unsent ones are
        re-balanced (keeping their variant where the split allows); after a
        promotion they all take the winner; with < 2 variants they go back to the
        template subject. rerender: labels whose subject text changed, so their
        unsent emails are re-rendered even when their label stays.
        Returns the number of emails whose subject or variant changed.
        """
        rerender = rerender or set()
        variants = self.list_variants(seq.sequence_id)
        by_label = {v.label: v for v in variants}
        pending = self._step_messages(seq.sequence_id).filter(
            EmailMessage.status.in_(PENDING_STATUSES),
            EmailMessage.sent_at.is_(None),
        ).all()
        if not pending:
            return 0

        winner = self.promoted(variants)
        if winner:
            plan = {m.message_id: winner.label for m in pending}
        else:
            plan = ab_testing.rebalance(
                {m.message_id: m.subject_variant for m in pending},
                [(v.label, v.weight) for v in variants],
                self._sent_counts(seq.sequence_id),
            )

        changed_msgs = [m for m in pending
                        if plan.get(m.message_id) != m.subject_variant
                        or (m.subject_variant and m.subject_variant in rerender)]
        if not changed_msgs:
            return 0

        prospect_ids = {m.prospect_id for m in changed_msgs}
        prospects = {p.prospect_id: p for p in self.db.query(Prospect).filter(
            Prospect.prospect_id.in_(prospect_ids)).all()}
        template_ids = {m.template_id for m in changed_msgs if m.template_id}
        templates = {t.template_id: t for t in self.db.query(EmailTemplate).filter(
            EmailTemplate.template_id.in_(template_ids)).all()} if template_ids else {}
        render = self._renderer(campaign)

        updated = 0
        for msg in changed_msgs:
            prospect = prospects.get(msg.prospect_id)
            if not prospect:
                continue
            label = plan.get(msg.message_id)
            if label:
                msg.subject = render(by_label[label].subject, prospect)
            else:
                template = templates.get(msg.template_id)
                if template and template.subject:
                    msg.subject = render(template.subject, prospect)
            msg.subject_variant = label
            updated += 1
        self.db.flush()
        return updated

    def assignments_for_new_messages(
        self, campaign_id: str, needed: Dict[str, List[str]],
    ) -> Dict[Tuple[str, str], EmailSubjectVariant]:
        """Variants for emails about to be pre-scheduled.

        needed: {sequence_id: [prospect_id, ...]} of the emails to be created.
        Returns {(prospect_id, sequence_id): variant}. New emails are balanced
        against the step's existing assignments, so the split stays even when
        prospects are enrolled later; after a promotion they all get the winner.
        """
        if not needed:
            return {}
        rows = self.db.query(EmailSubjectVariant).filter(
            EmailSubjectVariant.campaign_id == campaign_id,
            EmailSubjectVariant.sequence_id.in_(list(needed)),
        ).all()
        by_seq: Dict[str, List[EmailSubjectVariant]] = defaultdict(list)
        for v in rows:
            by_seq[v.sequence_id].append(v)

        result: Dict[Tuple[str, str], EmailSubjectVariant] = {}
        for sequence_id, prospect_ids in needed.items():
            variants = by_seq.get(sequence_id) or []
            if not variants:
                continue
            by_label = {v.label: v for v in variants}
            winner = self.promoted(variants)
            if winner:
                for pid in prospect_ids:
                    result[(pid, sequence_id)] = winner
                continue
            if len(variants) < ab_testing.MIN_VARIANTS:
                continue
            counts = dict(self.db.query(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).filter(
                EmailMessage.sequence_id == sequence_id,
                EmailMessage.subject_variant.isnot(None),
                EmailMessage.status != "CANCELLED",
            ).group_by(EmailMessage.subject_variant).all())
            labels = ab_testing.assign_variants(prospect_ids, [(v.label, v.weight) for v in variants], counts)
            for pid, label in labels.items():
                result[(pid, sequence_id)] = by_label[label]
        return result

    # ── CRUD ────────────────────────────────────────────────────

    def create(self, campaign: Campaign, seq: EmailSequence, subject: str, weight: Optional[int],
               user_id: str) -> Tuple[EmailSubjectVariant, List[str], int]:
        variants = self.list_variants(seq.sequence_id)
        self._ensure_not_promoted(variants)
        if len(variants) >= ab_testing.MAX_VARIANTS:
            raise SubjectVariantError(409, f"A step can have at most {ab_testing.MAX_VARIANTS} subject variants")
        clean, warnings = check_subject(subject)
        if any(v.subject == clean for v in variants):
            raise SubjectVariantError(409, "Another variant of this step already has this subject")
        label = ab_testing.next_label(v.label for v in variants)
        variant = EmailSubjectVariant(
            tenant_id=campaign.tenant_id, campaign_id=campaign.campaign_id, sequence_id=seq.sequence_id,
            label=label, subject=clean, weight=weight or ab_testing.DEFAULT_WEIGHT, created_by=user_id,
        )
        self.db.add(variant)
        self.db.flush()
        moved = self.resync_step(campaign, seq)
        self._audit(campaign, user_id, AuditService.ACTION_CREATE)
        return variant, warnings, moved

    def update(self, campaign: Campaign, seq: EmailSequence, label: str, subject: Optional[str],
               weight: Optional[int], user_id: str) -> Tuple[EmailSubjectVariant, List[str], int]:
        variants = self.list_variants(seq.sequence_id)
        self._ensure_not_promoted(variants)
        variant = self._get_variant(seq.sequence_id, label)
        warnings: List[str] = []
        rerender: Set[str] = set()
        if subject is not None:
            clean, warnings = check_subject(subject)
            if clean != variant.subject:
                sent = self._sent_counts(seq.sequence_id).get(variant.label, 0)
                if sent:
                    raise SubjectVariantError(
                        409, f"Variant {variant.label} was already sent {sent} time(s); its subject can no longer "
                             f"change. Add a new variant instead.")
                if any(v.subject == clean and v.label != variant.label for v in variants):
                    raise SubjectVariantError(409, "Another variant of this step already has this subject")
                variant.subject = clean
                rerender.add(variant.label)
        if weight is not None:
            variant.weight = weight
        self.db.flush()
        moved = self.resync_step(campaign, seq, rerender)
        self._audit(campaign, user_id, AuditService.ACTION_UPDATE)
        return variant, warnings, moved

    def delete(self, campaign: Campaign, seq: EmailSequence, label: str, user_id: str) -> int:
        variants = self.list_variants(seq.sequence_id)
        self._ensure_not_promoted(variants)
        variant = self._get_variant(seq.sequence_id, label)
        sent = self._sent_counts(seq.sequence_id).get(variant.label, 0)
        if sent:
            raise SubjectVariantError(
                409, f"Variant {variant.label} was already sent {sent} time(s) and is kept for the results")
        self.db.delete(variant)
        self.db.flush()
        moved = self.resync_step(campaign, seq)
        self._audit(campaign, user_id, AuditService.ACTION_DELETE)
        return moved

    def _audit(self, campaign: Campaign, user_id: str, action: str) -> None:
        self.audit.log_action(tenant_id=campaign.tenant_id, user_id=user_id, action=action,
                              entity_type=AuditService.ENTITY_SEQUENCE, entity_id=campaign.campaign_id)

    # ── stats / winner / promotion ──────────────────────────────

    def stats(self, seq: EmailSequence) -> Dict:
        variants = self.list_variants(seq.sequence_id)
        sid = seq.sequence_id

        def grouped(query) -> Dict[str, int]:
            return {label: count for label, count in query.group_by(EmailMessage.subject_variant).all()}

        sent = self._sent_counts(sid)
        scheduled = grouped(self.db.query(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).filter(
            EmailMessage.sequence_id == sid, EmailMessage.subject_variant.isnot(None),
            EmailMessage.status.in_(PENDING_STATUSES), EmailMessage.sent_at.is_(None)))

        def unique_with(event_type: int) -> Dict[str, int]:
            return grouped(self.db.query(EmailMessage.subject_variant, unique_prospects_expr()).join(
                EmailEvent, EmailEvent.message_id == EmailMessage.message_id
            ).filter(EmailMessage.sequence_id == sid, EmailMessage.subject_variant.isnot(None),
                     EmailEvent.event_type == event_type))

        opened = unique_with(EmailEvent.EVENT_OPEN)
        clicked = unique_with(EmailEvent.EVENT_CLICK)
        replied = grouped(self.db.query(
            EmailMessage.subject_variant, func.count(func.distinct(EmailEvent.message_id))
        ).join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id).filter(
            EmailMessage.sequence_id == sid, EmailMessage.subject_variant.isnot(None),
            EmailEvent.event_type == EmailEvent.EVENT_REPLY))

        rows = []
        for v in variants:
            s, o, c, r = sent.get(v.label, 0), opened.get(v.label, 0), clicked.get(v.label, 0), replied.get(v.label, 0)
            rows.append({
                "label": v.label, "subject": v.subject, "weight": v.weight, "is_winner": bool(v.is_winner),
                "sent": s, "scheduled": scheduled.get(v.label, 0), "opened": o, "clicked": c, "replied": r,
                # same rate definitions as the step analytics: open/reply over sent, click over opened
                "open_rate": ab_testing.rate(o, s), "click_rate": ab_testing.rate(c, o),
                "reply_rate": ab_testing.rate(r, s),
            })
        winner_row = self.promoted(variants)
        winner = ab_testing.pick_winner(rows, ab_testing.MIN_SENDS_PER_VARIANT,
                                        winner_row.label if winner_row else None)
        unassigned_sent = self.db.query(func.count(EmailMessage.message_id)).filter(
            EmailMessage.sequence_id == sid, EmailMessage.subject_variant.is_(None), *sent_message_filters(),
        ).scalar() or 0
        return {
            "step": seq.step_number, "sequence_id": sid, "variants": rows, "winner": winner,
            "ab_active": len(variants) >= ab_testing.MIN_VARIANTS,
            "min_sends_per_variant": ab_testing.MIN_SENDS_PER_VARIANT,
            "unassigned_sent": unassigned_sent,
        }

    def promote(self, campaign: Campaign, seq: EmailSequence, label: str, user_id: str) -> Dict:
        variants = self.list_variants(seq.sequence_id)
        self._ensure_not_promoted(variants)
        variant = self._get_variant(seq.sequence_id, label)
        decision = self.stats(seq)["winner"]
        if decision["status"] != ab_testing.WINNER_FOUND or decision["label"] != variant.label:
            raise SubjectVariantError(409, f"Variant {variant.label} is not the winner: {decision['reason']}")
        variant.is_winner = True
        variant.promoted_at = utcnow()
        variant.promoted_by = user_id
        self.db.flush()
        moved = self.resync_step(campaign, seq)
        self._audit(campaign, user_id, AuditService.ACTION_UPDATE)
        return {"promoted_label": variant.label, "subject": variant.subject, "updated_messages": moved}

    def messages(self, seq: EmailSequence, label: Optional[str], limit: int, offset: int) -> Dict:
        """The step's emails with their variant and subject (sent history first)."""
        q = self.db.query(EmailMessage, Prospect.first_name, Prospect.last_name).join(
            Prospect, Prospect.prospect_id == EmailMessage.prospect_id
        ).filter(
            EmailMessage.sequence_id == seq.sequence_id,
            func.coalesce(EmailMessage.direction, "OUTBOUND") != "INBOUND",
        )
        if label:
            q = q.filter(EmailMessage.subject_variant == label)
        total = q.count()
        rows = q.order_by(EmailMessage.sent_at.is_(None), EmailMessage.sent_at.desc(),
                          EmailMessage.scheduled_at.asc(), EmailMessage.message_id).offset(offset).limit(limit).all()
        return {
            "total": total,
            "items": [{
                "message_id": m.message_id, "prospect_id": m.prospect_id,
                "name": f"{first or ''} {last or ''}".strip(), "to_email": m.to_email,
                "subject": m.subject, "subject_variant": m.subject_variant, "status": m.status,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
            } for m, first, last in rows],
        }
