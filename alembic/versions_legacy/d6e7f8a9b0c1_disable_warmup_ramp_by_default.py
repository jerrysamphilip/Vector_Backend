"""Disable warmup ramp by default — mailboxes send at daily_limit immediately

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-01 00:00:00.000000

Explicit request: stop gating new/existing mailboxes behind the 10-day
warmup ramp — send at full configured daily_limit (500+) from day one.

- Changes the warmup_enabled column default to False for new rows.
- Backfills every existing inbox that is still mid-ramp (warmup_enabled=True
  and warmup_day < 10) to warmup_enabled=False / warmup_status='DISABLED',
  so they immediately jump to daily_limit instead of finishing the ramp.
- Inboxes that had ALREADY graduated (warmup_day >= 10) are left untouched —
  they were already sending at daily_limit, warmup_enabled being True or
  False makes no behavioral difference for them.

NOTE (deliverability): the ramp itself (WARMUP_SCHEDULE in
app/models/sending_inbox.py) is not deleted — it remains available as a
per-mailbox opt-in toggle for anyone who wants to gradually build reputation
on a brand-new domain. Disabling it by default removes that protection for
any newly connected mailbox going forward; this was an explicit, informed
product decision, not an oversight.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd6e7f8a9b0c1'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'sending_inboxes', 'warmup_enabled',
        existing_type=sa.Boolean(),
        server_default=sa.false(),
    )
    op.execute(
        "UPDATE sending_inboxes "
        "SET warmup_enabled = FALSE, warmup_status = 'DISABLED' "
        "WHERE warmup_enabled = TRUE AND (warmup_day IS NULL OR warmup_day < 10)"
    )


def downgrade():
    op.alter_column(
        'sending_inboxes', 'warmup_enabled',
        existing_type=sa.Boolean(),
        server_default=sa.true(),
    )
    # Data backfill is not reversible (which rows were mid-ramp vs.
    # already-disabled-for-other-reasons is not recoverable) — the
    # downgrade only restores the column default.
