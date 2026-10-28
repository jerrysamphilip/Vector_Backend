"""
Shared status vocabularies (chunk D5): every value a status column may hold, in one place.

The database enforces them with ``CHECK (col IN (...))`` constraints (alembic 0029_status_checks; the models
declare the same ``CheckConstraint`` through :func:`check_in`). Where an aggregate already owns the vocabulary
(``app.domain.identity``, ``app.domain.imports``, ``app.domain.sales``, ``app.domain.webhooks``) it is reused,
not copied. Vocabularies that still live in a service or util module are restated here and pinned to their
source by tests/unit/test_dba_status.py, so the two cannot drift.

A ``NULL`` status passes a CHECK constraint (``NULL IN (...)`` is unknown, not false), so nullable status
columns keep accepting NULL. MySQL compares with the column collation (utf8mb4_0900_ai_ci): case-insensitive.

Pure: no I/O, no SQLAlchemy.
"""

from __future__ import annotations

from typing import Iterable

from app.domain.identity import InvitationStatus, TenantStatus, UserStatus
from app.domain.imports import JobStatus
from app.domain.sales import LEAD_STAGES, PROPOSAL_STATUSES, StageStatus
from app.domain.webhooks import DELIVERY_STATUSES


def _values(cls: type) -> tuple[str, ...]:
    """The upper-case string constants of a plain constants class (UserStatus, TenantStatus, ...)."""
    return tuple(v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str))


# ── Sending ────────────────────────────────────────────────────────

# email_messages.status: the send pipeline (QUEUED/SCHEDULED -> SENDING -> SENT) and its outcomes. RECEIVED: an
# INBOUND row written by an import or a fixture (the IMAP sync writes inbound mail as SENT)
EMAIL_MESSAGE_STATUSES = (
    "QUEUED", "SCHEDULED", "SENDING", "SENT", "DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED", "FAILED",
    "CANCELLED", "PAUSED_BY_CAMPAIGN", "RECEIVED",
)
# email_messages.final_status (BR-DF-04): app.services.send_safety.FINAL_STATUSES + UNCONFIRMED (no SES answer)
EMAIL_FINAL_STATUSES = ("DELIVERED", "BOUNCED", "COMPLAINED", "REJECTED", "FAILED", "UNCONFIRMED")

# ── Campaigns ──────────────────────────────────────────────────────

# campaigns.status: app.schemas.campaign_schema.CampaignStatus
CAMPAIGN_STATUSES = ("DRAFT", "ACTIVE", "PAUSED", "COMPLETED")
# campaign_prospects.status: the keys of app.utils.campaign_prospect_status.STATUS_PRIORITY
CAMPAIGN_PROSPECT_STATUSES = (
    "ACTIVE", "OPENED", "COMPLETED", "PAUSED", "RECONNECT_ELIGIBLE", "BOUNCED", "REPLIED", "UNSUBSCRIBED",
)

# ── Sales ──────────────────────────────────────────────────────────

LEAD_STAGE_VALUES = tuple(LEAD_STAGES)                          # leads.stage
OPPORTUNITY_STATUSES = tuple(s.value for s in StageStatus)      # opportunities.status (from the stage)
PROPOSAL_STATUS_VALUES = tuple(PROPOSAL_STATUSES)               # proposals.status
# proposals.approval_status (BR-SF-17, app/services/quote_approval.py)
PROPOSAL_APPROVAL_STATUSES = ("NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED")

# ── Identity, imports, webhooks ──────────────────────────

USER_STATUSES = _values(UserStatus)                 # users.status
TENANT_STATUSES = _values(TenantStatus)             # tenants.status
INVITATION_STATUSES = _values(InvitationStatus)     # invitations.status
IMPORT_JOB_STATUSES = tuple(s.value for s in JobStatus)  # import_jobs.status
WEBHOOK_DELIVERY_STATUSES = tuple(DELIVERY_STATUSES)  # webhook_deliveries.status

# (table, column) -> allowed values: what 0029_status_checks and the models' CheckConstraints enforce
STATUS_CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("email_messages", "status"): EMAIL_MESSAGE_STATUSES,
    ("email_messages", "final_status"): EMAIL_FINAL_STATUSES,
    ("campaigns", "status"): CAMPAIGN_STATUSES,
    ("campaign_prospects", "status"): CAMPAIGN_PROSPECT_STATUSES,
    ("leads", "stage"): LEAD_STAGE_VALUES,
    ("opportunities", "status"): OPPORTUNITY_STATUSES,
    ("proposals", "status"): PROPOSAL_STATUS_VALUES,
    ("proposals", "approval_status"): PROPOSAL_APPROVAL_STATUSES,
    ("users", "status"): USER_STATUSES,
    ("tenants", "status"): TENANT_STATUSES,
    ("invitations", "status"): INVITATION_STATUSES,
    ("import_jobs", "status"): IMPORT_JOB_STATUSES,
    ("webhook_deliveries", "status"): WEBHOOK_DELIVERY_STATUSES,
}


def check_name(table: str, column: str) -> str:
    """The constraint name: ck_<table>_<column> (unique per schema, at most 64 characters)."""
    return f"ck_{table}_{column}"


def check_sql(column: str, values: Iterable[str]) -> str:
    """``col IN ('A', 'B')``: the CHECK expression (values are fixed upper-case identifiers, never input)."""
    allowed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({allowed})"


def check_for(table: str, column: str) -> tuple[str, str]:
    """(name, expression) of the status check on table.column."""
    return check_name(table, column), check_sql(column, STATUS_CHECKS[(table, column)])
