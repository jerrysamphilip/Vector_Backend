"""Schema parity: a database upgraded from the old migrations = one built fresh (columns, indexes, FKs)

Revision ID: 0035_schema_parity
Revises: 0034_drop_contact_fulltext
Create Date: 2026-10-28

A fresh build gets its tables from the baseline's create_all() (the models); a database upgraded through the
old start-up DDL / legacy chain (e.g. one at 0016) got them from hand-written DDL, and 0001-0034 do not
reconcile every attribute. information_schema of a fresh build at 0034 against an upgraded copy at 0034
(columns: IS_NULLABLE, COLUMN_DEFAULT, EXTRA; indexes; foreign keys) differed in the items below. The target
is the stricter shape in each case; the models now declare it, so a fresh build has it too.

NOT NULL (upgraded databases had it, fresh builds were NULLable; the DEFAULT is the one 0032 set). NULLs are
first set to the default; a column that still holds NULLs afterwards, has another default, or is missing is
skipped with a warning. MySQL rebuilds the table in place (``ALGORITHM=INPLACE, LOCK=NONE``: reads and writes
continue, time grows with the table; no copy). All three sending_domains columns go in one ALTER.

  campaigns.auto_paused                 0
  sending_domains.sends_24h             0
  sending_domains.bounce_rate_24h       0
  sending_domains.complaint_rate_24h    0
  sending_inboxes.auth_type             'PASSWORD'

ON UPDATE CURRENT_TIMESTAMP(6) (upgraded databases had it, fresh builds relied on the ORM's onupdate alone,
so a raw-SQL UPDATE left updated_at stale). Metadata only (INSTANT, no rebuild); the column's type,
nullability and DEFAULT are kept.

  imap_sync_state.updated_at
  import_mappings.updated_at
  webhook_endpoints.updated_at

Foreign keys the models declare that upgraded databases lack (create_all() never adds a constraint to an
existing table). Rows that point nowhere are counted first; with any, the key is skipped with a warning that
names the statement to run after repairing them. Adding a validated foreign key COPIES the table in MySQL
(``ALGORITHM=COPY``: reads continue, writes wait until it finishes); run on a large ``contact_activities`` /
``opportunities`` in a quiet period.

  company_profiles.tenant_id        -> tenants.tenant_id              fk_company_profiles_tenant
  contact_activities.opportunity_id -> opportunities.opportunity_id   fk_contact_activities_opportunity
  crm_tasks.opportunity_id          -> opportunities.opportunity_id   fk_crm_tasks_opportunity
  opportunities.campaign_id         -> campaigns.campaign_id          fk_opportunities_campaign
  users.manager_id                  -> users.user_id                  fk_users_manager

Legacy indexes only upgraded databases have, dropped online (``ALGORITHM=INPLACE, LOCK=NONE``, no rebuild).
An index is dropped only when it still has exactly the expected column and, where it supports a foreign key,
the index the fresh build uses for that key (same leading column) exists.

  domain_health_snapshots.domain_name   (no query filters on the display copy of the domain name)
  reputation_alerts.domain_name         (same)
  email_messages.inbox_id               (ix_email_messages_inbox_sent (inbox_id, sent_at) serves the FK)
  prospect_lists.tenant_id              (uq_prospect_lists_tenant_list (tenant_id, list_id) serves the FK)

users.invited_by carried two identical foreign keys (fk_users_invited_by from the start-up DDL and
users_ibfk_2): the duplicate fk_users_invited_by is dropped and its index renamed to ``invited_by`` as in a
fresh build (both metadata only). A sole fk_users_invited_by is kept.

Idempotent: every step reads information_schema first and does nothing when the target shape is already
there, so a fresh build (which has it from create_all) is unchanged and a second run is a no-op. No data is
deleted; the only data change is NULL -> default in the five NOT NULL columns.

Downgrade is a documented no-op. There is no record of which databases lacked which item (a fresh build at
0034 lacked the NOT NULLs, ON UPDATEs and nothing else; an upgraded one had those and lacked the keys), so
undoing would have to guess and would bring the fresh-vs-upgraded difference back; the code at 0034 works on
either shape (the ORM always writes the five columns and updated_at, and fresh builds always had the keys).
"""
import logging
import re
from typing import Any, Optional, Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0035_schema_parity"
down_revision: Union[str, Sequence[str], None] = "0034_drop_contact_fulltext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")
TAG = "0035_schema_parity"

# (table, column, SQL literal of the default, value information_schema.COLUMN_DEFAULT shows for it)
NOT_NULL = (
    ("campaigns", "auto_paused", "0", ("0",)),
    ("sending_domains", "sends_24h", "0", ("0",)),
    ("sending_domains", "bounce_rate_24h", "0", ("0",)),
    ("sending_domains", "complaint_rate_24h", "0", ("0",)),
    ("sending_inboxes", "auth_type", "'PASSWORD'", ("PASSWORD",)),
)

