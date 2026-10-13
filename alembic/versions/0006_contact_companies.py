"""Contacts associated with several companies, with association labels (CM-014)

Revision ID: 0006_contact_companies
Revises: 0005_tenant_sending_domains
Create Date: 2026-10-12

contact_companies holds every company a contact is associated with, with a free-text label
("Decision maker", "Billing", ...) and is_primary. prospects.account_id stays the primary
company (everything that reads it keeps working); the primary link row mirrors it.

Backfill: one primary link per contact that already has an account_id. Idempotent
(CREATE TABLE IF NOT EXISTS, INSERT IGNORE on the unique contact + company key); on an empty
database 0001_baseline's create_all() has already created the table from the model.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0006_contact_companies"
down_revision: Union[str, Sequence[str], None] = "0005_tenant_sending_domains"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS contact_companies ("
        " link_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " prospect_id VARCHAR(36) NOT NULL,"
        " account_id VARCHAR(36) NOT NULL,"
        " label VARCHAR(100) NULL,"
        " is_primary BOOL NOT NULL DEFAULT 0,"
        " created_by VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " CONSTRAINT uq_contact_companies_contact_account UNIQUE (prospect_id, account_id),"
        " INDEX ix_contact_companies_account (account_id),"
        " INDEX ix_contact_companies_tenant (tenant_id),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (prospect_id) REFERENCES prospects (prospect_id) ON DELETE CASCADE,"
        " FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE,"
        " FOREIGN KEY (created_by) REFERENCES users (user_id)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))
    added = conn.execute(text(
        "INSERT IGNORE INTO contact_companies (link_id, tenant_id, prospect_id, account_id, is_primary) "
        "SELECT UUID(), p.tenant_id, p.prospect_id, p.account_id, 1 FROM prospects p "
        "JOIN accounts a ON a.account_id = p.account_id "
        "WHERE p.account_id IS NOT NULL AND p.tenant_id = a.tenant_id")).rowcount
    if added:
        logger.info("contact_companies: %d primary link(s) backfilled from prospects.account_id", added)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS contact_companies")
