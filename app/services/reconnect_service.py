# app/services/reconnect_service.py
"""
Bounce re-validation service for re-engaging hard-bounced prospects.

ADMIN/SUPER_ADMIN only:
- Clears hard-bounce auto-suppression records
- Updates prospect email and marks valid
- Resets BOUNCED enrollments to RECONNECT_ELIGIBLE

Voluntary unsubscribes are never overrideable.
"""

import logging
from typing import List, Optional, Dict

from app.core.clock import utcnow
from app.models.prospect import GlobalUnsubscribe
from app.repositories.revalidation import Session, SqlAlchemyRevalidationRepository

logger = logging.getLogger(__name__)

HARD_BOUNCE_REASON_PREFIXES = (
    "Hard bounce:",
    "HARD_BOUNCE_AUTO_SUPPRESSED:",
)

VOLUNTARY_UNSUB_MIN_DAYS = 180


class ProspectRevalidationService:
    """
    ADMIN-only service for re-validating bounced prospects.

    STRICT RULE:
    Only clears GlobalUnsubscribe records whose `reason` starts with a
    known hard-bounce prefix written by the system. Never touches
    voluntary unsubscribes.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SqlAlchemyRevalidationRepository(db)

    def _is_voluntary_unsub(self, record: GlobalUnsubscribe) -> bool:
        reason = (record.reason or "").strip()
        for prefix in HARD_BOUNCE_REASON_PREFIXES:
            if reason.startswith(prefix):
                return False
        return True

    def _is_within_lockout(self, record: GlobalUnsubscribe) -> bool:
        if record.suppression_expires_at is None:
            return True
        return record.suppression_expires_at > utcnow()

    def get_blocked_reason(self, tenant_id: str, email: str) -> Optional[str]:
        record = self.repo.suppression(tenant_id, email)

        if not record:
            return None

        if self._is_voluntary_unsub(record):
            expires = record.suppression_expires_at
            if expires:
                return (
                    f"Voluntary unsubscribe - suppression expires on "
                    f"{expires.strftime('%Y-%m-%d')}. Cannot be overridden."
                )
            return "Permanent voluntary unsubscribe - cannot be overridden."

        return f"Hard bounce auto-suppression: {record.reason}"

    def revalidate_prospect(
        self,
        tenant_id: str,
        prospect_id: str,
        new_email: str,
        reason: str,
        performed_by: str,
    ) -> Dict:
        new_email = new_email.strip().lower()

        prospect = self.repo.contact(tenant_id, prospect_id)
        if not prospect:
            raise ValueError(f"Prospect {prospect_id!r} not found.")

        old_email = prospect.email

        conflict = self.repo.other_contact_with_email(tenant_id, new_email, prospect_id)
        if conflict:
            raise ValueError(f"Email {new_email!r} is already registered to another prospect.")

        old_unsub = self.repo.suppression(tenant_id, old_email)

        cleared_suppression = False

        if old_unsub:
            if self._is_voluntary_unsub(old_unsub):
                expires = old_unsub.suppression_expires_at
                msg = (
                    "Cannot re-validate: prospect voluntarily unsubscribed. "
                    + (
                        f"Suppression expires on {expires.strftime('%Y-%m-%d')}."
                        if expires
                        else "Suppression is permanent."
                    )
                )
                raise PermissionError(msg)

            self.repo.delete(old_unsub)
            cleared_suppression = True
            logger.warning(
                f"[Revalidation] Cleared hard-bounce suppression for {old_email!r} "
                f"by admin {performed_by!r}. Reason: {reason!r}"
            )

        prospect.email = new_email
        prospect.is_valid_email = True
        self.repo.flush()

        updated_cp = self.repo.bounced_enrolments(prospect_id)
        for cp in updated_cp:
            cp.status = "RECONNECT_ELIGIBLE"

        self.db.commit()

        logger.info(
            f"[Revalidation] Prospect {prospect_id!r} re-validated: "
            f"{old_email!r} -> {new_email!r}. "
            f"Campaigns reset: {len(updated_cp)}. "
            f"Suppression cleared: {cleared_suppression}."
        )

        return {
            "prospect_id": prospect_id,
            "old_email": old_email,
            "new_email": new_email,
            "suppression_cleared": cleared_suppression,
            "campaigns_reset": len(updated_cp),
            "message": (
                f"Prospect re-validated. "
                f"{len(updated_cp)} campaign enrollment(s) are now RECONNECT_ELIGIBLE."
            ),
        }

    def list_bounced_prospects(self, tenant_id: str) -> List[Dict]:
        hard_bounce_emails = set()
        all_unsubs = self.repo.suppressions(tenant_id)
        for rec in all_unsubs:
            if not self._is_voluntary_unsub(rec):
                hard_bounce_emails.add(rec.email)

        if not hard_bounce_emails:
            return []

        bounced_prospects = self.repo.contacts_with_emails(tenant_id, hard_bounce_emails)

        result = []
        for p in bounced_prospects:
            cp_count = self.repo.count_bounced_enrolments(p.prospect_id)
            unsub = self.repo.suppression(tenant_id, p.email)

            result.append({
                "prospect_id": p.prospect_id,
                "full_name": p.full_name,
                "email": p.email,
                "company_name": p.company_name,
                "designation": p.designation,
                "bounced_campaigns_count": cp_count,
                "bounce_reason": unsub.reason if unsub else "Unknown",
                "suppressed_at": unsub.unsubscribed_at if unsub else None,
            })

        return result