# (table, column) of updated_at columns the server refreshes on UPDATE
ON_UPDATE = (
    ("imap_sync_state", "updated_at"),
    ("import_mappings", "updated_at"),
    ("webhook_endpoints", "updated_at"),
)

# (constraint name, table, column, referenced table, referenced column)
FOREIGN_KEYS = (
    ("fk_company_profiles_tenant", "company_profiles", "tenant_id", "tenants", "tenant_id"),
    ("fk_contact_activities_opportunity", "contact_activities", "opportunity_id", "opportunities", "opportunity_id"),
    ("fk_crm_tasks_opportunity", "crm_tasks", "opportunity_id", "opportunities", "opportunity_id"),
    ("fk_opportunities_campaign", "opportunities", "campaign_id", "campaigns", "campaign_id"),
    ("fk_users_manager", "users", "manager_id", "users", "user_id"),
)

# (table, legacy index, its only column, index that must remain for the column's FK or None)
LEGACY_INDEXES = (
    ("domain_health_snapshots", "domain_name", "domain_name", None),
    ("reputation_alerts", "domain_name", "domain_name", None),
    ("email_messages", "inbox_id", "inbox_id", "ix_email_messages_inbox_sent"),
    ("prospect_lists", "tenant_id", "tenant_id", "uq_prospect_lists_tenant_list"),
)

# (table, duplicate FK to drop, its column, name of that column's index in a fresh build)
DUPLICATE_FKS = (
    ("users", "fk_users_invited_by", "invited_by", "invited_by"),
)

_NOW = re.compile(r"(?i)^(current_timestamp|now)(\(\d*\))?$")


def _rows(sql: str, **params: Any) -> list:
    return list(op.get_bind().execute(text(sql), params).all())


def _scalar(sql: str, **params: Any) -> int:
    return int(op.get_bind().execute(text(sql), params).scalar() or 0)


def _column(table: str, column: str) -> Optional[Any]:
    rows = _rows(
        "SELECT is_nullable AS is_nullable, column_type AS column_type, character_set_name AS character_set_name, "
        "collation_name AS collation_name, column_default AS column_default, extra AS extra, "
        "column_comment AS column_comment, datetime_precision AS datetime_precision FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c", t=table, c=column)
    return rows[0] if rows else None


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _type_sql(col: Any) -> str:
    """The column's type with its character set, collation and comment (so MODIFY keeps them)."""
    parts = [col.column_type]
    if col.character_set_name:
        parts.append(f"CHARACTER SET {col.character_set_name} COLLATE {col.collation_name}")
    return " ".join(parts)


def _comment_sql(col: Any) -> str:
    return f" COMMENT {_quote(col.column_comment)}" if col.column_comment else ""


# ── NOT NULL ───────────────────────────────────────────────────────

def _not_null_clause(table: str, column: str, literal: str, shown: tuple) -> Optional[str]:
    """The MODIFY clause for one column, or None (already NOT NULL, or skipped with a warning)."""
    col = _column(table, column)
    if col is None:
        logger.warning("%s: %s.%s does not exist; NOT NULL not set", TAG, table, column)
        return None
    if col.is_nullable == "NO":
        return None
    extra = (col.extra or "").replace("DEFAULT_GENERATED", "").strip()
    if col.column_default not in shown or extra:
        logger.warning("%s: %s.%s has DEFAULT %r %s; left NULLable (expected DEFAULT %s)",
                       TAG, table, column, col.column_default, extra, literal)
        return None
    filled = op.get_bind().execute(text(
        f"UPDATE `{table}` SET `{column}` = {literal} WHERE `{column}` IS NULL")).rowcount
    if filled:
        logger.info("%s: %s.%s: %d NULL row(s) set to %s", TAG, table, column, filled, literal)
    left = _scalar(f"SELECT COUNT(*) FROM `{table}` WHERE `{column}` IS NULL")
    if left:
        logger.warning("%s: %s.%s still has %d NULL row(s) (written meanwhile?); left NULLable. Re-run: "
                       "alembic downgrade 0034_drop_contact_fulltext && alembic upgrade head", TAG, table, column, left)
        return None
    return f"MODIFY `{column}` {_type_sql(col)} NOT NULL DEFAULT {literal}{_comment_sql(col)}"


def _not_null() -> None:
    by_table: dict[str, list[str]] = {}
    for table, column, literal, shown in NOT_NULL:
        clause = _not_null_clause(table, column, literal, shown)
        if clause:
            by_table.setdefault(table, []).append(clause)
    for table, clauses in by_table.items():
        op.execute(f"ALTER TABLE `{table}` " + ", ".join(clauses) + ", ALGORITHM=INPLACE, LOCK=NONE")


# ── ON UPDATE ──────────────────────────────────────────────────────

