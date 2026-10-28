"""
D5 unit: the shared status vocabularies (app/domain/status.py), the models' CHECK constraints and the literal
copy in alembic/versions/0029_status_checks.py agree; vocabularies restated from service / util modules are
pinned to their source. No database.
"""
import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401  (registers every table)
from app.domain import status
from app.models.base import Base

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0029_status_checks.py"


def _migration():
    spec = importlib.util.spec_from_file_location("m0029", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dba_vocabularies_reuse_the_domain_enums():
    from app.domain.identity import InvitationStatus, TenantStatus, UserStatus
    from app.domain.imports import JobStatus
    from app.domain.sales import LEAD_STAGES, PROPOSAL_STATUSES
    from app.domain.webhooks import DELIVERY_STATUSES

    assert status.USER_STATUSES == ("ACTIVE", "INVITED", "INACTIVE", "SUSPENDED") and UserStatus.ACTIVE in status.USER_STATUSES
    assert set(status.TENANT_STATUSES) == {TenantStatus.ACTIVE, TenantStatus.SUSPENDED, TenantStatus.INACTIVE,
                                           TenantStatus.DELETED}
    assert set(status.INVITATION_STATUSES) == {InvitationStatus.PENDING, InvitationStatus.ACCEPTED,
                                               InvitationStatus.EXPIRED, InvitationStatus.REVOKED}
    assert status.IMPORT_JOB_STATUSES == tuple(j.value for j in JobStatus)
    assert status.LEAD_STAGE_VALUES == tuple(LEAD_STAGES)
    assert status.PROPOSAL_STATUS_VALUES == tuple(PROPOSAL_STATUSES)
    assert status.OPPORTUNITY_STATUSES == ("OPEN", "WON", "LOST")
    assert status.WEBHOOK_DELIVERY_STATUSES == DELIVERY_STATUSES


def test_dba_restated_vocabularies_match_their_source():
    from app.schemas.campaign_schema import CampaignStatus
    from app.services.quote_approval import APPROVAL_STATUSES
    from app.services.send_safety import FINAL_STATUSES
    from app.utils.campaign_prospect_status import STATUS_PRIORITY

    assert set(status.CAMPAIGN_STATUSES) == {s.value for s in CampaignStatus}
    assert set(status.CAMPAIGN_PROSPECT_STATUSES) == set(STATUS_PRIORITY)
    assert set(status.EMAIL_FINAL_STATUSES) == {*FINAL_STATUSES, "UNCONFIRMED"}
    assert status.PROPOSAL_APPROVAL_STATUSES == APPROVAL_STATUSES


def test_dba_message_statuses_cover_every_pipeline_constant():
    from app.domain.campaigns import PENDING_MESSAGE_STATUSES
    from app.domain.campaigns_edit import EDITABLE_MESSAGE_STATUSES
    from app.domain.sending import DUE_STATUSES, FOLLOW_UP_STATUSES, PENDING_STEP_STATUSES, RESUME_STATUSES
    from app.domain.ses_events import OUTCOME_STATUSES

    used = {*PENDING_MESSAGE_STATUSES, *EDITABLE_MESSAGE_STATUSES, *DUE_STATUSES, *FOLLOW_UP_STATUSES,
            *PENDING_STEP_STATUSES, *RESUME_STATUSES, *OUTCOME_STATUSES}
    assert used <= set(status.EMAIL_MESSAGE_STATUSES)


def test_dba_check_sql_and_names():
    assert status.check_name("users", "status") == "ck_users_status"
    assert status.check_sql("status", ("A", "B")) == "status IN ('A', 'B')"
    assert status.check_for("campaigns", "status") == (
        "ck_campaigns_status", "status IN ('DRAFT', 'ACTIVE', 'PAUSED', 'COMPLETED')")
    assert all(len(status.check_name(t, c)) <= 64 for t, c in status.STATUS_CHECKS)


def test_dba_models_declare_every_status_check():
    for (table, column), values in status.STATUS_CHECKS.items():
        checks = {c.name: str(c.sqltext) for c in Base.metadata.tables[table].constraints
                  if isinstance(c, CheckConstraint)}
        name, sql = status.check_for(table, column)
        assert checks.get(name) == sql, (table, column)
        ddl = str(CreateTable(Base.metadata.tables[table]).compile(dialect=mysql.dialect()))
        assert f"CONSTRAINT {name} CHECK ({sql})" in ddl


def test_dba_migration_copies_the_vocabularies():
    m = _migration()
    flat = {(t, c): v for t, cols in m.CHECKS.items() for c, v in cols.items()}
    assert flat == status.STATUS_CHECKS
    assert m.revision == "0029_status_checks" and m.down_revision == "0028_child_tenant_ids"
    assert m._name("users", "status") == status.check_name("users", "status")
    assert m._in_list(("A", "B")) == "'A', 'B'"
