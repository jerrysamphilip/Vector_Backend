"""Raise sending_inboxes daily_limit floor to 500/day

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-01 00:00:00.000000

Raises the post-warmup-graduation sending ceiling from 100/day to 500/day:
- Changes the column default for new rows to 500.
- Backfills existing inboxes with daily_limit < 500 up to 500, so every
  mailbox (not just newly created ones) can sustain at least 500/day once
  warmup graduates (or immediately if warmup is disabled / overridden via
  max_emails_per_day). Rows already at or above 500 are left untouched.

This does NOT change the warmup ramp itself (days 0-9, 5..120/day) — that
curve is a deliverability protection for brand-new mailboxes building sender
reputation, not a capacity limit, and is intentionally left alone.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'sending_inboxes', 'daily_limit',
        existing_type=sa.Integer(),
        server_default='500',
    )
    op.execute(
        "UPDATE sending_inboxes SET daily_limit = 500 "
        "WHERE daily_limit IS NULL OR daily_limit < 500"
    )


def downgrade():
    op.alter_column(
        'sending_inboxes', 'daily_limit',
        existing_type=sa.Integer(),
        server_default='100',
    )
    # Data backfill is not reversible (original per-mailbox values are not
    # recoverable) — the downgrade only restores the column default.