def _on_update(table: str, column: str) -> None:
    col = _column(table, column)
    if col is None:
        logger.warning("%s: %s.%s does not exist; ON UPDATE not set", TAG, table, column)
        return
    if "on update" in (col.extra or "").lower():
        return
    if not col.column_type.startswith(("datetime", "timestamp")) or not _NOW.match(col.column_default or ""):
        logger.warning("%s: %s.%s is %s DEFAULT %r; ON UPDATE not set (expected a CURRENT_TIMESTAMP default)",
                       TAG, table, column, col.column_type, col.column_default)
        return
    now = f"CURRENT_TIMESTAMP({col.datetime_precision})" if col.datetime_precision else "CURRENT_TIMESTAMP"
    null = "NULL" if col.is_nullable == "YES" else "NOT NULL"
    op.execute(f"ALTER TABLE `{table}` MODIFY `{column}` {col.column_type} {null} DEFAULT {now} "
               f"ON UPDATE {now}{_comment_sql(col)}")


# ── foreign keys ───────────────────────────────────────────────────

def _has_fk_on(table: str, column: str, ref_table: str, ref_column: str) -> bool:
    """Any single-column FK table(column) -> ref_table(ref_column), whatever its name."""
    return bool(_scalar(
        "SELECT COUNT(*) FROM information_schema.key_column_usage k WHERE k.table_schema = DATABASE() "
        "AND k.table_name = :t AND k.column_name = :c AND k.referenced_table_name = :rt "
        "AND k.referenced_column_name = :rc AND (SELECT COUNT(*) FROM information_schema.key_column_usage k2 "
        "WHERE k2.table_schema = k.table_schema AND k2.table_name = k.table_name "
        "AND k2.constraint_name = k.constraint_name) = 1", t=table, c=column, rt=ref_table, rc=ref_column))


def _add_fk(name: str, table: str, column: str, ref_table: str, ref_column: str) -> None:
    if _column(table, column) is None or _column(ref_table, ref_column) is None:
        logger.warning("%s: %s.%s or %s.%s does not exist; %s not created",
                       TAG, table, column, ref_table, ref_column, name)
        return
    if _has_fk_on(table, column, ref_table, ref_column):
        return
    ddl = (f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY (`{column}`) "
           f"REFERENCES `{ref_table}` (`{ref_column}`)")
    orphans = _scalar(
        f"SELECT COUNT(*) FROM `{table}` c LEFT JOIN `{ref_table}` p ON p.`{ref_column}` = c.`{column}` "
        f"WHERE c.`{column}` IS NOT NULL AND p.`{ref_column}` IS NULL")
    if orphans:
        logger.warning("%s: %d row(s) of %s have a %s that is not in %s; %s was not created. Repair them "
                       "(set the column to NULL or to an existing %s), then run: %s",
                       TAG, orphans, table, column, ref_table, name, ref_column, ddl)
        return
    op.execute(ddl)


# ── legacy indexes / duplicate FK ──────────────────────────────────

def _index_columns(table: str, index: str) -> list:
    return [r[0] for r in _rows(
        "SELECT column_name FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = :t AND index_name = :i ORDER BY seq_in_index", t=table, i=index)]


def _index_leads_with(table: str, index: str, column: str) -> bool:
    cols = _index_columns(table, index)
    return bool(cols) and cols[0] == column


def _drop_legacy_index(table: str, index: str, column: str, keeper: Optional[str]) -> None:
    cols = _index_columns(table, index)
    if not cols:
        return
    if cols != [column]:
        logger.warning("%s: %s.%s is on (%s), not (%s); left in place", TAG, table, index, ", ".join(cols), column)
        return
    if keeper and not _index_leads_with(table, keeper, column):
        logger.warning("%s: %s.%s is kept: %s (which would serve the foreign key on %s) is missing",
                       TAG, table, index, keeper, column)
        return
    op.execute(f"ALTER TABLE `{table}` DROP INDEX `{index}`, ALGORITHM=INPLACE, LOCK=NONE")


def _fk_names_on(table: str, column: str) -> list:
    return [r[0] for r in _rows(
        "SELECT constraint_name FROM information_schema.key_column_usage WHERE table_schema = DATABASE() "
        "AND table_name = :t AND column_name = :c AND referenced_table_name IS NOT NULL", t=table, c=column)]


def _drop_duplicate_fk(table: str, fk: str, column: str, index_name: str) -> None:
    names = _fk_names_on(table, column)
    if fk in names and len(names) > 1:
        op.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk}`, ALGORITHM=INPLACE, LOCK=NONE")
    if fk not in _fk_names_on(table, column) and _index_columns(table, fk) == [column] \
            and not _index_columns(table, index_name):
        op.execute(f"ALTER TABLE `{table}` RENAME INDEX `{fk}` TO `{index_name}`, ALGORITHM=INPLACE, LOCK=NONE")


def upgrade() -> None:
    _not_null()
    for table, column in ON_UPDATE:
        _on_update(table, column)
    for entry in FOREIGN_KEYS:
        _add_fk(*entry)
    for table, index, column, keeper in LEGACY_INDEXES:
        _drop_legacy_index(table, index, column, keeper)
    for table, fk, column, index_name in DUPLICATE_FKS:
        _drop_duplicate_fk(table, fk, column, index_name)


def downgrade() -> None:
    # Documented no-op (see the module docstring): the stricter shape stays.
    pass
