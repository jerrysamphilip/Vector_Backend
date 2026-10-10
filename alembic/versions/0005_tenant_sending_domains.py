"""Tenant-owned sending domains and deliverability data

Revision ID: 0005_tenant_sending_domains
Revises: 0004_user_mfa
Create Date: 2026-10-10

Before this revision sending_domains was keyed by the domain name alone, so two tenants
sending from the same domain shared one health/reputation record (and one auto-pause).
Now every domain-keyed table carries tenant_id:

  sending_domains            new surrogate PK domain_id, tenant_id, UNIQUE (tenant_id, domain_name)
                             (the old PRIMARY KEY (domain_name) is dropped)
  domain_health_snapshots    domain_id -> sending_domains.domain_id (ON DELETE CASCADE), tenant_id
  reputation_alerts          domain_id -> sending_domains.domain_id (ON DELETE CASCADE), tenant_id
  external_reputation_metrics, external_feedback_events, external_ingestion_runs: tenant_id

The old FKs on domain_name (children -> sending_domains.domain_name) are dropped; domain_name
stays on the children as a display column.

Backfill (rows that have no tenant yet):
  * sending_domains: owners are the tenants with a mailbox (sending_inboxes.email_address) on
    the domain, most mailboxes first (ties: tenant_id). If no tenant has a mailbox there, the
    tenants that sent outbound mail from it (email_messages via inbox / campaign) are used.
    The row goes to the first owner; every other owner gets a copy of the row (current DNS /
    auth status, warm-up, reputation, 24h health; the 24h health is recomputed per tenant by
    the hourly health cycle). Rows no tenant can be matched to keep tenant_id NULL: kept,
    but invisible to every tenant.
  * snapshots and alerts (history) stay with the original row, i.e. the first owner.
  * external metrics and feedback events (facts about the domain) are copied to every owner;
    ingestion runs (an audit log) go to the first owner.
Every step checks information_schema / NULL tenant_id first, so re-running is a no-op.
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0005_tenant_sending_domains"
down_revision: Union[str, Sequence[str], None] = "0004_user_mfa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

CHILDREN = ("domain_health_snapshots", "reputation_alerts")
EXTERNAL = ("external_reputation_metrics", "external_feedback_events", "external_ingestion_runs")
EXTERNAL_COPIED = ("external_reputation_metrics", "external_feedback_events")
EXTERNAL_PK = {"external_reputation_metrics": "metric_id", "external_feedback_events": "event_id",
               "external_ingestion_runs": "run_id"}
EXTERNAL_TENANT_FKS = {"external_reputation_metrics": "fk_ext_metrics_tenant",
                       "external_feedback_events": "fk_ext_events_tenant",
                       "external_ingestion_runs": "fk_ext_runs_tenant"}
EXTERNAL_INDEXES = {
    "external_reputation_metrics": ("ix_ext_metrics_tenant_domain", "tenant_id, domain_name"),
    "external_feedback_events": ("ix_ext_events_tenant_domain", "tenant_id, domain_name"),
    "external_ingestion_runs": ("ix_ext_runs_tenant_provider", "tenant_id, provider, started_at"),
}


# ── information_schema helpers ───────────────────────────────

def _columns(conn, table):
    return [r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"), {"t": table})]


def _is_nullable(conn, table, column):
    return conn.execute(text(
        "SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND COLUMN_NAME = :c"), {"t": table, "c": column}).scalar() == "YES"


def _index_exists(conn, table, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND INDEX_NAME = :n"), {"t": table, "n": name}).first())


def _index_on(conn, table, first_column):
    """True when some index starts with this column (an FK can use it)."""
    return bool(conn.execute(text(
        "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND COLUMN_NAME = :c AND SEQ_IN_INDEX = 1"),
        {"t": table, "c": first_column}).first())


def _primary_key(conn, table):
    return [r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"), {"t": table})]


def _fks(conn, table, column=None, ref_table=None, ref_column=None):
    sql = ("SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
           "AND TABLE_NAME = :t AND REFERENCED_TABLE_NAME IS NOT NULL")
    params = {"t": table}
    if column:
        sql += " AND COLUMN_NAME = :c"
        params["c"] = column
    if ref_table:
        sql += " AND REFERENCED_TABLE_NAME = :rt"
        params["rt"] = ref_table
    if ref_column:
        sql += " AND REFERENCED_COLUMN_NAME = :rc"
        params["rc"] = ref_column
    return sorted({r[0] for r in conn.execute(text(sql), params)})


def _add_column(conn, table, column, ddl, after=None):
    if column in _columns(conn, table):
        return False
    conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}" + (f" AFTER `{after}`" if after else "")))
    logger.info("Added %s.%s", table, column)
    return True


def _add_fk(conn, table, column, ref_table, ref_column, name, on_delete=None):
    if _fks(conn, table, column, ref_table, ref_column):
        return
    if not _index_on(conn, table, column):
        conn.execute(text(f"CREATE INDEX `ix_{table}_{column}` ON `{table}` (`{column}`)"))
    conn.execute(text(f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` FOREIGN KEY (`{column}`) "
                      f"REFERENCES `{ref_table}` (`{ref_column}`)" + (f" ON DELETE {on_delete}" if on_delete else "")))
    logger.info("Added FK %s on %s(%s) -> %s(%s)", name, table, column, ref_table, ref_column)


# ── Backfill ─────────────────────────────────────────────────

def _owners_by_inbox(conn, domain):
    return [r[0] for r in conn.execute(text(
        "SELECT tenant_id FROM sending_inboxes "
        "WHERE email_address LIKE :p AND tenant_id IS NOT NULL "
        "GROUP BY tenant_id ORDER BY COUNT(*) DESC, tenant_id"), {"p": f"%@{domain}"})]


def _owners_by_sends(conn, domain):
    return [r[0] for r in conn.execute(text(
        "SELECT t FROM (SELECT COALESCE(i.tenant_id, c.tenant_id) AS t FROM email_messages m "
        " LEFT JOIN sending_inboxes i ON i.inbox_id = m.inbox_id "
        " LEFT JOIN campaigns c ON c.campaign_id = m.campaign_id "
        " WHERE m.direction = 'OUTBOUND' AND m.from_email LIKE :p) x "
        "WHERE t IS NOT NULL GROUP BY t ORDER BY COUNT(*) DESC, t"), {"p": f"%@{domain}"})]


def _has_row(conn, tenant_id, domain):
    return bool(conn.execute(text(
        "SELECT 1 FROM sending_domains WHERE tenant_id = :t AND domain_name = :d"),
        {"t": tenant_id, "d": domain}).first())


def _backfill_sending_domains(conn):
    rows = conn.execute(text(
        "SELECT domain_id, domain_name FROM sending_domains WHERE tenant_id IS NULL "
        "ORDER BY created_at, domain_id")).fetchall()
    if not rows:
        return
    copy_cols = [c for c in _columns(conn, "sending_domains")
                 if c not in ("domain_id", "tenant_id", "created_at", "updated_at")]
    col_list = ", ".join(f"`{c}`" for c in copy_cols)
    assigned = copied = orphaned = 0
    for domain_id, name in rows:
        owners = _owners_by_inbox(conn, name) or _owners_by_sends(conn, name)
        owners = [t for t in owners if not _has_row(conn, t, name)]
        if not owners:
            orphaned += 1
            logger.warning("sending_domains %s: no tenant has a mailbox on or sent from it; "
                           "kept with tenant_id NULL (invisible to tenants)", name)
            continue
        primary, others = owners[0], owners[1:]
        for tenant_id in others:
            conn.execute(text(
                f"INSERT INTO sending_domains (domain_id, tenant_id, {col_list}) "
                f"SELECT UUID(), :t, {col_list} FROM sending_domains WHERE domain_id = :id"),
                {"t": tenant_id, "id": domain_id})
            copied += 1
        conn.execute(text("UPDATE sending_domains SET tenant_id = :t WHERE domain_id = :id"),
                     {"t": primary, "id": domain_id})
        assigned += 1
        if others:
            logger.info("sending_domains %s: shared by %d tenants, one row each", name, len(owners))
    logger.info("sending_domains backfill: %d assigned, %d copies for other tenants, %d left without tenant",
                assigned, copied, orphaned)


def _first_owner_rows(conn):
    """{lower(domain_name): [tenant_id, ...]} — owners ordered by row age (original row first)."""
    owners = {}
    for tenant_id, name in conn.execute(text(
            "SELECT tenant_id, domain_name FROM sending_domains WHERE tenant_id IS NOT NULL "
            "ORDER BY created_at, domain_id")):
        owners.setdefault(name.lower(), []).append(tenant_id)
    return owners


def _backfill_external(conn):
    owners = _first_owner_rows(conn)
    for table in EXTERNAL:
        cols = _columns(conn, table)
        if not cols:
            continue
        names = [r[0] for r in conn.execute(text(
            f"SELECT DISTINCT domain_name FROM `{table}` WHERE tenant_id IS NULL AND domain_name IS NOT NULL"))]
        pk = EXTERNAL_PK[table]
        copy_cols = [c for c in cols if c not in (pk, "tenant_id")]
        col_list = ", ".join(f"`{c}`" for c in copy_cols)
        for name in names:
            tenants = owners.get((name or "").lower())
            if not tenants:
                continue
            if table in EXTERNAL_COPIED:
                for tenant_id in tenants[1:]:
                    conn.execute(text(
                        f"INSERT INTO `{table}` (`{pk}`, tenant_id, {col_list}) "
                        f"SELECT UUID(), :t, {col_list} FROM `{table}` WHERE tenant_id IS NULL AND domain_name = :d"),
                        {"t": tenant_id, "d": name})
            conn.execute(text(f"UPDATE `{table}` SET tenant_id = :t WHERE tenant_id IS NULL AND domain_name = :d"),
                         {"t": tenants[0], "d": name})


# ── Upgrade ──────────────────────────────────────────────────

def upgrade() -> None:
    conn = op.get_bind()
    if not _columns(conn, "sending_domains"):
        logger.warning("sending_domains does not exist; nothing to migrate")
        return

    # 1. New columns (nullable while backfilling)
    _add_column(conn, "sending_domains", "domain_id", "VARCHAR(36) NULL", after=None)
    _add_column(conn, "sending_domains", "tenant_id", "VARCHAR(36) NULL", after="domain_id")
    for table in CHILDREN:
        if _columns(conn, table):
            _add_column(conn, table, "domain_id", "VARCHAR(36) NULL")
            _add_column(conn, table, "tenant_id", "VARCHAR(36) NULL")
    for table in EXTERNAL:
        if _columns(conn, table):
            _add_column(conn, table, "tenant_id", "VARCHAR(36) NULL")

    conn.execute(text("UPDATE sending_domains SET domain_id = UUID() WHERE domain_id IS NULL"))

    # 2. Children point at the domain row by id (one row per name exists until step 4;
    #    on a re-run the oldest row for the name, i.e. the original, is taken)
    for table in CHILDREN:
        if _columns(conn, table):
            conn.execute(text(
                f"UPDATE `{table}` c SET c.domain_id = (SELECT d.domain_id FROM sending_domains d "
                f"WHERE d.domain_name = c.domain_name ORDER BY d.created_at, d.domain_id LIMIT 1) "
                f"WHERE c.domain_id IS NULL"))

    # 3. Drop FKs on domain_name, then move the primary key to domain_id
    for table in CHILDREN:
        if _columns(conn, table):
            for fk in _fks(conn, table, "domain_name", "sending_domains"):
                conn.execute(text(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk}`"))
                logger.info("Dropped FK %s on %s(domain_name)", fk, table)
    if _primary_key(conn, "sending_domains") != ["domain_id"]:
        conn.execute(text("ALTER TABLE sending_domains MODIFY COLUMN domain_id VARCHAR(36) NOT NULL, "
                          "DROP PRIMARY KEY, ADD PRIMARY KEY (domain_id)"))
        logger.info("sending_domains: primary key moved from domain_name to domain_id")
    if not _index_exists(conn, "sending_domains", "ix_sending_domains_domain_name"):
        conn.execute(text("CREATE INDEX ix_sending_domains_domain_name ON sending_domains (domain_name)"))

    # 4. Owners (DML only, committed together with step 5)
    _backfill_sending_domains(conn)
    for table in CHILDREN:
        if _columns(conn, table):
            conn.execute(text(
                f"UPDATE `{table}` c JOIN sending_domains d ON d.domain_id = c.domain_id "
                f"SET c.tenant_id = d.tenant_id WHERE c.tenant_id IS NULL AND d.tenant_id IS NOT NULL"))
    _backfill_external(conn)

    # 5. Keys
    if not _index_exists(conn, "sending_domains", "uq_sending_domains_tenant_domain"):
        dupes = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT 1 FROM sending_domains WHERE tenant_id IS NOT NULL "
            "GROUP BY tenant_id, domain_name HAVING COUNT(*) > 1) d")).scalar()
        if dupes:
            raise RuntimeError(f"sending_domains has {dupes} duplicate (tenant_id, domain_name) group(s)")
        conn.execute(text("ALTER TABLE sending_domains ADD CONSTRAINT uq_sending_domains_tenant_domain "
                          "UNIQUE (tenant_id, domain_name)"))
        logger.info("Added unique key uq_sending_domains_tenant_domain")
    _add_fk(conn, "sending_domains", "tenant_id", "tenants", "tenant_id", "fk_sending_domains_tenant")

    for table, short in (("domain_health_snapshots", "dhs"), ("reputation_alerts", "ra")):
        if not _columns(conn, table):
            continue
        orphans = conn.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE domain_id IS NULL")).scalar()
        if orphans:
            # Cannot happen while the old FK held; never delete data in a migration
            logger.warning("%s: %d row(s) reference no sending domain; domain_id left nullable", table, orphans)
        elif _is_nullable(conn, table, "domain_id"):
            conn.execute(text(f"ALTER TABLE `{table}` MODIFY COLUMN domain_id VARCHAR(36) NOT NULL"))
        _add_fk(conn, table, "domain_id", "sending_domains", "domain_id", f"fk_{short}_domain", on_delete="CASCADE")
        _add_fk(conn, table, "tenant_id", "tenants", "tenant_id", f"fk_{short}_tenant")
        for col in ("domain_id", "tenant_id"):
            name = f"ix_{table}_{col}"
            if not _index_exists(conn, table, name) and not _index_on(conn, table, col):
                conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` (`{col}`)"))

    for table in EXTERNAL:
        if not _columns(conn, table):
            continue
        name, cols = EXTERNAL_INDEXES[table]
        if not _index_exists(conn, table, name):
            conn.execute(text(f"CREATE INDEX `{name}` ON `{table}` ({cols})"))
        _add_fk(conn, table, "tenant_id", "tenants", "tenant_id", EXTERNAL_TENANT_FKS[table])


def downgrade() -> None:
    raise NotImplementedError(
        "Tenant-owned sending domains cannot be merged back into one row per domain name "
        "automatically; restore from a backup instead.")
