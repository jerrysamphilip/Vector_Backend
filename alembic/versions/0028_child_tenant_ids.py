"""tenant_id on child tables, backfilled from the parent; tenant-first indexes; DB-enforced tenant integrity (D3, C-22)

Revision ID: 0028_child_tenant_ids
Revises: 0027_campaign_edit
Create Date: 2026-10-28

D3. Fourteen child tables reached their tenant only through a join to the parent (an e-mail event through its
message, then the contact). Each gets ``tenant_id VARCHAR(36)`` next to the parent's foreign key (where the model has it):

  email_messages (from prospects), email_events (from email_messages), campaign_prospects, campaign_state_events,
  email_sequences, email_templates, automation_rules (from campaigns), email_template_versions,
  email_attachments, compliance_checks (from email_templates), prospect_list_members (from prospect_lists),
  proposal_lines (from proposals), campaign_group_members (from campaign_groups), prospect_personas (from
  prospects).

Not given one: campaign_metrics_realtime (keyed by campaign_id, one row per campaign), imap_sync_state (a worker
cursor keyed by inbox, no foreign key), campaign_inboxes (a plain association table written through the ORM
``secondary`` collection), and the global / per-user tables (ai_prompts, persona_blueprints, platform_flags,
tokens).

Backfill: ``UPDATE child JOIN parent SET child.tenant_id = parent.tenant_id`` in primary-key ranges of 10,000
rows, committed one by one, parents before children (messages before their events, templates before their
versions). Only rows still NULL are touched, so an interrupted run resumes where it stopped. Afterwards the column
becomes NOT NULL when every row has a tenant; a row whose parent is gone keeps NULL, the column stays NULLable
and a warning gives the count (delete or repair those rows, then run the MODIFY in the warning). The NOT NULL
change and the indexes go in one ALTER per table (one rebuild). The indexes lead with tenant_id and follow the
tenant-wide reads (reports, inbox, contact pages): see the models in app/models/.

New rows get the tenant from their parent through app/models/tenant_fill.py (a before_flush listener), so the
services did not change.

C-22. The database itself refuses a link across tenants:

* UNIQUE (tenant_id, <id>) on prospects, accounts and prospect_lists, the targets of
* composite foreign keys contact_companies (tenant_id, prospect_id) -> prospects and (tenant_id, account_id) ->
  accounts (ON DELETE CASCADE, like the single-column keys next to them), prospect_list_members (tenant_id,
  list_id) -> prospect_lists and (tenant_id, prospect_id) -> prospects (no action, like theirs);
* one primary company per contact: a VIRTUAL generated column contact_companies.primary_prospect_id (the
  contact on its primary row, NULL elsewhere) under the unique index ux_contact_companies_one_primary.

A key whose rows already break it (a link whose contact or company belongs to another tenant, a contact with
two primary rows) is NOT created: a warning names the count and the statement to run once the data is repaired.
duplicate_pairs gets no foreign key to prospects: a MERGED pair stays as history after the merged-away contact
is deleted, which a foreign key would either block (no action) or erase (cascade).

Idempotent (information_schema checks everywhere): on a fresh build 0001_baseline's create_all already made
all of it from the models and the revision changes nothing. No row is deleted.

Downgrade drops the keys, the generated column, the indexes and the tenant_id columns (the parents still hold
every tenant, so nothing is lost).
"""
import logging
import time
from typing import Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0028_child_tenant_ids"
down_revision: Union[str, Sequence[str], None] = "0027_campaign_edit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

BATCH = 10_000

# child: (primary key, foreign key, parent, parent primary key, tenant-first indexes {name: columns}).
# Parents before children. Literal copy of app/models/tenant_fill.py TENANT_PARENTS and the models' indexes:
# a migration must not change later.
CHILDREN: dict[str, tuple[str, str, str, str, dict[str, str]]] = {
    "email_messages": ("message_id", "prospect_id", "prospects", "prospect_id", {
        "ix_email_messages_tenant_sent": "tenant_id, direction, sent_at",
        "ix_email_messages_tenant_prospect": "tenant_id, prospect_id"}),
    "email_events": ("event_id", "message_id", "email_messages", "message_id", {
        "ix_email_events_tenant_type_time": "tenant_id, event_type, event_time"}),
    "campaign_prospects": ("id", "campaign_id", "campaigns", "campaign_id", {
        "ix_campaign_prospects_tenant_prospect": "tenant_id, prospect_id"}),
    "campaign_state_events": ("id", "campaign_id", "campaigns", "campaign_id", {
        "ix_campaign_state_events_tenant": "tenant_id, campaign_id, created_at"}),
    "email_sequences": ("sequence_id", "campaign_id", "campaigns", "campaign_id", {
        "ix_email_sequences_tenant_campaign": "tenant_id, campaign_id, step_number"}),
    "email_templates": ("template_id", "campaign_id", "campaigns", "campaign_id", {
        "ix_email_templates_tenant_campaign": "tenant_id, campaign_id"}),
    "automation_rules": ("rule_id", "campaign_id", "campaigns", "campaign_id", {
        "ix_automation_rules_tenant_campaign": "tenant_id, campaign_id"}),
    "email_template_versions": ("version_id", "template_id", "email_templates", "template_id", {
        "ix_email_template_versions_tenant": "tenant_id, template_id"}),
    "email_attachments": ("attachment_id", "template_id", "email_templates", "template_id", {
        "ix_email_attachments_tenant_template": "tenant_id, template_id"}),
    "compliance_checks": ("check_id", "template_id", "email_templates", "template_id", {
        "ix_compliance_checks_tenant_template": "tenant_id, template_id"}),
    "prospect_list_members": ("id", "list_id", "prospect_lists", "list_id", {
        "ix_prospect_list_members_tenant_list": "tenant_id, list_id",
        "ix_prospect_list_members_tenant_prospect": "tenant_id, prospect_id"}),
    "proposal_lines": ("line_id", "proposal_id", "proposals", "proposal_id", {
        "ix_proposal_lines_tenant_proposal": "tenant_id, proposal_id"}),
    "campaign_group_members": ("id", "group_id", "campaign_groups", "group_id", {
        "ix_campaign_group_members_tenant_group": "tenant_id, group_id"}),
    "prospect_personas": ("persona_id", "prospect_id", "prospects", "prospect_id", {
        "ix_prospect_personas_tenant_prospect": "tenant_id, prospect_id"}),
}

