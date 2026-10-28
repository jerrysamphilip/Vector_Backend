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
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from app.core.clock import utcnow
from app.core.config import settings
from app.models.campaign import Campaign
from app.models.email_message import EmailEvent
from app.models.email_sequence import EmailSequence
from app.models.prospect import Prospect
from app.models.subject_variant import EmailSubjectVariant
from app.repositories.campaigns import CampaignsUnitOfWork
from app.services import ab_testing
from app.services.audit_service import AuditService
from app.services.compliance_service import _check_subject_line, calculate_spam_score, check_disallowed_phrases
from app.services.email_render import RenderContext, substitute_placeholders
from app.services.sender_identity import campaign_sender_name, get_sender_identity

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
    """The SQL lives in app/repositories/campaigns_content.py and campaigns_messages.py. Nothing here commits:
    the caller (the subject-variant routes, the pre-schedule) owns the transaction."""

    def __init__(self, db: Any = None, *, uow: Any = None, audit: Any = None) -> None:
        self.uow = uow if uow is not None else CampaignsUnitOfWork.for_session(db)
        self.db = db
        self._audit = audit

    @property
    def audit(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService(self.uow.session)
        return self._audit

    # ── lookups ─────────────────────────────────────────────────

    def get_step(self, campaign_id: str, step_number: int) -> EmailSequence:
        seq = self.uow.content.step(campaign_id, step_number)
        if not seq:
            raise SubjectVariantError(404, "Sequence step not found")
        return seq

    def list_variants(self, sequence_id: str) -> List[EmailSubjectVariant]:
        return sorted(self.uow.content.variants(sequence_id), key=lambda v: ab_testing.VARIANT_LABELS.index(v.label))

    def _get_variant(self, sequence_id: str, label: str) -> EmailSubjectVariant:
        norm = ab_testing.normalize_label(label)
        variant = self.uow.content.variant(sequence_id, norm) if norm else None
        if not variant:
            raise SubjectVariantError(404, "Subject variant not found")
        return variant

    @staticmethod
    def promoted(variants: Iterable[EmailSubjectVariant]) -> Optional[EmailSubjectVariant]:
        return next((v for v in variants if v.is_winner), None)

    def _sent_counts(self, sequence_id: str) -> Dict[str, int]:
        return self.uow.messages.variant_sent_counts(sequence_id)

    def _ensure_not_promoted(self, variants: List[EmailSubjectVariant]) -> None:
        winner = self.promoted(variants)
        if winner:
            raise SubjectVariantError(
                409, f"Variant {winner.label} was already promoted; the A/B test for this step is closed")

    # ── rendering / re-balancing unsent emails ─────────────────

    def _renderer(self, campaign: Campaign) -> Callable[..., str]:
        ctx = RenderContext(sender_name=campaign_sender_name(self.uow.session, campaign),
                            sender_title=campaign.sender_title, cta_link=campaign.cta_link,
                            our_company=get_sender_identity(campaign.tenant_id).company_name,
                            default_sender_name=settings.SENDER_NAME)

        def render(subject: str, prospect: Prospect) -> str:
            return substitute_placeholders(subject or "", prospect, ctx)
        return render

    def _plan(self, seq: EmailSequence, variants: List[EmailSubjectVariant], pending: list) -> Dict[str, Any]:
        winner = self.promoted(variants)
        if winner:
            return {m.message_id: winner.label for m in pending}
        return ab_testing.rebalance(
            {m.message_id: m.subject_variant for m in pending},
            [(v.label, v.weight) for v in variants],
            self._sent_counts(seq.sequence_id),
        )

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
        pending = self.uow.messages.unsent_step_emails(seq.sequence_id)
        if not pending:
            return 0
        plan = self._plan(seq, variants, pending)
        changed_msgs = [m for m in pending
                        if plan.get(m.message_id) != m.subject_variant
                        or (m.subject_variant and m.subject_variant in rerender)]
        if not changed_msgs:
            return 0
        prospects = self.uow.campaigns.contacts(m.prospect_id for m in changed_msgs)
        templates = self.uow.content.templates_by_ids(m.template_id for m in changed_msgs if m.template_id)
        render = self._renderer(campaign)
        updated = 0
        for msg in changed_msgs:
            prospect = prospects.get(msg.prospect_id)
            if not prospect:
                continue
            label = plan.get(msg.message_id)
            template = templates.get(msg.template_id)
            if label:
                msg.subject = render(by_label[label].subject, prospect)
            elif template and template.subject:
                msg.subject = render(template.subject, prospect)
            msg.subject_variant = label
            updated += 1
        self.uow.messages.flush()
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
        by_seq: Dict[str, List[EmailSubjectVariant]] = defaultdict(list)
        for v in self.uow.content.variants_of_steps(campaign_id, list(needed)):
            by_seq[v.sequence_id].append(v)
        result: Dict[Tuple[str, str], EmailSubjectVariant] = {}
        for sequence_id, prospect_ids in needed.items():
            variants = by_seq.get(sequence_id) or []
            winner = self.promoted(variants)
            if winner:
                result.update({(pid, sequence_id): winner for pid in prospect_ids})
            elif len(variants) >= ab_testing.MIN_VARIANTS:
                by_label = {v.label: v for v in variants}
                counts = self.uow.messages.variant_assignment_counts(sequence_id)
                labels = ab_testing.assign_variants(prospect_ids, [(v.label, v.weight) for v in variants], counts)
                result.update({(pid, sequence_id): by_label[label] for pid, label in labels.items()})
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
        self.uow.content.add(variant)
        self.uow.content.flush()
        moved = self.resync_step(campaign, seq)
        self._audit_action(campaign, user_id, AuditService.ACTION_CREATE)
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
                self._check_subject_change(variant, variants, clean)
                variant.subject = clean
                rerender.add(variant.label)
        if weight is not None:
            variant.weight = weight
        self.uow.content.flush()
        moved = self.resync_step(campaign, seq, rerender)
        self._audit_action(campaign, user_id, AuditService.ACTION_UPDATE)
        return variant, warnings, moved

    def _check_subject_change(self, variant: EmailSubjectVariant, variants: List[EmailSubjectVariant],
                              clean: str) -> None:
        sent = self._sent_counts(variant.sequence_id).get(variant.label, 0)
        if sent:
            raise SubjectVariantError(
                409, f"Variant {variant.label} was already sent {sent} time(s); its subject can no longer "
                     f"change. Add a new variant instead.")
        if any(v.subject == clean and v.label != variant.label for v in variants):
            raise SubjectVariantError(409, "Another variant of this step already has this subject")

    def delete(self, campaign: Campaign, seq: EmailSequence, label: str, user_id: str) -> int:
        variants = self.list_variants(seq.sequence_id)
        self._ensure_not_promoted(variants)
        variant = self._get_variant(seq.sequence_id, label)
        sent = self._sent_counts(seq.sequence_id).get(variant.label, 0)
        if sent:
            raise SubjectVariantError(
                409, f"Variant {variant.label} was already sent {sent} time(s) and is kept for the results")
        self.uow.content.delete(variant)
        self.uow.content.flush()
        moved = self.resync_step(campaign, seq)
        self._audit_action(campaign, user_id, AuditService.ACTION_DELETE)
        return moved

    def _audit_action(self, campaign: Campaign, user_id: str, action: str) -> None:
        self.audit.log_action(tenant_id=campaign.tenant_id, user_id=user_id, action=action,
                              entity_type=AuditService.ENTITY_SEQUENCE, entity_id=campaign.campaign_id)

    # ── stats / winner / promotion ──────────────────────────────

    def stats(self, seq: EmailSequence) -> Dict:
        variants = self.list_variants(seq.sequence_id)
        sid = seq.sequence_id
        msgs = self.uow.messages
        sent = self._sent_counts(sid)
        scheduled = msgs.variant_scheduled_counts(sid)
        opened = msgs.variant_unique_contacts(sid, EmailEvent.EVENT_OPEN)
        clicked = msgs.variant_unique_contacts(sid, EmailEvent.EVENT_CLICK)
        replied = msgs.variant_replied(sid)
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
        return {
            "step": seq.step_number, "sequence_id": sid, "variants": rows, "winner": winner,
            "ab_active": len(variants) >= ab_testing.MIN_VARIANTS,
            "min_sends_per_variant": ab_testing.MIN_SENDS_PER_VARIANT,
            "unassigned_sent": msgs.unassigned_sent(sid),
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
        self.uow.content.flush()
        moved = self.resync_step(campaign, seq)
        self._audit_action(campaign, user_id, AuditService.ACTION_UPDATE)
        return {"promoted_label": variant.label, "subject": variant.subject, "updated_messages": moved}

    def messages(self, seq: EmailSequence, label: Optional[str], limit: int, offset: int) -> Dict:
        """The step's emails with their variant and subject (sent history first)."""
        total, rows = self.uow.messages.step_page(seq.sequence_id, label, limit, offset)
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
