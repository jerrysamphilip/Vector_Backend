"""Outbound webhooks (BRD v2.0 BR-SF-20)

Revision ID: 0011_webhooks
Revises: 0010_ab_tests
Create Date: 2026-10-13

webhook_endpoints:   per workspace, an endpoint URL, its signing secret (encrypted at rest,
                     "enc:v1:" Fernet like mailbox passwords), the subscribed event types,
                     the active flag and the consecutive-failure counter used for auto-disable.
webhook_deliveries:  the outbox: one row per event per endpoint, written in the same transaction
                     as the change; the worker's "webhooks" loop sends due PENDING rows.
Idempotent (CREATE TABLE IF NOT EXISTS).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_webhooks"
down_revision: Union[str, Sequence[str], None] = "0010_ab_tests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS webhook_endpoints ("
        " endpoint_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " url VARCHAR(2000) NOT NULL,"
        " description VARCHAR(255) NULL,"
        " secret VARCHAR(1024) NOT NULL,"
        " event_types JSON NOT NULL,"
        " active BOOLEAN NOT NULL DEFAULT TRUE,"
        " consecutive_failures INTEGER NOT NULL DEFAULT 0,"
        " disabled_reason VARCHAR(255) NULL,"
        " disabled_at TIMESTAMP NULL,"
        " last_success_at TIMESTAMP NULL,"
        " last_failure_at TIMESTAMP NULL,"
        " created_by VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
        " INDEX ix_webhook_endpoints_tenant (tenant_id, active),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (created_by) REFERENCES users (user_id) ON DELETE SET NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    op.execute(
        "CREATE TABLE IF NOT EXISTS webhook_deliveries ("
        " delivery_id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " tenant_id VARCHAR(36) NOT NULL,"
        " endpoint_id VARCHAR(36) NOT NULL,"
        " event_id VARCHAR(36) NOT NULL,"
        " event_type VARCHAR(50) NOT NULL,"
        " payload MEDIUMTEXT NOT NULL,"
        " status VARCHAR(12) NOT NULL DEFAULT 'PENDING',"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " response_code INTEGER NULL,"
        " last_error VARCHAR(500) NULL,"
        " next_attempt_at TIMESTAMP NULL,"
        " last_attempt_at TIMESTAMP NULL,"
        " delivered_at TIMESTAMP NULL,"
        " duration_ms INTEGER NULL,"
        " is_test BOOLEAN NOT NULL DEFAULT FALSE,"
        " redelivery_of VARCHAR(36) NULL,"
        " created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,"
        " INDEX ix_webhook_deliveries_due (status, next_attempt_at),"
        " INDEX ix_webhook_deliveries_endpoint (endpoint_id, created_at),"
        " INDEX ix_webhook_deliveries_tenant (tenant_id, created_at),"
        " FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),"
        " FOREIGN KEY (endpoint_id) REFERENCES webhook_endpoints (endpoint_id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS webhook_endpoints")