# Where the column goes when it is not right after the parent's key (the models' column order)
AFTER = {"campaign_prospects": "prospect_id", "prospect_list_members": "prospect_id"}

# C-22: parents' (tenant_id, id) keys, then the composite foreign keys that point at them
PARENT_KEYS = {
    "prospects": ("uq_prospects_tenant_prospect", "prospect_id"),
    "accounts": ("uq_accounts_tenant_account", "account_id"),
    "prospect_lists": ("uq_prospect_lists_tenant_list", "list_id"),
}
CHILD_INDEXES = {"contact_companies": {
    "ix_contact_companies_tenant_prospect": "tenant_id, prospect_id",
    "ix_contact_companies_tenant_account": "tenant_id, account_id"}}
# name: (child, column, parent, ON DELETE clause)
COMPOSITE_FKS = {
    "fk_contact_companies_tenant_prospect": ("contact_companies", "prospect_id", "prospects", " ON DELETE CASCADE"),
    "fk_contact_companies_tenant_account": ("contact_companies", "account_id", "accounts", " ON DELETE CASCADE"),
    "fk_prospect_list_members_tenant_list": ("prospect_list_members", "list_id", "prospect_lists", ""),
    "fk_prospect_list_members_tenant_prospect": ("prospect_list_members", "prospect_id", "prospects", ""),
}
# Literal copy of app/models/crm.py PRIMARY_KEY_SQL
PRIMARY_KEY_SQL = "CASE WHEN is_primary THEN prospect_id END"
PRIMARY_COLUMN = "primary_prospect_id"
PRIMARY_INDEX = "ux_contact_companies_one_primary"


def _scalar(sql: str, **params: object) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _has_column(table: str, column: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c", t=table, c=column))


def _nullable(table: str, column: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c AND is_nullable = 'YES'", t=table, c=column))


def _has_index(table: str, index: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i", t=table, i=index))


def _has_fk(table: str, name: str) -> bool:
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = DATABASE() "
        "AND table_name = :t AND constraint_name = :n AND constraint_type = 'FOREIGN KEY'", t=table, n=name))


def _commit() -> None:
    """End the batch's transaction so a long backfill neither holds its locks nor loses its work."""
    op.get_bind().execute(text("COMMIT"))


# ── D3 ─────────────────────────────────────────────────────────────

def _backfill(child: str) -> int:
    """Fill tenant_id from the parent in primary-key ranges of BATCH rows; returns the rows updated."""
    pk, fk, parent, parent_pk, _ = CHILDREN[child]
    conn, last, updated = op.get_bind(), "", 0
    while True:
        high: Optional[str] = conn.execute(text(
            f"SELECT MAX(k) FROM (SELECT {pk} AS k FROM {child} WHERE {pk} > :last AND tenant_id IS NULL "
            f"ORDER BY {pk} LIMIT {BATCH}) batch"), {"last": last}).scalar()
        if high is None:
            return updated
        updated += conn.execute(text(
            f"UPDATE {child} c JOIN {parent} p ON p.{parent_pk} = c.{fk} SET c.tenant_id = p.tenant_id "
            f"WHERE c.{pk} > :last AND c.{pk} <= :high AND c.tenant_id IS NULL"),
            {"last": last, "high": high}).rowcount
        _commit()
        last = high


def _finish(child: str) -> None:
    """NOT NULL when every row has a tenant, plus the missing indexes: one ALTER (one rebuild)."""
    indexes = CHILDREN[child][4]
    parts = [f"ADD INDEX {name} ({cols})" for name, cols in indexes.items() if not _has_index(child, name)]
    if _nullable(child, "tenant_id"):
        orphans = _scalar(f"SELECT COUNT(*) FROM {child} WHERE tenant_id IS NULL")
        if orphans:
            logger.warning(
                "0028: %d row(s) of %s have no parent %s, so no tenant; tenant_id stays NULLable. Repair or "
                "delete them, then run: ALTER TABLE %s MODIFY tenant_id VARCHAR(36) NOT NULL",
                orphans, child, CHILDREN[child][2], child)
        else:
            parts.insert(0, "MODIFY tenant_id VARCHAR(36) NOT NULL")
    if parts:
        op.execute(f"ALTER TABLE {child} " + ", ".join(parts))


def _tenant_columns() -> None:
    for child, (_pk, fk, _parent, _ppk, _ix) in CHILDREN.items():
        started = time.monotonic()
        if not _has_column(child, "tenant_id"):
            op.execute(f"ALTER TABLE {child} ADD COLUMN tenant_id VARCHAR(36) NULL AFTER {AFTER.get(child, fk)}")
        filled = _backfill(child)
        _finish(child)
        logger.info("0028: %s.tenant_id: %d row(s) filled in %.1fs", child, filled, time.monotonic() - started)


# ── C-22 ───────────────────────────────────────────────────────────

def _cross_tenant_rows(child: str, column: str, parent: str) -> int:
    """Child rows whose parent belongs to another tenant (or none of theirs)."""
    return _scalar(
        f"SELECT COUNT(*) FROM {child} c JOIN {parent} p ON p.{column} = c.{column} "
        f"WHERE c.tenant_id IS NULL OR c.tenant_id <> p.tenant_id")


def _composite_keys() -> None:
    for parent, (name, column) in PARENT_KEYS.items():
        if not _has_index(parent, name):
            op.execute(f"ALTER TABLE {parent} ADD UNIQUE KEY {name} (tenant_id, {column})")
    for child, indexes in CHILD_INDEXES.items():
        parts = [f"ADD INDEX {n} ({cols})" for n, cols in indexes.items() if not _has_index(child, n)]
        if parts:
            op.execute(f"ALTER TABLE {child} " + ", ".join(parts))
    for name, (child, column, parent, on_delete) in COMPOSITE_FKS.items():
        if _has_fk(child, name):
            continue
        ddl = (f"ALTER TABLE {child} ADD CONSTRAINT {name} FOREIGN KEY (tenant_id, {column}) "
               f"REFERENCES {parent} (tenant_id, {column}){on_delete}")
        bad = _cross_tenant_rows(child, column, parent)
        if bad:
            logger.warning("0028: %d row(s) of %s point at a %s of another tenant; %s was not created. "
                           "Repair them, then run: %s", bad, child, parent, name, ddl)
            continue
        op.execute(ddl)


def _one_primary() -> None:
    if not _has_column("contact_companies", PRIMARY_COLUMN):
        op.execute(f"ALTER TABLE contact_companies ADD COLUMN {PRIMARY_COLUMN} VARCHAR(36) "
                   f"GENERATED ALWAYS AS ({PRIMARY_KEY_SQL}) VIRTUAL")
    if _has_index("contact_companies", PRIMARY_INDEX):
        return
    ddl = f"CREATE UNIQUE INDEX {PRIMARY_INDEX} ON contact_companies ({PRIMARY_COLUMN})"
    clashes = _scalar("SELECT COUNT(*) FROM (SELECT prospect_id FROM contact_companies WHERE is_primary "
                      "GROUP BY prospect_id HAVING COUNT(*) > 1) d")
    if clashes:
        logger.warning("0028: %d contact(s) have more than one primary company; %s was not created. "
                       "Keep one primary row per contact, then run: %s", clashes, PRIMARY_INDEX, ddl)
        return
    op.execute(ddl)


def upgrade() -> None:
    _tenant_columns()
    _composite_keys()
    _one_primary()


def downgrade() -> None:
    if _has_index("contact_companies", PRIMARY_INDEX):
        op.execute(f"DROP INDEX {PRIMARY_INDEX} ON contact_companies")
    if _has_column("contact_companies", PRIMARY_COLUMN):
        op.execute(f"ALTER TABLE contact_companies DROP COLUMN {PRIMARY_COLUMN}")
    for name, (child, _column, _parent, _on_delete) in COMPOSITE_FKS.items():
        if _has_fk(child, name):
            op.execute(f"ALTER TABLE {child} DROP FOREIGN KEY {name}")
    for child, indexes in CHILD_INDEXES.items():
        for n in indexes:
            if _has_index(child, n):
                op.execute(f"DROP INDEX {n} ON {child}")
    for parent, (name, _column) in PARENT_KEYS.items():
        if _has_index(parent, name):
            op.execute(f"ALTER TABLE {parent} DROP INDEX {name}")
    for child, (_pk, _fk, _parent, _ppk, indexes) in reversed(list(CHILDREN.items())):
        parts = [f"DROP INDEX {n}" for n in indexes if _has_index(child, n)]
        if _has_column(child, "tenant_id"):
            parts.append("DROP COLUMN tenant_id")
        if parts:
            op.execute(f"ALTER TABLE {child} " + ", ".join(parts))
